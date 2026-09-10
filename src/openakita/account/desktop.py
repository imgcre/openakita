"""Boundary between the local desktop credential vault and web/remote clients."""

import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from openakita.account.native_credential import load_native_account_token
from openakita.api.auth import is_trusted_local


def require_desktop_account(request: Request) -> None:
    supplied = request.headers.get("X-OpenAkita-Desktop-Token", "")
    if (
        not supplied
        or not is_trusted_local(request)
        or any(name in request.headers for name in ("forwarded", "x-forwarded-for"))
    ):
        raise HTTPException(status_code=403, detail="desktop_account_access_required")
    # Keep compatibility with already-running desktop-spawned backends. The
    # persistent native key also permits same-user standalone/reused backends.
    inherited = os.environ.get("OPENAKITA_DESKTOP_SESSION_TOKEN", "")
    if inherited and secrets.compare_digest(inherited, supplied):
        return
    native = load_native_account_token()
    if not native or not secrets.compare_digest(native, supplied):
        raise HTTPException(status_code=403, detail="desktop_account_access_required")


def trusted_marketplace_origin(value: str) -> str:
    configured = os.environ.get("OPENAKITA_MARKETPLACE_URL", "https://marketplace.openakita.cn")
    allowed = {"https://marketplace.openakita.cn", configured.rstrip("/")}
    candidate = value.rstrip("/")
    parsed = urlsplit(candidate)
    # Developer targets must be explicitly configured; deep links cannot choose
    # the destination to which the desktop sends identity proofs.
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        candidate not in allowed
        or not parsed.netloc
        or (parsed.scheme != "https" and not local)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise HTTPException(status_code=400, detail="marketplace_origin_invalid")
    return candidate


def require_marketplace_access(request: Request) -> None:
    """Allow the native desktop or an explicitly authenticated instance client.

    Remote App installs use the instance's own account and access token. Browser
    cookies, query-string tokens and the local-IP exemption cannot grant access
    to installation proofs. Browser identity handoff remains desktop-only.
    """
    supplied = request.headers.get("Authorization", "")
    config = getattr(request.app.state, "web_access_config", None)
    if config is not None and supplied.startswith("Bearer "):
        if config.validate_access_token(supplied[7:]):
            return
    require_desktop_account(request)
