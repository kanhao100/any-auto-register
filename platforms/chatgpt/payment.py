"""
Payment helpers for ChatGPT Plus / Team checkout links and subscription probes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from curl_cffi import requests as cffi_requests

from core.browser_runtime import ensure_browser_display_available
from core.proxy_utils import build_requests_proxy_config

logger = logging.getLogger(__name__)

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
TEAM_CHECKOUT_BASE_URL = "https://chatgpt.com/checkout/openai_llc/"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
PLUS_PROMO_CAMPAIGN_ID = "plus-1-month-free"
TEAM_PROMO_CAMPAIGN_ID = "team-1-month-free"

_PROMO_INELIGIBLE_MARKERS = (
    "not eligible",
    "ineligible",
    "not available",
    "not applicable",
    "offer unavailable",
    "promotion unavailable",
    "coupon",
    "promo",
)
_PROMO_ALREADY_SUBSCRIBED_MARKERS = (
    "already subscribed",
    "active subscription",
    "already has",
    "existing subscription",
)


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    return build_requests_proxy_config(proxy)


_COUNTRY_CURRENCY_MAP = {
    "SG": "SGD",
    "US": "USD",
    "ID": "IDR",
    "TR": "TRY",
    "JP": "JPY",
    "HK": "HKD",
    "GB": "GBP",
    "EU": "EUR",
    "AU": "AUD",
    "CA": "CAD",
    "IN": "INR",
    "BR": "BRL",
    "MX": "MXN",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_loads(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _stringify_message(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("message", "detail", "reason", "type", "code"):
            nested = _stringify_message(value.get(key))
            if nested:
                return nested
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    if isinstance(value, list):
        for item in value:
            nested = _stringify_message(item)
            if nested:
                return nested
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value).strip()


def _extract_oai_did(cookies_str: str) -> Optional[str]:
    for part in cookies_str.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            return part[len("oai-did="):].strip()
    return None


def _build_account_headers(
    account: Any,
    *,
    include_language: bool = False,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }
    if include_language:
        headers["oai-language"] = "zh-CN"
    cookies = str(getattr(account, "cookies", "") or "").strip()
    if cookies:
        headers["cookie"] = cookies
        oai_did = _extract_oai_did(cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did
    return headers


def _parse_cookie_str(cookies_str: str, domain: str) -> list[dict[str, str]]:
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            }
        )
    return cookies


def _open_url_system_browser(url: str) -> bool:
    platform = sys.platform
    try:
        if platform == "win32":
            for browser, flag in [("chrome", "--incognito"), ("msedge", "--inprivate")]:
                try:
                    subprocess.Popen(f'start {browser} {flag} "{url}"', shell=True)
                    return True
                except Exception:
                    continue
        elif platform == "darwin":
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", "--incognito", url])
            return True
        else:
            for binary in ["google-chrome", "chromium-browser", "chromium"]:
                try:
                    subprocess.Popen([binary, "--incognito", url])
                    return True
                except FileNotFoundError:
                    continue
    except Exception as exc:
        logger.warning("Failed to open system browser in private mode: %s", exc)
    return False


def _extract_checkout_session_id(data: dict[str, Any]) -> str:
    return str(data.get("checkout_session_id") or data.get("session_id") or "").strip()


def _build_checkout_url(data: dict[str, Any]) -> str:
    direct_url = str(
        data.get("checkout_url")
        or data.get("cashier_url")
        or data.get("url")
        or ""
    ).strip()
    if direct_url:
        return direct_url

    session_id = _extract_checkout_session_id(data)
    if session_id:
        return TEAM_CHECKOUT_BASE_URL + session_id
    return ""


def _extract_response_message(data: dict[str, Any], body_text: str = "") -> str:
    candidates = [
        data.get("message"),
        data.get("detail"),
        data.get("error"),
        (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else "",
        (data.get("error") or {}).get("detail") if isinstance(data.get("error"), dict) else "",
    ]
    for candidate in candidates:
        message = _stringify_message(candidate)
        if message:
            return message
    return str(body_text or "").strip()


def _request_checkout(
    account: Any,
    payload: dict[str, Any],
    *,
    proxy: Optional[str] = None,
) -> tuple[int, dict[str, Any], str]:
    response = cffi_requests.post(
        PAYMENT_CHECKOUT_URL,
        headers=_build_account_headers(account, include_language=True),
        json=payload,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    body_text = response.text or ""
    return response.status_code, _safe_json_loads(body_text), body_text


def _fetch_me_context(account: Any, proxy: Optional[str] = None) -> tuple[int, dict[str, Any], str]:
    response = cffi_requests.get(
        CHATGPT_ME_URL,
        headers=_build_account_headers(account),
        proxies=_build_proxies(proxy),
        timeout=20,
        impersonate="chrome110",
    )
    body_text = response.text or ""
    return response.status_code, _safe_json_loads(body_text), body_text


def _normalize_plan_type(plan_type: str, workspace_plan_type: str = "") -> str:
    raw = f"{plan_type} {workspace_plan_type}".strip().lower()
    if not raw:
        return "unknown"
    if "enterprise" in raw:
        return "enterprise"
    if "team" in raw:
        return "team"
    if "plus" in raw:
        return "plus"
    if "pro" in raw:
        return "pro"
    if "free" in raw:
        return "free"
    return plan_type.strip().lower() or workspace_plan_type.strip().lower() or "unknown"


def _extract_workspace_plan_type(me_data: dict[str, Any]) -> str:
    orgs = ((me_data.get("orgs") or {}).get("data") if isinstance(me_data.get("orgs"), dict) else []) or []
    if not isinstance(orgs, list):
        return ""
    for org in orgs:
        if not isinstance(org, dict):
            continue
        settings = org.get("settings") or {}
        if isinstance(settings, dict) and str(settings.get("workspace_plan_type") or "").strip():
            return str(settings.get("workspace_plan_type") or "").strip()
    return ""


def probe_plus_promo_eligibility(
    account: Any,
    proxy: Optional[str] = None,
    country: str = "ID",
) -> dict[str, Any]:
    country = str(country or "ID").strip().upper()
    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    result = {
        "state": "not_checked",
        "eligible": None,
        "checked_at": _utcnow_iso(),
        "source": "payments_checkout",
        "offer_kind": "plus",
        "offer_title": "Plus 优惠",
        "country": country,
        "currency": currency,
        "campaign_id": PLUS_PROMO_CAMPAIGN_ID,
        "checkout_ui_mode": "hosted",
        "subscription_plan": "unknown",
        "workspace_plan_type": "",
        "http_status": 0,
        "message": "",
        "checkout_url": "",
        "checkout_session_id": "",
    }

    if not getattr(account, "access_token", ""):
        result.update(
            {
                "state": "missing_access_token",
                "message": "Account is missing access_token",
            }
        )
        return result

    me_status, me_data, me_body_text = _fetch_me_context(account, proxy=proxy)
    me_message = _extract_response_message(me_data, me_body_text)
    result["http_status"] = me_status

    if me_status == 200:
        workspace_plan_type = _extract_workspace_plan_type(me_data)
        plan = _normalize_plan_type(str(me_data.get("plan_type") or "").strip(), workspace_plan_type)
        result["subscription_plan"] = plan
        result["workspace_plan_type"] = workspace_plan_type
        if plan in {"plus", "team", "enterprise", "pro"}:
            result.update(
                {
                    "state": "already_subscribed",
                    "eligible": False,
                    "message": f"Current account plan is {plan}; skip Plus promo check",
                }
            )
            return result
    elif me_status == 401:
        result.update(
            {
                "state": "unauthorized",
                "eligible": False,
                "message": me_message or "access_token failed /backend-api/me validation",
            }
        )
        return result
    elif me_status == 403:
        result.update(
            {
                "state": "probe_failed",
                "eligible": False,
                "message": me_message or "Account cannot access /backend-api/me",
            }
        )
        return result
    elif me_status >= 400:
        result.update(
            {
                "state": "probe_failed",
                "eligible": False,
                "message": me_message or f"/backend-api/me returned HTTP {me_status}",
            }
        )
        return result

    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "promo_campaign": {
            "promo_campaign_id": PLUS_PROMO_CAMPAIGN_ID,
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }
    checkout_status, checkout_data, checkout_body_text = _request_checkout(account, payload, proxy=proxy)
    checkout_message = _extract_response_message(checkout_data, checkout_body_text)
    result["http_status"] = checkout_status
    result["message"] = checkout_message

    checkout_session_id = _extract_checkout_session_id(checkout_data)
    checkout_url = _build_checkout_url(checkout_data)
    if checkout_session_id or checkout_url:
        result.update(
            {
                "state": "eligible",
                "eligible": True,
                "checkout_session_id": checkout_session_id,
                "checkout_url": checkout_url,
                "message": checkout_message or "Hosted checkout link created with Plus promo parameters",
            }
        )
        return result

    lowered_message = checkout_message.lower()
    if any(marker in lowered_message for marker in _PROMO_ALREADY_SUBSCRIBED_MARKERS):
        result.update({"state": "already_subscribed", "eligible": False})
        return result
    if any(marker in lowered_message for marker in _PROMO_INELIGIBLE_MARKERS):
        result.update({"state": "ineligible", "eligible": False})
        return result

    if checkout_status >= 400:
        result.update(
            {
                "state": "probe_failed",
                "eligible": False,
                "message": checkout_message or f"Promo probe failed with HTTP {checkout_status}",
            }
        )
        return result

    result.update(
        {
            "state": "unknown",
            "eligible": None,
            "message": checkout_message or "Could not confirm promo eligibility from the checkout response",
        }
    )
    return result


def generate_plus_link(
    account: Any,
    proxy: Optional[str] = None,
    country: str = "ID",
) -> str:
    if not getattr(account, "access_token", ""):
        raise ValueError("Account is missing access_token")

    country = str(country or "ID").strip().upper()
    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "promo_campaign": {
            "promo_campaign_id": PLUS_PROMO_CAMPAIGN_ID,
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }
    status_code, data, body_text = _request_checkout(account, payload, proxy=proxy)
    if status_code >= 400:
        raise ValueError(_extract_response_message(data, body_text) or f"HTTP {status_code}")
    checkout_url = _build_checkout_url(data)
    if checkout_url:
        return checkout_url
    raise ValueError(_extract_response_message(data, body_text) or "API did not return a checkout session")


def generate_team_link(
    account: Any,
    workspace_name: str = "MyTeam",
    price_interval: str = "month",
    seat_quantity: int = 5,
    proxy: Optional[str] = None,
    country: str = "SG",
) -> str:
    if not getattr(account, "access_token", ""):
        raise ValueError("Account is missing access_token")

    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    payload = {
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": workspace_name,
            "price_interval": price_interval,
            "seat_quantity": seat_quantity,
        },
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {
            "promo_campaign_id": TEAM_PROMO_CAMPAIGN_ID,
            "is_coupon_from_query_param": True,
        },
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
    }
    status_code, data, body_text = _request_checkout(account, payload, proxy=proxy)
    if status_code >= 400:
        raise ValueError(_extract_response_message(data, body_text) or f"HTTP {status_code}")
    checkout_url = _build_checkout_url(data)
    if checkout_url:
        return checkout_url
    raise ValueError(_extract_response_message(data, body_text) or "API did not return a checkout session")


def open_url_incognito(url: str, cookies_str: Optional[str] = None) -> bool:
    import threading

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright is not installed, falling back to system browser")
        return _open_url_system_browser(url)

    def _launch() -> None:
        try:
            with sync_playwright() as playwright:
                ensure_browser_display_available(False)
                browser = playwright.chromium.launch(headless=False, args=["--incognito"])
                context = browser.new_context()
                if cookies_str:
                    context.add_cookies(_parse_cookie_str(cookies_str, "chatgpt.com"))
                page = context.new_page()
                page.goto(url)
                page.wait_for_timeout(300_000)
        except Exception as exc:
            logger.warning("Playwright private window open failed: %s", exc)

    threading.Thread(target=_launch, daemon=True).start()
    return True


def check_subscription_status(account: Any, proxy: Optional[str] = None) -> str:
    if not getattr(account, "access_token", ""):
        raise ValueError("Account is missing access_token")

    status_code, data, body_text = _fetch_me_context(account, proxy=proxy)
    if status_code >= 400:
        raise ValueError(_extract_response_message(data, body_text) or f"HTTP {status_code}")

    plan = str(data.get("plan_type") or "").strip()
    if "team" in plan.lower():
        return "team"
    if "plus" in plan.lower():
        return "plus"

    orgs = ((data.get("orgs") or {}).get("data") if isinstance(data.get("orgs"), dict) else []) or []
    if isinstance(orgs, list):
        for org in orgs:
            if not isinstance(org, dict):
                continue
            settings_ = org.get("settings", {})
            if settings_.get("workspace_plan_type") in ("team", "enterprise"):
                return "team"

    return "free"
