import sys
import types
import unittest
from unittest import mock

try:
    import curl_cffi  # noqa: F401
except ModuleNotFoundError:
    curl_cffi_module = types.ModuleType("curl_cffi")
    curl_cffi_module.requests = types.SimpleNamespace(get=None, post=None)
    sys.modules["curl_cffi"] = curl_cffi_module

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, AccountStatus, RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform


class _BlankMailbox:
    def get_email(self):
        return MailboxAccount(email="", account_id="blank-mailbox")

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class _TrackingMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="demo@example.com", account_id="tracked-mailbox")
        self.wait_call = None
        self.current_ids_calls = []

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        self.current_ids_calls.append(account)
        return {"mid-1"}

    def wait_for_code(self, *args, **kwargs):
        self.wait_call = (args, kwargs)
        return "123456"


class _RequeueMailbox(_TrackingMailbox):
    def __init__(self):
        super().__init__()
        self.requeued = []

    def requeue_account(self, account):
        self.requeued.append(account)


class _FakeAdapter:
    def run(self, context):
        context.email_service.create_email()
        raise AssertionError("create_email 应该先报错")


class _VerificationAdapter:
    def __init__(self):
        self.run_called = False

    def run(self, context):
        self.run_called = True
        context.email_service.create_email()
        code = context.email_service.get_verification_code(
            timeout=30,
            otp_sent_at=123.0,
            exclude_codes={"654321"},
        )
        self.last_code = code
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True, "password": fallback_password}


class _FailingAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=False, error_message="boom")


class _SuccessfulAccountAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return Account(
            platform="chatgpt",
            email="demo@example.com",
            password=fallback_password,
            token="",
            status=AccountStatus.REGISTERED,
            extra={},
        )


class ChatGPTPluginTests(unittest.TestCase):
    def test_platform_actions_include_promo_probe(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
        )

        actions = platform.get_platform_actions()

        self.assertTrue(any(action["id"] == "probe_promo_eligibility" for action in actions))

    def test_platform_actions_include_relogin(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
        )

        actions = platform.get_platform_actions()

        self.assertTrue(any(action["id"] == "relogin" for action in actions))
        relogin_action = next(action for action in actions if action["id"] == "relogin")
        self.assertEqual(relogin_action.get("run_mode"), "task")

    def test_platform_actions_include_reauthorize_rt(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
        )

        actions = platform.get_platform_actions()

        self.assertTrue(any(action["id"] == "reauthorize_rt" for action in actions))
        reauthorize_action = next(action for action in actions if action["id"] == "reauthorize_rt")
        self.assertEqual(reauthorize_action.get("run_mode"), "task")

    def test_custom_provider_rejects_blank_email(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_BlankMailbox(),
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FakeAdapter(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register()

        self.assertIn("custom_provider 返回空邮箱地址", str(ctx.exception))

    def test_custom_provider_uses_mailbox_baseline_for_verification_code(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(adapter.run_called)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(result["success"], True)
        self.assertEqual(mailbox.current_ids_calls, [mailbox.account])
        self.assertIsNotNone(mailbox.wait_call)
        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("before_ids"), {"mid-1"})
        self.assertEqual(kwargs.get("otp_sent_at"), 123.0)
        self.assertEqual(kwargs.get("exclude_codes"), {"654321"})

    def test_custom_provider_prefers_configured_mailbox_timeout(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mailbox_otp_timeout_seconds": 90,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("timeout"), 90)

    def test_custom_provider_does_not_requeue_mailbox_account_on_failure(self):
        mailbox = _RequeueMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FailingAdapter(),
        ):
            with self.assertRaises(RuntimeError):
                platform.register()

        self.assertEqual(mailbox.requeued, [])

    def test_register_stores_mailbox_snapshot_in_account_extra(self):
        mailbox = _TrackingMailbox()
        mailbox.account.extra = {"provider": "microsoft", "tenant": "demo"}
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_SuccessfulAccountAdapter(),
        ):
            account = platform.register()

        self.assertIsInstance(account, Account)
        self.assertEqual(account.extra["mailbox_account"]["email"], "demo@example.com")
        self.assertEqual(account.extra["mailbox_account"]["account_id"], "tracked-mailbox")
        self.assertEqual(
            account.extra["mailbox_account"]["extra"],
            {"provider": "microsoft", "tenant": "demo"},
        )

    def test_execute_action_probe_promo_eligibility_merges_local_probe(self):
        platform = ChatGPTPlatform(config=RegisterConfig())
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="access-token",
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": "access-token",
                "chatgpt_local": {
                    "auth": {"state": "access_token_valid"},
                },
            },
        )

        with mock.patch(
            "platforms.chatgpt.status_probe.probe_local_chatgpt_status",
            return_value={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free", "workspace_plan_type": "individual"},
                "codex": {"state": "usable"},
            },
        ) as local_probe_mock, mock.patch(
            "platforms.chatgpt.payment.probe_plus_promo_eligibility",
            return_value={
                "state": "eligible",
                "eligible": True,
                "country": "ID",
                "offer_title": "Plus 优惠",
                "subscription_plan": "free",
                "message": "ok",
            },
        ):
            result = platform.execute_action("probe_promo_eligibility", account, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["promo"]["state"], "eligible")
        self.assertEqual(result["data"]["probe"]["auth"]["state"], "access_token_valid")
        self.assertEqual(result["data"]["probe"]["subscription"]["plan"], "free")
        self.assertEqual(result["account_extra_patch"]["chatgpt_local"]["promo"]["country"], "ID")
        local_probe_mock.assert_called_once()

    def test_execute_action_relogin_returns_tokens_and_probe_patch(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={"default_executor": "headed"}))
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="stale-access-token",
            status=AccountStatus.INVALID,
            extra={
                "access_token": "stale-access-token",
                "refresh_token": "stale-refresh-token",
            },
        )

        with mock.patch(
            "platforms.chatgpt.plugin.relogin_chatgpt_account",
            return_value={
                "access_token": "fresh-access-token",
                "refresh_token": "fresh-refresh-token",
                "id_token": "fresh-id-token",
                "session_token": "fresh-session-token",
                "workspace_id": "ws_123",
                "probe": {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "subscription": {"plan": "free"},
                    "codex": {"state": "usable"},
                },
                "relogin_method": "password",
                "mailbox": {"available": False},
            },
        ) as relogin_mock:
            result = platform.execute_action("relogin", account, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["access_token"], "fresh-access-token")
        self.assertEqual(result["data"]["refresh_token"], "fresh-refresh-token")
        self.assertEqual(result["data"]["probe"]["auth"]["state"], "access_token_valid")
        self.assertEqual(result["account_extra_patch"]["chatgpt_token_source"], "relogin")
        self.assertEqual(result["account_extra_patch"]["chatgpt_last_relogin"]["method"], "password")
        relogin_mock.assert_called_once()

    def test_execute_action_relogin_includes_action_logs(self):
        platform = ChatGPTPlatform(config=RegisterConfig())
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="stale-access-token",
            status=AccountStatus.INVALID,
            extra={"access_token": "stale-access-token"},
        )

        def _fake_relogin(*args, **kwargs):
            kwargs["log_fn"]("[relogin] step 1")
            kwargs["log_fn"]("[relogin] step 2")
            return {
                "access_token": "fresh-access-token",
                "refresh_token": "fresh-refresh-token",
                "id_token": "fresh-id-token",
                "session_token": "fresh-session-token",
                "workspace_id": "ws_123",
                "probe": {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "subscription": {"plan": "free"},
                    "codex": {"state": "usable"},
                },
                "relogin_method": "password",
                "mailbox": {"available": False},
            }

        with mock.patch(
            "platforms.chatgpt.plugin.relogin_chatgpt_account",
            side_effect=_fake_relogin,
        ):
            result = platform.execute_action("relogin", account, {})

        self.assertEqual(result["data"]["logs"], ["[relogin] step 1", "[relogin] step 2"])

    def test_execute_action_relogin_returns_logs_on_failure(self):
        platform = ChatGPTPlatform(config=RegisterConfig())
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="stale-access-token",
            status=AccountStatus.INVALID,
            extra={"access_token": "stale-access-token"},
        )

        def _failing_relogin(*args, **kwargs):
            kwargs["log_fn"]("[relogin] step before failure")
            raise RuntimeError("boom")

        with mock.patch(
            "platforms.chatgpt.plugin.relogin_chatgpt_account",
            side_effect=_failing_relogin,
        ):
            result = platform.execute_action("relogin", account, {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        self.assertEqual(result["data"]["logs"], ["[relogin] step before failure"])

    def test_execute_action_reauthorize_rt_returns_tokens_and_probe_patch(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={"default_executor": "headed"}))
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="stale-access-token",
            status=AccountStatus.INVALID,
            extra={
                "access_token": "stale-access-token",
                "refresh_token": "stale-refresh-token",
            },
        )

        with mock.patch(
            "platforms.chatgpt.plugin.reauthorize_chatgpt_tokens",
            return_value={
                "access_token": "fresh-access-token",
                "refresh_token": "fresh-refresh-token",
                "id_token": "fresh-id-token",
                "session_token": "fresh-session-token",
                "workspace_id": "ws_456",
                "probe": {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "subscription": {"plan": "plus"},
                    "codex": {"state": "usable"},
                },
                "relogin_method": "password",
                "mailbox": {"available": True},
                "token_source": "reauthorize_rt",
            },
        ) as reauthorize_mock:
            result = platform.execute_action("reauthorize_rt", account, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["access_token"], "fresh-access-token")
        self.assertEqual(result["data"]["refresh_token"], "fresh-refresh-token")
        self.assertEqual(
            result["account_extra_patch"]["chatgpt_token_source"],
            "reauthorize_rt",
        )
        self.assertEqual(
            result["account_extra_patch"]["chatgpt_last_rt_reauthorization"]["method"],
            "password",
        )
        reauthorize_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
