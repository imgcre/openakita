"""Local API for Marketplace deep-link installation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openakita.account.desktop import require_marketplace_access, trusted_marketplace_origin
from openakita.account.oidc import AccountOIDCManager
from openakita.integrations.marketplace import MarketplaceInstallManager
from openakita.integrations.marketplace.installer import MarketplaceInstallError

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


class PrepareBody(BaseModel):
    token: str = Field(min_length=64, max_length=64)
    endpoint: str = Field(min_length=1, max_length=500)


def _manager(request: Request) -> MarketplaceInstallManager:
    manager = getattr(request.app.state, "marketplace_install_manager", None)
    if manager is None:
        manager = MarketplaceInstallManager()
        request.app.state.marketplace_install_manager = manager
    return manager


def _error(exc: MarketplaceInstallError) -> HTTPException:
    status = (
        404
        if exc.code == "marketplace_install_not_found"
        else 409
        if exc.code == "marketplace_install_busy"
        else 400
    )
    return HTTPException(status_code=status, detail={"code": exc.code})


def _account(request: Request) -> AccountOIDCManager:
    require_marketplace_access(request)
    account = getattr(request.app.state, "account_oidc_manager", None)
    if account is None or request.app.state.account_capability.get("mode") != "openakita":
        raise MarketplaceInstallError("marketplace_account_required")
    return account


@router.post("/installs/prepare")
async def prepare_install(body: PrepareBody, request: Request):
    try:
        account = _account(request)
        endpoint = trusted_marketplace_origin(body.endpoint)
        return {"data": await _manager(request).prepare(body.token, endpoint, account=account)}
    except MarketplaceInstallError as exc:
        raise _error(exc) from exc


@router.get("/installs")
async def list_installs(request: Request):
    require_marketplace_access(request)
    return {"data": _manager(request).list_jobs()}


@router.get("/installs/{job_id}")
async def get_install(job_id: str, request: Request):
    require_marketplace_access(request)
    try:
        return {"data": await _manager(request).get(job_id)}
    except MarketplaceInstallError as exc:
        raise _error(exc) from exc


@router.post("/installs/{job_id}/confirm")
async def confirm_install(job_id: str, request: Request):
    try:
        return {"data": await _manager(request).confirm(job_id, request, account=_account(request))}
    except MarketplaceInstallError as exc:
        raise _error(exc) from exc


@router.post("/installs/{job_id}/cancel")
async def cancel_install(job_id: str, request: Request):
    require_marketplace_access(request)
    try:
        return {"data": await _manager(request).cancel(job_id)}
    except MarketplaceInstallError as exc:
        raise _error(exc) from exc
