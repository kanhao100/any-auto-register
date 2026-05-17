"""ChatGPT / Codex CLI platform plugin."""

import random
import string

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.registry import register
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.relogin import (
    reauthorize_chatgpt_tokens,
    relogin_chatgpt_account,
    snapshot_mailbox_account,
)


def _merge_local_probe(existing: dict | None, **patch: dict) -> dict:
    merged = dict(existing or {})
    for key, value in patch.items():
        if isinstance(value, dict):
            merged[key] = value
    return merged


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            status = check_subscription_status(
                a,
                proxy=self.config.proxy if self.config else None,
            )
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    @staticmethod
    def _attach_mailbox_snapshot(account_obj, email_service) -> None:
        if not hasattr(account_obj, "extra") or not isinstance(account_obj.extra, dict):
            return
        mailbox_snapshot = snapshot_mailbox_account(getattr(email_service, "_acct", None))
        if mailbox_snapshot:
            account_obj.extra.setdefault("mailbox_account", mailbox_snapshot)

    def register(self, email: str = None, password: str = None) -> Account:
        if not password:
            password = "".join(
                random.choices(string.ascii_letters + string.digits + "!@#$", k=16)
            )

        proxy = self.config.proxy if self.config else None
        browser_mode = (self.config.executor_type if self.config else None) or "protocol"
        extra_config = (
            (self.config.extra or {})
            if self.config and getattr(self.config, "extra", None)
            else {}
        )
        log_fn = getattr(self, "_log_fn", print)
        max_retries = 3
        try:
            max_retries = int(extra_config.get("register_max_retries", 3) or 3)
        except Exception:
            max_retries = 3

        def _resolve_mailbox_timeout(requested_timeout: int) -> int:
            candidates = (
                extra_config.get("mailbox_otp_timeout_seconds"),
                extra_config.get("email_otp_timeout_seconds"),
                extra_config.get("otp_timeout"),
                requested_timeout,
            )
            for value in candidates:
                if value in (None, ""):
                    continue
                try:
                    seconds = int(value)
                except (TypeError, ValueError):
                    continue
                if seconds > 0:
                    return seconds
            return requested_timeout

        if self.mailbox:
            _mailbox = self.mailbox
            _fixed_email = email

            def _resolve_email(candidate_email: str = "") -> str:
                resolved_email = str(_fixed_email or candidate_email or "").strip()
                if not resolved_email:
                    raise RuntimeError("custom_provider 返回空邮箱地址")
                return resolved_email

            class GenericEmailService:
                service_type = type("ST", (), {"value": "custom_provider"})()

                def __init__(self):
                    self._acct = None
                    self._email = _fixed_email
                    self._before_ids = set()

                def create_email(self, config=None):
                    if self._email and self._acct and _fixed_email:
                        return {
                            "email": self._email,
                            "service_id": self._acct.account_id,
                            "token": "",
                        }
                    self._acct = _mailbox.get_email()
                    get_current_ids = getattr(_mailbox, "get_current_ids", None)
                    if callable(get_current_ids):
                        self._before_ids = set(get_current_ids(self._acct) or [])
                    else:
                        self._before_ids = set()
                    generated_email = getattr(self._acct, "email", "")
                    if not self._email:
                        self._email = _resolve_email(generated_email)
                    elif not _fixed_email:
                        self._email = _resolve_email(generated_email)
                    return {
                        "email": self._email,
                        "service_id": self._acct.account_id,
                        "token": "",
                    }

                def get_verification_code(
                    self,
                    email=None,
                    email_id=None,
                    timeout=120,
                    pattern=None,
                    otp_sent_at=None,
                    exclude_codes=None,
                ):
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法获取验证码")
                    return _mailbox.wait_for_code(
                        self._acct,
                        keyword="",
                        timeout=_resolve_mailbox_timeout(timeout),
                        before_ids=self._before_ids,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=exclude_codes,
                    )

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = GenericEmailService()
        else:
            from core.base_mailbox import TempMailLolMailbox

            _tmail = TempMailLolMailbox(proxy=proxy)
            _tmail._task_control = getattr(self, "_task_control", None)

            class TempMailEmailService:
                service_type = type("ST", (), {"value": "tempmail_lol"})()

                def __init__(self):
                    self._acct = None
                    self._before_ids = set()

                def create_email(self, config=None):
                    acct = _tmail.get_email()
                    self._acct = acct
                    self._before_ids = set(_tmail.get_current_ids(acct) or [])
                    resolved_email = str(getattr(acct, "email", "") or "").strip()
                    if not resolved_email:
                        raise RuntimeError("tempmail_lol 返回空邮箱地址")
                    return {
                        "email": resolved_email,
                        "service_id": acct.account_id,
                        "token": acct.account_id,
                    }

                def get_verification_code(
                    self,
                    email=None,
                    email_id=None,
                    timeout=120,
                    pattern=None,
                    otp_sent_at=None,
                    exclude_codes=None,
                ):
                    return _tmail.wait_for_code(
                        self._acct,
                        keyword="",
                        timeout=_resolve_mailbox_timeout(timeout),
                        before_ids=self._before_ids,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=exclude_codes,
                    )

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = TempMailEmailService()

        adapter = build_chatgpt_registration_mode_adapter(extra_config)
        context = ChatGPTRegistrationContext(
            email_service=email_service,
            proxy_url=proxy,
            callback_logger=log_fn,
            email=email,
            password=password,
            browser_mode=browser_mode,
            max_retries=max_retries,
            extra_config=extra_config,
        )
        result = adapter.run(context)
        if not result or not result.success:
            raise RuntimeError(result.error_message if result else "注册失败")

        account_obj = adapter.build_account(result, password)
        self._attach_mailbox_snapshot(account_obj, email_service)
        return account_obj

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "probe_promo_eligibility", "label": "检测 Plus 优惠资格", "params": []},
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "relogin", "label": "二次登录取 Token", "params": [], "run_mode": "task"},
            {"id": "reauthorize_rt", "label": "重新授权 RT (AT+RT)", "params": [], "run_mode": "task"},
            {
                "id": "payment_link",
                "label": "生成 Plus GoPay 长链接",
                "params": [
                    {
                        "key": "country",
                        "label": "地区",
                        "type": "select",
                        "options": ["ID", "US", "SG", "TR", "HK", "JP", "GB", "AU", "CA"],
                    },
                    {
                        "key": "plan",
                        "label": "套餐",
                        "type": "select",
                        "options": ["plus", "team"],
                    },
                ],
            },
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_tm",
                "label": "上传 Team Manager",
                "params": [
                    {"key": "api_url", "label": "TM API URL", "type": "text"},
                    {"key": "api_key", "label": "TM API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex_proxy",
                "label": "上传 CodexProxy",
                "params": [
                    {"key": "api_url", "label": "API URL", "type": "text"},
                    {"key": "api_key", "label": "Admin Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A:
            pass

        a = _A()
        a.email = account.email
        a.password = account.password
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id
        a.extra = extra

        def _execute_oauth_repair_action(
            *,
            runner,
            success_label: str,
            failure_label: str,
            result_extra_key: str,
            default_token_source: str,
        ) -> dict:
            outer_log_fn = getattr(self, "_log_fn", None)
            action_logs: list[str] = []

            def _capture_action_log(message: str) -> None:
                text = str(message or "").strip()
                if not text:
                    return
                action_logs.append(text)
                if callable(outer_log_fn):
                    outer_log_fn(text)

            try:
                action_result = runner(
                    a,
                    config=self.config.extra if self.config else {},
                    proxy=proxy,
                    browser_mode=(
                        (self.config.executor_type if self.config else None)
                        or ((self.config.extra or {}).get("default_executor") if self.config else None)
                        or "protocol"
                    ),
                    log_fn=_capture_action_log,
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "data": {
                        "message": f"{failure_label}: {exc}",
                        "logs": action_logs,
                    },
                }

            probe_result = (
                action_result.get("probe")
                if isinstance(action_result.get("probe"), dict)
                else {}
            )
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "access_token": action_result.get("access_token", ""),
                    "refresh_token": action_result.get("refresh_token", ""),
                    "id_token": action_result.get("id_token", ""),
                    "session_token": action_result.get("session_token", ""),
                    "workspace_id": action_result.get("workspace_id", ""),
                    "message": f"{success_label}：{summary}",
                    "probe": probe_result,
                    "logs": action_logs,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                    result_extra_key: {
                        "method": action_result.get("relogin_method", ""),
                        "mailbox": action_result.get("mailbox", {}),
                    },
                    "chatgpt_token_source": action_result.get(
                        "token_source",
                        default_token_source,
                    ),
                },
            }

        if action_id == "probe_local_status":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            probe_result = probe_local_chatgpt_status(a, proxy=proxy)
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "message": f"本地状态探测完成：{summary}",
                    "probe": probe_result,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                },
            }

        if action_id == "probe_promo_eligibility":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status
            from platforms.chatgpt.payment import probe_plus_promo_eligibility

            fresh_probe = probe_local_chatgpt_status(a, proxy=proxy)
            promo_result = probe_plus_promo_eligibility(
                a,
                proxy=proxy,
                country=str(params.get("country", "ID") or "ID").strip().upper(),
                local_probe=fresh_probe,
            )
            local_probe = _merge_local_probe(fresh_probe, promo=promo_result)
            summary = (
                f"认证={local_probe.get('auth', {}).get('state', 'unknown')}, "
                f"优惠={promo_result.get('state', 'unknown')}, "
                f"plan={promo_result.get('subscription_plan', 'unknown')}"
            )
            return {
                "ok": promo_result.get("state")
                not in {"probe_failed", "unauthorized", "missing_access_token"},
                "data": {
                    "message": f"Plus 优惠资格探测完成：{summary}",
                    "probe": local_probe,
                    "promo": promo_result,
                },
                "error": promo_result.get("message", ""),
                "account_extra_patch": {
                    "chatgpt_local": local_probe,
                },
            }

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = bool(sync_result.get("uploaded")) and sync_result.get("remote_state") not in {
                "unreachable",
                "not_found",
            }
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "relogin":
            return _execute_oauth_repair_action(
                runner=relogin_chatgpt_account,
                success_label="二次登录完成",
                failure_label="二次登录失败",
                result_extra_key="chatgpt_last_relogin",
                default_token_source="relogin",
            )

        if action_id == "reauthorize_rt":
            return _execute_oauth_repair_action(
                runner=reauthorize_chatgpt_tokens,
                success_label="重新授权 RT 完成",
                failure_label="重新授权 RT 失败",
                result_extra_key="chatgpt_last_rt_reauthorization",
                default_token_source="reauthorize_rt",
            )

        if action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link

            plan = params.get("plan", "plus")
            country = params.get("country", "ID")
            if plan == "plus":
                url = generate_plus_link(a, proxy=proxy, country=country)
            else:
                url = generate_team_link(
                    a,
                    workspace_name=params.get("workspace_name", "MyTeam"),
                    price_interval=params.get("price_interval", "month"),
                    seat_quantity=int(params.get("seat_quantity", 5) or 5),
                    proxy=proxy,
                    country=country,
                )
            return {"ok": bool(url), "data": {"url": url}}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager

            ok, msg = upload_to_team_manager(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex_proxy":
            upload_type = str(
                params.get("upload_type")
                or (self.config.extra or {}).get("codex_proxy_upload_type")
                or "at"
            ).strip().lower()

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy

                ok, msg = upload_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy

                ok, msg = upload_at_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
