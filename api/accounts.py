from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, get_session
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging, zipfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


SUPPORTED_EXPORT_FORMATS = {"csv", "json", "txt", "cpa", "sub2api"}


def _parse_account_extra(account: AccountModel) -> dict:
    try:
        data = json.loads(account.extra_json or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _format_datetime(value: Optional[datetime]) -> str:
    if not value:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _normalize_export_format(value: str) -> str:
    export_format = str(value or "csv").strip().lower() or "csv"
    if export_format not in SUPPORTED_EXPORT_FORMATS:
        raise HTTPException(400, f"不支持的导出格式: {export_format}")
    return export_format


def _parse_export_account_ids(raw: Optional[str]) -> list[int]:
    text = str(raw or "").strip()
    if not text:
        return []

    values: list[int] = []
    seen: set[int] = set()
    for part in text.split(","):
        piece = str(part or "").strip()
        if not piece:
            continue
        try:
            account_id = int(piece)
        except ValueError as exc:
            raise HTTPException(400, f"无效的账号 ID: {piece}") from exc
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        values.append(account_id)

    if text and not values:
        raise HTTPException(400, "导出账号 ID 列表不能为空")
    return values


def _build_account_export_row(account: AccountModel) -> dict[str, str]:
    extra = _parse_account_extra(account)
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = chatgpt_local.get("subscription") if isinstance(chatgpt_local.get("subscription"), dict) else {}
    auth = chatgpt_local.get("auth") if isinstance(chatgpt_local.get("auth"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}
    promo = chatgpt_local.get("promo") if isinstance(chatgpt_local.get("promo"), dict) else {}

    return {
        "platform": str(account.platform or ""),
        "email": str(account.email or ""),
        "password": str(account.password or ""),
        "user_id": str(account.user_id or ""),
        "region": str(account.region or ""),
        "status": str(account.status or ""),
        "cashier_url": str(account.cashier_url or ""),
        "token": str(account.token or ""),
        "refresh_token": str(extra.get("refresh_token") or extra.get("refreshToken") or ""),
        "mail_provider": str(extra.get("mail_provider") or ""),
        "mailbox_email": str((extra.get("mailbox_account") or {}).get("email") or "") if isinstance(extra.get("mailbox_account"), dict) else "",
        "local_auth_state": str(auth.get("state") or ""),
        "local_plan": str(subscription.get("plan") or ""),
        "local_workspace_plan_type": str(subscription.get("workspace_plan_type") or ""),
        "local_codex_state": str(codex.get("state") or ""),
        "local_promo_state": str(promo.get("state") or ""),
        "created_at": _format_datetime(account.created_at),
        "updated_at": _format_datetime(account.updated_at),
        "extra_json": str(account.extra_json or "{}"),
    }


def _build_export_filename(platform: Optional[str], export_format: str) -> str:
    platform_name = str(platform or "accounts").strip() or "accounts"
    if export_format == "cpa":
        return f"{platform_name}_cpa_auth_files.zip"
    if export_format == "sub2api":
        return f"{platform_name}_sub2api_accounts.json"
    return f"{platform_name}_accounts.{export_format}"


def _build_chatgpt_oauth_export_account(account: AccountModel):
    extra = _parse_account_extra(account)

    class _ExportAccount:
        pass

    export_account = _ExportAccount()
    export_account.id = account.id
    export_account.email = str(account.email or "")
    export_account.user_id = str(account.user_id or "")
    export_account.token = str(account.token or "")
    export_account.access_token = str(
        extra.get("access_token") or extra.get("accessToken") or account.token or ""
    )
    export_account.refresh_token = str(
        extra.get("refresh_token") or extra.get("refreshToken") or ""
    )
    export_account.id_token = str(extra.get("id_token") or extra.get("idToken") or "")
    export_account.session_token = str(
        extra.get("session_token") or extra.get("sessionToken") or ""
    )
    export_account.client_id = str(
        extra.get("client_id")
        or extra.get("clientId")
        or "app_EMoamEEZ73f0CkXaXp7hrann"
    )
    export_account.extra = extra
    return export_account


def _safe_export_stem(value: str, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    sanitized = "".join(
        char if char.isalnum() or char in {"@", ".", "-", "_"} else "_"
        for char in text
    ).strip("._")
    return sanitized or fallback


def _build_csv_export(accounts: list[AccountModel]) -> str:
    rows = [_build_account_export_row(account) for account in accounts]
    output = io.StringIO()
    writer = csv.writer(output)
    header = [
        "platform",
        "email",
        "password",
        "user_id",
        "region",
        "status",
        "cashier_url",
        "token",
        "refresh_token",
        "mail_provider",
        "mailbox_email",
        "local_auth_state",
        "local_plan",
        "local_workspace_plan_type",
        "local_codex_state",
        "local_promo_state",
        "created_at",
        "updated_at",
        "extra_json",
    ]
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(key, "") for key in header])
    output.seek(0)
    return output.getvalue()


def _build_json_export(accounts: list[AccountModel]) -> str:
    rows = [_build_account_export_row(account) for account in accounts]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def _build_txt_export(accounts: list[AccountModel]) -> str:
    lines: list[str] = []
    for index, account in enumerate(accounts, start=1):
        row = _build_account_export_row(account)
        lines.extend(
            [
                f"[{index}]",
                f"platform: {row['platform']}",
                f"email: {row['email']}",
                f"password: {row['password']}",
                f"status: {row['status']}",
                f"region: {row['region']}",
                f"token: {row['token']}",
                f"refresh_token: {row['refresh_token']}",
                f"local_auth_state: {row['local_auth_state']}",
                f"local_plan: {row['local_plan']}",
                f"local_workspace_plan_type: {row['local_workspace_plan_type']}",
                f"local_codex_state: {row['local_codex_state']}",
                f"local_promo_state: {row['local_promo_state']}",
                f"cashier_url: {row['cashier_url']}",
                f"created_at: {row['created_at']}",
                f"updated_at: {row['updated_at']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _build_cpa_export(accounts: list[AccountModel]) -> bytes:
    from platforms.chatgpt.cpa_upload import generate_token_json

    buffer = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for account in accounts:
            export_account = _build_chatgpt_oauth_export_account(account)
            token_data = generate_token_json(export_account)
            stem = _safe_export_stem(
                str(token_data.get("email") or account.email or ""),
                fallback=f"account_{account.id or 'unknown'}",
            )
            filename = f"{stem}.json"
            if filename in used_names:
                filename = f"{stem}_{account.id or len(used_names) + 1}.json"
            used_names.add(filename)
            archive.writestr(
                filename,
                json.dumps(token_data, ensure_ascii=False, indent=2),
            )
    return buffer.getvalue()


def _build_sub2api_export(accounts: list[AccountModel]) -> str:
    from core.config_store import config_store
    from platforms.chatgpt.sub2api_upload import (
        _build_sub2api_account_payload,
        _parse_group_ids,
    )

    resolved_group_ids = _parse_group_ids(config_store.get("sub2api_group_ids", ""))
    rows = [
        _build_sub2api_account_payload(
            _build_chatgpt_oauth_export_account(account),
            group_ids=resolved_group_ids,
        )
        for account in accounts
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def _resolve_export_accounts(
    *,
    session: Session,
    platform: Optional[str],
    status: Optional[str],
    email: Optional[str],
    created_at_start: Optional[datetime],
    created_at_end: Optional[datetime],
    account_ids: list[int],
) -> list[AccountModel]:
    if account_ids:
        query = select(AccountModel).where(AccountModel.id.in_(account_ids))
        if platform:
            query = query.where(AccountModel.platform == platform)
        rows = session.exec(query).all()
        row_map = {int(row.id or 0): row for row in rows}
        return [row_map[account_id] for account_id in account_ids if account_id in row_map]

    query = select(AccountModel)
    if platform:
        query = query.where(AccountModel.platform == platform)
    if status:
        query = query.where(AccountModel.status == status)
    if email:
        query = query.where(AccountModel.email.contains(email))
    if created_at_start:
        query = query.where(AccountModel.created_at >= created_at_start)
    if created_at_end:
        query = query.where(AccountModel.created_at <= created_at_end)
    return session.exec(query).all()


def _build_export_response(
    *,
    accounts: list[AccountModel],
    platform: Optional[str],
    export_format: str,
) -> StreamingResponse:
    if export_format == "cpa":
        content = _build_cpa_export(accounts)
        media_type = "application/zip"
        encoded_content = content
    elif export_format == "sub2api":
        content = _build_sub2api_export(accounts)
        media_type = "application/json"
        encoded_content = content.encode("utf-8-sig")
    elif export_format == "json":
        content = _build_json_export(accounts)
        media_type = "application/json"
        encoded_content = content.encode("utf-8-sig")
    elif export_format == "txt":
        content = _build_txt_export(accounts)
        media_type = "text/plain"
        encoded_content = content.encode("utf-8-sig")
    else:
        content = _build_csv_export(accounts)
        media_type = "text/csv"
        encoded_content = content.encode("utf-8-sig")

    filename = _build_export_filename(platform, export_format)
    return StreamingResponse(
        iter([encoded_content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))
    if created_at_start:
        q = q.where(AccountModel.created_at >= created_at_start)
    if created_at_end:
        q = q.where(AccountModel.created_at <= created_at_end)
    total = len(session.exec(q).all())
    items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "items": items}


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    ids: Optional[str] = None,
    format: str = Query(default="csv"),
    session: Session = Depends(get_session),
):
    export_format = _normalize_export_format(format)
    account_ids = _parse_export_account_ids(ids)
    accounts = _resolve_export_accounts(
        session=session,
        platform=platform,
        status=status,
        email=email,
        created_at_start=created_at_start,
        created_at_end=created_at_end,
        account_ids=account_ids,
    )
    if export_format in {"cpa", "sub2api"}:
        non_chatgpt = [account.email for account in accounts if str(account.platform or "") != "chatgpt"]
        if non_chatgpt:
            raise HTTPException(400, "CPA / Sub2API 导出仅支持 ChatGPT 账号")
    return _build_export_response(
        accounts=accounts,
        platform=platform,
        export_format=export_format,
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)
        
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return acc


@router.get("/{account_id}/mailbox")
def get_account_mailbox(
    account_id: int,
    folder: str = Query(default="inbox"),
    limit: int = Query(default=25, ge=1, le=50),
    session: Session = Depends(get_session),
):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")

    from services.account_mailbox_viewer import load_account_mailbox_messages

    try:
        return load_account_mailbox_messages(
            acc,
            folder=folder,
            limit=limit,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
