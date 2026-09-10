"""PKCE and RFC 8628 device authorization with OpenAkita Account."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from openakita.account.config import (
    DEFAULT_ACCOUNT_BASE_URL,
    DEFAULT_ACCOUNT_CLIENT_ID,
    disabled_credential_usernames,
)
from openakita.account.status_store import AccountStatusStore

logger = logging.getLogger(__name__)

CLIENT_ID = DEFAULT_ACCOUNT_CLIENT_ID
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/auth/callback"
NATIVE_CALLBACK_URIS = frozenset(
    {
        "https://account.openakita.cn/oauth/mobile/callback",
        "com.openakita.mobile:/oauth/callback",
    }
)
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
LOGIN_TTL = 180


class AccountOIDCError(Exception):
    pass


class TokenStore(Protocol):
    async def load_refresh_token(self) -> str | None: ...

    async def save_refresh_token(self, token: str) -> None: ...

    async def clear(self) -> None: ...


class KeyringTokenStore:
    service = "OpenAkita Account"
    username = "openakita-desktop-refresh-token"

    def __init__(self, *, username: str | None = None) -> None:
        self.username = username or type(self).username

    async def load_refresh_token(self) -> str | None:
        def _load() -> str | None:
            try:
                import keyring
            except ImportError as exc:
                raise AccountOIDCError("OS keyring support is not installed") from exc
            return keyring.get_password(self.service, self.username)

        return await asyncio.to_thread(_load)

    async def save_refresh_token(self, token: str) -> None:
        def _save() -> None:
            try:
                import keyring
            except ImportError as exc:
                raise AccountOIDCError("OS keyring support is not installed") from exc
            keyring.set_password(self.service, self.username, token)

        await asyncio.to_thread(_save)

    async def clear(self) -> None:
        def _clear() -> None:
            try:
                import keyring

                keyring.delete_password(self.service, self.username)
            except Exception:
                return

        await asyncio.to_thread(_clear)


async def clear_disabled_account_credentials() -> None:
    """Remove every locally known account credential slot without reading it."""

    await asyncio.gather(
        *(
            KeyringTokenStore(username=username).clear()
            for username in disabled_credential_usernames()
        )
    )


@dataclass
class LoginAttempt:
    attempt_id: str
    state: str
    verifier: str
    authorization_url: str
    redirect_uri: str = CALLBACK_URI
    flow: str = "loopback"
    user_code: str = ""
    verification_uri: str = ""
    device_code: str = field(default="", repr=False)
    expires_in: int = LOGIN_TTL
    poll_interval: float = 5
    next_poll_at: float = 0
    status: str = "pending"
    error: str | None = None
    created_at: float = field(default_factory=time.time)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _preferred_callback_language(accept_language: str) -> str:
    """Choose a supported callback language from an HTTP Accept-Language value."""
    preferences: list[tuple[float, int, str]] = []
    for index, item in enumerate(accept_language.split(",")):
        parts = [part.strip() for part in item.split(";")]
        tag = parts[0].lower()
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0:
            preferences.append((quality, -index, tag))
    for _quality, _index, tag in sorted(preferences, reverse=True):
        if tag == "zh" or tag.startswith("zh-"):
            return "zh"
        if tag == "en" or tag.startswith("en-"):
            return "en"
    return "en"


def _callback_page_html(*, success: bool, language: str) -> bytes:
    copy = {
        "zh": {
            "page_title": "登录成功" if success else "登录失败",
            "eyebrow": "OpenAkita",
            "title": "登录成功" if success else "登录失败",
            "message": (
                "你已成功登录 OpenAkita。"
                if success
                else "登录过程中遇到问题，请返回 OpenAkita 重新登录。"
            ),
            "hint": "现在可以关闭此页面。",
        },
        "en": {
            "page_title": "Signed in successfully" if success else "Sign-in failed",
            "eyebrow": "OpenAkita",
            "title": "Signed in successfully" if success else "Sign-in failed",
            "message": (
                "You’re now signed in to OpenAkita."
                if success
                else "Something went wrong. Return to OpenAkita and try signing in again."
            ),
            "hint": "You can close this tab now.",
        },
    }
    lang = "zh" if language == "zh" else "en"
    text = copy[lang]
    document_language = "zh-CN" if lang == "zh" else "en"
    state_class = "success" if success else "error"
    state_icon = "&#10003;" if success else "!"
    status_role = "status" if success else "alert"
    html = f"""<!doctype html>
<html lang="{document_language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{text["page_title"]} · OpenAkita</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 24px;
      color: #182033; background:
        radial-gradient(circle at 50% 0%, rgba(59, 130, 246, .13), transparent 38%),
        linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%);
    }}
    .card {{
      width: min(100%, 440px); padding: 42px 38px 30px; text-align: center;
      border: 1px solid rgba(148, 163, 184, .28); border-radius: 22px;
      background: rgba(255, 255, 255, .92); box-shadow: 0 24px 70px rgba(15, 23, 42, .12);
    }}
    .mark {{
      width: 64px; height: 64px; margin: 0 auto 24px; display: grid; place-items: center;
      border-radius: 20px; font-size: 32px; font-weight: 700;
    }}
    .success .mark {{ color: #047857; background: #d1fae5; box-shadow: 0 0 0 8px rgba(16, 185, 129, .08); }}
    .error .mark {{ color: #b91c1c; background: #fee2e2; box-shadow: 0 0 0 8px rgba(239, 68, 68, .07); }}
    .eyebrow {{ margin: 0 0 8px; color: #2563eb; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{ margin: 0; font-size: 28px; line-height: 1.25; letter-spacing: -.02em; }}
    .message {{ margin: 14px auto 0; max-width: 340px; color: #64748b; font-size: 15px; line-height: 1.65; }}
    .hint {{ margin: 26px 0 0; padding: 12px 14px; border-radius: 12px; color: #475569; background: #f1f5f9; font-size: 13px; }}
    @media (prefers-color-scheme: dark) {{
      body {{ color: #e5e7eb; background: radial-gradient(circle at 50% 0%, rgba(59, 130, 246, .18), transparent 38%), #0f172a; }}
      .card {{ border-color: rgba(148, 163, 184, .2); background: rgba(17, 24, 39, .94); box-shadow: 0 24px 70px rgba(0, 0, 0, .35); }}
      .message {{ color: #94a3b8; }}
      .hint {{ color: #cbd5e1; background: rgba(51, 65, 85, .65); }}
      .success .mark {{ color: #6ee7b7; background: rgba(6, 78, 59, .72); }}
      .error .mark {{ color: #fca5a5; background: rgba(127, 29, 29, .62); }}
    }}
  </style>
</head>
<body>
  <main class="card {state_class}" role="{status_role}" aria-labelledby="callback-title">
    <div class="mark" aria-hidden="true">{state_icon}</div>
    <p class="eyebrow">{text["eyebrow"]}</p>
    <h1 id="callback-title">{text["title"]}</h1>
    <p class="message">{text["message"]}</p>
    <p class="hint">{text["hint"]}</p>
  </main>
</body>
</html>"""
    return html.encode("utf-8")


class AccountOIDCManager:
    def __init__(
        self,
        *,
        store: AccountStatusStore,
        token_store: TokenStore | None = None,
        account_base_url: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self._store = store
        self._tokens = token_store or KeyringTokenStore()
        self._base_url = (
            account_base_url
            or os.environ.get("OPENAKITA_ACCOUNT_BASE_URL", DEFAULT_ACCOUNT_BASE_URL)
        ).rstrip("/")
        self._client_id = (
            client_id or os.environ.get("OPENAKITA_ACCOUNT_CLIENT_ID", DEFAULT_ACCOUNT_CLIENT_ID)
        ).strip()
        if not self._client_id:
            raise ValueError("account client ID must not be empty")
        self._attempts: dict[str, LoginAttempt] = {}
        self._server: asyncio.Server | None = None
        self._listener_attempt: LoginAttempt | None = None
        self._login_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._session_id: str | None = None
        self._account_user_id: str | None = None

    async def start(
        self, *, flow: str = "loopback", redirect_uri: str | None = None
    ) -> LoginAttempt:
        if flow not in {"loopback", "device", "native"}:
            raise AccountOIDCError("unsupported login flow")
        if flow == "native" and redirect_uri not in NATIVE_CALLBACK_URIS:
            raise AccountOIDCError("invalid native redirect URI")
        self._attempts = {
            key: value
            for key, value in self._attempts.items()
            if time.time() - value.created_at < value.expires_in + 60
        }
        if len(self._attempts) >= 32:
            raise AccountOIDCError("too many login attempts")
        if flow == "device":
            return await self._start_device()
        if flow == "loopback" and self._listener_attempt is not None:
            await self.cancel(self._listener_attempt.attempt_id)
        redirect_uri = redirect_uri if flow == "native" else CALLBACK_URI
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        attempt_id = secrets.token_urlsafe(18)
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid profile email offline_access entitlements organizations",
                "state": state,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        attempt = LoginAttempt(
            attempt_id=attempt_id,
            state=state,
            verifier=verifier,
            authorization_url=f"{self._base_url}/oauth/authorize?{query}",
            redirect_uri=redirect_uri,
            flow=flow,
            expires_in=600 if flow == "native" else LOGIN_TTL,
        )
        self._attempts[attempt_id] = attempt
        if flow == "native":
            asyncio.get_running_loop().call_later(attempt.expires_in, self._expire_attempt, attempt)
            return attempt
        try:
            self._server = await asyncio.start_server(
                lambda reader, writer: self._callback(reader, writer, attempt),
                CALLBACK_HOST,
                CALLBACK_PORT,
            )
        except OSError:
            self._attempts.pop(attempt_id, None)
            raise
        self._listener_attempt = attempt
        asyncio.get_running_loop().call_later(LOGIN_TTL, self._expire_attempt, attempt)
        return attempt

    async def _start_device(self) -> LoginAttempt:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base_url}/oauth/device_authorization",
                    data={
                        "client_id": self._client_id,
                        "scope": "openid profile email offline_access entitlements organizations",
                    },
                )
            if response.status_code in {404, 405, 501}:
                raise AccountOIDCError("account_device_not_supported")
            if response.status_code != 200:
                raise AccountOIDCError("account_device_start_failed")
            payload = response.json()
            device_code = payload["device_code"]
            user_code = payload["user_code"]
            verification_uri = payload["verification_uri"]
            authorization_url = payload.get("verification_uri_complete") or verification_uri
            expires_in = payload["expires_in"]
            interval = payload.get("interval", 5)
            if (
                not all(
                    isinstance(value, str) and 0 < len(value) <= 8192
                    for value in (device_code, user_code, verification_uri, authorization_url)
                )
                or type(expires_in) is not int
                or not 0 < expires_in <= 3600
                or type(interval) is not int
                or not 0 < interval <= expires_in
            ):
                raise ValueError("invalid device authorization response")
            for uri in (verification_uri, authorization_url):
                parsed = urlsplit(uri)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError("invalid verification URI")
        except AccountOIDCError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AccountOIDCError("account_device_start_failed") from exc
        attempt = LoginAttempt(
            attempt_id=secrets.token_urlsafe(18),
            state="",
            verifier="",
            authorization_url=authorization_url,
            flow="device",
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in=expires_in,
            poll_interval=interval,
            next_poll_at=time.monotonic() + interval,
        )
        self._attempts[attempt.attempt_id] = attempt
        asyncio.get_running_loop().call_later(expires_in, self._expire_attempt, attempt)
        return attempt

    async def attempt_status(self, attempt_id: str) -> dict:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise AccountOIDCError("unknown login attempt")
        if attempt.flow == "device" and attempt.status == "pending":
            await self._poll_device(attempt)
        return {"status": attempt.status, "error": attempt.error}

    async def _poll_device(self, attempt: LoginAttempt) -> None:
        async with self._login_lock:
            if attempt.status != "pending":
                return
            if time.time() - attempt.created_at >= attempt.expires_in:
                self._expire_attempt(attempt)
                return
            if time.monotonic() < attempt.next_poll_at:
                return
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.post(
                        f"{self._base_url}/oauth/token",
                        data={
                            "grant_type": DEVICE_GRANT_TYPE,
                            "client_id": self._client_id,
                            "device_code": attempt.device_code,
                        },
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    attempt.poll_interval *= 2
                    return
                if attempt.status != "pending":
                    return
                payload = response.json()
                if response.status_code != 200:
                    error = payload.get("error")
                    if error == "authorization_pending":
                        return
                    if error == "slow_down":
                        attempt.poll_interval += 5
                        return
                    attempt.status = "expired" if error == "expired_token" else "failed"
                    attempt.error = (
                        "account_authorization_denied"
                        if error == "access_denied"
                        else "account_login_expired"
                        if error == "expired_token"
                        else "account_token_exchange_failed"
                    )
                    return
                # Once tokens have been issued, do not poll the one-time grant
                # again even if userinfo or local credential persistence fails.
                attempt.status = "exchanging"
                await self._accept_tokens(payload)
                attempt.status = "complete"
            except asyncio.CancelledError:
                # A disconnected request may have consumed the one-time grant.
                # Never leave an attempt stuck in exchanging or retry it blindly.
                attempt.status = "failed"
                attempt.error = "account_token_exchange_failed"
                raise
            except httpx.HTTPError:
                if attempt.status == "exchanging":
                    attempt.status = "failed"
                    attempt.error = "account_token_exchange_failed"
                else:
                    attempt.poll_interval *= 2
            except Exception as exc:
                logger.warning("Device login failed: %s", type(exc).__name__)
                attempt.status = "failed"
                attempt.error = "account_token_exchange_failed"
            finally:
                attempt.next_poll_at = time.monotonic() + attempt.poll_interval
                if attempt.status != "pending":
                    attempt.device_code = ""

    async def complete_callback(
        self,
        attempt: LoginAttempt,
        *,
        state: str,
        code: str = "",
        error: str = "",
    ) -> bool:
        async with self._login_lock:
            if not secrets.compare_digest(attempt.state.encode(), state.encode()):
                raise AccountOIDCError("invalid OAuth state")
            if attempt.flow == "native" and attempt.status == "complete":
                return True
            if attempt.status != "pending":
                raise AccountOIDCError("login attempt is no longer pending")
            if time.time() - attempt.created_at >= attempt.expires_in:
                attempt.status = "expired"
                raise AccountOIDCError("account_login_expired")
            if error:
                attempt.status = "failed"
                attempt.error = "account_authorization_denied"
                return False
            if not code:
                raise AccountOIDCError("missing OAuth code")
            attempt.status = "exchanging"
            try:
                await self._complete(
                    code=code, verifier=attempt.verifier, redirect_uri=attempt.redirect_uri
                )
            except asyncio.CancelledError:
                attempt.status = "failed"
                attempt.error = "account_token_exchange_failed"
                raise
            except Exception as exc:
                # HTTP exceptions can include token request URLs. Do not expose
                # provider payloads, authorization codes or tokens in logs/UI.
                logger.warning(
                    "OpenAkita Account login failed: %s",
                    str(exc) if isinstance(exc, AccountOIDCError) else type(exc).__name__,
                )
                attempt.status = "failed"
                attempt.error = "account_token_exchange_failed"
                return False
            finally:
                attempt.verifier = ""
            attempt.status = "complete"
            return True

    async def complete_native_callback(
        self, attempt_id: str, *, state: str, code: str = "", error: str = ""
    ) -> dict:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.flow != "native":
            raise AccountOIDCError("unknown native login attempt")
        # A retry after a lost response must not redeem the one-time code again.
        if not secrets.compare_digest(attempt.state.encode(), state.encode()):
            raise AccountOIDCError("invalid OAuth state")
        if code and error:
            raise AccountOIDCError("ambiguous OAuth response")
        if attempt.status == "complete":
            return {"status": "complete", "error": None}
        if attempt.status in {"failed", "expired", "cancelled"}:
            return {"status": attempt.status, "error": attempt.error or "account_login_expired"}
        try:
            await self.complete_callback(attempt, state=state, code=code, error=error)
        except AccountOIDCError:
            if attempt.status not in {"expired", "cancelled"}:
                raise
            return {"status": attempt.status, "error": "account_login_expired"}
        return {"status": attempt.status, "error": attempt.error}

    async def _close_listener(self, attempt: LoginAttempt) -> None:
        if self._listener_attempt is attempt and self._server is not None:
            server = self._server
            self._server = None
            self._listener_attempt = None
            server.close()
            await server.wait_closed()

    async def cancel(self, attempt_id: str) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise AccountOIDCError("unknown login attempt")
        async with self._login_lock:
            if attempt.status == "pending":
                attempt.status = "cancelled"
                attempt.device_code = ""
            await self._close_listener(attempt)

    async def snapshot(self) -> dict:
        # The identity snapshot is intentionally retained for offline cache and
        # audit purposes. Its presence alone must not make the UI appear signed
        # in after the OS-vault refresh token has been cleared on logout.
        if not await self._tokens.load_refresh_token():
            return {"status": "signed_out"}
        return (await self._store.snapshot()) or {"status": "signed_out"}

    async def marketplace_install_proof(self, token: str, device_id: str) -> str:
        """Keep account credentials on the instance; export only a scoped proof."""
        if self._client_id != "openakita-desktop":
            raise AccountOIDCError("marketplace_account_unsupported")
        async with self._login_lock:
            refresh = await self._tokens.load_refresh_token()
            if not refresh:
                raise AccountOIDCError("marketplace_account_required")
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                    response = await client.post(
                        f"{self._base_url}/oauth/desktop-install-proof",
                        json={
                            "client_id": self._client_id,
                            "refresh_token": refresh,
                            "target_client_id": "marketplace",
                            "installation_token": token,
                            "device_id": device_id,
                        },
                    )
            except httpx.HTTPError as exc:
                raise AccountOIDCError("marketplace_identity_unavailable") from exc
            if response.status_code in (400, 401, 403):
                raise AccountOIDCError("marketplace_account_required")
            if response.status_code != 200:
                raise AccountOIDCError("marketplace_identity_unavailable")
            proof = response.json().get("proof")
            if not isinstance(proof, str) or not proof:
                raise AccountOIDCError("marketplace_identity_unavailable")
            return proof

    async def refresh_entitlements(self) -> dict:
        access_token = await self._valid_access_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/me/entitlements",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code != 200:
            raise AccountOIDCError(f"entitlement refresh failed with HTTP {response.status_code}")
        payload = response.json()
        if not self._account_user_id:
            snapshot = await self._store.snapshot()
            self._account_user_id = snapshot.get("account_user_id") if snapshot else None
        if not self._account_user_id:
            raise AccountOIDCError("account identity is unavailable")
        await self._store.save_entitlements(
            account_user_id=self._account_user_id,
            entitlements_json=json.dumps(payload, separators=(",", ":")),
        )
        return payload

    async def logout(self) -> str:
        for attempt_id in list(self._attempts):
            await self.cancel(attempt_id)
        if self._session_id:
            await self._store.revoke_session(self._session_id)
        await self._tokens.clear()
        self._access_token = None
        self._session_id = None
        self._account_user_id = None
        query = urlencode({"client_id": self._client_id})
        return f"{self._base_url}/oauth/end-session?{query}"

    async def _callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        attempt: LoginAttempt,
    ) -> None:
        language = "en"
        try:
            request_line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode(
                "ascii", errors="replace"
            )
            parts = request_line.strip().split(" ")
            if len(parts) != 3 or parts[0] != "GET":
                raise AccountOIDCError("invalid loopback callback")
            accept_language = ""
            for _ in range(64):
                raw_header = await asyncio.wait_for(reader.readline(), timeout=5)
                if raw_header in {b"", b"\r\n", b"\n"}:
                    break
                if len(raw_header) > 8_192:
                    raise AccountOIDCError("loopback callback header is too large")
                header = raw_header.decode("latin-1", errors="replace")
                name, separator, value = header.partition(":")
                if separator and name.strip().lower() == "accept-language":
                    accept_language = value.strip()
            language = _preferred_callback_language(accept_language)
            target = urlsplit(parts[1])
            if target.path != "/auth/callback":
                raise AccountOIDCError("invalid loopback callback path")
            query = parse_qs(target.query, max_num_fields=20)
            if any(len(query.get(key, [])) > 1 for key in ("state", "code", "error")):
                raise AccountOIDCError("duplicate OAuth parameters")
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            success = await self.complete_callback(
                attempt, state=state, code=code, error=query.get("error", [""])[0]
            )
            body = _callback_page_html(success=success, language=language)
            status = b"200 OK" if success else b"400 Bad Request"
        except Exception:
            body = _callback_page_html(success=False, language=language)
            status = b"400 Bad Request"
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Language: {'zh-CN' if language == 'zh' else 'en'}\r\n".encode()
            + b"Cache-Control: no-store\r\n"
            + b"X-Content-Type-Options: nosniff\r\n"
            + b"Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        if attempt.status != "pending":
            await self._close_listener(attempt)

    async def _complete(
        self,
        *,
        code: str,
        verifier: str,
        redirect_uri: str = CALLBACK_URI,
    ) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                f"{self._base_url}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self._client_id,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            if token_response.status_code != 200:
                raise AccountOIDCError(
                    f"token exchange failed with HTTP {token_response.status_code}"
                )
            tokens = token_response.json()
        await self._accept_tokens(tokens)

    async def _accept_tokens(self, tokens: dict) -> None:
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not isinstance(refresh_token, str)
            or not access_token
            or not refresh_token
        ):
            raise AccountOIDCError("token response is incomplete")
        async with httpx.AsyncClient(timeout=10.0) as client:
            userinfo_response = await client.get(
                f"{self._base_url}/oauth/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_response.status_code != 200:
                raise AccountOIDCError("userinfo request failed")
            profile = userinfo_response.json()
        account_user_id = str(profile.get("sub", ""))
        if not account_user_id:
            raise AccountOIDCError("userinfo is missing sub")
        session_id = secrets.token_urlsafe(18)
        await self._tokens.save_refresh_token(refresh_token)
        try:
            await self._store.save_authenticated(
                account_user_id=account_user_id,
                profile_json=json.dumps(profile, separators=(",", ":")),
                session_id=session_id,
            )
        except Exception:
            await self._tokens.clear()
            raise
        self._access_token = access_token
        self._session_id = session_id
        self._account_user_id = account_user_id
        await self.refresh_entitlements()

    async def _valid_access_token(self) -> str:
        if self._session_id and not await self._store.session_is_active(self._session_id):
            self._access_token = None
            raise AccountOIDCError("account is suspended or session was revoked")
        if self._access_token:
            return self._access_token
        snapshot = await self._store.snapshot()
        if snapshot and snapshot.get("status") != "active":
            raise AccountOIDCError("account is suspended")
        refresh_token = await self._tokens.load_refresh_token()
        if not refresh_token:
            raise AccountOIDCError("not signed in")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                },
            )
        if response.status_code != 200:
            await self._tokens.clear()
            raise AccountOIDCError("refresh token is no longer valid")
        tokens = response.json()
        self._access_token = str(tokens.get("access_token", ""))
        rotated = str(tokens.get("refresh_token", ""))
        if rotated:
            await self._tokens.save_refresh_token(rotated)
        if not self._access_token:
            raise AccountOIDCError("refresh response is incomplete")
        return self._access_token

    def _expire_attempt(self, attempt: LoginAttempt) -> None:
        if attempt.status == "pending":
            attempt.status = "expired"
            attempt.device_code = ""
            if self._listener_attempt is attempt and self._server is not None:
                self._server.close()
                self._server = None
                self._listener_attempt = None
