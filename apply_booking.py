"""Search an ITS Kenpo facility, hold a matching room, and request email verification."""

import argparse
import json
import os
import re
import sys
from datetime import date
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from booking_ledger import BookingLedger

BASE_URL = "https://as.its-kenpo.or.jp"
TIMEOUT = (10, 30)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facility-id", required=True, help="Facility id from targets.json")
    parser.add_argument("--date", required=True, help="Stay start date (YYYY-MM-DD)")
    parser.add_argument("--nights", type=int, choices=(1, 2), default=1)
    parser.add_argument("--persons", type=int, required=True)
    parser.add_argument("--rooms", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--room-persons", required=True, help="Comma-separated guests per room")
    parser.add_argument("--room-number", type=int, help="Preferred room number; otherwise first room that fits")
    parser.add_argument("--email", default=os.environ.get("BOOKING_EMAIL", ""))
    return parser.parse_args()


def fail(message):
    raise RuntimeError(message)


def get_apply_url(facility_id):
    data = json.loads(open("targets.json", encoding="utf-8").read())
    for target in data.get("targets", []):
        if str(target.get("id")) == str(facility_id):
            apply_url = target.get("access_url", "").strip()
            if not apply_url:
                fail(f"{facility_id} に access_url がありません")
            if not apply_url.startswith("https://as.its-kenpo.or.jp/apply/empty_new?"):
                fail(f"{facility_id} の access_url が申込ページ形式ではありません")
            return apply_url
    fail(f"targets.json に施設ID {facility_id} がありません")


def form_values(form):
    values = {}

    for field in form.select("input[name], select[name], textarea[name]"):
        name = field.get("name")
        if not name or field.has_attr("disabled"):
            continue

        tag = field.name
        field_type = field.get("type", "").lower()

        if tag == "input":
            if field_type in ("submit", "button", "reset", "file"):
                continue

            if field_type in ("checkbox", "radio"):
                if not field.has_attr("checked"):
                    continue

            values[name] = field.get("value", "")

        elif tag == "select":
            selected = field.select_one("option[selected]")
            if selected is None:
                selected = field.select_one("option")

            if selected:
                values[name] = selected.get("value", selected.get_text(strip=True))

        elif tag == "textarea":
            values[name] = field.get_text()

    return values


def booking_url(current_url, destination):
    url = urljoin(current_url, destination)
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.netloc) != ("https", urlsplit(BASE_URL).netloc):
        fail("ITS健保以外の申込先が指定されました")
    return url


def submit_consent(session, page, current_url):
    control = next((element for element in page.select("input,button")
                    if (element.get("value") or element.get_text(" ", strip=True)) == "同意する"), None)
    if not control:
        fail("個人情報の取扱い同意ボタンが見つかりません")
    form = control.find_parent("form")
    if form:
        action = booking_url(current_url, form.get("action", current_url))
        method = form.get("method", "post").lower()
        values = form_values(form)
        if control.get("name"):
            values[control["name"]] = control.get("value", "")
        response = session.request(method, action, data=values, timeout=TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        return response

    href = control.get("href")
    onclick = control.get("onclick", "")
    match = re.search(r"(?:location(?:\.href)?\s*=|window\.location\s*=|window\.open\()\s*['\"]([^'\"]+)", onclick)
    destination = href or (match.group(1) if match else None)
    if destination:
        response = session.get(booking_url(current_url, destination), timeout=TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        return response
    fail("同意操作の送信方法を特定できません")


def submit_email(session, page, current_url, email):
    if not email or "@" not in email:
        fail("BOOKING_EMAILに有効なメールアドレスを設定してください")
    field = page.select_one("input[name='email']")
    form = field.find_parent("form") if field else None
    if not form or not field:
        fail("メールアドレス入力フォームが見つかりません")
    action = booking_url(current_url, form.get("action", current_url))
    values = form_values(form)
    values[field["name"]] = email
    submit = form.select_one("input[type='submit'][name],button[type='submit'][name]")
    if submit:
        values[submit["name"]] = submit.get("value", submit.get_text(" ", strip=True))
    response = session.post(action, data=values, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    return response


def main():
    args = parse_args()
    if date.fromisoformat(args.date).isoformat() != args.date:
        fail("宿泊日は YYYY-MM-DD で指定してください")
    args.email = args.email.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", args.email):
        fail("BOOKING_EMAILに有効なメールアドレスを設定してください")

    ledger = None
    if os.environ.get("GITHUB_ACTIONS") == "true":
        ledger = BookingLedger(args.facility_id, args.date)
        status = ledger.existing_status()
        if status == "pending":
            print(f"::warning::{args.facility_id} {args.date}: 前回の結果が未確定です。確認メールと申込状況を確認してください。")
            return
        if status == "email_sent":
            print(f"{args.facility_id} {args.date}: 確認メール送信済みです。")
            return

    room_persons = [int(value) for value in args.room_persons.split(",")]
    if len(room_persons) != args.rooms or any(value < 1 for value in room_persons):
        fail("--room-persons must give one positive guest count for every requested room")
    if sum(room_persons) != args.persons:
        fail("The room guest counts must add up to --persons")

    apply_url = get_apply_url(args.facility_id)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "ja,en-US;q=0.8,en;q=0.7"})
    response = session.get(apply_url, timeout=TIMEOUT)
    response.raise_for_status()
    page = BeautifulSoup(response.text, "lxml")
    form = page.select_one("form#new_apply")
    csrf_meta = page.select_one('meta[name="csrf-token"]')
    csrf_token = csrf_meta.get("content") if csrf_meta else ""

    if not csrf_token:
        raise RuntimeError("CSRF tokenが取得できません")
    if not form:
        fail("申込フォームが見つかりません。ログイン画面・エラー画面・画面変更の可能性があります")

    form_data = form_values(form)
    dates = {option.get("value") for option in form.select("select[name='apply[join_time]'] option")}
    if args.date not in dates:
        fail("指定日は現在申込可能な日付一覧にありません")
    form_data.update({
        "apply[join_time]": args.date,
        "apply[night_count]": str(args.nights),
        "apply[stay_persons]": str(args.persons),
        "apply[hope_rooms]": str(args.rooms),
    })
    for index, count in enumerate(room_persons, start=1):
        form_data[f"apply[hope_room{index}]"] = str(count)
    search_button = page.select_one("input[value='空き検索'][onclick]")
    token_match = re.search(r"coma_search\(['\"]([^'\"]+)", search_button.get("onclick", "")) if search_button else None
    if not token_match:
        fail("空き検索用のページ情報を特定できません")
    search_url = booking_url(response.url, "empty_new?s=" + token_match.group(1))
    search_response = session.post(
        search_url,
        data=form_data,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf_token,
            "Referer": response.url,
            "Origin": BASE_URL,
            "Accept": "*/*",
        },
        timeout=TIMEOUT,
    )
    search_response.raise_for_status()
    html_match = re.search(
        r"""\$\('#coma'\)\.html\(""",
        search_response.text
    )

    if not html_match:
        fail("空室一覧を含むJavaScriptレスポンスが見つかりません")

    js_literal = search_response.text[html_match.end():].lstrip()
    js_literal = js_literal.replace(r"\$", "$")
    js_literal = js_literal.replace(r"\'", "'")

    try:
        html_fragment, _ = json.JSONDecoder().raw_decode(js_literal)
    except json.JSONDecodeError as error:
        fail(f"検索結果のHTMLを取り出せません: {error}")

    results = BeautifulSoup(html_fragment, "lxml")

    checkboxes = results.select(
        "#coma_search_result input[type='checkbox'][name^='apply[coma[']"
    )

    if not checkboxes:
        fail("空室検索結果にチェックボックスがありません")

    rows = []
    for checkbox in checkboxes:
        row = checkbox.find_parent("tr")
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")] if row else []
        if len(cells) < 6:
            continue
        room_no = int(cells[1]) if cells[1].isdigit() else None
        capacity = re.search(r"(\d+)\s*[～〜~]\s*(\d+)", cells[4])
        if cells[5] != "空き" or not capacity or not room_no:
            continue
        low, high = map(int, capacity.groups())
        wanted = room_persons[0]
        if len(room_persons) == 1 and low <= wanted <= high:
            rows.append((room_no, checkbox.get("name"), checkbox.get("value"), cells))

    if args.room_number is not None:
        rows = [item for item in rows if item[0] == args.room_number]

    if not rows:
        fail("希望人数に合う空室が見つかりません")

    chosen = sorted(
        rows,
        key=lambda item: (
            -int(re.search(r"\d+\s*[～〜~]\s*(\d+)", item[3][4]).group(1)),
            item[0]
        )
    )[0]

    print(f"選択: 部屋番号 {chosen[0]} / {chosen[3][3]} / 定員 {chosen[3][4]} / {chosen[3][5]}")
    # Only 1 room is advanced automatically until multi-room result semantics are confirmed.
    if args.rooms != 1:
        fail("現在の実装は1室の選択までです。複数室では部屋割りと連泊の対応を追加してください")
    for key in list(form_data):
        if key.startswith("apply[coma["):
            del form_data[key]
    form_data[chosen[1]] = chosen[2]
    session_guid = results.select_one("input[name='apply_session_guid']")
    if not session_guid:
        fail("検索結果の一時セッション情報が見つかりません")
    form_data[session_guid["name"]] = session_guid.get("value", "")

    create_url = booking_url(response.url, form.get("action", ""))
    sha = ledger.write("pending") if ledger else None
    try:
        request_confirmation_email(session, create_url, form_data, args.email)
        if ledger:
            ledger.write("email_sent", sha)
    except Exception as error:
        raise RuntimeError(
            f"部屋選択以降の結果を確定できません ({type(error).__name__})。"
            "再実行する前に確認メールと申込状況を確認してください。"
        ) from None
    print(f"部屋番号 {chosen[0]} の確認メール送信完了画面を確認しました。")
    print("確認メール内のURLを開いて申込を完了してください。")


def request_confirmation_email(session, create_url, form_data, email):
    final = session.post(create_url, data=form_data, timeout=TIMEOUT, allow_redirects=True)
    final.raise_for_status()
    final_page = BeautifulSoup(final.text, "lxml")
    if "申込データにエラー" in final_page.get_text(" ", strip=True):
        fail("施設側で申込エラーになりました。部屋は選択されていない可能性があります")
    if "/apply/rule" in final.url or "個人情報の取り扱い" in final_page.get_text(" ", strip=True):
        final = submit_consent(session, final_page, final.url)
        final_page = BeautifulSoup(final.text, "lxml")

    if "/apply/email_input" not in final.url and not final_page.select_one("input[name='email']"):
        fail("同意後のメール入力画面を確認できませんでした")

    sent = submit_email(session, final_page, final.url, email)
    sent_path = urlsplit(booking_url(sent.url, "")).path.rstrip("/")
    if sent_path != "/apply/send_complete":
        fail("確認メール送信後の完了画面を確認できませんでした")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = "通信またはHTTPエラー" if isinstance(error, requests.RequestException) else str(error)
        print(f"ERROR: {type(error).__name__}: {detail}", file=sys.stderr)
        sys.exit(1)
