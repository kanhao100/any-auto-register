"""ChatGPT-specific API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from core.db import AccountModel, get_session
from services.chatgpt_account_state import apply_chatgpt_status_policy

router = APIRouter(prefix="/chatgpt", tags=["chatgpt"])

COUNTRIES = ["ID", "SG", "US", "TR", "JP", "HK", "GB", "AU", "CA", "IN", "BR", "MX"]


class UploadRequest(BaseModel):
    account_ids: list[int]
    cpa_api_url: Optional[str] = None
    cpa_api_token: Optional[str] = None
    team_manager_url: Optional[str] = None
    team_manager_key: Optional[str] = None


class PaymentReq(BaseModel):
    plan: str = "plus"
    country: str = "ID"
    proxy: Optional[str] = None
    workspace_name: str = "MyTeam"
    seat_quantity: int = 5
    price_interval: str = "month"


class PromoEligibilityReq(BaseModel):
    country: str = "ID"
    proxy: Optional[str] = None


class CpaUploadReq(BaseModel):
    api_url: str
    api_key: str = ""


class Sub2ApiUploadReq(BaseModel):
    api_url: str
    api_key: str = ""


class MicrosoftMailboxBackfillReq(BaseModel):
    file_paths: list[str] = []
    overwrite_existing_snapshot: bool = False
    overwrite_mail_provider: bool = False


def _get_account(account_id: int, session: Session) -> AccountModel:
    account = session.get(AccountModel, account_id)
    if not account or account.platform != "chatgpt":
        raise HTTPException(404, "账号不存在")
    return account


def _to_codex_account(account: AccountModel):
    extra = account.get_extra()

    class _Acc:
        pass

    acc = _Acc()
    acc.email = account.email
    acc.access_token = extra.get("access_token") or account.token
    acc.refresh_token = extra.get("refresh_token", "")
    acc.id_token = extra.get("id_token", "")
    acc.session_token = extra.get("session_token", "")
    acc.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    acc.cookies = extra.get("cookies", "")
    acc.user_id = account.user_id
    return acc


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value
    return base


def _persist_local_probe(
    account: AccountModel,
    probe: dict[str, Any],
    session: Session,
    *,
    merge: bool = False,
) -> dict[str, Any]:
    extra = account.get_extra()
    if merge and isinstance(extra.get("chatgpt_local"), dict):
        stored_probe = _merge_dict(dict(extra.get("chatgpt_local") or {}), probe)
    else:
        stored_probe = probe
    extra["chatgpt_local"] = stored_probe
    account.set_extra(extra)
    apply_chatgpt_status_policy(account, local_probe=stored_probe)
    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    session.commit()
    return stored_probe


@router.post("/{account_id}/refresh-token")
def refresh_token(
    account_id: int,
    proxy: Optional[str] = None,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.token_refresh import TokenRefreshManager

    manager = TokenRefreshManager(proxy_url=proxy)
    result = manager.refresh_account(codex_acc)

    if result.success:
        extra = account.get_extra()
        extra["access_token"] = result.access_token
        if result.refresh_token:
            extra["refresh_token"] = result.refresh_token
        account.set_extra(extra)
        account.token = result.access_token
        account.updated_at = datetime.now(timezone.utc)
        session.add(account)
        session.commit()
        return {"ok": True, "access_token": result.access_token[:40] + "..."}
    raise HTTPException(400, result.error_message)


@router.post("/{account_id}/payment-link")
def generate_payment_link(
    account_id: int,
    req: PaymentReq,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.payment import generate_plus_link, generate_team_link

    if req.plan == "plus":
        url = generate_plus_link(codex_acc, proxy=req.proxy, country=req.country)
    else:
        url = generate_team_link(
            codex_acc,
            workspace_name=req.workspace_name,
            price_interval=req.price_interval,
            seat_quantity=req.seat_quantity,
            proxy=req.proxy,
            country=req.country,
        )
    return {"url": url, "plan": req.plan, "country": req.country}


@router.get("/{account_id}/subscription")
def check_subscription(
    account_id: int,
    proxy: Optional[str] = None,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    stored_probe = _persist_local_probe(account, probe, session)
    return {
        "email": account.email,
        "subscription": stored_probe.get("subscription", {}).get("plan", "unknown"),
        "probe": stored_probe,
    }


@router.post("/{account_id}/promo-eligibility")
def probe_promo_eligibility(
    account_id: int,
    req: PromoEligibilityReq,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.payment import probe_plus_promo_eligibility

    promo = probe_plus_promo_eligibility(codex_acc, proxy=req.proxy, country=req.country)
    stored_probe = _persist_local_probe(account, {"promo": promo}, session, merge=True)
    return {
        "ok": promo.get("state") not in {"probe_failed", "unauthorized", "missing_access_token"},
        "email": account.email,
        "promo": promo,
        "probe": stored_probe,
    }


@router.post("/{account_id}/probe-local")
def probe_local_status(
    account_id: int,
    proxy: Optional[str] = None,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    stored_probe = _persist_local_probe(account, probe, session)
    return {"ok": True, "email": account.email, "probe": stored_probe}


@router.post("/{account_id}/upload-cpa")
def upload_cpa(
    account_id: int,
    req: CpaUploadReq,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

    token_data = generate_token_json(codex_acc)
    ok, message = upload_to_cpa(token_data, api_url=req.api_url, api_key=req.api_key)
    return {"ok": ok, "message": message}


@router.post("/{account_id}/upload-sub2api")
def upload_sub2api(
    account_id: int,
    req: Sub2ApiUploadReq,
    session: Session = Depends(get_session),
):
    account = _get_account(account_id, session)
    codex_acc = _to_codex_account(account)

    from platforms.chatgpt.sub2api_upload import upload_to_sub2api

    ok, message = upload_to_sub2api(
        codex_acc,
        api_url=req.api_url,
        api_key=req.api_key,
    )
    return {"ok": ok, "message": message}


@router.post("/backfill-microsoft-mailboxes")
def backfill_microsoft_mailboxes(
    req: MicrosoftMailboxBackfillReq,
    session: Session = Depends(get_session),
):
    from services.chatgpt_mailbox_backfill import backfill_chatgpt_microsoft_mailboxes

    result = backfill_chatgpt_microsoft_mailboxes(
        file_paths=req.file_paths or None,
        overwrite_existing_snapshot=req.overwrite_existing_snapshot,
        overwrite_mail_provider=req.overwrite_mail_provider,
        session=session,
    )
    session.commit()
    return result
