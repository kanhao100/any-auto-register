from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from core.base_mailbox import MailboxAccount, create_mailbox

from .oauth_client import OAuthClient
from .status_probe import extract_chatgpt_account_id, probe_local_chatgpt_status


def snapshot_mailbox_account(account: Any) -> dict[str, Any]:
    if not isinstance(account, MailboxAccount):
        return {}
    extra = account.extra if isinstance(account.extra, dict) else {}
    try:
        safe_extra = deepcopy(extra)
    except Exception:
        safe_extra = dict(extra or {})
    return {
        "email": str(account.email or "").strip(),
        "account_id": str(account.account_id or "").strip(),
        "extra": safe_extra,
    }


def _normalize_mail_provider(
    provider: Any,
    *,
    account_extra: dict[str, Any],
    config_extra: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    resolved = str(provider or "").strip().lower()
    snapshot_extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
    snapshot_provider = str(snapshot_extra.get("provider") or "").strip().lower()

    if not resolved:
        resolved = snapshot_provider or str(config_extra.get("mail_provider") or "").strip().lower()

    if resolved == "outlook":
        return "microsoft"

    if resolved == "mail_import":
        source = (
            snapshot_provider
            or str(account_extra.get("mail_import_source") or "").strip().lower()
            or str(config_extra.get("mail_import_source") or "").strip().lower()
        )
        return "applemail" if source == "applemail" else "microsoft"

    return resolved


def _hydrate_mailbox_runtime(mailbox: Any, mailbox_account: MailboxAccount) -> None:
    email = str(getattr(mailbox_account, "email", "") or "").strip()
    account_id = str(getattr(mailbox_account, "account_id", "") or "").strip()

    for attr, value in (("_email", email), ("email", email), ("_token", account_id)):
        if not value:
            continue
        if not hasattr(mailbox, attr):
            continue
        try:
            setattr(mailbox, attr, value)
        except Exception:
            pass


class ExistingMailboxEmailService:
    service_type = type("ST", (), {"value": "existing_mailbox"})()

    def __init__(self, mailbox: Any, mailbox_account: MailboxAccount, *, log_fn=None):
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account
        self._before_ids: set[str] = set()
        self._baseline_ready = False
        self._used_codes: set[str] = set()
        self._log_fn = log_fn

    def _log(self, message: str) -> None:
        if callable(self._log_fn):
            self._log_fn(message)

    def prime(self) -> None:
        if self._baseline_ready:
            return
        get_current_ids = getattr(self._mailbox, "get_current_ids", None)
        if callable(get_current_ids):
            try:
                self._before_ids = set(get_current_ids(self._mailbox_account) or [])
            except Exception as exc:
                self._log(f"[relogin] 初始化邮箱基线失败: {exc}")
                self._before_ids = set()
        self._baseline_ready = True

    def wait_for_verification_code(
        self,
        email: str,
        timeout: int = 120,
        otp_sent_at: float | None = None,
        exclude_codes=None,
    ) -> str:
        self.prime()
        excluded = set(exclude_codes or set()) | set(self._used_codes)
        code = self._mailbox.wait_for_code(
            self._mailbox_account,
            keyword="",
            timeout=timeout,
            before_ids=self._before_ids,
            otp_sent_at=otp_sent_at,
            exclude_codes=excluded,
        )
        if code:
            self._used_codes.add(str(code))
        return code


def _restore_existing_mailbox_service(
    account: Any,
    *,
    account_extra: dict[str, Any],
    config_extra: dict[str, Any],
    proxy: Optional[str],
    log_fn=None,
) -> tuple[Optional[ExistingMailboxEmailService], dict[str, Any]]:
    snapshot = account_extra.get("mailbox_account") if isinstance(account_extra.get("mailbox_account"), dict) else {}
    provider = _normalize_mail_provider(
        account_extra.get("mail_provider"),
        account_extra=account_extra,
        config_extra=config_extra,
        snapshot=snapshot,
    )
    info = {
        "available": False,
        "provider": provider,
        "source": "",
        "message": "",
    }

    if not provider:
        info["message"] = "缺少 mail_provider，无法恢复邮箱接码能力"
        return None, info

    merged_extra = dict(config_extra or {})
    merged_extra.update(account_extra or {})

    mailbox_snapshot = snapshot if snapshot else None
    if not mailbox_snapshot and provider == "luckmail":
        mailbox_snapshot = {
            "email": str(getattr(account, "email", "") or "").strip(),
            "account_id": str(account_extra.get("mailbox_token") or "").strip(),
            "extra": {
                "provider": "luckmail",
                "token": str(account_extra.get("mailbox_token") or "").strip(),
                "project_code": str(account_extra.get("luckmail_project_code") or "").strip(),
            },
        }
        info["source"] = "luckmail_token"
    elif mailbox_snapshot:
        info["source"] = "account_snapshot"

    try:
        mailbox = create_mailbox(provider=provider, extra=merged_extra, proxy=proxy)
    except Exception as exc:
        info["message"] = f"邮箱服务初始化失败: {exc}"
        return None, info

    if mailbox_snapshot:
        mailbox_account = MailboxAccount(
            email=str(mailbox_snapshot.get("email") or getattr(account, "email", "") or "").strip(),
            account_id=str(mailbox_snapshot.get("account_id") or "").strip(),
            extra=(
                deepcopy(mailbox_snapshot.get("extra"))
                if isinstance(mailbox_snapshot.get("extra"), dict)
                else {}
            ),
        )
    elif provider == "laoudo":
        try:
            mailbox_account = mailbox.get_email()
            info["source"] = "provider_fixed_account"
        except Exception as exc:
            info["message"] = f"固定邮箱恢复失败: {exc}"
            return None, info
    else:
        info["message"] = "当前账号没有可复用的邮箱快照"
        return None, info

    if not mailbox_account.email:
        info["message"] = "邮箱快照缺少 email"
        return None, info

    if str(getattr(account, "email", "") or "").strip():
        mailbox_account.email = str(getattr(account, "email", "") or "").strip()
    _hydrate_mailbox_runtime(mailbox, mailbox_account)

    info["available"] = True
    info["message"] = "邮箱接码能力已恢复"
    return ExistingMailboxEmailService(mailbox, mailbox_account, log_fn=log_fn), info


def _extract_session_token(oauth_client: OAuthClient) -> str:
    getter = getattr(oauth_client, "_get_cookie_value", None)
    if not callable(getter):
        return ""
    return str(
        getter("__Secure-next-auth.session-token", "chatgpt.com")
        or getter("__Secure-authjs.session-token", "chatgpt.com")
        or ""
    ).strip()


def _extract_workspace_id(oauth_client: OAuthClient) -> str:
    workspace_id = str(getattr(oauth_client, "last_workspace_id", "") or "").strip()
    if workspace_id:
        return workspace_id

    decode_cookie = getattr(oauth_client, "_decode_oauth_session_cookie", None)
    if not callable(decode_cookie):
        return ""

    try:
        session_data = decode_cookie() or {}
    except Exception:
        session_data = {}

    workspaces = session_data.get("workspaces") or []
    if not workspaces:
        return ""
    return str((workspaces[0] or {}).get("id") or "").strip()


def _build_probe_account(
    account: Any,
    *,
    access_token: str,
    refresh_token: str,
    id_token: str,
    session_token: str,
) -> Any:
    class _ProbeAccount:
        pass

    probe_account = _ProbeAccount()
    probe_account.email = getattr(account, "email", "")
    probe_account.user_id = getattr(account, "user_id", "") or ""
    probe_account.token = access_token
    probe_account.access_token = access_token
    probe_account.refresh_token = refresh_token
    probe_account.session_token = session_token
    probe_account.id_token = id_token
    probe_account.extra = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": session_token,
        "id_token": id_token,
    }
    return probe_account


def _run_chatgpt_oauth_repair_flow(
    account: Any,
    *,
    config: Optional[dict[str, Any]] = None,
    proxy: Optional[str] = None,
    browser_mode: str = "protocol",
    log_fn=None,
    log_tag: str = "relogin",
    action_label: str = "二次登录",
    token_source: str = "relogin",
    force_chatgpt_entry: bool = False,
) -> dict[str, Any]:
    email = str(getattr(account, "email", "") or "").strip()
    password = str(getattr(account, "password", "") or "").strip()
    log_prefix = f"[{log_tag}]"
    if not email:
        raise RuntimeError(f"账号缺少邮箱，无法{action_label}")
    if not password:
        raise RuntimeError(f"账号缺少密码，无法{action_label}")

    account_extra = getattr(account, "extra", {}) or {}
    config_extra = dict(config or {})
    resolved_browser_mode = (
        str(browser_mode or "").strip().lower()
        or str(config_extra.get("default_executor") or "").strip().lower()
        or "protocol"
    )

    mailbox_service, mailbox_info = _restore_existing_mailbox_service(
        account,
        account_extra=account_extra,
        config_extra=config_extra,
        proxy=proxy,
        log_fn=log_fn,
    )
    if callable(log_fn):
        log_fn(
            f"{log_prefix} 邮箱恢复结果: "
            f"provider={mailbox_info.get('provider') or '-'} "
            f"available={'yes' if mailbox_info.get('available') else 'no'} "
            f"source={mailbox_info.get('source') or '-'}"
        )
        if mailbox_info.get("message"):
            log_fn(f"{log_prefix} 邮箱说明: {mailbox_info['message']}")
    if mailbox_service is not None:
        mailbox_service.prime()
        if callable(log_fn):
            log_fn(f"{log_prefix} 已建立邮箱基线，后续可轮询 OTP")

    oauth_client = OAuthClient(
        config_extra,
        proxy=proxy,
        verbose=False,
        browser_mode=resolved_browser_mode,
    )
    if callable(log_fn):
        oauth_client._log = log_fn

    attempts = [
        {
            "label": "password",
            "prefer_passwordless_login": False,
            "force_password_login": True,
            "skymail_client": mailbox_service,
        }
    ]
    if mailbox_service is not None:
        attempts.append(
            {
                "label": "passwordless",
                "prefer_passwordless_login": True,
                "force_password_login": False,
                "skymail_client": mailbox_service,
            }
        )

    last_error = ""
    relogin_method = ""
    tokens = None
    for attempt in attempts:
        if callable(log_fn):
            log_fn(
                f"{log_prefix} 尝试{action_label}: "
                f"mode={attempt['label']} mailbox={'on' if attempt['skymail_client'] else 'off'}"
            )
        tokens = oauth_client.login_and_get_tokens(
            email,
            password,
            device_id="",
            skymail_client=attempt["skymail_client"],
            prefer_passwordless_login=attempt["prefer_passwordless_login"],
            allow_phone_verification=False,
            force_new_browser=True,
            force_chatgpt_entry=force_chatgpt_entry,
            screen_hint="login",
            force_password_login=attempt["force_password_login"],
            complete_about_you_if_needed=False,
            login_source=f"manual_{token_source}:{attempt['label']}",
        )
        if tokens:
            relogin_method = attempt["label"]
            break
        last_error = str(getattr(oauth_client, "last_error", "") or "").strip()
        if callable(log_fn):
            log_fn(
                f"{log_prefix} mode={attempt['label']} 失败: {last_error or '未返回 tokens'}"
            )

    if not tokens:
        if not mailbox_info.get("available") and mailbox_info.get("message"):
            detail = f"{last_error or f'{action_label}失败'}；{mailbox_info['message']}"
        else:
            detail = last_error or f"{action_label}失败"
        raise RuntimeError(detail)

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    session_token = _extract_session_token(oauth_client)
    workspace_id = _extract_workspace_id(oauth_client)
    if callable(log_fn):
        log_fn(
            f"{log_prefix} Token 获取成功: "
            f"has_access_token={bool(access_token)} "
            f"has_refresh_token={bool(refresh_token)} "
            f"has_session_token={bool(session_token)} "
            f"workspace_id={workspace_id or '-'}"
        )

    probe_account = _build_probe_account(
        account,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        session_token=session_token,
    )
    account_id = extract_chatgpt_account_id(probe_account) or str(getattr(account, "user_id", "") or "").strip()
    probe_account.user_id = account_id
    if callable(log_fn):
        log_fn(f"{log_prefix} 开始执行本地状态探测")
    probe = probe_local_chatgpt_status(probe_account, proxy=proxy)
    if callable(log_fn):
        log_fn(
            f"{log_prefix} 本地状态探测完成: "
            f"auth={probe.get('auth', {}).get('state', 'unknown')} "
            f"subscription={probe.get('subscription', {}).get('plan', 'unknown')} "
            f"codex={probe.get('codex', {}).get('state', 'unknown')}"
        )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "workspace_id": workspace_id,
        "account_id": account_id,
        "probe": probe,
        "relogin_method": relogin_method or "password",
        "mailbox": mailbox_info,
        "token_source": token_source,
        "message": (
            f"{action_label}成功，已重新获取 Token"
            + (f"（mode={relogin_method}）" if relogin_method else "")
        ),
    }


def relogin_chatgpt_account(
    account: Any,
    *,
    config: Optional[dict[str, Any]] = None,
    proxy: Optional[str] = None,
    browser_mode: str = "protocol",
    log_fn=None,
) -> dict[str, Any]:
    return _run_chatgpt_oauth_repair_flow(
        account,
        config=config,
        proxy=proxy,
        browser_mode=browser_mode,
        log_fn=log_fn,
        log_tag="relogin",
        action_label="二次登录",
        token_source="relogin",
        force_chatgpt_entry=False,
    )


def reauthorize_chatgpt_tokens(
    account: Any,
    *,
    config: Optional[dict[str, Any]] = None,
    proxy: Optional[str] = None,
    browser_mode: str = "protocol",
    log_fn=None,
) -> dict[str, Any]:
    return _run_chatgpt_oauth_repair_flow(
        account,
        config=config,
        proxy=proxy,
        browser_mode=browser_mode,
        log_fn=log_fn,
        log_tag="reauthorize_rt",
        action_label="重新授权 RT",
        token_source="reauthorize_rt",
        force_chatgpt_entry=True,
    )
