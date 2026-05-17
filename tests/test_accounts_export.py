import json
import io
import unittest
import zipfile
from datetime import datetime, timezone

from api.accounts import (
    _build_account_export_row,
    _build_cpa_export,
    _build_csv_export,
    _build_json_export,
    _build_sub2api_export,
    _build_txt_export,
    _parse_export_account_ids,
)
from core.db import AccountModel


class AccountsExportTests(unittest.TestCase):
    def _build_account(self) -> AccountModel:
        extra = {
            "refresh_token": "refresh-token",
            "mail_provider": "microsoft",
            "mailbox_account": {"email": "mailbox@example.com"},
            "chatgpt_local": {
                "auth": {"state": "access_token_valid"},
                "subscription": {
                    "plan": "plus",
                    "workspace_plan_type": "individual",
                },
                "codex": {"state": "usable"},
                "promo": {"state": "eligible"},
            },
        }
        return AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            user_id="user-1",
            region="US",
            token="access-token",
            status="subscribed",
            cashier_url="https://example.com/checkout",
            extra_json=json.dumps(extra, ensure_ascii=False),
            created_at=datetime(2026, 5, 17, 9, 38, 48, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_build_account_export_row_includes_local_plan_fields(self):
        row = _build_account_export_row(self._build_account())

        self.assertEqual(row["platform"], "chatgpt")
        self.assertEqual(row["email"], "demo@example.com")
        self.assertEqual(row["refresh_token"], "refresh-token")
        self.assertEqual(row["mailbox_email"], "mailbox@example.com")
        self.assertEqual(row["local_auth_state"], "access_token_valid")
        self.assertEqual(row["local_plan"], "plus")
        self.assertEqual(row["local_workspace_plan_type"], "individual")
        self.assertEqual(row["local_codex_state"], "usable")
        self.assertEqual(row["local_promo_state"], "eligible")

    def test_build_csv_export_contains_local_plan_column(self):
        content = _build_csv_export([self._build_account()])
        self.assertIn("local_plan", content)
        self.assertIn("plus", content)
        self.assertIn("demo@example.com", content)

    def test_build_json_export_contains_structured_rows(self):
        content = _build_json_export([self._build_account()])
        payload = json.loads(content)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["local_plan"], "plus")
        self.assertEqual(payload[0]["local_workspace_plan_type"], "individual")

    def test_build_txt_export_contains_human_readable_plan(self):
        content = _build_txt_export([self._build_account()])
        self.assertIn("local_plan: plus", content)
        self.assertIn("email: demo@example.com", content)

    def test_parse_export_account_ids_supports_csv_list(self):
        self.assertEqual(_parse_export_account_ids("3, 5,5, 9"), [3, 5, 9])
        self.assertEqual(_parse_export_account_ids(""), [])

    def test_build_cpa_export_creates_zip_auth_files(self):
        content = _build_cpa_export([self._build_account()])

        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            names = archive.namelist()
            self.assertEqual(names, ["demo@example.com.json"])
            payload = json.loads(archive.read(names[0]).decode("utf-8"))

        self.assertEqual(payload["email"], "demo@example.com")
        self.assertEqual(payload["refresh_token"], "refresh-token")

    def test_build_sub2api_export_contains_payload_array(self):
        content = _build_sub2api_export([self._build_account()])
        payload = json.loads(content)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "demo@example.com")
        self.assertEqual(payload[0]["platform"], "openai")
        self.assertEqual(
            payload[0]["credentials"]["refresh_token"],
            "refresh-token",
        )


if __name__ == "__main__":
    unittest.main()
