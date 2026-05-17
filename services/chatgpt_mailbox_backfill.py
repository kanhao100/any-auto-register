from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select, func

from core.db import AccountModel, OutlookAccountModel, engine
from services.mail_imports.microsoft_import_rules import (
    ACCOUNT_TYPE_MICROSOFT_OAUTH,
    AutoDetectRowParser,
    MicrosoftMailImportRecord,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def discover_default_backfill_files(base_dirs: list[Path] | None = None) -> list[Path]:
    roots = list(base_dirs or [])
    if not roots:
        roots = [Path.cwd(), Path(__file__).resolve().parents[1]]

    patterns = [
        "Free*邮箱*.txt",
        "*账号的邮箱*.txt",
        "卡密导出_*.txt",
    ]

    results: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved_root = root.resolve()
        except Exception:
            resolved_root = root
        if not resolved_root.exists():
            continue
        for pattern in patterns:
            for path in resolved_root.glob(pattern):
                try:
                    resolved = path.resolve()
                except Exception:
                    resolved = path
                key = str(resolved).lower()
                if key in seen or not resolved.is_file():
                    continue
                seen.add(key)
                results.append(resolved)
    return results


def _read_text_with_fallbacks(path: Path) -> str:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"无法读取文件 {path}: {'; '.join(errors)}")


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lstrip("\ufeff")


def load_microsoft_records_from_files(file_paths: list[str] | list[Path]) -> tuple[list[MicrosoftMailImportRecord], dict[str, Any]]:
    parser = AutoDetectRowParser()
    record_map: dict[str, MicrosoftMailImportRecord] = {}
    stats = {
        "files": [],
        "total_lines": 0,
        "actionable_lines": 0,
        "duplicate_emails": 0,
    }

    for raw_path in file_paths:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"文件不存在: {path}")

        content = _read_text_with_fallbacks(path)
        file_total_lines = 0
        file_actionable_lines = 0

        for idx, raw_line in enumerate(content.splitlines(), start=1):
            file_total_lines += 1
            line = str(raw_line or "").strip()
            if (
                not line
                or line.startswith("#")
                or line.startswith("卡密导出")
                or "----" not in line
            ):
                continue

            file_actionable_lines += 1
            record = parser.parse(idx, line)
            record.email = _normalize_email(record.email)
            key = record.email.lower()
            if key in record_map:
                stats["duplicate_emails"] += 1
            record_map[key] = record

        stats["total_lines"] += file_total_lines
        stats["actionable_lines"] += file_actionable_lines
        stats["files"].append(
            {
                "path": str(path),
                "total_lines": file_total_lines,
                "actionable_lines": file_actionable_lines,
            }
        )

    return list(record_map.values()), stats


def _upsert_outlook_account(
    session: Session,
    record: MicrosoftMailImportRecord,
) -> tuple[OutlookAccountModel, bool]:
    existing = session.exec(
        select(OutlookAccountModel).where(
            OutlookAccountModel.email.in_(
                [record.email, f"\ufeff{record.email}"]
            )
        )
    ).first()
    created = existing is None

    if existing is None:
        existing = OutlookAccountModel(
            email=record.email,
            password=record.password,
            client_id=record.client_id,
            refresh_token=record.refresh_token,
            account_type=record.account_type,
            mailapi_url=record.mailapi_url,
            enabled=True,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
    else:
        existing.password = record.password
        existing.client_id = record.client_id
        existing.refresh_token = record.refresh_token
        existing.account_type = record.account_type
        existing.mailapi_url = record.mailapi_url
        existing.enabled = True
        existing.updated_at = _utcnow()

    session.add(existing)
    session.flush()
    return existing, created


def _normalize_existing_outlook_rows(session: Session) -> dict[str, int]:
    rows = session.exec(
        select(OutlookAccountModel).where(OutlookAccountModel.email.contains("\ufeff"))
    ).all()
    normalized = 0
    merged = 0

    for row in rows:
        cleaned_email = _normalize_email(row.email)
        if not cleaned_email or cleaned_email == row.email:
            continue

        target = session.exec(
            select(OutlookAccountModel)
            .where(OutlookAccountModel.email == cleaned_email)
            .where(OutlookAccountModel.id != row.id)
        ).first()

        if target is not None:
            if not target.password:
                target.password = row.password
            if not target.client_id:
                target.client_id = row.client_id
            if not target.refresh_token:
                target.refresh_token = row.refresh_token
            if not getattr(target, "mailapi_url", ""):
                target.mailapi_url = getattr(row, "mailapi_url", "")
            if not getattr(target, "account_type", ""):
                target.account_type = getattr(row, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH)
            target.enabled = bool(target.enabled or row.enabled)
            target.updated_at = _utcnow()
            session.add(target)
            session.delete(row)
            merged += 1
            continue

        row.email = cleaned_email
        row.updated_at = _utcnow()
        session.add(row)
        normalized += 1

    if rows:
        session.flush()
    return {"normalized": normalized, "merged": merged}


def _build_mailbox_snapshot(outlook_account: OutlookAccountModel) -> dict[str, Any]:
    account_type = str(
        getattr(outlook_account, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH)
        or ACCOUNT_TYPE_MICROSOFT_OAUTH
    ).strip() or ACCOUNT_TYPE_MICROSOFT_OAUTH

    return {
        "email": str(outlook_account.email or "").strip(),
        "account_id": str(outlook_account.id or "").strip(),
        "extra": {
            "provider": "microsoft",
            "password": str(outlook_account.password or ""),
            "client_id": str(outlook_account.client_id or ""),
            "refresh_token": str(outlook_account.refresh_token or ""),
            "account_type": account_type,
            "mailapi_url": str(getattr(outlook_account, "mailapi_url", "") or "").strip(),
            "outlook_backend": "graph",
        },
    }


def _patch_chatgpt_account_mailbox(
    account: AccountModel,
    snapshot: dict[str, Any],
    *,
    overwrite_existing_snapshot: bool,
    overwrite_mail_provider: bool,
) -> str:
    extra = account.get_extra()
    existing_snapshot = extra.get("mailbox_account")
    if isinstance(existing_snapshot, dict) and existing_snapshot and not overwrite_existing_snapshot:
        return "skipped_existing_snapshot"

    provider = str(extra.get("mail_provider") or "").strip().lower()
    if provider == "outlook":
        extra["mail_provider"] = "microsoft"
    elif not provider:
        extra["mail_provider"] = "microsoft"
    elif provider == "mail_import":
        extra.setdefault("mail_import_source", "microsoft")
    elif overwrite_mail_provider:
        extra["mail_provider"] = "microsoft"

    extra["mailbox_account"] = snapshot
    account.set_extra(extra)
    account.updated_at = _utcnow()
    return "linked"


def backfill_chatgpt_microsoft_mailboxes(
    *,
    file_paths: list[str] | None = None,
    overwrite_existing_snapshot: bool = False,
    overwrite_mail_provider: bool = False,
    session: Session | None = None,
) -> dict[str, Any]:
    resolved_files = [Path(path) for path in (file_paths or [])]
    if not resolved_files:
        resolved_files = discover_default_backfill_files()
    if not resolved_files:
        raise RuntimeError("未找到可用于回填的邮箱导出文件")

    records, load_stats = load_microsoft_records_from_files(resolved_files)
    if not records:
        raise RuntimeError("导出文件中没有可用的微软邮箱记录")

    owns_session = session is None
    session = session or Session(engine)
    try:
        cleanup_stats = _normalize_existing_outlook_rows(session)
        created_count = 0
        updated_count = 0
        record_by_email: dict[str, dict[str, Any]] = {}

        for record in records:
            outlook_account, created = _upsert_outlook_account(session, record)
            if created:
                created_count += 1
            else:
                updated_count += 1
            record_by_email[record.email.strip().lower()] = {
                "outlook_account": outlook_account,
                "record": record,
            }

        emails = list(record_by_email.keys())
        accounts = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(func.lower(AccountModel.email).in_(emails))
        ).all()

        linked_count = 0
        skipped_existing_snapshot = 0
        matched_count = len(accounts)
        matched_emails: list[str] = []

        for account in accounts:
            entry = record_by_email.get(str(account.email or "").strip().lower())
            if not entry:
                continue
            snapshot = _build_mailbox_snapshot(entry["outlook_account"])
            status = _patch_chatgpt_account_mailbox(
                account,
                snapshot,
                overwrite_existing_snapshot=overwrite_existing_snapshot,
                overwrite_mail_provider=overwrite_mail_provider,
            )
            session.add(account)
            matched_emails.append(account.email)
            if status == "linked":
                linked_count += 1
            else:
                skipped_existing_snapshot += 1

        if owns_session:
            session.commit()
        else:
            session.flush()

        missing_chatgpt_emails = sorted(
            email for email in record_by_email.keys() if email not in {str(item).strip().lower() for item in matched_emails}
        )

        return {
            "ok": True,
            "files": load_stats["files"],
            "parsed": {
                "total_lines": load_stats["total_lines"],
                "actionable_lines": load_stats["actionable_lines"],
                "unique_emails": len(record_by_email),
                "duplicate_emails": load_stats["duplicate_emails"],
            },
            "outlook_pool": {
                "cleanup": cleanup_stats,
                "created": created_count,
                "updated": updated_count,
                "total_upserted": created_count + updated_count,
            },
            "chatgpt_accounts": {
                "matched": matched_count,
                "linked": linked_count,
                "skipped_existing_snapshot": skipped_existing_snapshot,
                "missing_matches": len(missing_chatgpt_emails),
                "missing_match_samples": missing_chatgpt_emails[:20],
            },
        }
    finally:
        if owns_session:
            session.close()
