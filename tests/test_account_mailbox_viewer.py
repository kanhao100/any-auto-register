import json
import types
import unittest
from email.message import EmailMessage
from unittest import mock

from core.db import AccountModel
from core.base_mailbox import OutlookMailbox
from services.account_mailbox_viewer import load_account_mailbox_messages


def _build_account(*, extra: dict) -> AccountModel:
    return AccountModel(
        platform="chatgpt",
        email="demo@outlook.com",
        password="secret",
        extra_json=json.dumps(extra, ensure_ascii=False),
    )


def _build_mailbox_snapshot(*, account_type: str = "microsoft_oauth") -> dict:
    return {
        "mail_provider": "microsoft",
        "mailbox_account": {
            "email": "demo@outlook.com",
            "account_id": "42",
            "extra": {
                "provider": "microsoft",
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "account_type": account_type,
                "mailapi_url": "https://mailapi.example/messages",
            },
        },
    }


class AccountMailboxViewerTests(unittest.TestCase):
    def test_load_account_mailbox_messages_reads_graph_folders(self):
        account = _build_account(extra=_build_mailbox_snapshot())
        mailbox = OutlookMailbox()
        mailbox._resolve_backend = lambda account: types.SimpleNamespace(backend_name="graph")
        mailbox._get_oauth_access_token = lambda account, preferred_backend=None: "access-token"
        mailbox._graph_list_messages = lambda access_token, folder: [
            {
                "id": "msg-1",
                "subject": "Welcome",
                "bodyPreview": "preview",
                "receivedDateTime": "2026-05-17T01:38:48Z",
                "from": {"emailAddress": {"name": "OpenAI", "address": "noreply@openai.com"}},
            }
        ]
        mailbox._graph_message_text = lambda message: "Message body"

        with mock.patch("services.account_mailbox_viewer.create_mailbox", return_value=mailbox), \
             mock.patch("services.account_mailbox_viewer.config_store.get_all", return_value={}):
            result = load_account_mailbox_messages(account, folder="junk", limit=10)

        self.assertEqual(result["provider"], "microsoft")
        self.assertEqual(result["backend"], "graph")
        self.assertEqual(result["active_folder"], "junk")
        self.assertEqual([item["key"] for item in result["folders"]], ["inbox", "junk", "trash"])
        self.assertEqual(result["messages"][0]["id"], "junk:msg-1")
        self.assertEqual(result["messages"][0]["sender"], "OpenAI")
        self.assertEqual(result["messages"][0]["body"], "Message body")

    def test_load_account_mailbox_messages_reads_imap_messages(self):
        account = _build_account(extra=_build_mailbox_snapshot())
        mailbox = OutlookMailbox()
        mailbox._resolve_backend = lambda account: types.SimpleNamespace(backend_name="imap")

        message = EmailMessage()
        message["Subject"] = "Your code"
        message["From"] = "OpenAI <noreply@openai.com>"
        message["Date"] = "Sun, 17 May 2026 09:38:48 +0800"
        message.set_content("Verification code 123456")
        raw = message.as_bytes()

        class _FakeImap:
            def select(self, folder, readonly=True):
                return ("OK", [b""])

            def uid(self, action, *args):
                if action == "search":
                    return ("OK", [b"1"])
                if action == "fetch":
                    return ("OK", [(b"1", raw)])
                return ("NO", [])

            def logout(self):
                return None

        mailbox._open_imap = lambda account: _FakeImap()

        with mock.patch("services.account_mailbox_viewer.create_mailbox", return_value=mailbox), \
             mock.patch("services.account_mailbox_viewer.config_store.get_all", return_value={}):
            result = load_account_mailbox_messages(account, folder="trash", limit=10)

        self.assertEqual(result["backend"], "imap")
        self.assertEqual(result["active_folder"], "trash")
        self.assertEqual(result["messages"][0]["subject"], "Your code")
        self.assertIn("Verification code", result["messages"][0]["body"])

    def test_load_account_mailbox_messages_reads_mailapi_url_content(self):
        account = _build_account(extra=_build_mailbox_snapshot(account_type="mailapi_url"))
        mailbox = OutlookMailbox()
        mailbox._resolve_backend = lambda account: types.SimpleNamespace(backend_name="mailapi_url")
        mailbox._backends["mailapi_url"] = types.SimpleNamespace(
            _fetch_mailapi_text=lambda account: "Subject: Test\n\nMailAPI content body",
        )

        with mock.patch("services.account_mailbox_viewer.create_mailbox", return_value=mailbox), \
             mock.patch("services.account_mailbox_viewer.config_store.get_all", return_value={}):
            result = load_account_mailbox_messages(account, folder="trash", limit=10)

        self.assertEqual(result["backend"], "mailapi_url")
        self.assertEqual(result["active_folder"], "inbox")
        self.assertEqual([item["key"] for item in result["folders"]], ["inbox"])
        self.assertEqual(result["messages"][0]["id"], "mailapi:latest")
        self.assertIn("MailAPI content body", result["messages"][0]["body"])


if __name__ == "__main__":
    unittest.main()
