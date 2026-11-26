"""Durable booking guard shared by automatic and manual GitHub Actions runs."""
import base64
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests

BOOKING_OK = 0
BOOKING_FAILED = 1
BOOKING_RETRY = 75
BOOKING_PAUSED = 76
MAX_BROWSER_WORKERS = 3


class BookingAccessPaused(RuntimeError):
    pass


def booking_paused():
    path = os.environ.get("BOOKING_PAUSE_FILE")
    return bool(path and Path(path).exists())


def pause_booking():
    path = os.environ.get("BOOKING_PAUSE_FILE")
    if path:
        Path(path).touch(exist_ok=True)


def check_booking_access():
    if booking_paused():
        raise BookingAccessPaused("施設側のアクセス制限を検知したため、この実行の新規アクセスを停止します")


def check_site_status(status, retry_after=None):
    if status in (403, 429) or (status == 503 and retry_after):
        pause_booking()
        raise BookingAccessPaused(f"施設側が HTTP {status} を返したため、この実行の新規アクセスを停止します")


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


class BookingLedger:
    def __init__(self, facility_id, stay_date):
        key = hashlib.sha256(f"{facility_id}\n{stay_date}".encode()).hexdigest()
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
        if (sha is None and status != "pending") or (sha is not None and status not in expected_previous):
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