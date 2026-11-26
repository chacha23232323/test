import time

SCRIPT_START = time.perf_counter()

import json
import os
import re
import sys
import subprocess
import calendar
import threading
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from lxml import html as lxml_html
from booking_ledger import (
    BOOKING_OK, BOOKING_FAILED, BOOKING_RETRY, BOOKING_PAUSED, BOOKING_DEFERRED,
    BookingAccessDenied, BookingAccessPaused, cooldown_remaining,
    deferred_ledger_enabled, booking_operation_lock, recover_local_attempt, local_attempt_path,
    booking_paused, check_booking_access, check_site_status,
)

STATE_PATH = Path("state.json")
AUTO_BOOKING_SECTION = "## 自動申込チェックリスト"
JST = ZoneInfo("Asia/Tokyo")

MAX_WORKERS = 17
MAX_BOOKING_WORKERS = 10
MAX_BOOKING_ATTEMPTS = 3
REQUEST_TIMEOUT = (5, 25)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TOTAL_CHARS = 5800
DISCORD_MAX_RETRIES = 5

COLOR_HIGHLIGHTED = 0xE74C3C
COLOR_STANDARD = 0x3498DB

_thread_local = threading.local()
_discord_session = None
_denied_urls = set()
_denied_urls_lock = threading.Lock()

DAY_PATTERN = re.compile(r"^\s*(\d{1,2})\s*")

WEEKDAY_MAP = {
    "月": 0,
    "火": 1,
    "水": 2,
    "木": 3,
    "金": 4,
    "土": 5,
    "日": 6,
}

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def log_duration(label, start, end, result="完了"):
    print(
        f"[監視計測] {label} | {result} | 所要 {(end - start) * 1000:.1f} ms"
        f" | 監視開始から {(start - SCRIPT_START) * 1000:.1f} → {(end - SCRIPT_START) * 1000:.1f} ms",
        flush=True,
    )


@contextmanager
def timed_step(label):
    start = time.perf_counter()
    try:
        yield
    except BaseException:
        log_duration(label, start, time.perf_counter(), "中断")
        raise
    else:
        log_duration(label, start, time.perf_counter())


def get_session():
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
        })

        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=2,
            max_retries=0,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session

    return _thread_local.session


def get_discord_session():
    global _discord_session

    if _discord_session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
        })

        adapter = HTTPAdapter(
            pool_connections=1,
            pool_maxsize=1,
            max_retries=0,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _discord_session = session

    return _discord_session


def load_targets():
    data = json.loads(
        Path("targets.json").read_text(encoding="utf-8")
    )
    targets = data.get("targets", [])

    if not targets:
        raise RuntimeError("targets.json に対象施設がありません")

    for target in targets:
        if not target.get("location"):
            raise RuntimeError(
                f'{target.get("name", "不明な施設")} の所在地が未設定です'
            )

        if not target.get("id"):
            raise RuntimeError("施設IDが未設定です")

    return targets


def parse_highlight_weekdays(label, line_number):
    day_match = re.search(
        r"[（(]([^（）()]*)[）)]\s*$",
        label,
    )

    if not day_match:
        return None

    day_text = day_match.group(1).strip()

    if day_text == "全曜日":
        return None

    normalized = re.sub(
        r"[・、,，\s]+",
        "",
        day_text,
    )

    if not normalized:
        raise RuntimeError(
            f"highlight.md {line_number}行目の曜日指定が空です"
        )

    weekdays = set()

    for character in normalized:
        if character not in WEEKDAY_MAP:
            raise RuntimeError(
                f"highlight.md {line_number}行目の曜日指定が正しくありません: "
                f"{day_text}"
            )

        weekdays.add(WEEKDAY_MAP[character])

    return weekdays


def load_highlighted_ids(targets, lines):
    target_ids = [str(target["id"]) for target in targets]
    valid_ids = set(target_ids)

    if len(valid_ids) != len(target_ids):
        raise RuntimeError("targets.json の施設IDが重複しています")

    checkbox_pattern = re.compile(
        r"^\s*-\s+\[(?P<checked>[ xX])\]\s+"
        r"(?P<label>.*?)\s*"
        r"<!--\s*(?P<id>[A-Za-z0-9_-]+)\s*-->\s*$"
    )

    seen_ids = set()
    highlighted_ids = {}

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()

        if stripped == AUTO_BOOKING_SECTION:
            break
        if not stripped.startswith("- ["):
            continue

        match = checkbox_pattern.fullmatch(line)

        if not match:
            raise RuntimeError(
                f"highlight.md {line_number}行目のチェックボックス形式が正しくありません"
            )

        target_id = match.group("id")

        if target_id in seen_ids:
            raise RuntimeError(
                f"highlight.md に施設IDが重複しています: {target_id}"
            )

        seen_ids.add(target_id)

        if match.group("checked").lower() == "x":
            if target_id not in valid_ids:
                raise RuntimeError(
                    f"highlight.md の注目施設IDがtargets.jsonにありません: {target_id}"
                )

            label = match.group("label").strip()

            weekdays = parse_highlight_weekdays(
                label,
                line_number,
            )

            highlighted_ids[target_id] = weekdays

    return highlighted_ids


def load_auto_bookings(targets, lines):
    valid_ids = {str(target["id"]) for target in targets}
    sections = [
        index
        for index, line in enumerate(lines)
        if line.strip() == AUTO_BOOKING_SECTION
    ]

    if not sections:
        return {}

    if len(sections) > 1:
        raise ValueError("自動申込チェックリストの見出しが重複しています")

    pattern = re.compile(
        r"-\s+\[([ xX])\]\s+([A-Za-z0-9_-]+)\s+([0-9]{4}-[0-9]{2}-[0-9]{2})"
    )
    bookings = {}

    for line_number, line in enumerate(lines[sections[0] + 1:], sections[0] + 2):
        line = line.strip()

        if line.startswith("#"):
            break
        if not line.startswith("- ["):
            continue

        match = pattern.fullmatch(line)
        if not match:
            raise ValueError(
                f"highlight.md {line_number}行目の自動申込欄の書式が正しくありません"
            )

        checked, facility_id, day = match.groups()
        stay_date = date.fromisoformat(day)

        if checked.lower() != "x":
            continue

        if facility_id not in valid_ids:
            raise ValueError(
                f"highlight.md {line_number}行目の自動申込施設IDが targets.json にありません"
            )

        bookings.setdefault(facility_id, set()).add(stay_date)

    return bookings


def recover_failed_child(facility_id, day):
    if not deferred_ledger_enabled():
        return False
    try:
        if local_attempt_path(facility_id, day).exists():
            with booking_operation_lock(facility_id, day):
                recover_local_attempt(facility_id, day)
            return True
        return False
    except Exception as error:
        print(f"::warning::{facility_id} {day}: 子プロセス終了後の履歴復旧失敗 ({type(error).__name__})。再申込せず手動確認してください。", flush=True)
        return True


def run_auto_booking(facility_id, day, detected_at):
    if booking_paused():
        return BOOKING_DEFERRED
    key = f"{facility_id} {day}"
    log_duration("申込ワーカー待ち " + key, detected_at, time.perf_counter())
    print(f"自動申込確認: {facility_id} {day}", flush=True)

    try:
        with timed_step("申込子プロセス全体 " + key):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("apply_booking.py")),
                    "--facility-id", facility_id,
                    "--date", day,
                    "--nights", "1",
                    "--persons", "2",
                    "--rooms", "1",
                    "--room-persons", "2",
                    "--finish-from-gmail",
                ],
                env={**os.environ, "BOOKING_DETECTED_AT": str(detected_at),
                     "BOOKING_LAUNCHED_AT": str(time.perf_counter()), "BOOKING_TIMING_KEY": key},
                check=True,
                timeout=270,
            )
    except subprocess.CalledProcessError as error:
        if recover_failed_child(facility_id, day):
            return BOOKING_FAILED
        if error.returncode == BOOKING_RETRY:
            print(f"自動申込の再確認対象: {key}", flush=True)
            return BOOKING_RETRY
        if error.returncode in (BOOKING_PAUSED, BOOKING_DEFERRED):
            return error.returncode
        print(f"自動申込を確認できませんでした: {key} (終了コード {error.returncode})", flush=True)
        return BOOKING_FAILED
    except (subprocess.SubprocessError, OSError) as error:
        recover_failed_child(facility_id, day)
        print(
            f"自動申込を確認できませんでした: "
            f"{facility_id} {day} ({type(error).__name__})"
        )
        return BOOKING_FAILED
    return BOOKING_OK


def is_highlighted_entry(entry, highlighted_ids):
    target_id = str(entry["target_id"])

    if target_id not in highlighted_ids:
        return False

    weekdays = highlighted_ids[target_id]

    if weekdays is None:
        return True

    entry_weekday = date.fromisoformat(
        entry["day"]
    ).weekday()

    return entry_weekday in weekdays


def make_month_url(original_url, year, month, base_date):
    last_day = calendar.monthrange(year, month)[1]
    day = min(base_date.day, last_day)
    new_date = date(year, month, day).isoformat()

    parts = urlsplit(original_url)
    params = parse_qs(parts.query, keep_blank_values=True)
    params["join_date"] = [new_date]

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params, doseq=True),
        parts.fragment,
    ))


def target_months(today):
    result = []

    for offset in range(3):
        month_index = today.year * 12 + today.month - 1 + offset
        year = month_index // 12
        month = month_index % 12 + 1
        result.append((year, month))

    return result


def fetch_html(url):
    check_booking_access()
    with _denied_urls_lock:
        if url in _denied_urls:
            raise BookingAccessDenied("HTTP 403 を受けたカレンダーの再取得を見送ります")
    with get_session().get(
        url,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        try:
            check_site_status(response.status_code, response.headers.get("Retry-After"))
        except BookingAccessDenied:
            with _denied_urls_lock:
                _denied_urls.add(url)
            raise
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text


def detect_daily_statuses(html, year, month):
    if not html or not html.strip():
        return {}

    document = lxml_html.fromstring(html)

    cells = document.xpath(
        "//td[contains(concat(' ', normalize-space(@class), ' '), ' empty ') "
        "or contains(concat(' ', normalize-space(@class), ' '), ' a_little ')]"
    )

    results = {}

    for cell in cells:
        value = " ".join(
            part.strip()
            for part in cell.itertext()
            if part.strip()
        )

        match = DAY_PATTERN.match(value)

        if not match:
            continue

        day = int(match.group(1))

        try:
            calendar_date = date(year, month, day)
        except ValueError:
            continue

        classes = (cell.get("class") or "").split()

        if "a_little" in classes:
            results[calendar_date.isoformat()] = "△"
        elif "empty" in classes:
            results[calendar_date.isoformat()] = "○"

    return results


def load_state():
    if not STATE_PATH.exists():
        return {}

    try:
        return json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError:
        raise RuntimeError(
            "state.json が壊れています。安全のため停止します"
        ) from None


def save_state(state):
    temp_path = STATE_PATH.with_suffix(".tmp")

    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    os.replace(temp_path, STATE_PATH)


def discord_webhook(messages):
    webhook_url = os.environ.get(
        "DISCORD_WEBHOOK_URL", ""
    ).strip()

    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定です")

    session = get_discord_session()

    for message in messages:
        embeds = message.get("embeds", [])

        if not embeds:
            continue

        if len(embeds) > MAX_EMBEDS_PER_MESSAGE:
            raise RuntimeError(
                "DiscordのEmbed数上限を超えています"
            )

        payload = {
            "embeds": embeds
        }

        for attempt in range(DISCORD_MAX_RETRIES + 1):
            try:
                with session.post(
                    webhook_url,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                ) as response:

                    if response.status_code == 429:
                        if attempt >= DISCORD_MAX_RETRIES:
                            raise RuntimeError(
                                f"Discord送信に失敗しました "
                                f"(HTTP 429、再試行上限{DISCORD_MAX_RETRIES}回)"
                            )

                        retry_after = None

                        try:
                            retry_after = float(
                                response.json().get("retry_after")
                            )
                        except (ValueError, TypeError, AttributeError):
                            pass

                        if retry_after is None:
                            try:
                                retry_after = float(
                                    response.headers.get(
                                        "Retry-After",
                                        "1",
                                    )
                                )
                            except (ValueError, TypeError):
                                retry_after = 1.0

                        wait_time = max(
                            0.0,
                            min(retry_after, 60.0),
                        )

                        with timed_step("Discord 429 再試行待ち"):
                            time.sleep(wait_time)
                        continue

                    if not 200 <= response.status_code < 300:
                        error_text = response.text[:500]

                        raise RuntimeError(
                            f"Discord送信に失敗しました "
                            f"(HTTP {response.status_code}: "
                            f"{error_text})"
                        )

                    break

            except requests.RequestException as error:
                raise RuntimeError(
                    f"Discord送信中に通信エラーが発生しました "
                    f"({type(error).__name__}: {error})"
                ) from error


def format_japanese_date(day_text):
    day = date.fromisoformat(day_text)

    return (
        f"{day.year}年{day.month}月{day.day}日"
        f"（{WEEKDAYS_JA[day.weekday()]}）"
    )


def build_notice_blocks(entries, max_block_chars=3800):
    grouped = {}

    for entry in entries:
        day = date.fromisoformat(entry["day"])
        target_id = str(entry["target_id"])
        group_key = (target_id, entry["name"], day.year, day.month)

        if group_key not in grouped:
            grouped[group_key] = {
                "location": entry["location"],
                "url": entry["url"],
                "trip_purpose": entry.get("trip_purpose", ""),
                "bike_trip": entry.get("bike_trip", ""),
                "entries": [],
            }

        grouped[group_key]["entries"].append(entry)

    blocks = []

    for group_key, group in sorted(grouped.items()):
        target_id, facility_name, year, month = group_key
        tag = (target_id, f"{year:04d}-{month:02d}")

        sorted_entries = sorted(group["entries"], key=lambda entry: entry["day"])
        month_heading = []
        if len(sorted_entries) > 1:
            month_heading.append(f"> **📅 {year}年{month}月**")

        daily_lines = []

        for entry in sorted_entries:
            if month_heading:
                day = date.fromisoformat(entry["day"])
                date_label = f"{day.day}日（{WEEKDAYS_JA[day.weekday()]}）"
            else:
                date_label = f'📅 {format_japanese_date(entry["day"])}'

            daily_lines.append(
                f'> **{date_label}｜{entry["status"]}｜{entry["nights"]}泊**'
            )

        details = [f'📍 {group["location"]}']

        if group["trip_purpose"]:
            details.append(f'🎯 {group["trip_purpose"]}')

        if group["bike_trip"]:
            details.append(f'🏍 足立発 {group["bike_trip"]}')

        tail = ["", f'🔗 [申し込みする]({group["url"]})', "", *details]
        current_lines = []

        for daily_line in daily_lines:
            if len("\n".join(month_heading + [daily_line] + tail)) > max_block_chars:
                raise RuntimeError(f"{facility_name} の日付情報とURLが長すぎます")

            candidate = month_heading + current_lines + [daily_line] + tail

            if len("\n".join(candidate)) > max_block_chars:
                blocks.append({
                    "text": "\n".join(month_heading + current_lines + tail),
                    "tags": {tag},
                    "facility_name": facility_name,
                })
                current_lines = []

            current_lines.append(daily_line)

        blocks.append({
            "text": "\n".join(month_heading + current_lines + tail),
            "tags": {tag},
            "facility_name": facility_name,
        })

    return blocks


def pack_notice_messages(blocks):
    messages = []
    current_embeds = []
    current_tags = set()
    current_chars = 0

    for block in blocks:
        title = block["embed_title"]
        description = block["text"]

        if len(title) > 256:
            raise RuntimeError(
                "Embedタイトルが長すぎます"
            )

        if len(description) > 4096:
            raise RuntimeError(
                "Embed本文が長すぎます"
            )

        embed = {
            "title": title,
            "description": description,
            "color": block["color"],
        }

        embed_chars = len(title) + len(description)

        needs_new_message = (
            len(current_embeds) >= MAX_EMBEDS_PER_MESSAGE
            or current_chars + embed_chars > MAX_EMBED_TOTAL_CHARS
        )

        if needs_new_message:
            messages.append({
                "embeds": current_embeds,
                "tags": set(current_tags),
            })

            current_embeds = []
            current_tags = set()
            current_chars = 0

        current_embeds.append(embed)
        current_tags.update(block["tags"])
        current_chars += embed_chars

    if current_embeds:
        messages.append({
            "embeds": current_embeds,
            "tags": set(current_tags),
        })

    return messages


RECHECK_INTERVAL = 4


class BookingRound:
    def __init__(self, executor, jobs):
        self.executor = executor
        self.jobs = jobs
        self.attempts = {}
        self.lock = threading.Lock()

    def _state(self, key):
        if self.attempts.get(key, 0) >= MAX_BOOKING_ATTEMPTS:
            return "stop"
        job = self.jobs.get(key)
        if job is not None:
            if not job.done():
                return "wait"
            if job.result() not in (BOOKING_RETRY, BOOKING_DEFERRED):
                return "stop"
        return "wait" if booking_paused() else "ready"

    def state(self, facility_id, day):
        with self.lock:
            return self._state((facility_id, day))

    def submit(self, facility_id, day):
        detected_at = time.perf_counter()
        key = (facility_id, day)
        with self.lock:
            if self._state(key) != "ready":
                return False
            attempt = self.attempts.get(key, 0) + 1
            self.attempts[key] = attempt
            print(f"自動申込対象の空室を検知: {facility_id} {day} (試行 {attempt}/{MAX_BOOKING_ATTEMPTS})", flush=True)
            self.jobs[key] = self.executor.submit(run_auto_booking, facility_id, day, detected_at)
            return True


def recheck_month(target_id, year, month, url, days, started, booking_round, request_slots):
    label = f"{target_id} {year:04d}-{month:02d}"
    remaining = set(days)
    had_error = False
    for offset in range(RECHECK_INTERVAL, 60, RECHECK_INTERVAL):
        remaining = {day for day in remaining if booking_round.state(target_id, day) != "stop"}
        if not remaining:
            break
        deadline = started + offset
        delay = deadline - time.perf_counter()
        if delay <= 0:
            continue
        time.sleep(delay)
        ready = [day for day in remaining if booking_round.state(target_id, day) == "ready"]
        if not ready:
            continue
        # Skip stale slots instead of issuing catch-up requests after a slow response.
        latest = deadline + RECHECK_INTERVAL
        wait = latest - time.perf_counter()
        if wait <= 0 or not request_slots.acquire(timeout=wait):
            continue
        try:
            if time.perf_counter() >= latest:
                continue
            with timed_step(f"追加カレンダー GET {label} +{offset}秒"):
                html = fetch_html(url)
            with timed_step(f"追加カレンダー解析 {label} +{offset}秒"):
                current = detect_daily_statuses(html, year, month)
            for day in sorted(ready):
                if current.get(day) in ("○", "△"):
                    booking_round.submit(target_id, day)
        except BookingAccessDenied as error:
            print(f"追加確認中止 {label}: {error}", flush=True)
            return False
        except BookingAccessPaused as error:
            print(f"追加確認休止 {label}: {error}", flush=True)
            had_error = True
        except Exception as error:
            print(f"追加確認失敗 {label} +{offset}秒 ({type(error).__name__})", flush=True)
            had_error = True
        finally:
            request_slots.release()
    return not had_error


def scan_target(target, months_to_scan, previous_months, is_baseline, booking_days, booking_round, queued_at, schedule_recheck):
    target_id = str(target["id"])
    log_duration(f"監視ワーカー待ち {target_id}", queued_at, time.perf_counter())
    name = target["name"]
    location = target["location"]
    nights = target["nights"]
    base_date = date.fromisoformat(target["date"])

    current_months = {}
    newly_available_entries = []

    for year, month in months_to_scan:
        month_key = f"{year:04d}-{month:02d}"

        url = make_month_url(
            target["url"],
            year,
            month,
            base_date,
        )

        remaining = [day for day in booking_days if day[:7] == month_key]
        started = time.perf_counter()
        try:
            with timed_step(f"カレンダー GET {target_id} {month_key}"):
                html = fetch_html(url)
            with timed_step(f"カレンダー解析 {target_id} {month_key}"):
                current = detect_daily_statuses(html, year, month)
            for day in remaining.copy():
                if current.get(day) in ("○", "△"):
                    booking_round.submit(target_id, day)
        finally:
            remaining = [day for day in remaining if booking_round.state(target_id, day) != "stop"]
            if remaining:
                schedule_recheck(target_id, year, month, url, remaining, started)

        current_months[month_key] = current
        previous = previous_months.get(month_key, {})

        if is_baseline or month_key not in previous_months:
            continue

        newly_available = {
            day: status
            for day, status in current.items()
            if day not in previous
        }

        for day, status in newly_available.items():
            newly_available_entries.append({
                "target_id": target_id,
                "name": name,
                "location": location,
                "day": day,
                "status": status,
                "nights": nights,
                "trip_purpose": target.get("trip_purpose", ""),
                "bike_trip": target.get("bike_trip", ""),
                "url": target.get("access_url", url),
            })

    return {
        "target_id": target_id,
        "months": current_months,
        "entries": newly_available_entries,
        "is_baseline": is_baseline,
    }


def fetch_checklist():
    branch = os.environ.get("BOOKING_BRANCH") or "main"
    subprocess.run(["git", "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{branch}"],
                   check=True, timeout=20)
    snapshot = subprocess.check_output(["git", "show", "FETCH_HEAD:highlight.md"], timeout=5)
    return snapshot.decode("utf-8-sig").splitlines()


def main(hourly=False):
    with _denied_urls_lock:
        _denied_urls.clear()
    with tempfile.TemporaryDirectory(prefix="its-booking-run-") as directory:
        previous = os.environ.get("BOOKING_PAUSE_FILE")
        os.environ["BOOKING_PAUSE_FILE"] = str(Path(directory) / "paused")
        try:
            run_monitor(hourly)
        finally:
            if previous is None:
                os.environ.pop("BOOKING_PAUSE_FILE", None)
            else:
                os.environ["BOOKING_PAUSE_FILE"] = previous


def run_monitor(hourly=False):
    lines = Path("highlight.md").read_text(encoding="utf-8-sig").splitlines()
    booking_jobs = {}
    ok = True
    started = time.monotonic()
    end = started + 3600
    next_round = started
    with ThreadPoolExecutor(max_workers=MAX_BOOKING_WORKERS) as booking_executor, ThreadPoolExecutor(max_workers=1) as settings_executor:
        refresh = None
        while True:
            if hourly and time.monotonic() >= end:
                break
            remaining = cooldown_remaining()
            if remaining > 0:
                if not hourly:
                    ok = False
                    break
                wait = min(remaining, max(0.0, end - time.monotonic()))
                print(f"施設アクセス休止: 約 {wait:.1f} 秒待機します", flush=True)
                time.sleep(wait)
                # Re-read the shared deadline because another in-flight request may extend it.
                if not booking_paused() and time.monotonic() < end:
                    print("施設アクセス休止解除: 監視を再開します", flush=True)
                    next_round = time.monotonic()
                continue
            allow_booking = True
            if refresh is not None:
                try:
                    lines = refresh.result()
                except (OSError, UnicodeError, subprocess.SubprocessError) as error:
                    print(f"チェックリスト更新失敗。この回の自動申込を休止: {type(error).__name__}", flush=True)
                    allow_booking = False
                    ok = False
            if hourly:
                if time.monotonic() >= end:
                    break
                refresh = settings_executor.submit(fetch_checklist)
            try:
                with ExitStack() as background:
                    if not monitor(booking_executor, background, booking_jobs, lines, allow_booking):
                        ok = False
            except (OSError, ValueError, RuntimeError) as error:
                if not hourly:
                    raise
                ok = False
                print(f"監視エラー: {type(error).__name__}: {error}", flush=True)
            if not hourly:
                break
            if booking_paused():
                continue
            next_round = max(next_round + 60, time.monotonic())
            if next_round >= end:
                break
            time.sleep(max(0, next_round - time.monotonic()))

        with timed_step("残りの申込完了待ち"):
            for job in booking_jobs.values():
                if job.result() != BOOKING_OK:
                    ok = False
    if not ok:
        sys.exit(1)


def monitor(booking_executor, background, booking_jobs, lines, allow_booking):
    today = datetime.now(JST).date()

    with timed_step("施設・注目設定読込"):
        targets = load_targets()
        highlighted_ids = load_highlighted_ids(targets, lines)
    auto_bookings = {}
    auto_ok = True

    try:
        with timed_step("自動申込設定読込"):
            auto_bookings = load_auto_bookings(targets, lines) if allow_booking else {}

        if auto_bookings and os.environ.get("GITHUB_ACTIONS") != "true":
            auto_bookings = {}
            raise RuntimeError("自動申込は GitHub Actions から実行してください")
    except (ValueError, RuntimeError) as error:
        print(f"自動申込設定エラー: {error}")
        auto_ok = False

    print(
        f"監視対象: {len(targets)}施設、"
        f"注目施設: {len(highlighted_ids)}施設"
    )

    with timed_step("状態読込"):
        state = load_state()
    failures = []
    successful_results = []
    baseline_count = 0
    months_to_scan = target_months(today)
    jobs = {}
    for key, job in list(booking_jobs.items()):
        if job.done() and job.result() in (BOOKING_RETRY, BOOKING_DEFERRED):
            if job.result() == BOOKING_RETRY:
                auto_ok = False
            del booking_jobs[key]
    booking_lock = threading.Lock()
    booking_round = BookingRound(booking_executor, booking_jobs)
    recheck_jobs = []
    request_slots = threading.BoundedSemaphore(MAX_WORKERS)

    def schedule_recheck(target_id, year, month, url, days, started):
        job = recheck_executor.submit(
            recheck_month, target_id, year, month, url, days, started, booking_round, request_slots
        )
        with booking_lock:
            recheck_jobs.append(job)

    active_days = {
        facility_id: sorted(day.isoformat() for day in days if day >= today)
        for facility_id, days in auto_bookings.items()
    }

    month_keys = {f"{year:04d}-{month:02d}" for year, month in months_to_scan}
    recheck_count = sum(len({day[:7] for day in days} & month_keys) for days in active_days.values())
    recheck_executor = background.enter_context(ThreadPoolExecutor(max_workers=max(1, recheck_count)))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for target in sorted(targets, key=lambda item: not active_days.get(str(item["id"]))):
            target_id = str(target["id"])
            old_entry = state.get(target_id, {})
            is_baseline = not old_entry or "months" not in old_entry
            previous_months = old_entry.get("months", {})

            booking_days = active_days.get(target_id, ())
            priority_months = {day[:7] for day in booking_days}
            ordered_months = sorted(months_to_scan, key=lambda ym: f"{ym[0]:04d}-{ym[1]:02d}" not in priority_months)

            future = executor.submit(
                scan_target,
                target,
                ordered_months,
                previous_months,
                is_baseline,
                booking_days,
                booking_round,
                time.perf_counter(),
                schedule_recheck,
            )

            jobs[future] = target_id

        for future in as_completed(jobs):
            target_id = jobs[future]

            try:
                result = future.result()
                successful_results.append(result)

                if result["is_baseline"]:
                    baseline_count += 1

            except Exception as error:
                failures.append(target_id)
                print(f"ERROR target={target_id}: {type(error).__name__}: {error}")

    all_notices = []
    tag_current_data = {}

    with timed_step("差分集計"):
        for result in successful_results:
            target_id = result["target_id"]
            current_months = result["months"]
            entries = result["entries"]

            old_entry = state.setdefault(target_id, {"months": {}})
            old_months = old_entry.setdefault("months", {})

            pending_month_keys = {
                date.fromisoformat(entry["day"]).strftime("%Y-%m")
                for entry in entries
            }

            for month_key, current_data in current_months.items():
                if month_key in pending_month_keys:
                    tag_current_data[(target_id, month_key)] = current_data
                else:
                    old_months[month_key] = current_data

            all_notices.extend(entries)

    with timed_step("状態保存"):
        save_state(state)

    notification_error = None

    if all_notices:
        try:
            with timed_step("通知組立"):
                highlighted_notices = []
                standard_notices = []

                for entry in all_notices:
                    if is_highlighted_entry(entry, highlighted_ids):
                        highlighted_notices.append(entry)
                    else:
                        standard_notices.append(entry)

                notice_groups = [
                    (highlighted_notices, "🚨", COLOR_HIGHLIGHTED),
                    (standard_notices, "🆕", COLOR_STANDARD),
                ]

                all_blocks = []

                for notice_group, title, color in notice_groups:
                    if not notice_group:
                        continue

                    blocks = build_notice_blocks(notice_group)

                    for block in blocks:
                        block["embed_title"] = f'{title} {block["facility_name"]}'
                        block["color"] = color

                    all_blocks.extend(blocks)

                messages = pack_notice_messages(all_blocks)

                tag_message_indexes = {}

                for index, message in enumerate(messages):
                    for tag in message["tags"]:
                        tag_message_indexes.setdefault(tag, set()).add(index)

                delivered_indexes = set()
                committed_tags = set()
                total_batches = len(messages)

            print("\n通知メッセージ数:", total_batches)
            print("Embed数:", sum(len(message["embeds"]) for message in messages))

            for index, message in enumerate(messages):
                print(f"Discord送信: {index + 1}/{total_batches} （1メッセージ）")
                with timed_step(f"Discord送信 {index + 1}/{total_batches}"):
                    discord_webhook([message])
                delivered_indexes.add(index)

                for tag, indexes in tag_message_indexes.items():
                    if tag in committed_tags:
                        continue

                    if indexes.issubset(delivered_indexes):
                        target_id, month_key = tag
                        state[target_id]["months"][month_key] = tag_current_data[tag]
                        committed_tags.add(tag)

                with timed_step("通知後の状態保存"):
                    save_state(state)

            print(f"\n通知送信成功: {len(all_notices)}件")

        except Exception as error:
            notification_error = error
            print("\nDiscord通知エラー:", f"{type(error).__name__}: {error}")

    else:
        print("\n新しい空室はありません。")

    if baseline_count:
        print(f"\n通知なしで基準を作成した施設数: {baseline_count}")

    if failures:
        print("失敗した施設:", ", ".join(failures))

    if notification_error:
        print("通知の一部または全部が未完了です。")

    if recheck_jobs:
        with timed_step("追加確認の完了待ち"):
            for job in recheck_jobs:
                if not job.result():
                    auto_ok = False

    return not (failures or notification_error or not auto_ok)


if __name__ == "__main__":
    log_duration("初期読込", SCRIPT_START, time.perf_counter())
    with timed_step("監視全体"):
        main(hourly="--hourly" in sys.argv[1:])