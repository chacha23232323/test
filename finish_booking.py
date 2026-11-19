"""Read the current ITS message and submit the matching application."""

import base64
import json
import os
import re
import time
from datetime import date
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

HOST = "as.its-kenpo.or.jp"
SENDER = "noreply@mail.its-kenpo.or.jp"
SUBJECT_SUFFIX = "申込手続きのご案内"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
FIELDS = (
    "sign_no", "insured_no", "office_name", "kana_name", "year", "month", "day",
    "gender", "relationship", "contact_phone", "postal", "state", "address",
)
LINK_PATTERN = re.compile(
    r"https://as\.its-kenpo\.or\.jp/apply/new\?c="
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![\w%&=#/?-])"
)
TIMEOUT = (5, 10)


def config_from_env():
    oauth = json.loads(os.environ["BOOKING_GMAIL_OAUTH"])
    profile = json.loads(os.environ["BOOKING_APPLICANT"])
    if not all(isinstance(oauth.get(k), str) and oauth[k].strip()
               for k in ("client_id", "client_secret", "refresh_token")):
        raise RuntimeError("Gmail OAuth設定が不足しています")
    if set(profile) != set(FIELDS) or not all(
        isinstance(profile[k], str) and profile[k].strip() for k in FIELDS
    ):
        raise RuntimeError("申込代表者の設定項目を確認してください")
    return oauth, profile


def json_response(response):
    if response.status_code != 200:
        raise RuntimeError(f"Gmail認証または取得に失敗しました (HTTP {response.status_code})")
    return response.json()


def gmail_headers(oauth, expected_email):
    token = json_response(requests.post(
        "https://oauth2.googleapis.com/token",
        data={k: oauth[k] for k in ("client_id", "client_secret", "refresh_token")}
        | {"grant_type": "refresh_token"},
        timeout=TIMEOUT, allow_redirects=False,
    ))["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    profile = json_response(requests.get(
        f"{GMAIL}/profile", headers=headers, timeout=TIMEOUT, allow_redirects=False
    ))
    if profile["emailAddress"].casefold() != expected_email.casefold():
        raise RuntimeError("GmailのOAuthアカウントとBOOKING_EMAILが一致しません")
    return headers


def message_links(raw, facility):
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    if parseaddr(str(message.get("From", "")))[1].lower() != SENDER:
        return set()
    if str(message.get("Subject", "")).strip() != facility + SUBJECT_SUFFIX:
        return set()
    links = set()
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html") and not part.is_attachment():
            links.update(LINK_PATTERN.findall(part.get_content()))
    return links


def site_page(response, expected_path):
    parsed = urlsplit(response.url)
    if response.status_code != 200 or (
        parsed.scheme, parsed.netloc, parsed.path
    ) != ("https", HOST, expected_path):
        raise RuntimeError("ITS健保の想定した画面を確認できません")
    return BeautifulSoup(response.text, "lxml")


def form_at(page, current_url, path, correlation):
    candidates = []
    for form in page.select("form[method][action]"):
        url = urljoin(current_url, form["action"])
        parsed = urlsplit(url)
        if form["method"].lower() == "post" and (
            parsed.scheme, parsed.netloc, parsed.path
        ) == ("https", HOST, path):
            candidates.append((form, url))
    if len(candidates) != 1:
        raise RuntimeError("ITS健保の送信フォームを特定できません")
    form, url = candidates[0]
    query = parse_qs(urlsplit(url).query)
    if "c" in query and query["c"] != [correlation]:
        raise RuntimeError("申込フォームの識別子が一致しません")
    return form, url


def matching_summary(page, stay_date, facility):
    summary = {}
    service = ""
    for row in page.select("dl"):
        label = row.select_one("dt")
        value = row.select_one("dd.elements")
        if value is None:
            entries = row.select("dd")
            value = entries[-1] if entries else None
        if label is not None and value is not None:
            name = label.get_text(strip=True)
            summary[name] = re.sub(r"\s+", "", value.get_text())
            if name == "申込対象サービス":
                parts = list(value.stripped_strings)
                service = parts[-1] if parts else ""
    day = date.fromisoformat(stay_date)
    expected = {
        "希望日": f"{day.year}年{day.month}月{day.day}日より1泊",
        "宿泊人数": "2", "希望部屋数": "1",
    }
    return (
        re.sub(r"\s+", "", service) == re.sub(r"\s+", "", facility + "申込")
        and all(summary.get(label) == value for label, value in expected.items())
        and summary.get("第一部屋人数", "2") == "2"
    )


def find_application(session, headers, since_ms, stay_date, facility):
    deadline = time.monotonic() + 90
    seen = set()
    fresh_count = 0
    link_count = 0
    while True:
        listing = json_response(requests.get(
            f"{GMAIL}/messages",
            params={"q": f'from:{SENDER} after:{since_ms // 1000}',
                    "maxResults": 15},
            headers=headers, timeout=TIMEOUT, allow_redirects=False,
        ))
        if listing.get("nextPageToken"):
            raise RuntimeError("Gmail候補が多すぎて対象を特定できません")
        matches = set()
        for item in listing.get("messages", []):
            message_id = item["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id) or message_id in seen:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("Gmailの照合時間を超えました")
            seen.add(message_id)
            message = json_response(requests.get(
                f"{GMAIL}/messages/{message_id}", params={"format": "raw"},
                headers=headers, timeout=TIMEOUT, allow_redirects=False,
            ))
            if int(message["internalDate"]) < since_ms:
                continue
            fresh_count += 1
            links = message_links(message["raw"], facility)
            link_count += len(links)
            for link in links:
                if time.monotonic() >= deadline:
                    raise RuntimeError("申込リンクの照合時間を超えました")
                response = session.get(link, timeout=TIMEOUT, allow_redirects=False)
                page = site_page(response, "/apply/new")
                if matching_summary(page, stay_date, facility):
                    matches.add(link)
        if len(matches) == 1:
            link = next(iter(matches))
            response = session.get(link, timeout=TIMEOUT, allow_redirects=False)
            page = site_page(response, "/apply/new")
            if not matching_summary(page, stay_date, facility):
                raise RuntimeError("再表示した入力画面の宿泊条件が一致しません")
            return response, page, parse_qs(urlsplit(link).query)["c"][0]
        if len(matches) > 1:
            raise RuntimeError("同じ宿泊日の申込リンクが複数あります")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Gmailで今回の申込手続きメールを確認できません "
                f"(新着候補 {fresh_count} 件、差出人・件名が一致した申込リンク {link_count} 件)"
            )
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def hidden_fields(form):
    return {field["name"]: field.get("value", "")
            for field in form.select("input[type=hidden][name]:not([disabled])")}


def final_submission(session, headers, profile, since_ms, stay_date, facility, before_final):
    response, page, correlation = find_application(session, headers, since_ms, stay_date, facility)
    form, url = form_at(page, response.url, "/apply/confirm", correlation)
    data = hidden_fields(form)
    if not data.get("authenticity_token"):
        raise RuntimeError("入力画面の送信用情報がありません")
    for field, value in profile.items():
        name = f"apply[{field}]"
        control = form.find(["input", "select"], attrs={"name": name, "disabled": False})
        if control is None:
            raise RuntimeError(f"申込画面の項目 {field} を確認できません")
        if control.name == "select":
            options = [option for option in control.select("option[value]:not([disabled])")
                       if value in (option["value"], option.get_text(strip=True))]
            if not options or len({opt["value"] for opt in options}) != 1:
                raise RuntimeError(f"申込画面の選択肢 {field} が一致しません")
            value = options[0]["value"]
        data[name] = value
    confirmed = session.post(url, data=data, timeout=TIMEOUT, allow_redirects=False)
    page = site_page(confirmed, "/apply/confirm")
    if not matching_summary(page, stay_date, facility) or "申込内容確認画面" not in page.get_text(strip=True):
        raise RuntimeError("確認画面の施設・宿泊条件を確認できません")
    form, url = form_at(page, confirmed.url, "/apply/complete", correlation)
    data = hidden_fields(form)
    if data.get("c") != correlation or not data.get("authenticity_token"):
        raise RuntimeError("最終送信用の申込情報が一致しません")

    before_final()
    complete = session.post(
        url, data={key: data[key] for key in ("c", "authenticity_token")},
        timeout=TIMEOUT, allow_redirects=False,
    )
    page = site_page(complete, "/apply/complete")
    content = page.get_text(" ", strip=True)
    if "申込を完了しました。" not in content or not re.search(r"申込受付番号\s*[:：]\s*\d+", content):
        raise RuntimeError("申込完了画面または受付番号を確認できません")
