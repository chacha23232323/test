import json
import os
import re
import sys
import calendar
import threading
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
JST = ZoneInfo("Asia/Tokyo")

# 同時アクセス数。まずは8件に抑えてサイトへの負荷を避ける
MAX_WORKERS = 8
REQUEST_TIMEOUT = (5, 25)

_thread_local = threading.local()


def get_session():
    """スレッドごとにHTTPセッションを1つ用意して接続を再利用する"""
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
        })
        _thread_local.session = session
    return _thread_local.session


def load_targets():
    data = json.loads(Path("targets.json").read_text(encoding="utf-8"))
    targets = data.get("targets", [])
    if not targets:
        raise RuntimeError("targets.json に対象施設がありません")

    for target in targets:
        if not target.get("location"):
            raise RuntimeError(
                f'{target.get("name", "不明な施設")} の所在地が未設定です'
            )

    return targets


def make_month_url(original_url, year, month, base_date):
    """施設固有のURL情報を維持し、join_dateの年月を変更する"""
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
    """当月・翌月・翌々月を返す"""
    result = []
    for offset in range(3):
        month_index = today.year * 12 + today.month - 1 + offset
        year = month_index // 12
        month = month_index % 12 + 1
        result.append((year, month))
    return result


def fetch_html(url):
    response = get_session().get(url, timeout=REQUEST_TIMEOUT)
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
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError(
            "state.json が壊れています。安全のため停止します"
        )


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def line_push(message):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user = os.environ.get("LINE_USER_ID", "").strip()

    if not token or not user:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定です"
        )

    if len(message) > 4900:
        raise RuntimeError(
            f"LINE通知が長すぎます（{len(message)}文字）。通知は送信していません"
        )

    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user,
            "messages": [{"type": "text", "text": message}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def format_japanese_date(day_text):
    day = date.fromisoformat(day_text)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    return (
        f"{day.year}年{day.month}月{day.day}日"
        f"（{weekdays[day.weekday()]}）"
    )


def format_notice(title, entries):
    """施設・所在地・対象月ごとにまとめ、月別カレンダーURLを付ける"""
    lines = [title, ""]

    grouped = {}

    for entry in entries:
        day = date.fromisoformat(entry["day"])
        month_key = (entry["name"], day.year, day.month)

        if month_key not in grouped:
            grouped[month_key] = {
                "location": entry["location"],
                "url": entry["url"],
                "nights": entry["nights"],
                "entries": [],
            }

        grouped[month_key]["entries"].append(entry)

    for (name, year, month), group in sorted(grouped.items()):
        lines.append(f"■ {name}")
        lines.append(f'📍 {group["location"]}')
        lines.append(f"【{year}年{month}月】")

        for entry in sorted(
            group["entries"],
            key=lambda x: x["day"],
        ):
            lines.append(
                f'・{format_japanese_date(entry["day"])} '
                f'{entry["status"]}｜{entry["nights"]}泊'
            )

        lines.append("▼ この月の空室カレンダー")
        lines.append(group["url"])
        lines.append("")

    lines.append("空室状況は変動します。予約前に最新状況をご確認ください。")

    return "\n".join(lines)


def scan_target(target, today, previous_months, is_baseline):
    """1施設の3か月分を確認する。施設単位で並行実行する"""
    target_id = target["id"]
    name = target["name"]
    location = target["location"]
    nights = target["nights"]
    base_date = date.fromisoformat(target["date"])

    current_months = {}
    newly_available_entries = []

    print("\n" + "=" * 45)
    print("施設:", name)
    print("所在地:", location)
    print("=" * 45)

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

        print(f"\n【{year}年{month}月】")
        print(f"空室日数: {len(current)}日")

        if current:
            for day_text, status in sorted(current.items()):
                print(
                    f"  {format_japanese_date(day_text)} "
                    f"{status}"
                )
        else:
            print("  空室はありません")

        previous = previous_months.get(month_key, {})

        if is_baseline or month_key not in previous_months:
            continue

        newly_available = {
            day: status
            for day, status in current.items()
            if day not in previous
        }

        if newly_available:
            print(f"新しく空室になった日: {len(newly_available)}日")

        for day, status in newly_available.items():
            newly_available_entries.append({
                "name": name,
                "location": location,
                "day": day,
                "status": status,
                "nights": nights,
                "url": url,
            })

    if is_baseline:
        print("\n初回・旧形式からの移行: 通知せず基準を保存します。")

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
    state = load_state()

    meta = state.setdefault("_meta", {})

    failures = []
    normal_notices = []
    release_notices = []
    baseline_count = 0

    release_key = today.strftime("%Y-%m")
    is_release_day = today.day == 27

    release_already_sent = (
        meta.get("release_batch_sent") == release_key
    )

    # 施設ごとに過去状態を読み取り、最大8施設を同時に確認する
    jobs = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for target in targets:
            target_id = target["id"]
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
                state[result["target_id"]] = {
                    "months": result["months"]
                }

                if result["is_baseline"]:
                    baseline_count += 1

                if is_release_day:
                    if not release_already_sent:
                        release_notices.extend(result["entries"])
                else:
                    normal_notices.extend(result["entries"])

            except Exception as error:
                failures.append(target_id)
                print(
                    f"ERROR target={target_id}: "
                    f"{type(error).__name__}: {error}"
                )

    # 27日の受付開始通知は全施設分を1通にまとめる
    if (
        is_release_day
        and not release_already_sent
        and not failures
    ):
        if release_notices:
            message = format_notice(
                "【ITS健保 受付開始・空室まとめ】",
                release_notices,
            )

            line_push(message)

            print(
                f"27日まとめ通知成功: "
                f"{len(release_notices)}件"
            )

            meta["release_batch_sent"] = release_key

        else:
            print(
                "27日まとめ通知: 新しい空室はありません。"
                "次回の確認でもう一度チェックします。"
            )

    # 通常日は新しく空いた日だけまとめて通知
    if not is_release_day and normal_notices:
        message = format_notice(
            "【ITS健保 新しい空室】",
            normal_notices,
        )

        line_push(message)

        print(f"通常通知成功: {len(normal_notices)}件")

    save_state(state)

    if baseline_count:
        print(
            f"\n通知なしで基準を作成した施設数: "
            f"{baseline_count}"
        )

    elapsed = (datetime.now(JST) - started_at).total_seconds()
    print(f"\n処理時間: {elapsed:.1f}秒")
    print(f"同時アクセス上限: {MAX_WORKERS}施設")

    if failures:
        print("失敗した施設:", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
