import json
import os
import re
import sys
import calendar
import threading
import time
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
JST = ZoneInfo("Asia/Tokyo")

MAX_WORKERS = 12
REQUEST_TIMEOUT = (5, 25)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TOTAL_CHARS = 5800
DISCORD_MAX_RETRIES = 5

COLOR_HIGHLIGHTED = 0xE74C3C
COLOR_STANDARD = 0x3498DB
COLOR_COMMON = 0x5865F2

COMMON_CALENDAR_URL = os.environ.get("COMMON_CALENDAR_URL", "").strip()

_thread_local = threading.local()


def get_session():
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
        })
        _thread_local.session = session

    return _thread_local.session


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


def load_highlighted_ids(targets):
    try:
        lines = Path("highlight.md").read_text(
            encoding="utf-8-sig"
        ).splitlines()
    except FileNotFoundError:
        raise RuntimeError("highlight.md がありません") from None

    target_ids = [str(target["id"]) for target in targets]
    valid_ids = set(target_ids)

    if len(valid_ids) != len(target_ids):
        raise RuntimeError("targets.json の施設IDが重複しています")

    checkbox_pattern = re.compile(
        r"^\s*-\s+\[(?P<checked>[ xX])\]\s+.*?"
        r"<!--\s*(?P<id>[A-Za-z0-9_-]+)\s*-->\s*$"
    )

    seen_ids = set()
    highlighted_ids = set()

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()

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

            highlighted_ids.add(target_id)

    return highlighted_ids


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
    response = get_session().get(
        url,
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding

    return response.text


def detect_daily_statuses(html, year, month):
    soup = BeautifulSoup(html, "html.parser")
    results = {}

    for cell in soup.select("td.empty, td.a_little"):
        value = cell.get_text(" ", strip=True)
        match = re.match(r"^\s*(\d{1,2})\s*", value)

        if not match:
            continue

        day = int(match.group(1))

        try:
            calendar_date = date(year, month, day)
        except ValueError:
            continue

        classes = cell.get("class", [])

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
        )


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
                response = requests.post(
                    webhook_url,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as error:
                raise RuntimeError(
                    f"Discord送信中に通信エラーが発生しました "
                    f"({type(error).__name__})"
                ) from None

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
                            response.headers.get("Retry-After", "1")
                        )
                    except (ValueError, TypeError):
                        retry_after = 1.0

                time.sleep(max(0.0, min(retry_after, 60.0)))
                continue

            if not 200 <= response.status_code < 300:
                raise RuntimeError(
                    f"Discord送信に失敗しました "
                    f"(HTTP {response.status_code})"
                )

            break


def format_japanese_date(day_text):
    day = date.fromisoformat(day_text)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    return (
        f"{day.year}年{day.month}月{day.day}日"
        f"（{weekdays[day.weekday()]}）"
    )


def build_notice_blocks(
    entries,
    include_facility_urls,
    common_url=None,
    max_block_chars=3800,
):

    grouped = {}

    for entry in entries:
        day = date.fromisoformat(entry["day"])
        target_id = str(entry["target_id"])

        month_key = f"{day.year:04d}-{day.month:02d}"

        group_key = (
            target_id,
            entry["name"],
            day.year,
            day.month,
        )

        if group_key not in grouped:
            grouped[group_key] = {
                "target_id": target_id,
                "month_key": month_key,
                "location": entry["location"],
                "url": entry["url"],
                "nights": entry["nights"],
                "trip_purpose": entry.get("trip_purpose", ""),
                "bike_trip": entry.get("bike_trip", ""),
                "entries": [],
            }

        grouped[group_key]["entries"].append(entry)

    blocks = []
    all_tags = set()

    for group_key, group in sorted(grouped.items()):
        target_id = group["target_id"]
        month_key = group["month_key"]
        tag = (target_id, month_key)

        all_tags.add(tag)

        year = group_key[2]
        month = group_key[3]

        header = [
            f'🏨 **{group_key[1]}**',
            f'📍 {group["location"]}',
            f'📅 {year}年{month}月',
        ]

        if group["trip_purpose"]:
            header.append(
                f'🎯 {group["trip_purpose"]}'
            )

        if group["bike_trip"]:
            header.append(
                f'🏍 足立発 {group["bike_trip"]}'
            )

        daily_lines = []

        for entry in sorted(
            group["entries"],
            key=lambda x: x["day"],
        ):
            daily_lines.append(
                f'> **📅 {format_japanese_date(entry["day"])}**\n'
                f'> **{entry["status"]}｜{entry["nights"]}泊**'
            )

        tail = []

        if include_facility_urls:
            tail = [
                f'🔗 [申し込みする]({group["url"]})'
            ]

        current_lines = []

        for daily_line in daily_lines:
            candidate = (
                header
                + current_lines
                + [daily_line]
                + tail
            )

            if len("\n".join(candidate)) <= max_block_chars:
                current_lines.append(daily_line)
                continue

            if not current_lines:
                raise RuntimeError(
                    f"{group_key[1]} の日付情報1件が長すぎます"
                )

            blocks.append({
                "text": "\n".join(header + current_lines + tail),
                "tags": {tag},
            })

            current_lines = [daily_line]

            if len(
                "\n".join(header + current_lines + tail)
            ) > max_block_chars:
                raise RuntimeError(
                    f"{group_key[1]} の日付情報とURLが長すぎます"
                )

        if current_lines:
            blocks.append({
                "text": "\n".join(header + current_lines + tail),
                "tags": {tag},
            })

    if common_url and all_tags:
        footer = "\n".join([
            "受付開始した施設の申し込みはこちらから確認できます。",
            f"🔗 [施設申し込みを開く]({common_url})",
        ])

        if len(footer) > max_block_chars:
            raise RuntimeError(
                "共通URLの通知ブロックが長すぎます"
            )

        blocks.append({
            "text": footer,
            "tags": set(all_tags),
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


def scan_target(target, today, previous_months, is_baseline):
    target_id = str(target["id"])
    name = target["name"]
    location = target["location"]
    nights = target["nights"]
    base_date = date.fromisoformat(target["date"])

    current_months = {}
    newly_available_entries = []

    for year, month in target_months(today):
        month_key = f"{year:04d}-{month:02d}"
        url = make_month_url(
            target["url"],
            year,
            month,
            base_date,
        )

        html = fetch_html(url)
        current = detect_daily_statuses(html, year, month)
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


def main():
    started_at = datetime.now(JST)
    today = started_at.date()

    targets = load_targets()
    highlighted_ids = load_highlighted_ids(targets)

    print(
        f"監視対象: {len(targets)}施設、"
        f"注目施設: {len(highlighted_ids)}施設"
    )

    state = load_state()
    meta = state.setdefault("_meta", {})

    failures = []
    successful_results = []
    baseline_count = 0

    is_release_day = today.day == 27
    release_key = today.strftime("%Y-%m")

    if is_release_day:
        if meta.get("release_progress_month") != release_key:
            meta["release_progress_month"] = release_key
            meta["release_done_targets"] = []

        release_done = set(
            str(x) for x in meta.get("release_done_targets", [])
        )

        if meta.get("release_batch_sent") == release_key:
            release_done = {
                str(target["id"]) for target in targets
            }
            meta["release_done_targets"] = list(release_done)

        print("\n【27日 受付開始通知モード】")

    else:
        release_done = set()

    scan_targets = [
        target
        for target in targets
        if not (
            is_release_day
            and str(target["id"]) in release_done
        )
    ]

    if not scan_targets:
        print("27日の対象施設はすべて処理済みです。")
        return

    jobs = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for target in scan_targets:
            target_id = str(target["id"])
            old_entry = state.get(target_id, {})

            is_baseline = (
                not old_entry
                or "months" not in old_entry
            )

            previous_months = old_entry.get("months", {})

            future = executor.submit(
                scan_target,
                target,
                today,
                previous_months,
                is_baseline,
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

                print(
                    f"ERROR target={target_id}: "
                    f"{type(error).__name__}"
                )

    all_notices = []
    pending_tags = set()
    target_tags = {}
    tag_current_data = {}

    for result in successful_results:
        target_id = result["target_id"]
        current_months = result["months"]
        entries = result["entries"]

        old_entry = state.setdefault(
            target_id,
            {"months": {}}
        )

        old_months = old_entry.setdefault("months", {})

        pending_month_keys = {
            date.fromisoformat(entry["day"]).strftime("%Y-%m")
            for entry in entries
        }

        for month_key, current_data in current_months.items():
            if month_key not in pending_month_keys:
                old_months[month_key] = current_data

        for month_key in pending_month_keys:
            tag = (target_id, month_key)

            pending_tags.add(tag)
            target_tags.setdefault(target_id, set()).add(tag)
            tag_current_data[tag] = current_months[month_key]

        all_notices.extend(entries)

        if is_release_day and not pending_month_keys:
            release_done.add(target_id)

    meta["release_done_targets"] = sorted(release_done)

    save_state(state)

    notification_error = None

    if all_notices:
        try:
            if is_release_day:
                standard_title = "🆕 通常施設"
                highlighted_title = "🚨 注目施設"
            else:
                standard_title = "🆕 通常施設"
                highlighted_title = "🚨 注目施設"

            highlighted_notices = [
                entry for entry in all_notices
                if entry["target_id"] in highlighted_ids
            ]

            standard_notices = [
                entry for entry in all_notices
                if entry["target_id"] not in highlighted_ids
            ]

            notice_groups = [
                (
                    highlighted_notices,
                    highlighted_title,
                    COLOR_HIGHLIGHTED,
                ),
                (
                    standard_notices,
                    standard_title,
                    COLOR_STANDARD,
                ),
            ]

            all_blocks = []

            for notice_group, title, color in notice_groups:
                if not notice_group:
                    continue

                blocks = build_notice_blocks(
                    notice_group,
                    include_facility_urls=not is_release_day,
                    common_url=None,
                    max_block_chars=3800,
                )

                for block in blocks:
                    block["embed_title"] = title
                    block["color"] = color

                all_blocks.extend(blocks)

            if is_release_day and pending_tags:
                if not COMMON_CALENDAR_URL.startswith(
                    ("https://", "http://")
                ):
                    raise RuntimeError(
                        "COMMON_CALENDAR_URLが正しくありません"
                    )

                all_blocks.append({
                    "text": (
                        "受付開始した施設の申し込みはこちらから確認できます。\n"
                        f"🔗 [施設申し込みを開く]({COMMON_CALENDAR_URL})"
                    ),
                    "tags": set(pending_tags),
                    "embed_title": "🔗 施設申し込み",
                    "color": COLOR_COMMON,
                })

            messages = pack_notice_messages(all_blocks)

            tag_message_indexes = {}

            for index, message in enumerate(messages):
                for tag in message["tags"]:
                    tag_message_indexes.setdefault(
                        tag,
                        set()
                    ).add(index)

            delivered_indexes = set()
            committed_tags = set()

            total_batches = len(messages)

            print("\n通知メッセージ数:", len(messages))
            print(
                "Embed数:",
                sum(
                    len(message["embeds"])
                    for message in messages
                )
            )

            for batch_number, start in enumerate(
                range(0, len(messages), 1),
                start=1,
            ):
                batch = messages[start:start + 1]

                print(
                    f"Discord送信: "
                    f"{batch_number}/{total_batches} "
                    f"（{len(batch)}メッセージ）"
                )

                discord_webhook(batch)

                delivered_indexes.update(
                    range(start, start + len(batch))
                )

                for tag, indexes in tag_message_indexes.items():
                    if tag in committed_tags:
                        continue

                    if indexes.issubset(delivered_indexes):
                        target_id, month_key = tag

                        state.setdefault(
                            target_id,
                            {"months": {}}
                        )

                        state[target_id].setdefault(
                            "months", {}
                        )[month_key] = tag_current_data[tag]

                        committed_tags.add(tag)

                if is_release_day:
                    for target_id, tags in target_tags.items():
                        if tags.issubset(committed_tags):
                            release_done.add(target_id)

                    meta["release_done_targets"] = sorted(
                        release_done
                    )

                save_state(state)

            print(
                f"\n通知送信成功: "
                f"{len(all_notices)}件"
            )

        except Exception as error:
            notification_error = error

            print(
                "\nDiscord通知エラー:",
                f"{type(error).__name__}"
            )

    else:
        print("\n新しい空室はありません。")

    if is_release_day:
        meta["release_done_targets"] = sorted(release_done)
        save_state(state)

    if baseline_count:
        print(
            f"\n通知なしで基準を作成した施設数: "
            f"{baseline_count}"
        )

    elapsed = (
        datetime.now(JST) - started_at
    ).total_seconds()

    print(f"\n処理時間: {elapsed:.1f}秒")
    print(f"同時アクセス上限: {MAX_WORKERS}施設")

    if failures:
        print("失敗した施設:", ", ".join(failures))

    if notification_error:
        print("通知の一部または全部が未完了です。")

    if failures or notification_error:
        sys.exit(1)


if __name__ == "__main__":
    main()

