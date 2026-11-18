"""Search an ITS Kenpo facility, hold a matching room, and request email verification."""

import argparse
import json
import os
import re
import sys
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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
        field_type = field.get("type", "").lower()
        if field_type in ("submit", "button", "checkbox", "radio", "file"):
            continue
        if field_type == "hidden" or field.name in ("input", "textarea"):
            values[field["name"]] = field.get("value", "")
    return values


def submit_consent(session, page, current_url):
    control = next((element for element in page.select("input,button")
                    if (element.get("value") or element.get_text(" ", strip=True)) == "同意する"), None)
    if not control:
        fail("個人情報の取扱い同意ボタンが見つかりません")
    form = control.find_parent("form")
    if form:
        action = urljoin(current_url, form.get("action", current_url))
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
        response = session.get(urljoin(current_url, destination), timeout=TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        return response
    fail("同意操作の送信方法を特定できません")


def submit_email(session, page, current_url, email):
    if not email or "@" not in email:
        fail("BOOKING_EMAILに有効なメールアドレスを設定してください")
    form = page.select_one("form")
    field = page.select_one("input[name='email']")
    if not form or not field:
        fail("メールアドレス入力フォームが見つかりません")
    action = urljoin(current_url, form.get("action", current_url))
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
    date.fromisoformat(args.date)
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
    search_url = urljoin(response.url, "empty_new?s=" + token_match.group(1))
    search_response = session.post(search_url, data=form_data, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT)
    search_response.raise_for_status()
    results = BeautifulSoup(search_response.text, "lxml")
    checkboxes = results.select("#coma_search_result input[type='checkbox'][name^='apply[coma[' ]")
    if not checkboxes:
        # The server can return a full page or an AJAX fragment depending on session state.
        checkboxes = results.select("input[type='checkbox'][name^='apply[coma[' ]")
    if not checkboxes:
        fail("空室検索結果がありません。日付、人数、施設の状態を確認してください")

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
            -int(re.search(r"(\d+)\s*[～〜~]", item[3][4]).group(2)),
            item[0]
        )
    )[0]

    print(f"選択: 部屋番号 {chosen[0]} / {chosen[3][3]} / 定員 {chosen[3][4]} / {chosen[3][5]}")
    # Only 1 room is advanced automatically until multi-room result semantics are confirmed.
    if args.rooms != 1:
        fail("現在の実装は1室の選択までです。複数室では部屋割りと連泊の対応を追加してください")
    form_data[chosen[1]] = chosen[2]
    session_guid = results.select_one("input[name='apply_session_guid']")
    if not session_guid:
        fail("検索結果の一時セッション情報が見つかりません")
    form_data[session_guid["name"]] = session_guid.get("value", "")

    create_url = urljoin(response.url, form.get("action", ""))
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

    sent = submit_email(session, final_page, final.url, args.email)
    sent_page = BeautifulSoup(sent.text, "lxml")
    sent_text = sent_page.get_text(" ", strip=True)
    if "確認メール" in sent_text or "メールを送信" in sent_text or "/apply/send_complete" in sent.url:
        print(f"部屋番号 {chosen[0]} を選択し、確認メールを {args.email} に送信しました。")
        print("確認メール内のURLを開いて申込を完了してください。そこから先は自動操作しません。")
        return
    fail("確認メール送信後の画面を確認できませんでした")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
