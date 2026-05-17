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

from platforms.chatgpt.payment import probe_plus_promo_eligibility


class DummyAccount:
    def __init__(self, *, access_token="", cookies=""):
        self.access_token = access_token
        self.cookies = cookies


class ChatGPTPromoProbeTests(unittest.TestCase):
    def test_probe_requires_access_token(self):
        result = probe_plus_promo_eligibility(DummyAccount())

        self.assertEqual(result["state"], "missing_access_token")
        self.assertIsNone(result["eligible"])

    def test_probe_short_circuits_for_paid_plan(self):
        account = DummyAccount(access_token="access-token")

        with mock.patch(
            "platforms.chatgpt.payment._fetch_me_context",
            return_value=(200, {"plan_type": "chatgptplusplan"}, ""),
        ):
            with mock.patch("platforms.chatgpt.payment._request_checkout") as checkout_mock:
                result = probe_plus_promo_eligibility(account)

        self.assertEqual(result["state"], "already_subscribed")
        self.assertFalse(result["eligible"])
        checkout_mock.assert_not_called()

    def test_probe_marks_eligible_when_hosted_checkout_url_exists(self):
        account = DummyAccount(access_token="access-token")

        with mock.patch(
            "platforms.chatgpt.payment._fetch_me_context",
            return_value=(200, {"plan_type": "free"}, ""),
        ):
            with mock.patch(
                "platforms.chatgpt.payment._request_checkout",
                return_value=(200, {"url": "https://pay.openai.com/gopay/hosted-demo"}, ""),
            ):
                result = probe_plus_promo_eligibility(account, country="ID")

        self.assertEqual(result["state"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["offer_title"], "Plus 优惠")
        self.assertEqual(result["currency"], "IDR")
        self.assertEqual(result["checkout_ui_mode"], "hosted")
        self.assertEqual(result["checkout_url"], "https://pay.openai.com/gopay/hosted-demo")

    def test_probe_marks_ineligible_from_checkout_message(self):
        account = DummyAccount(access_token="access-token")

        with mock.patch(
            "platforms.chatgpt.payment._fetch_me_context",
            return_value=(200, {"plan_type": "free"}, ""),
        ):
            with mock.patch(
                "platforms.chatgpt.payment._request_checkout",
                return_value=(400, {"detail": "This account is not eligible for this promo"}, ""),
            ):
                result = probe_plus_promo_eligibility(account, country="US")

        self.assertEqual(result["state"], "ineligible")
        self.assertFalse(result["eligible"])

    def test_probe_uses_local_probe_to_skip_paid_plan(self):
        account = DummyAccount(access_token="access-token")

        with mock.patch("platforms.chatgpt.payment._fetch_me_context") as me_mock, mock.patch(
            "platforms.chatgpt.payment._request_checkout"
        ) as checkout_mock:
            result = probe_plus_promo_eligibility(
                account,
                local_probe={
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "subscription": {"plan": "plus", "workspace_plan_type": "individual"},
                },
            )

        self.assertEqual(result["state"], "already_subscribed")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["subscription_plan"], "plus")
        me_mock.assert_not_called()
        checkout_mock.assert_not_called()

    def test_probe_uses_local_probe_to_short_circuit_unauthorized(self):
        account = DummyAccount(access_token="access-token")

        with mock.patch("platforms.chatgpt.payment._fetch_me_context") as me_mock, mock.patch(
            "platforms.chatgpt.payment._request_checkout"
        ) as checkout_mock:
            result = probe_plus_promo_eligibility(
                account,
                local_probe={
                    "auth": {
                        "state": "access_token_invalidated",
                        "http_status": 401,
                        "message": "token invalidated",
                    },
                    "subscription": {"plan": "unknown"},
                },
            )

        self.assertEqual(result["state"], "unauthorized")
        self.assertFalse(result["eligible"])
        self.assertIn("token invalidated", result["message"])
        me_mock.assert_not_called()
        checkout_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
