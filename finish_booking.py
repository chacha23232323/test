"""Read the current ITS message and submit the matching application."""

import base64
import json
import os
import re
import time
from datetime import UTC, date, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from types import SimpleNamespace
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
        stage = {
            "/apply/new": "案内リンクを開く",
            "/apply/confirm": "入力内容を送信して確認画面を取得",
            "/apply/complete": "最終送信後の完了画面を取得",
        }[expected_path]
        known_paths = {"/", "/apply/new", "/apply/confirm", "/apply/complete",
                       "/apply/email_input", "/apply/send_complete", "/apply/rule",
                       "/service_category/index"}
        destination = parsed.path if parsed.netloc == HOST and parsed.path in known_paths else "その他"
        if 300 <= response.status_code < 400:
            location = urlsplit(urljoin(response.url, response.headers.get("Location", "")))
            if location.netloc == HOST and location.path in known_paths:
                destination += f" → {location.path}"
        raise RuntimeError(f"{stage}: HTTP {response.status_code}、応答パス {destination}")
    page = BeautifulSoup(response.text, "lxml")
    if page.select_one(
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
        'iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha, .cf-turnstile'
    ) or "人間であることを確認" in page.get_text(" ", strip=True):
        raise RuntimeError("本人確認画面が表示されました。案内メールから手動で進めてください")
    return page


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


def summary_mismatches(page, stay_date, facility):
    def compact(value):
        return re.sub(r"\s+", "", value)

    day = date.fromisoformat(stay_date)
    expected = {
        "申込対象サービス": compact(facility + "申込"),
        "希望日": f"{day.year}年{day.month}月{day.day}日より1泊",
        "宿泊人数": "2",
        "希望部屋数": "1",
        "第一部屋人数": "2",
    }
    values = {}
    rows = []
    for row in page.select("dl"):
        label = row.select_one("dt")
        entries = row.select("dd")
        value = row.select_one("dd.elements")
        if value is None and entries:
            value = entries[-1]
        if label is not None and value is not None:
            rows.append((label, value))
    for row in page.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            rows.append(tuple(cells))
    for label, value in rows:
        name = compact(label.get_text(strip=True)).rstrip(":：")
        if name not in expected:
            continue
        text = value.get_text()
        if name == "申込対象サービス":
            parts = list(value.stripped_strings)
            text = parts[-1] if parts else ""
        values.setdefault(name, set()).add(compact(text))
    return [name for name, expected_value in expected.items()
            if values.get(name, {"2"} if name == "第一部屋人数" else set()) != {expected_value}]


def matching_summary(page, stay_date, facility):
    return not summary_mismatches(page, stay_date, facility)


def find_application(session, headers, since_ms, stay_date, facility):
    # Allow for clock differences and Gmail timestamps rounded to seconds.
    first_second = since_ms // 1000 - 60
    deadline = time.monotonic() + 30
    seen = set()
    listed_ids = set()
    fresh_count = 0
    link_count = 0
    while True:
        listing = json_response(requests.get(
            f"{GMAIL}/messages",
            params={"q": f'after:{first_second - 1}',
                    "maxResults": 15},
            headers=headers, timeout=TIMEOUT, allow_redirects=False,
        ))
        if listing.get("nextPageToken"):
            raise RuntimeError("Gmail候補が多すぎて対象を特定できません")
        matches = set()
        loaded_link = None
        for item in listing.get("messages", []):
            message_id = item["id"]
            listed_ids.add(message_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id) or message_id in seen:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("Gmailの照合時間を超えました")
            seen.add(message_id)
            message = json_response(requests.get(
                f"{GMAIL}/messages/{message_id}", params={"format": "raw"},
                headers=headers, timeout=TIMEOUT, allow_redirects=False,
            ))
            if int(message["internalDate"]) // 1000 < first_second:
                continue
            fresh_count += 1
            links = message_links(message["raw"], facility)
            link_count += len(links)
            for link in links:
                if time.monotonic() >= deadline:
                    raise RuntimeError("申込リンクの照合時間を超えました")
                response = session.get(link, timeout=TIMEOUT, allow_redirects=False)
                page = site_page(response, "/apply/new")
                loaded_link = link
                if matching_summary(page, stay_date, facility):
                    matches.add(link)
        if len(matches) == 1:
            link = next(iter(matches))
            if loaded_link != link:
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
                f"(API検索結果 {len(listed_ids)} 件、新着候補 {fresh_count} 件、"
                f"差出人・件名が一致した申込リンク {link_count} 件、"
                f"送信開始 {datetime.fromtimestamp(since_ms / 1000, UTC).isoformat()}、"
                f"検索終了 {datetime.now(UTC).isoformat()})"
            )
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def hidden_fields(form):
    return {field["name"]: field.get("value", "")
            for field in form.select("input[type=hidden][name]:not([disabled])")}


class BrowserReader:
    def __init__(self, page):
        self.page = page

    def get(self, url, *, timeout, allow_redirects):
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=10_000)
        return browser_response(self.page, response)


def browser_response(page, response):
    return SimpleNamespace(
        url=page.url,
        status_code=response.status if response else 0,
        headers=response.headers if response else {},
        text=page.content(),
    )


def form_button(page, form, label):
    action = json.dumps(form["action"], ensure_ascii=False)
    button = page.locator(f'form[action={action}]').get_by_role("button", name=label, exact=True)
    if button.count() != 1:
        raise RuntimeError(f"申込画面の「{label}」ボタンを特定できません")
    return button


def submit_in_browser(page, headers, profile, since_ms, stay_date, facility, before_final):
    response, document, correlation = find_application(
        BrowserReader(page), headers, since_ms, stay_date, facility
    )
    form, _ = form_at(document, response.url, "/apply/confirm", correlation)
    if not hidden_fields(form).get("authenticity_token"):
        raise RuntimeError("入力画面の送信用情報がありません")

    action = json.dumps(form["action"], ensure_ascii=False)
    live_form = page.locator(f'form[action={action}]')
    for field in FIELDS:
        value = profile[field]
        name = f"apply[{field}]"
        control = form.find(["input", "select"], attrs={"name": name, "disabled": False})
        if control is None:
            raise RuntimeError(f"申込画面の項目 {field} を確認できません")
        locator = live_form.locator(f'[name="{name}"]')
        if control.name == "select":
            options = {option["value"] for option in control.select("option[value]:not([disabled])")
                       if value in (option["value"], option.get_text(strip=True))}
            if len(options) != 1:
                raise RuntimeError(f"申込画面の選択肢 {field} が一致しません")
            locator.select_option(value=next(iter(options)))
        else:
            locator.fill(value)

    button = form_button(page, form, "申込する")
    with page.expect_navigation(wait_until="domcontentloaded") as navigated:
        button.click()
    confirmed = browser_response(page, navigated.value)
    document = site_page(confirmed, "/apply/confirm")
    mismatches = summary_mismatches(document, stay_date, facility)
    if "申込内容確認画面" not in re.sub(r"\s+", "", document.get_text()):
        mismatches.insert(0, "画面見出し")
    if mismatches:
        raise RuntimeError(f"確認画面の施設・宿泊条件を確認できません: {', '.join(mismatches)}")
    form, _ = form_at(document, confirmed.url, "/apply/complete", correlation)
    data = hidden_fields(form)
    if data.get("c") != correlation or not data.get("authenticity_token"):
        raise RuntimeError("最終送信用の申込情報が一致しません")
    button = form_button(page, form, "確認")

    before_final()
    with page.expect_navigation(wait_until="domcontentloaded") as navigated:
        button.click()
    complete = browser_response(page, navigated.value)
    document = site_page(complete, "/apply/complete")
    content = document.get_text(" ", strip=True)
    if "申込を完了しました。" not in content or not re.search(r"申込受付番号\s*[:：]\s*\d+", content):
        raise RuntimeError("申込完了画面または受付番号を確認できません")


def final_submission(headers, profile, since_ms, stay_date, facility, before_final):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(locale="ja-JP")
            page.set_default_timeout(12_000)
            submit_in_browser(page, headers, profile, since_ms, stay_date, facility, before_final)
        finally:
            browser.close()
