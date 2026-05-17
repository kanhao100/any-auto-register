from __future__ import annotations

from copy import deepcopy
from email import message_from_bytes
from email.policy import default as email_default_policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from core.base_mailbox import MailboxAccount, OutlookMailbox, create_mailbox
from core.config_store import config_store


MICROSOFT_STANDARD_FOLDERS = [
    {"key": "inbox", "label": "收件箱"},
    {"key": "junk", "label": "垃圾箱"},
    {"key": "trash", "label": "已删除"},
]

MAILAPI_ONLY_FOLDERS = [
    {"key": "inbox", "label": "邮件内容"},
]

GRAPH_FOLDER_MAP = {
    "inbox": "inbox",
    "junk": "junkemail",
    "trash": "deleteditems",
}

IMAP_FOLDER_MAP = {
    "inbox": ["INBOX"],
    "junk": ["Junk", "Junk E-mail", "Spam"],
    "trash": ["Deleted Items", "Trash", "Deleted Messages"],
}


def _safe_copy_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return deepcopy(value)
    except Exception:
        return dict(value)


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
        if not value or not hasattr(mailbox, attr):
            continue
        try:
            setattr(mailbox, attr, value)
        except Exception:
            pass


def _normalize_text(value: Any, limit: int = 12000) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n\n[内容过长，已截断]"


def _build_preview(value: str, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _normalize_received_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text).isoformat()
    except Exception:
        return text


def _account_extra(account: Any) -> dict[str, Any]:
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            result = getter()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    extra_json = getattr(account, "extra_json", "{}")
    if isinstance(extra_json, dict):
        return extra_json
    try:
        import json

        data = json.loads(extra_json or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _restore_mailbox(account: Any, *, config_extra: dict[str, Any] | None = None, proxy: str | None = None) -> tuple[str, OutlookMailbox, MailboxAccount]:
    account_extra = _account_extra(account)
    snapshot = account_extra.get("mailbox_account") if isinstance(account_extra.get("mailbox_account"), dict) else {}
    resolved_config = dict(config_extra or config_store.get_all() or {})
    provider = _normalize_mail_provider(
        account_extra.get("mail_provider"),
        account_extra=account_extra,
        config_extra=resolved_config,
        snapshot=snapshot,
    )

    if provider != "microsoft":
        raise RuntimeError("当前账号未绑定可浏览的微软邮箱快照")
    if not snapshot:
        raise RuntimeError("当前账号没有可复用的邮箱快照")

    mailbox = create_mailbox(provider=provider, extra={**resolved_config, **account_extra}, proxy=proxy)
    if not isinstance(mailbox, OutlookMailbox):
        raise RuntimeError("当前邮箱提供商暂不支持邮件浏览")

    mailbox_account = MailboxAccount(
        email=str(snapshot.get("email") or getattr(account, "email", "") or "").strip(),
        account_id=str(snapshot.get("account_id") or "").strip(),
        extra=_safe_copy_dict(snapshot.get("extra")),
    )
    if str(getattr(account, "email", "") or "").strip():
        mailbox_account.email = str(getattr(account, "email", "") or "").strip()
    if not mailbox_account.email:
        raise RuntimeError("邮箱快照缺少 email")

    _hydrate_mailbox_runtime(mailbox, mailbox_account)
    return provider, mailbox, mailbox_account


def _normalize_graph_sender(message: dict[str, Any]) -> tuple[str, str]:
    sender = message.get("from") or {}
    if not isinstance(sender, dict):
        return "", ""
    address = sender.get("emailAddress") or {}
    if not isinstance(address, dict):
        return "", ""
    return (
        str(address.get("name") or "").strip(),
        str(address.get("address") or "").strip(),
    )


def _normalize_graph_message(mailbox: OutlookMailbox, folder_key: str, message: dict[str, Any]) -> dict[str, Any]:
    message_id = str(message.get("id") or "").strip()
    subject = str(message.get("subject") or "").strip() or "(无主题)"
    sender_name, sender_address = _normalize_graph_sender(message)
    body = _normalize_text(mailbox._graph_message_text(message))
    return {
        "id": f"{folder_key}:{message_id}" if message_id else "",
        "folder": folder_key,
        "subject": subject,
        "sender": sender_name or sender_address or "-",
        "sender_address": sender_address,
        "received_at": str(message.get("receivedDateTime") or "").strip(),
        "preview": _build_preview(body or str(message.get("bodyPreview") or "").strip()),
        "body": body,
    }


def _list_graph_messages(mailbox: OutlookMailbox, mailbox_account: MailboxAccount, folder_key: str, limit: int) -> list[dict[str, Any]]:
    actual_folder = GRAPH_FOLDER_MAP.get(folder_key, "inbox")
    access_token = mailbox._get_oauth_access_token(
        mailbox_account,
        preferred_backend="graph",
    )
    try:
        messages = mailbox._graph_list_messages(
            access_token=access_token,
            folder=actual_folder,
        )
    except Exception as exc:
        if "HTTP 401" not in str(exc):
            raise
        cache = (mailbox_account.extra or {}).get("_oauth_token_cache")
        if isinstance(cache, dict):
            cache.pop(mailbox._normalize_backend_name("graph"), None)
        access_token = mailbox._get_oauth_access_token(
            mailbox_account,
            preferred_backend="graph",
        )
        messages = mailbox._graph_list_messages(
            access_token=access_token,
            folder=actual_folder,
        )

    return [
        _normalize_graph_message(mailbox, folder_key, message)
        for message in list(messages or [])[: max(int(limit or 0), 1)]
        if isinstance(message, dict)
    ]


def _normalize_imap_message(mailbox: OutlookMailbox, folder_key: str, folder_name: str, uid: bytes | str, raw: bytes) -> dict[str, Any]:
    uid_text = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid or "")
    msg = message_from_bytes(raw, policy=email_default_policy)
    subject = mailbox._decode_header_value(msg.get("Subject", "")) or "(无主题)"
    sender_raw = mailbox._decode_header_value(msg.get("From", ""))
    sender_name, sender_address = parseaddr(sender_raw)
    body = _normalize_text(mailbox._extract_message_text(msg))
    return {
        "id": f"{folder_name}:{uid_text}" if uid_text else f"{folder_key}:{folder_name}",
        "folder": folder_key,
        "subject": subject,
        "sender": sender_name or sender_address or "-",
        "sender_address": sender_address,
        "received_at": _normalize_received_at(mailbox._decode_header_value(msg.get("Date", ""))),
        "preview": _build_preview(body),
        "body": body,
    }


def _list_imap_messages(mailbox: OutlookMailbox, mailbox_account: MailboxAccount, folder_key: str, limit: int) -> list[dict[str, Any]]:
    candidates = IMAP_FOLDER_MAP.get(folder_key, IMAP_FOLDER_MAP["inbox"])
    imap_conn = mailbox._open_imap(mailbox_account)
    try:
        for folder_name in candidates:
            status, _ = imap_conn.select(folder_name, readonly=True)
            if status != "OK":
                continue
            status, data = imap_conn.uid("search", None, "ALL")
            if status != "OK":
                return []
            ids = data[0].split() if data and data[0] else []
            selected_ids = list(reversed(ids[-max(int(limit or 0), 1):]))
            messages: list[dict[str, Any]] = []
            for uid in selected_ids:
                status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = None
                for item in msg_data or []:
                    if isinstance(item, tuple) and item[1]:
                        raw = item[1]
                        break
                if not raw:
                    continue
                messages.append(
                    _normalize_imap_message(
                        mailbox,
                        folder_key,
                        folder_name,
                        uid,
                        raw,
                    )
                )
            return messages
        return []
    finally:
        try:
            imap_conn.logout()
        except Exception:
            pass


def _list_mailapi_messages(mailbox: OutlookMailbox, mailbox_account: MailboxAccount) -> list[dict[str, Any]]:
    backend = mailbox._backends.get("mailapi_url")
    if backend is None:
        raise RuntimeError("当前邮箱不支持 MailAPI 预览")
    text = str(backend._fetch_mailapi_text(mailbox_account) or "")
    body = _normalize_text(mailbox._decode_raw_content(text) or text)
    return [
        {
            "id": "mailapi:latest",
            "folder": "inbox",
            "subject": "MailAPI 最新内容",
            "sender": "-",
            "sender_address": "",
            "received_at": "",
            "preview": _build_preview(body),
            "body": body,
        }
    ]


def load_account_mailbox_messages(
    account: Any,
    *,
    folder: str = "inbox",
    limit: int = 25,
    config_extra: dict[str, Any] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    _, mailbox, mailbox_account = _restore_mailbox(
        account,
        config_extra=config_extra,
        proxy=proxy,
    )

    backend = mailbox._resolve_backend(mailbox_account)
    backend_name = str(getattr(backend, "backend_name", "") or "").strip().lower() or "graph"
    folder_key = str(folder or "inbox").strip().lower() or "inbox"

    if backend_name == "mailapi_url":
        active_folder = "inbox"
        folders = MAILAPI_ONLY_FOLDERS
        messages = _list_mailapi_messages(mailbox, mailbox_account)
    elif backend_name == "imap":
        active_folder = folder_key if folder_key in {"inbox", "junk", "trash"} else "inbox"
        folders = MICROSOFT_STANDARD_FOLDERS
        messages = _list_imap_messages(mailbox, mailbox_account, active_folder, limit)
    else:
        active_folder = folder_key if folder_key in {"inbox", "junk", "trash"} else "inbox"
        folders = MICROSOFT_STANDARD_FOLDERS
        messages = _list_graph_messages(mailbox, mailbox_account, active_folder, limit)

    return {
        "provider": "microsoft",
        "backend": backend_name,
        "email": mailbox_account.email,
        "active_folder": active_folder,
        "folders": folders,
        "messages": messages,
        "limit": max(int(limit or 0), 1),
    }
