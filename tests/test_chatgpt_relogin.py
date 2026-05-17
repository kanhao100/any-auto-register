import sys
import types
import unittest
from unittest import mock

try:
    import curl_cffi  # noqa: F401
except ModuleNotFoundError:
    curl_cffi_module = types.ModuleType("curl_cffi")
    curl_cffi_module.requests = types.SimpleNamespace(get=None, post=None, Session=object)
    sys.modules["curl_cffi"] = curl_cffi_module

from platforms.chatgpt.relogin import relogin_chatgpt_account


class _FakeMailboxService:
    def __init__(self):
        self.primed = False

    def prime(self):
        self.primed = True


class _FakeOAuthClient:
    instances = []

    def __init__(self, config, proxy=None, verbose=True, browser_mode="protocol"):
        self.config = dict(config or {})
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode
        self.last_error = ""
        self.last_workspace_id = "ws_123"
        self.calls = []
        _FakeOAuthClient.instances.append(self)

    def login_and_get_tokens(self, email, password, **kwargs):
        self.calls.append(
            {
                "email": email,
                "password": password,
                **kwargs,
            }
        )
        if kwargs.get("force_password_login"):
            self.last_error = "password login failed"
            return {}
        return {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "id_token": "fresh-id-token",
        }

    def _get_cookie_value(self, name, domain):
        if name == "__Secure-next-auth.session-token" and domain == "chatgpt.com":
            return "fresh-session-token"
        return ""


class ChatGPTReloginTests(unittest.TestCase):
    def setUp(self):
        _FakeOAuthClient.instances.clear()

    def test_relogin_falls_back_to_passwordless_when_mailbox_is_available(self):
        mailbox_service = _FakeMailboxService()
        account = types.SimpleNamespace(
            email="demo@example.com",
            password="secret",
            user_id="legacy-user-id",
            extra={"mailbox_account": {"email": "demo@example.com"}},
        )
        probe = {
            "auth": {"state": "access_token_valid", "http_status": 200},
            "subscription": {"plan": "free"},
            "codex": {"state": "usable", "http_status": 200},
        }

        with mock.patch(
            "platforms.chatgpt.relogin._restore_existing_mailbox_service",
            return_value=(
                mailbox_service,
                {"available": True, "provider": "microsoft", "message": "ok"},
            ),
        ), mock.patch(
            "platforms.chatgpt.relogin.OAuthClient",
            _FakeOAuthClient,
        ), mock.patch(
            "platforms.chatgpt.relogin.extract_chatgpt_account_id",
            return_value="acct_123",
        ), mock.patch(
            "platforms.chatgpt.relogin.probe_local_chatgpt_status",
            return_value=probe,
        ):
            result = relogin_chatgpt_account(
                account,
                config={"default_executor": "headed"},
                proxy="http://proxy.local:8080",
                browser_mode="headed",
            )

        self.assertTrue(mailbox_service.primed)
        self.assertEqual(result["relogin_method"], "passwordless")
        self.assertEqual(result["access_token"], "fresh-access-token")
        self.assertEqual(result["refresh_token"], "fresh-refresh-token")
        self.assertEqual(result["id_token"], "fresh-id-token")
        self.assertEqual(result["session_token"], "fresh-session-token")
        self.assertEqual(result["workspace_id"], "ws_123")
        self.assertEqual(result["account_id"], "acct_123")
        self.assertEqual(result["probe"], probe)

        oauth_client = _FakeOAuthClient.instances[-1]
        self.assertEqual(len(oauth_client.calls), 2)
        self.assertTrue(oauth_client.calls[0]["force_password_login"])
        self.assertFalse(oauth_client.calls[0]["prefer_passwordless_login"])
        self.assertTrue(oauth_client.calls[1]["prefer_passwordless_login"])
        self.assertFalse(oauth_client.calls[1]["force_password_login"])
        self.assertFalse(oauth_client.calls[0]["allow_phone_verification"])
        self.assertIs(oauth_client.calls[0]["skymail_client"], mailbox_service)
        self.assertIs(oauth_client.calls[1]["skymail_client"], mailbox_service)


if __name__ == "__main__":
    unittest.main()
