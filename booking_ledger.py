"""Durable booking guard shared by automatic and manual GitHub Actions runs."""

import base64
import hashlib
import json
import os

import requests


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

    def existing_status(self):
        result = requests.get(
            self.url, params={"ref": self.branch}, headers=self.headers, timeout=15
        )
        if result.status_code == 404:
            return None
        if result.status_code != 200:
            raise RuntimeError(f"申込記録の読み取りに失敗しました (HTTP {result.status_code})")
        record = json.loads(base64.b64decode(result.json()["content"]))
        status = record.get("status")
        if status not in ("pending", "email_sent", "finalizing", "accepted"):
            raise RuntimeError("既存の申込記録の状態を確認できません")
        return status

    def write(self, status, sha=None):
        record = dict(self.record, status=status)
        payload = {
            "branch": self.branch,
            "message": f"Record ITS booking {status}",
            "content": base64.b64encode(
                (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
            ).decode("ascii"),
        }
        if sha is not None:
            payload["sha"] = sha
        result = requests.put(self.url, json=payload, headers=self.headers, timeout=15)
        expected = 201 if sha is None else 200
        if result.status_code != expected:
            raise RuntimeError(f"申込記録の書き込みに失敗しました (HTTP {result.status_code})")
        return result.json()["content"]["sha"]
