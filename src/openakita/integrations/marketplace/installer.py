"""Durable, verified installation of Marketplace resource packages."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from openakita import __version__
from openakita.config import settings
from openakita.utils.atomic_io import atomic_json_write, read_json_safe

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[a-f0-9]{64}$")
RESOURCE_TYPES = frozenset({"plugin", "skill", "mcp"})
ACTIVE_STATUSES = frozenset({"ready", "downloading", "verifying", "installing"})
TERMINAL_STATUSES = frozenset({"installed", "failed", "cancelled"})


class MarketplaceInstallError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def validate_marketplace_endpoint(value: str) -> str:
    parsed = urlparse((value or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MarketplaceInstallError("marketplace_endpoint_invalid")
    if parsed.path not in ("", "/"):
        raise MarketplaceInstallError("marketplace_endpoint_invalid")
    configured = {
        item.strip().lower()
        for item in os.environ.get("OPENAKITA_MARKETPLACE_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    allowed_https = {"marketplace.openakita.cn", *configured}
    if parsed.scheme == "https" and host in allowed_https:
        return f"https://{parsed.netloc}"
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return f"http://{parsed.netloc}"
    raise MarketplaceInstallError("marketplace_endpoint_invalid")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        if not source.infolist() or len(source.infolist()) > 10_000:
            raise MarketplaceInstallError("marketplace_package_invalid")
        total = 0
        root = destination.resolve()
        for member in source.infolist():
            normalized = member.filename.replace("\\", "/")
            parts = [part for part in normalized.split("/") if part not in ("", ".")]
            if normalized.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise MarketplaceInstallError("marketplace_package_unsafe_path")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise MarketplaceInstallError("marketplace_package_unsafe_path")
            total += member.file_size
            if total > 512 * 1024 * 1024:
                raise MarketplaceInstallError("marketplace_package_too_large")
            target = (destination / normalized).resolve()
            if target != root and root not in target.parents:
                raise MarketplaceInstallError("marketplace_package_unsafe_path")
        source.extractall(destination)


def _package_root(extracted: Path) -> Path:
    entries = [entry for entry in extracted.iterdir() if entry.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir() and not (extracted / "manifest.json").exists():
        return entries[0]
    return extracted


class MarketplaceInstallManager:
    def __init__(self, root: Path | None = None) -> None:
        data_root = (
            Path(root) / "data" / "marketplace"
            if root is not None
            else settings.openakita_home / "marketplace"
        )
        self.root = data_root
        self.jobs_dir = data_root / "jobs"
        self.packages_dir = data_root / "packages"
        self.device_path = data_root / "device.json"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.device_id = self._load_device_id()
        self._load_jobs()

    def start(self) -> None:
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(self._reconcile_reports())

    async def stop(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

    async def _reconcile_reports(self) -> None:
        while True:
            for job in list(self._jobs.values()):
                if job.get("report_pending") and job.get("token"):
                    await self._flush_terminal_report(job)
            await asyncio.sleep(15)

    def _load_device_id(self) -> str:
        existing = read_json_safe(self.device_path) or {}
        value = str(existing.get("device_id") or "").strip()
        if value:
            return value
        value = str(uuid.uuid4())
        atomic_json_write(self.device_path, {"device_id": value}, backup=False, fsync=True)
        try:
            os.chmod(self.device_path, 0o600)
        except OSError:
            pass
        return value

    def _load_jobs(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            job = read_json_safe(path)
            if not isinstance(job, dict) or not job.get("id"):
                continue
            if job.get("status") in ACTIVE_STATUSES:
                job["status"] = "failed"
                job["failure_code"] = "marketplace_install_interrupted"
                job["report_pending"] = True
                self._write(job)
            self._jobs[str(job["id"])] = job
            if job.get("status") in TERMINAL_STATUSES:
                (self.packages_dir / f"{job['id']}.zip").unlink(missing_ok=True)

    def _write(self, job: dict[str, Any]) -> None:
        atomic_json_write(self.jobs_dir / f"{job['id']}.json", job, backup=False, fsync=True)

    async def get(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job:
            raise MarketplaceInstallError("marketplace_install_not_found")
        if job.get("report_pending") and job.get("token"):
            await self._flush_terminal_report(job)
        return self._public(job)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        hidden = {"token", "download_url", "signature", "verification"}
        return {key: value for key, value in job.items() if key not in hidden}

    async def _authorize(self, token: str, endpoint: str, request: Any, *, confirm: bool = False) -> dict:
        from openakita.account.oidc import AccountOIDCError

        account = getattr(request.app.state, "account_oidc_manager", None)
        if account is None:
            raise MarketplaceInstallError("marketplace_account_required")
        try:
            proof = await account.marketplace_install_proof(token, self.device_id)
        except AccountOIDCError as exc:
            raise MarketplaceInstallError(str(exc)) from exc
        action = "authorize" if confirm else "consume"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    f"{endpoint}/api/internal/openakita/install-instructions/{action}",
                    json={"token": token, "device_id": self.device_id, "account_proof": proof},
                )
        except httpx.HTTPError as exc:
            raise MarketplaceInstallError("marketplace_connection_failed") from exc
        if response.status_code != 200:
            errors = {401: "marketplace_account_required", 403: "marketplace_account_mismatch",
                      404: "marketplace_instruction_unavailable", 409: "marketplace_instruction_unavailable",
                      503: "marketplace_identity_unavailable"}
            raise MarketplaceInstallError(errors.get(response.status_code, "marketplace_connection_failed"))
        return response.json().get("data") or {}

    async def prepare(self, token: str, endpoint: str, request: Any) -> dict[str, Any]:
        token = (token or "").strip().lower()
        if not TOKEN_RE.fullmatch(token):
            raise MarketplaceInstallError("marketplace_instruction_invalid")
        endpoint = validate_marketplace_endpoint(endpoint)
        async with self._lock:
            for existing in self._jobs.values():
                if existing.get("token") == token and existing.get("endpoint") == endpoint:
                    return self._public(existing)
            payload = await self._authorize(token, endpoint, request)
            self._validate_instruction(payload)
            job_id = str(payload["id"])
            job = {
                "id": job_id,
                "token": token,
                "endpoint": endpoint,
                "status": "ready",
                "progress": 0,
                "resource_id": payload["resource_id"],
                "resource_name": payload["resource_name"],
                "resource_slug": payload["resource_slug"],
                "resource_type": payload["resource_type"],
                "version_id": payload["version_id"],
                "version": payload["version"],
                "digest_sha256": payload["digest_sha256"],
                "signature": payload["signature"],
                "size_bytes": payload["size_bytes"],
                "download_url": payload["download_url"],
                "manifest": payload.get("manifest") or {},
                "permissions": payload.get("permissions") or [],
                "dependencies": payload.get("dependencies") or [],
                "verification": payload["verification"],
                "failure_code": "",
            }
            self._jobs[job_id] = job
            self._write(job)
            return self._public(job)

    @staticmethod
    def _validate_instruction(payload: dict[str, Any]) -> None:
        required = (
            "id", "resource_id", "resource_name", "resource_slug", "resource_type", "version_id", "version",
            "digest_sha256", "signature", "size_bytes", "download_url", "verification",
        )
        if any(not payload.get(key) for key in required):
            raise MarketplaceInstallError("marketplace_instruction_invalid")
        if payload["resource_type"] not in RESOURCE_TYPES:
            raise MarketplaceInstallError("marketplace_resource_unsupported")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", str(payload["digest_sha256"])):
            raise MarketplaceInstallError("marketplace_instruction_invalid")
        verification = payload.get("verification") or {}
        if verification.get("algorithm") != "Ed25519" or verification.get("digest_algorithm") != "SHA-256":
            raise MarketplaceInstallError("marketplace_signature_unsupported")

    async def confirm(self, job_id: str, request: Any) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise MarketplaceInstallError("marketplace_install_not_found")
            if job.get("status") == "installed":
                return self._public(job)
            if job.get("status") != "ready":
                raise MarketplaceInstallError("marketplace_install_busy")
            # Recheck the current account and entitlement after the preview;
            # neither a cached proof nor the preview authorizes an installation.
            await self._authorize(job["token"], job["endpoint"], request, confirm=True)
            job["status"] = "downloading"
            job["progress"] = 5
            job["failure_code"] = ""
            self._write(job)
            task = asyncio.create_task(self._run(job_id, request))
            self._tasks[job_id] = task
            return self._public(job)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise MarketplaceInstallError("marketplace_install_not_found")
            if job.get("status") == "ready":
                job["status"] = "cancelled"
                job["failure_code"] = ""
                job["report_pending"] = bool(job.get("token"))
                self._write(job)
                if job.get("token"):
                    await self._flush_terminal_report(job)
            return self._public(job)

    async def _report(self, job: dict[str, Any], status: str, **extra: Any) -> bool:
        body = {"token": job["token"], "device_id": self.device_id, "status": status, **extra}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(
                    f"{job['endpoint']}/api/internal/openakita/install-instructions/report",
                    json=body,
                )
                return response.status_code == 200
        except httpx.HTTPError:
            logger.warning("Marketplace install status report failed for %s", job["id"])
            return False

    async def _run(self, job_id: str, request: Any) -> None:
        job = self._jobs[job_id]
        package_path = self.packages_dir / f"{job_id}.zip"
        try:
            await self._report(job, "downloading")
            await self._download(job, package_path)
            job["status"] = "verifying"
            job["progress"] = 45
            self._write(job)
            self._verify(job, package_path)
            job["status"] = "installing"
            job["progress"] = 70
            self._write(job)
            await self._report(job, "installing")
            job["restart_required"] = await self._install(job, package_path, request)
            token = job.get("token", "")
            job["status"] = "installed"
            job["progress"] = 100
            job["failure_code"] = ""
            job["token"] = token
            job["report_pending"] = True
            self._write(job)
            await self._flush_terminal_report(job)
        except MarketplaceInstallError as exc:
            await self._fail(job, exc.code)
        except Exception:
            logger.exception("Marketplace installation failed for %s", job_id)
            await self._fail(job, "marketplace_install_failed")
        finally:
            self._tasks.pop(job_id, None)
            package_path.unlink(missing_ok=True)

    async def _fail(self, job: dict[str, Any], code: str) -> None:
        token = job.get("token", "")
        job["status"] = "failed"
        job["failure_code"] = code
        job["report_pending"] = bool(token)
        self._write(job)
        if token:
            await self._flush_terminal_report(job)

    async def _flush_terminal_report(self, job: dict[str, Any]) -> None:
        status = str(job.get("status") or "")
        if status not in {"installed", "failed", "cancelled"} or not job.get("token"):
            return
        extra: dict[str, Any] = {}
        if status == "installed":
            extra = {"signature_verified": True, "compatibility_verified": True}
        elif status == "failed":
            extra = {"failure_code": job.get("failure_code") or "marketplace_install_failed"}
        if await self._report(job, status, **extra):
            job["report_pending"] = False
            job.pop("token", None)
            job.pop("download_url", None)
            self._write(job)

    async def _download(self, job: dict[str, Any], destination: Path) -> None:
        expected = int(job["size_bytes"])
        if expected <= 0 or expected > 512 * 1024 * 1024:
            raise MarketplaceInstallError("marketplace_package_too_large")
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                async with client.stream("GET", job["download_url"]) as response:
                    response.raise_for_status()
                    written = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > expected or written > 512 * 1024 * 1024:
                                raise MarketplaceInstallError("marketplace_package_size_mismatch")
                            output.write(chunk)
                            job["progress"] = min(40, 5 + int(35 * written / expected))
                    if written != expected:
                        raise MarketplaceInstallError("marketplace_package_size_mismatch")
        except httpx.HTTPError as exc:
            raise MarketplaceInstallError("marketplace_download_failed") from exc

    @staticmethod
    def _verify(job: dict[str, Any], package_path: Path) -> None:
        digest_builder = hashlib.sha256()
        with package_path.open("rb") as package:
            for chunk in iter(lambda: package.read(1024 * 1024), b""):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        if digest.lower() != str(job["digest_sha256"]).lower():
            raise MarketplaceInstallError("marketplace_digest_mismatch")
        contract = job["verification"]
        try:
            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(contract["public_key"]))
            signature = base64.b64decode(job["signature"])
            message = (
                f"sha256:{digest.lower()}\nresource:{job['resource_id']}\nversion:{job['version']}"
            ).encode()
            public_key.verify(signature, message)
        except Exception as exc:
            raise MarketplaceInstallError("marketplace_signature_invalid") from exc
        try:
            with zipfile.ZipFile(package_path) as archive:
                if "manifest.json" not in archive.namelist():
                    raise MarketplaceInstallError("marketplace_manifest_missing")
                info = archive.getinfo("manifest.json")
                if info.file_size > 1024 * 1024:
                    raise MarketplaceInstallError("marketplace_manifest_invalid")
                manifest = json.loads(archive.read(info).decode("utf-8"))
        except MarketplaceInstallError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise MarketplaceInstallError("marketplace_manifest_invalid") from exc
        if not isinstance(manifest, dict):
            raise MarketplaceInstallError("marketplace_manifest_invalid")
        if manifest.get("resource_id") != job["resource_id"] or manifest.get("version") != job["version"]:
            raise MarketplaceInstallError("marketplace_manifest_mismatch")
        if manifest.get("resource_type") != job["resource_type"]:
            raise MarketplaceInstallError("marketplace_manifest_mismatch")
        if list(manifest.get("permissions") or []) != list(job.get("permissions") or []):
            raise MarketplaceInstallError("marketplace_manifest_mismatch")
        if list(manifest.get("dependencies") or []) != list(job.get("dependencies") or []):
            raise MarketplaceInstallError("marketplace_manifest_mismatch")
        requirement = str(manifest.get("openakita") or "").strip()
        if requirement:
            try:
                if Version(__version__) not in SpecifierSet(requirement):
                    raise MarketplaceInstallError("marketplace_version_incompatible")
            except (InvalidSpecifier, InvalidVersion) as exc:
                raise MarketplaceInstallError("marketplace_compatibility_invalid") from exc

    async def _install(self, job: dict[str, Any], package_path: Path, request: Any) -> bool:
        temp_root = Path(tempfile.mkdtemp(prefix="openakita-marketplace-"))
        try:
            extracted = temp_root / "content"
            await asyncio.to_thread(_safe_extract, package_path, extracted)
            source = _package_root(extracted)
            kind = job["resource_type"]
            if kind == "plugin":
                return await self._install_plugin(source, request)
            elif kind == "skill":
                await self._install_skill(source, job["resource_slug"], request)
            elif kind == "mcp":
                await self._install_mcp(source, request)
            else:
                raise MarketplaceInstallError("marketplace_resource_unsupported")
            return False
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    @staticmethod
    async def _install_plugin(source: Path, request: Any) -> bool:
        from openakita.api.routes.plugins import (
            InstallProgress,
            _do_install,
            _finalize_plugin_install,
            _plugins_dir,
        )

        try:
            progress = InstallProgress()
            plugin_id, hot_loaded = await _do_install(
                str(source), _plugins_dir(), progress, request
            )
            _finalize_plugin_install(request, progress, plugin_id, hot_loaded)
            return not hot_loaded
        except Exception as exc:
            raise MarketplaceInstallError("marketplace_plugin_install_failed") from exc

    @staticmethod
    async def _install_skill(source: Path, resource_slug: str, request: Any) -> None:
        skill_source = source
        if not (skill_source / "SKILL.md").is_file():
            candidates = list(skill_source.rglob("SKILL.md"))
            if len(candidates) != 1:
                raise MarketplaceInstallError("marketplace_skill_manifest_missing")
            skill_source = candidates[0].parent
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", resource_slug):
            raise MarketplaceInstallError("marketplace_skill_manifest_invalid")
        skills_root = settings.skills_path
        skills_root.mkdir(parents=True, exist_ok=True)
        target = skills_root / resource_slug
        staging = skills_root / f".{resource_slug}.marketplace-{uuid.uuid4().hex[:8]}"
        backup = skills_root / f".{resource_slug}.backup-{uuid.uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(shutil.copytree, skill_source, staging)
            if target.exists():
                target.rename(backup)
            staging.rename(target)
            from openakita.api.routes.skills import _propagate

            await _propagate(request, "install")
            shutil.rmtree(backup, ignore_errors=True)
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists():
                shutil.rmtree(target, ignore_errors=True)
                backup.rename(target)
            raise MarketplaceInstallError("marketplace_skill_install_failed") from exc

    @staticmethod
    async def _install_mcp(source: Path, request: Any) -> None:
        metadata_path = source / "SERVER_METADATA.json"
        if not metadata_path.is_file():
            raise MarketplaceInstallError("marketplace_mcp_manifest_missing")
        metadata = read_json_safe(metadata_path)
        if not isinstance(metadata, dict):
            raise MarketplaceInstallError("marketplace_mcp_manifest_invalid")
        name = str(metadata.get("serverIdentifier") or "").strip()
        transport = str(metadata.get("transport") or "stdio").strip()
        command = str(metadata.get("command") or "").strip()
        url = str(metadata.get("url") or "").strip()
        if not name or transport not in {"stdio", "streamable_http", "sse"}:
            raise MarketplaceInstallError("marketplace_mcp_manifest_invalid")
        if transport == "stdio" and not command:
            raise MarketplaceInstallError("marketplace_mcp_manifest_invalid")
        if transport != "stdio" and not url:
            raise MarketplaceInstallError("marketplace_mcp_manifest_invalid")
        from openakita.api.routes.mcp import _get_mcp_catalog, _get_mcp_client

        client = _get_mcp_client(request)
        catalog = _get_mcp_catalog(request)
        if client is None or catalog is None:
            raise MarketplaceInstallError("marketplace_runtime_unavailable")
        from openakita.tools.mcp_workspace import add_server_to_workspace

        server_dir = settings.mcp_config_path / name
        backup_dir = settings.mcp_config_path / f".{name}.backup-{uuid.uuid4().hex[:8]}"
        try:
            if server_dir.exists():
                server_dir.rename(backup_dir)
            await asyncio.to_thread(shutil.copytree, source, server_dir)
            result = await add_server_to_workspace(
                name=name,
                transport=transport,
                command=command,
                args=list(metadata.get("args") or []),
                env=dict(metadata.get("env") or {}),
                url=url,
                description=str(metadata.get("serverName") or name),
                instructions=(source / "INSTRUCTIONS.md").read_text(encoding="utf-8")
                if (source / "INSTRUCTIONS.md").is_file()
                else "",
                auto_connect=bool(metadata.get("autoConnect", False)),
                headers=dict(metadata.get("headers") or {}),
                config_base_dir=settings.mcp_config_path,
                search_bases=[server_dir, settings.project_root],
                client=client,
                catalog=catalog,
            )
            if result.get("status") != "ok":
                raise MarketplaceInstallError("marketplace_mcp_install_failed")
            from openakita.runtime_config_coordinator import get_runtime_config_coordinator

            get_runtime_config_coordinator(request).mcp_changed(name, "added")
            shutil.rmtree(backup_dir, ignore_errors=True)
        except MarketplaceInstallError:
            shutil.rmtree(server_dir, ignore_errors=True)
            if backup_dir.exists():
                backup_dir.rename(server_dir)
            raise
        except Exception as exc:
            shutil.rmtree(server_dir, ignore_errors=True)
            if backup_dir.exists():
                backup_dir.rename(server_dir)
            raise MarketplaceInstallError("marketplace_mcp_install_failed") from exc
