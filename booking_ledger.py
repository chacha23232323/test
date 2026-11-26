"""Durable booking guard shared by automatic and manual GitHub Actions runs."""
import base64
import hashlib
import math
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

BOOKING_OK = 0
BOOKING_FAILED = 1
BOOKING_RETRY = 75
BOOKING_PAUSED = 76
BOOKING_DEFERRED = 77
DEFAULT_COOLDOWN = 60
MAX_BROWSER_WORKERS = 3


class BookingAccessPaused(RuntimeError):
    pass


class BookingAccessDenied(BookingAccessPaused):
    pass


def cooldown_deadline():
    path = os.environ.get("BOOKING_PAUSE_FILE")
    if not path:
        return 0.0
    try:
        value = float(Path(path).read_text(encoding="ascii"))
    except FileNotFoundError:
        return 0.0
    if not math.isfinite(value):
        raise RuntimeError("アクセス休止記録が不正です")
    return value


def cooldown_remaining():
    return max(0.0, cooldown_deadline() - time.time())


def booking_paused():
    return cooldown_remaining() > 0


def retry_after_seconds(value, now):
    if value:
        value = str(value).strip()
        if value.isascii() and value.isdecimal():
            try:
                seconds = float(value)
                if math.isfinite(seconds):
                    return max(1.0, seconds)
            except (ValueError, OverflowError):
                pass
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(1.0, when.timestamp() - now)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return float(DEFAULT_COOLDOWN)


def pause_booking(retry_after=None):
    now = time.time()
    seconds = retry_after_seconds(retry_after, now)
    path = os.environ.get("BOOKING_PAUSE_FILE")
    if not path:
        return seconds
    path = Path(path)
    # Atomic publication under a cross-process lock; a shorter response cannot shorten an active pause.
    with local_slots("site-cooldown", timeout=15):
        deadline = max(cooldown_deadline(), now + seconds)
        fd, temporary = tempfile.mkstemp(prefix="cooldown-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(str(deadline))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return max(0.0, deadline - time.time())


def check_booking_access():
    remaining = cooldown_remaining()
    if remaining > 0:
        raise BookingAccessPaused(f"施設側のアクセス制限により休止中 (残り約 {math.ceil(remaining)} 秒)")


def check_site_status(status, retry_after=None):
    if status == 403:
        raise BookingAccessDenied("施設側が HTTP 403 を返しました。該当アクセスを中止します")
    if status == 429 or (status == 503 and retry_after):
        remaining = pause_booking(retry_after)
        raise BookingAccessPaused(f"施設側が HTTP {status} を返しました。新規アクセスを約 {math.ceil(remaining)} 秒休止します")


def lock_root():
    scope = os.environ.get("BOOKING_LOCK_DIR")
    if scope:
        root = Path(scope)
    else:
        repo = os.environ.get("GITHUB_REPOSITORY", "local")
        branch = os.environ.get("BOOKING_BRANCH", "main")
        key = hashlib.sha256(f"{repo}\n{branch}".encode()).hexdigest()[:20]
        root = Path(tempfile.gettempdir()) / f"its-booking-{os.getuid()}-{key}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@contextmanager
def local_slots(name, count=1, timeout=45):
    import fcntl

    root = lock_root()
    deadline = time.monotonic() + timeout
    while True:
        for index in range(count):
            handle = (root / f"{name}-{index}.lock").open("a+b")
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                return
            finally:
                handle.close()
        if time.monotonic() >= deadline:
            raise RuntimeError(f"同時実行制御の待機上限に達しました ({name})")
        time.sleep(0.15)


def booking_key(facility_id, stay_date):
    return hashlib.sha256(f"{facility_id}\n{stay_date}".encode()).hexdigest()


def deferred_ledger_enabled():
    return os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("BOOKING_DEFER_LEDGER_WRITE", "").lower() == "true"


def booking_operation_lock(facility_id, stay_date):
    return local_slots("booking-" + booking_key(facility_id, stay_date), timeout=270)


def local_attempt_path(facility_id, stay_date):
    return lock_root() / ("attempt-" + booking_key(facility_id, stay_date) + ".json")


def mark_local_attempt(facility_id, stay_date, status):
    if status not in ("pending", "email_sent"):
        raise RuntimeError("ローカル申込状態が不正です")
    path = local_attempt_path(facility_id, stay_date)
    record = {"run_id": os.environ["GITHUB_RUN_ID"], "status": status}
    fd, temporary = tempfile.mkstemp(prefix="attempt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_local_attempt(facility_id, stay_date):
    local_attempt_path(facility_id, stay_date).unlink(missing_ok=True)


def recover_local_attempt(facility_id, stay_date, ledger=None):
    """Caller holds the per-booking local lock. Never resubmit to the facility."""
    path = local_attempt_path(facility_id, stay_date)
    if not path.exists():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    status = record.get("status")
    if record.get("run_id") != os.environ["GITHUB_RUN_ID"] or status not in ("pending", "email_sent"):
        raise RuntimeError("ローカル申込記録の所有者または状態を確認できません")
    ledger = ledger if ledger is not None else BookingLedger(facility_id, stay_date)
    previous, _ = ledger._read()
    if previous is None:
        ledger.write(status)
    elif previous.get("run_id") != record["run_id"] or previous.get("status") != status:
        raise RuntimeError("ローカルとGitHubの申込記録が一致しません。手動確認してください")
    clear_local_attempt(facility_id, stay_date)
    print(f"::warning::{facility_id} {stay_date}: 申込履歴を復旧しました ({status})。メールと申込状況を確認してください。", flush=True)
    return True


class BookingLedger:
    def __init__(self, facility_id, stay_date):
        key = booking_key(facility_id, stay_date)
        self.branch = os.environ["BOOKING_BRANCH"]
        repo = os.environ["GITHUB_REPOSITORY"]
        self.url = f"{os.environ['GITHUB_API_URL']}/repos/{repo}/contents/.booking-ledger/{key}.json"
        self.headers = {
            "Authorization": f"Bearer {os.environ['BOOKING_GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        }
        self.record = {
            "facility_id": facility_id,
            "stay_date": stay_date,
            "run_id": os.environ["GITHUB_RUN_ID"],
        }

    def _read(self):
        result = requests.get(
            self.url, params={"ref": self.branch}, headers=self.headers, timeout=15
        )
        if result.status_code == 404:
            return None, None
        if result.status_code != 200:
            raise RuntimeError(f"申込記録の読み取りに失敗しました (HTTP {result.status_code})")
        body = result.json()
        record = json.loads(base64.b64decode(body["content"]))
        if (record.get("facility_id"), record.get("stay_date")) != (
            self.record["facility_id"], self.record["stay_date"]
        ):
            raise RuntimeError("既存の申込記録の対象が一致しません")
        if record.get("status") not in ("pending", "email_sent", "finalizing", "accepted"):
            raise RuntimeError("既存の申込記録の状態を確認できません")
        return record, body["sha"]

    def existing_status(self):
        record, _ = self._read()
        return record["status"] if record else None

    def write(self, status, sha=None):
        expected_previous = {"email_sent": "pending", "finalizing": "email_sent", "accepted": "finalizing"}
        if (sha is None and status not in ("pending", "email_sent")) or (sha is not None and status not in expected_previous):
            raise RuntimeError("申込記録の状態遷移が正しくありません")
        record = dict(self.record, status=status)
        payload = {
            "branch": self.branch,
            "message": f"Update operation status {status}",
            "content": base64.b64encode(
                (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
            ).decode("ascii"),
        }
        if sha is not None:
            payload["sha"] = sha
        expected = 201 if sha is None else 200
        with local_slots("ledger-write", timeout=45):
            for attempt in range(3):
                previous, current_sha = self._read()
                if sha is None:
                    if previous is not None:
                        raise RuntimeError("同一施設・宿泊日の申込記録が既に存在します。申込を停止します")
                elif (previous is None or current_sha != sha
                      or previous.get("run_id") != self.record["run_id"]
                      or previous.get("status") != expected_previous[status]):
                    raise RuntimeError("申込記録の所有者または状態が変化しました。申込を停止します")
                result = requests.put(self.url, json=payload, headers=self.headers, timeout=15)
                if result.status_code == expected:
                    return result.json()["content"]["sha"]
                if result.status_code != 409 or attempt == 2:
                    raise RuntimeError(f"申込記録の書き込みに失敗しました (HTTP {result.status_code})")
                time.sleep(0.25 * (attempt + 1))
