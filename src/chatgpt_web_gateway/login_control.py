"""Loopback-only control plane for isolated Provider login browsers.

The service never receives or stores account credentials.  It maps a sanitized
account ID to a deployment-managed Chrome profile and worker LaunchAgent, then
performs only three bounded actions: open the login browser, close it, and
restart that account's worker after the Gateway has verified the live session.

This macOS implementation reports ``local_window``.  A Linux remote-browser
broker may implement the same API with ``remote_browser`` and return one HTTPS
session URL only from the explicit ``open`` action; the Gateway validates and
forwards that short-lived capability without treating it as ChatGPT auth.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import io
import json
import os
import plistlib
import re
import sys
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .chrome_launcher import _dedicated_chrome_state, main as chrome_main
from .remote_browser_sessions import (
    RemoteBrowserSessionError,
    issue_pending_session,
    revoke_account_sessions,
)


ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SERVICE_LABEL_RE = re.compile(
    r"^(?:com\.turtleligpt\.[A-Za-z0-9._-]{1,80}|turtle-gpt-[A-Za-z0-9._-]{1,80})$"
)
SERVICE_MANAGERS = {"launchd", "systemd-user"}
LOGIN_MODES = {"local_window", "remote_browser"}


class LoginControlError(RuntimeError):
    """A sanitized login-control failure safe to return to administrators."""


class LoginControlUnavailable(LoginControlError):
    pass


class LoginRuntimeMissing(LoginControlError):
    pass


@dataclass(frozen=True, slots=True)
class AccountRuntime:
    account_id: str
    provider: str
    cdp_port: int
    auth_dir: Path
    profile_dir: Path
    pid_file: Path
    login_url: str
    worker_service_label: str
    worker_port: int


@dataclass(frozen=True, slots=True)
class ControlSettings:
    project_root: Path
    runtime_root: Path
    manifest_path: Path
    secret_path: Path
    launch_agents_dir: Path | None = None
    service_manager: str = "launchd"
    login_mode: str = "local_window"
    remote_browser_public_url: str | None = None
    remote_browser_session_store: Path | None = None
    remote_browser_ttl_seconds: int = 600
    worker_port_start: int = 8360
    cdp_port_start: int = 9260
    bind_host: str = "127.0.0.1"
    port: int = 8340

    @classmethod
    def from_env(cls) -> "ControlSettings":
        default_root = Path(__file__).resolve().parents[2]
        project_root = Path(
            os.getenv("TURTLE_LOGIN_PROJECT_ROOT", str(default_root))
        ).expanduser().resolve()
        runtime_root = project_root / ".runtime"
        bind_host = os.getenv("TURTLE_LOGIN_CONTROL_HOST", "127.0.0.1").strip()
        if bind_host not in {"127.0.0.1", "::1"}:
            raise ValueError("TURTLE_LOGIN_CONTROL_HOST must remain loopback-only")
        port = int(os.getenv("TURTLE_LOGIN_CONTROL_PORT", "8340"))
        if not 1 <= port <= 65535:
            raise ValueError("TURTLE_LOGIN_CONTROL_PORT must be between 1 and 65535")
        worker_port_start = int(
            os.getenv("TURTLE_ACCOUNT_WORKER_PORT_START", "8360")
        )
        cdp_port_start = int(
            os.getenv("TURTLE_ACCOUNT_CDP_PORT_START", "9260")
        )
        if not 1 <= worker_port_start <= 65535 or not 1 <= cdp_port_start <= 65535:
            raise ValueError("automatic account runtime port ranges are invalid")
        service_manager = os.getenv(
            "TURTLE_LOGIN_SERVICE_MANAGER",
            "launchd" if sys.platform == "darwin" else "systemd-user",
        ).strip()
        if service_manager not in SERVICE_MANAGERS:
            raise ValueError("TURTLE_LOGIN_SERVICE_MANAGER is invalid")
        login_mode = os.getenv("TURTLE_LOGIN_MODE", "local_window").strip()
        if login_mode not in LOGIN_MODES:
            raise ValueError("TURTLE_LOGIN_MODE is invalid")
        remote_browser_public_url = (
            os.getenv("TURTLE_REMOTE_BROWSER_PUBLIC_URL", "").strip() or None
        )
        remote_browser_session_store = Path(
            os.getenv(
                "TURTLE_REMOTE_BROWSER_SESSION_STORE",
                str(runtime_root / "remote-browser-sessions.json"),
            )
        ).expanduser().absolute()
        remote_browser_ttl_seconds = int(
            os.getenv("TURTLE_REMOTE_BROWSER_TTL_SECONDS", "600")
        )
        if not 30 <= remote_browser_ttl_seconds <= 900:
            raise ValueError("TURTLE_REMOTE_BROWSER_TTL_SECONDS must be between 30 and 900")
        if login_mode == "remote_browser":
            parsed_public_url = urlsplit(str(remote_browser_public_url or ""))
            if (
                parsed_public_url.scheme != "https"
                or not parsed_public_url.hostname
                or parsed_public_url.username
                or parsed_public_url.password
                or parsed_public_url.query
                or parsed_public_url.fragment
            ):
                raise ValueError("TURTLE_REMOTE_BROWSER_PUBLIC_URL must be an HTTPS URL")
        launch_agents_default = (
            Path.home() / "Library" / "LaunchAgents"
            if service_manager == "launchd"
            else Path.home() / ".config" / "systemd" / "user"
        )
        return cls(
            project_root=project_root,
            runtime_root=runtime_root,
            manifest_path=Path(
                os.getenv(
                    "TURTLE_LOGIN_RUNTIME_MANIFEST",
                    str(runtime_root / "account-runtimes.json"),
                )
            ).expanduser().absolute(),
            secret_path=Path(
                os.getenv(
                    "TURTLE_LOGIN_CONTROL_SECRET_FILE",
                    str(runtime_root / "secrets" / "turtle_login_control_secret"),
                )
            ).expanduser().absolute(),
            launch_agents_dir=Path(
                os.getenv(
                    "TURTLE_LOGIN_LAUNCH_AGENTS_DIR",
                    str(launch_agents_default),
                )
            ).expanduser().absolute(),
            service_manager=service_manager,
            login_mode=login_mode,
            remote_browser_public_url=remote_browser_public_url,
            remote_browser_session_store=remote_browser_session_store,
            remote_browser_ttl_seconds=remote_browser_ttl_seconds,
            worker_port_start=worker_port_start,
            cdp_port_start=cdp_port_start,
            bind_host=bind_host,
            port=port,
        )

    def resolved_launch_agents_dir(self) -> Path:
        return (
            self.launch_agents_dir
            if self.launch_agents_dir is not None
            else (
                Path.home() / "Library" / "LaunchAgents"
                if self.service_manager == "launchd"
                else Path.home() / ".config" / "systemd" / "user"
            )
        )


def _private_regular_file(path: Path, description: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LoginControlError(f"{description}尚未配置") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LoginControlError(f"{description}必须是普通文件")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LoginControlError(f"{description}权限必须为 0600")


def _read_secret(path: Path, *, enforce_private_mode: bool) -> str:
    if enforce_private_mode:
        _private_regular_file(path, "登录控制 Secret")
    else:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LoginControlUnavailable("登录控制 Secret 尚未挂载") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LoginControlUnavailable("登录控制 Secret 无效")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LoginControlUnavailable("登录控制 Secret 无法读取") from exc
    if not 32 <= len(value) <= 512 or any(character.isspace() for character in value):
        raise LoginControlUnavailable("登录控制 Secret 无效")
    return value


def _runtime_path(settings: ControlSettings, raw: Any, field: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise LoginControlError(f"账号运行时缺少 {field}")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = settings.project_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(settings.runtime_root.resolve())
    except ValueError as exc:
        raise LoginControlError(f"账号运行时 {field} 必须位于 .runtime") from exc
    if candidate.is_symlink():
        raise LoginControlError(f"账号运行时 {field} 不能是符号链接")
    return resolved


def _positive_port(value: Any, field: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise LoginControlError(f"账号运行时 {field} 无效") from exc
    if not 1 <= port <= 65535:
        raise LoginControlError(f"账号运行时 {field} 无效")
    return port


def _read_manifest_payload(settings: ControlSettings) -> dict[str, Any]:
    _private_regular_file(settings.manifest_path, "账号运行时清单")
    try:
        if settings.manifest_path.stat().st_size > 64 * 1024:
            raise LoginControlError("账号运行时清单过大")
        payload = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LoginControlError("账号运行时清单无效") from exc
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
        raise LoginControlError("账号运行时清单版本无效")
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        raise LoginControlError("账号运行时清单缺少 accounts")
    return payload


def load_runtime_manifest(settings: ControlSettings) -> dict[str, AccountRuntime]:
    payload = _read_manifest_payload(settings)
    accounts = payload["accounts"]

    result: dict[str, AccountRuntime] = {}
    used_ports: set[int] = set()
    used_paths: set[Path] = set()
    for raw_account_id, raw in accounts.items():
        account_id = str(raw_account_id or "")
        if not ACCOUNT_ID_RE.fullmatch(account_id) or not isinstance(raw, dict):
            raise LoginControlError("账号运行时清单包含无效账号")
        provider = str(raw.get("provider") or "gpt").strip().lower()
        if provider not in {"gpt", "claude"}:
            raise LoginControlError("账号运行时 Provider 无效")
        cdp_port = _positive_port(raw.get("cdp_port"), "cdp_port")
        worker_port = _positive_port(raw.get("worker_port"), "worker_port")
        if cdp_port in used_ports or worker_port in used_ports or cdp_port == worker_port:
            raise LoginControlError("账号运行时端口必须相互隔离")
        used_ports.update({cdp_port, worker_port})
        parsed_url = urlsplit(str(raw.get("login_url") or ""))
        expected_hostname = "claude.ai" if provider == "claude" else "chatgpt.com"
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != expected_hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.fragment
        ):
            raise LoginControlError("账号运行时登录地址无效")
        service_label = str(raw.get("worker_service_label") or "").strip()
        if not SERVICE_LABEL_RE.fullmatch(service_label):
            raise LoginControlError("账号运行时 LaunchAgent 标识无效")
        auth_default = (
            ".runtime/gpt4free-auth"
            if account_id == "legacy-primary"
            else f".runtime/accounts/{account_id}/auth"
        )
        auth_dir = _runtime_path(
            settings,
            raw.get("auth_dir") or auth_default,
            "auth_dir",
        )
        profile_dir = _runtime_path(settings, raw.get("profile_dir"), "profile_dir")
        pid_file = _runtime_path(settings, raw.get("pid_file"), "pid_file")
        isolated_paths = {auth_dir, profile_dir, pid_file}
        if len(isolated_paths) != 3 or isolated_paths & used_paths:
            raise LoginControlError("账号运行时目录必须相互隔离")
        used_paths.update(isolated_paths)
        result[account_id] = AccountRuntime(
            account_id=account_id,
            provider=provider,
            cdp_port=cdp_port,
            auth_dir=auth_dir,
            profile_dir=profile_dir,
            pid_file=pid_file,
            login_url=parsed_url.geturl(),
            worker_service_label=service_label,
            worker_port=worker_port,
        )
    return result


def _chrome_arguments(
    runtime: AccountRuntime,
    action: str | None = None,
    *,
    manual: bool = False,
) -> list[str]:
    values = [
        "--port",
        str(runtime.cdp_port),
        "--profile-dir",
        str(runtime.profile_dir),
        "--pid-file",
        str(runtime.pid_file),
        "--url",
        runtime.login_url,
        "--service-label",
        runtime.account_id,
    ]
    if manual:
        values.append("--manual")
    if action:
        values.append(action)
    return values


def _run_chrome(
    runtime: AccountRuntime,
    action: str | None = None,
    *,
    manual: bool = False,
) -> None:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            chrome_main(_chrome_arguments(runtime, action, manual=manual))
    except SystemExit as exc:
        raise LoginControlError("专用登录浏览器操作失败") from exc


def _worker_ready(port: int, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _worker_reachable(port: int, timeout_seconds: float = 0.08) -> bool:
    """Perform one bounded loopback probe for status pages.

    Provisioning and restart paths deliberately wait for a worker to become
    ready.  A read-only status request must not repeat that wait for every
    account, otherwise simply opening the Provider page becomes noticeably
    slow as the pool grows.
    """

    try:
        with socket.create_connection(
            ("127.0.0.1", int(port)),
            timeout=max(0.01, min(float(timeout_seconds), 0.25)),
        ):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _atomic_write(
    path: Path,
    data: bytes,
    mode: int,
    *,
    private_parent: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private_parent:
        os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _secure_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise LoginControlError("账号运行时目录无效")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _credential_state(auth_dir: Path) -> str:
    if not auth_dir.exists():
        return "empty"
    if auth_dir.is_symlink() or not auth_dir.is_dir():
        return "invalid"
    try:
        for current_root, directory_names, file_names in os.walk(
            auth_dir,
            followlinks=False,
        ):
            current = Path(current_root)
            if current.is_symlink():
                return "invalid"
            for name in directory_names:
                if (current / name).is_symlink():
                    return "invalid"
            for name in file_names:
                candidate = current / name
                if candidate.is_symlink():
                    return "invalid"
                if candidate.is_file():
                    return "stored"
    except OSError:
        return "invalid"
    return "empty"


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def _next_available_port(start: int, used: set[int]) -> int:
    first = _positive_port(start, "port_start")
    for port in range(first, min(65535, first + 2000) + 1):
        if port not in used and _port_available(port):
            used.add(port)
            return port
    raise LoginControlError("没有可用的本机隔离端口")


def _worker_launch_agent(settings: ControlSettings, runtime: AccountRuntime) -> bytes:
    python = settings.project_root / ".venv" / "bin" / "python"
    launcher = settings.project_root / "scripts" / (
        "run_claude_worker.py"
        if runtime.provider == "claude"
        else "run_gpt4free.py"
    )
    if not python.is_file() or not launcher.is_file():
        raise LoginControlError("账号 worker 启动环境尚未安装")
    logs = runtime.auth_dir.parent / "logs"
    _secure_directory(logs)
    stdout_path = logs / "worker.stdout.log"
    stderr_path = logs / "worker.stderr.log"
    for log_path in (stdout_path, stderr_path):
        if log_path.exists() and (log_path.is_symlink() or not log_path.is_file()):
            raise LoginControlError("账号 worker 日志路径无效")
        if not log_path.exists():
            _atomic_write(log_path, b"", 0o600)
        else:
            os.chmod(log_path, 0o600)
    environment = (
        {
            "CLAUDE_AUTH_PATH": str(runtime.auth_dir / "session.json"),
            "CLAUDE_VERIFIED_MODELS_PATH": str(
                runtime.auth_dir / "verified-models.json"
            ),
            "CLAUDE_BIND_HOST": "127.0.0.1",
            "CLAUDE_PORT": str(runtime.worker_port),
            "CLAUDE_BROWSER_PORT": str(runtime.cdp_port),
        }
        if runtime.provider == "claude"
        else {
            "GPT4FREE_AUTH_DIR": str(runtime.auth_dir),
            "GPT4FREE_BIND_HOST": "127.0.0.1",
            "GPT4FREE_PORT": str(runtime.worker_port),
            "GPT4FREE_BROWSER_HOST": "127.0.0.1",
            "GPT4FREE_BROWSER_PORT": str(runtime.cdp_port),
        }
    )
    payload = {
        "Label": runtime.worker_service_label,
        "ProgramArguments": [str(python), str(launcher)],
        "WorkingDirectory": str(settings.project_root),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _systemd_quote(value: str | Path) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_path(value: str | Path) -> str:
    """Encode one absolute path for a scalar systemd directive.

    Unlike ExecStart/Environment, WorkingDirectory does not strip shell-style
    quotes. Keep ordinary deployment paths readable and use systemd's C-style
    escapes for characters that would otherwise be parsed ambiguously.
    """

    raw = str(value)
    if not raw.startswith("/") or "\x00" in raw:
        raise LoginControlError("账号 worker 服务路径无效")
    encoded: list[str] = []
    for character in raw:
        if character == "%":
            encoded.append("%%")
        elif character in {" ", "\t", "\n", "\r", "\\", '"', "'"}:
            encoded.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
        else:
            encoded.append(character)
    return "".join(encoded)


def _worker_systemd_unit(settings: ControlSettings, runtime: AccountRuntime) -> bytes:
    python = settings.project_root / ".venv" / "bin" / "python"
    launcher = settings.project_root / "scripts" / (
        "run_claude_worker.py"
        if runtime.provider == "claude"
        else "run_gpt4free.py"
    )
    if not python.is_file() or not launcher.is_file():
        raise LoginControlError("账号 worker 启动环境尚未安装")
    logs = runtime.auth_dir.parent / "logs"
    _secure_directory(logs)
    stdout_path = logs / "worker.stdout.log"
    stderr_path = logs / "worker.stderr.log"
    for log_path in (stdout_path, stderr_path):
        if log_path.exists() and (log_path.is_symlink() or not log_path.is_file()):
            raise LoginControlError("账号 worker 日志路径无效")
        if not log_path.exists():
            _atomic_write(log_path, b"", 0o600)
        else:
            os.chmod(log_path, 0o600)
    values = (
        {
            "CLAUDE_AUTH_PATH": runtime.auth_dir / "session.json",
            "CLAUDE_VERIFIED_MODELS_PATH": runtime.auth_dir
            / "verified-models.json",
            "CLAUDE_BIND_HOST": "127.0.0.1",
            "CLAUDE_PORT": str(runtime.worker_port),
            "CLAUDE_BROWSER_PORT": str(runtime.cdp_port),
        }
        if runtime.provider == "claude"
        else {
            "GPT4FREE_AUTH_DIR": runtime.auth_dir,
            "GPT4FREE_BIND_HOST": "127.0.0.1",
            "GPT4FREE_PORT": str(runtime.worker_port),
            "GPT4FREE_BROWSER_HOST": "127.0.0.1",
            "GPT4FREE_BROWSER_PORT": str(runtime.cdp_port),
        }
    )
    environment = "\n".join(
        f"Environment={_systemd_quote(f'{name}={value}')}"
        for name, value in values.items()
    )
    payload = f"""[Unit]
Description=Turtle Chat isolated {runtime.provider} worker {runtime.account_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={_systemd_path(settings.project_root)}
ExecStart={_systemd_quote(python)} {_systemd_quote(launcher)}
{environment}
Restart=always
RestartSec=5
UMask=0077
StandardOutput=append:{_systemd_path(stdout_path)}
StandardError=append:{_systemd_path(stderr_path)}

[Install]
WantedBy=default.target
"""
    return payload.encode("utf-8")


def _worker_service_definition(
    settings: ControlSettings,
    runtime: AccountRuntime,
) -> bytes:
    if settings.service_manager == "launchd":
        return _worker_launch_agent(settings, runtime)
    if settings.service_manager == "systemd-user":
        return _worker_systemd_unit(settings, runtime)
    raise LoginControlError("账号 worker 服务管理方式无效")


def _service_command(arguments: list[str], error_message: str) -> None:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoginControlError(error_message) from exc
    if result.returncode != 0:
        raise LoginControlError(error_message)


def _bootstrap_worker_agent(
    settings: ControlSettings,
    unit_path: Path,
    service_label: str,
) -> None:
    if settings.service_manager == "launchd":
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(unit_path)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _service_command(
            ["launchctl", "bootstrap", domain, str(unit_path)],
            "账号 worker 保活服务创建失败",
        )
        _service_command(
            ["launchctl", "kickstart", "-k", f"{domain}/{service_label}"],
            "账号 worker 启动失败",
        )
        return
    if settings.service_manager == "systemd-user":
        _service_command(
            ["systemctl", "--user", "daemon-reload"],
            "账号 worker 保活服务创建失败",
        )
        _service_command(
            ["systemctl", "--user", "enable", "--now", f"{service_label}.service"],
            "账号 worker 启动失败",
        )
        return
    raise LoginControlError("账号 worker 服务管理方式无效")


def _bootout_worker_agent(
    settings: ControlSettings,
    unit_path: Path,
    service_label: str,
) -> None:
    if settings.service_manager == "launchd":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(unit_path)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if settings.service_manager == "systemd-user":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{service_label}.service"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return


class LoginControlService:
    def __init__(self, settings: ControlSettings):
        self.settings = settings
        self._lock = RLock()
        self._events: dict[str, dict[str, Any]] = {}
        _read_secret(settings.secret_path, enforce_private_mode=True)
        load_runtime_manifest(settings)
        if settings.service_manager not in SERVICE_MANAGERS:
            raise LoginControlError("账号 worker 服务管理方式无效")
        if settings.login_mode not in LOGIN_MODES:
            raise LoginControlError("服务器登录方式无效")
        if settings.login_mode == "remote_browser":
            if settings.remote_browser_session_store is None:
                raise LoginControlError("服务器登录会话存储尚未配置")
            resolved_store = settings.remote_browser_session_store.resolve()
            try:
                resolved_store.relative_to(settings.runtime_root.resolve())
            except ValueError as exc:
                raise LoginControlError("服务器登录会话存储必须位于 .runtime") from exc
            if settings.remote_browser_session_store.is_symlink():
                raise LoginControlError("服务器登录会话存储不能是符号链接")

    def _runtimes(self) -> dict[str, AccountRuntime]:
        return load_runtime_manifest(self.settings)

    def _runtime(self, account_id: str) -> AccountRuntime:
        if not ACCOUNT_ID_RE.fullmatch(str(account_id or "")):
            raise LoginRuntimeMissing("账号没有可用的独立登录运行时")
        runtime = self._runtimes().get(account_id)
        if runtime is None:
            raise LoginRuntimeMissing("账号没有可用的独立登录运行时")
        return runtime

    def _plist_path(self, runtime: AccountRuntime) -> Path:
        suffix = "plist" if self.settings.service_manager == "launchd" else "service"
        return self.settings.resolved_launch_agents_dir() / (
            f"{runtime.worker_service_label}.{suffix}"
        )

    def _runtime_status(self, runtime: AccountRuntime) -> dict[str, Any]:
        browser_state = _dedicated_chrome_state(
            runtime.cdp_port,
            runtime.profile_dir,
            runtime.pid_file,
        )
        with self._lock:
            event = dict(self._events.get(runtime.account_id, {}))
        return {
            "account_id": runtime.account_id,
            "provider": runtime.provider,
            "configured": True,
            "login_mode": self.settings.login_mode,
            "browser_state": browser_state,
            "worker_state": (
                "ready" if _worker_reachable(runtime.worker_port) else "starting"
            ),
            "credential_state": _credential_state(runtime.auth_dir),
            "last_action": event.get("last_action"),
            "updated_at": event.get("updated_at"),
        }

    def provision(self, account_id: str, provider: str = "gpt") -> dict[str, Any]:
        """Create one isolated local runtime without reading any credential content."""

        normalized = str(account_id or "")
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in {"gpt", "claude"}:
            raise LoginControlError("账号 Provider 无效")
        if not ACCOUNT_ID_RE.fullmatch(normalized) or normalized == "legacy-primary":
            raise LoginControlError("账号标识不允许自动创建运行时")
        with self._lock:
            existing = self._runtimes().get(normalized)
            if existing is not None:
                if existing.provider != normalized_provider:
                    raise LoginControlError("账号运行时 Provider 与账号池不一致")
                return {
                    **self._runtime_status(existing),
                    "worker_port": existing.worker_port,
                }

            payload = _read_manifest_payload(self.settings)
            plist_path: Path | None = None
            manifest_added = False

            def rollback_partial_provision() -> None:
                if plist_path is not None:
                    with contextlib.suppress(OSError):
                        _bootout_worker_agent(
                            self.settings,
                            plist_path,
                            runtime.worker_service_label,
                        )
                if manifest_added:
                    payload["accounts"].pop(normalized, None)
                    with contextlib.suppress(OSError):
                        _atomic_write(
                            self.settings.manifest_path,
                            (
                                json.dumps(payload, ensure_ascii=False, indent=2)
                                + "\n"
                            ).encode(),
                            0o600,
                        )
                if plist_path is not None:
                    with contextlib.suppress(OSError):
                        plist_path.unlink()

            try:
                used_ports = {
                    port
                    for runtime in self._runtimes().values()
                    for port in (runtime.worker_port, runtime.cdp_port)
                }
                worker_port = _next_available_port(
                    self.settings.worker_port_start,
                    used_ports,
                )
                cdp_port = _next_available_port(
                    self.settings.cdp_port_start,
                    used_ports,
                )
                account_root = self.settings.runtime_root / "accounts" / normalized
                runtime = AccountRuntime(
                    account_id=normalized,
                    provider=normalized_provider,
                    cdp_port=cdp_port,
                    auth_dir=account_root / "auth",
                    profile_dir=account_root / "chrome-profile",
                    pid_file=account_root / "chrome.pid",
                    login_url=(
                        "https://claude.ai/login"
                        if normalized_provider == "claude"
                        else "https://chatgpt.com/"
                    ),
                    worker_service_label=(
                        f"com.turtleligpt.claude.{normalized}"
                        if normalized_provider == "claude"
                        else f"com.turtleligpt.gpt4free.{normalized}"
                    ),
                    worker_port=worker_port,
                )
                for directory in (
                    self.settings.runtime_root,
                    account_root,
                    runtime.auth_dir,
                    runtime.profile_dir,
                ):
                    _secure_directory(directory)
                launch_agents = self.settings.resolved_launch_agents_dir()
                if launch_agents.exists() and (
                    launch_agents.is_symlink() or not launch_agents.is_dir()
                ):
                    raise LoginControlError("本机 LaunchAgents 目录无效")
                launch_agents.mkdir(parents=True, exist_ok=True)
                plist_path = self._plist_path(runtime)
                _atomic_write(
                    plist_path,
                    _worker_service_definition(self.settings, runtime),
                    0o644,
                    private_parent=False,
                )
                entry = {
                    "provider": runtime.provider,
                    "cdp_port": runtime.cdp_port,
                    "auth_dir": str(
                        runtime.auth_dir.relative_to(self.settings.project_root)
                    ),
                    "profile_dir": str(
                        runtime.profile_dir.relative_to(self.settings.project_root)
                    ),
                    "pid_file": str(
                        runtime.pid_file.relative_to(self.settings.project_root)
                    ),
                    "login_url": runtime.login_url,
                    "worker_service_label": runtime.worker_service_label,
                    "worker_port": runtime.worker_port,
                }
                payload["version"] = 2
                payload["accounts"][normalized] = entry
                _atomic_write(
                    self.settings.manifest_path,
                    (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                    0o600,
                )
                manifest_added = True
                _bootstrap_worker_agent(
                    self.settings,
                    plist_path,
                    runtime.worker_service_label,
                )
                if not _worker_ready(runtime.worker_port, timeout_seconds=30.0):
                    raise LoginControlError("账号 worker 未能及时启动")
            except LoginControlError:
                rollback_partial_provision()
                raise
            except OSError as exc:
                rollback_partial_provision()
                raise LoginControlError("账号隔离运行环境创建失败") from exc
            self._record(normalized, "runtime_provisioned")
            return {
                **self._runtime_status(runtime),
                "worker_port": runtime.worker_port,
            }

    def rollback_provision(self, account_id: str) -> dict[str, Any]:
        """Rollback only an unused runtime created during a failed onboarding."""

        normalized = str(account_id or "")
        if not ACCOUNT_ID_RE.fullmatch(normalized) or normalized == "legacy-primary":
            raise LoginControlError("账号运行时不能回滚")
        with self._lock:
            runtime = self._runtimes().get(normalized)
            if runtime is None:
                return {"account_id": normalized, "configured": False}
            if _credential_state(runtime.auth_dir) == "stored":
                raise LoginControlError("账号已经保存登录状态，不能自动回滚")
            plist_path = self._plist_path(runtime)
            _bootout_worker_agent(
                self.settings,
                plist_path,
                runtime.worker_service_label,
            )
            payload = _read_manifest_payload(self.settings)
            payload["accounts"].pop(normalized, None)
            _atomic_write(
                self.settings.manifest_path,
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                0o600,
            )
            with contextlib.suppress(OSError):
                plist_path.unlink()
            self._record(normalized, "runtime_rolled_back")
            return {"account_id": normalized, "configured": False}

    def status(self, account_id: str) -> dict[str, Any]:
        runtime = self._runtime(account_id)
        return self._runtime_status(runtime)

    def open(self, account_id: str) -> dict[str, Any]:
        runtime = self._runtime(account_id)
        state = _dedicated_chrome_state(
            runtime.cdp_port,
            runtime.profile_dir,
            runtime.pid_file,
        )
        if state == "occupied":
            raise LoginControlError("登录端口已被其他浏览器占用")
        if state == "starting":
            raise LoginControlError("登录浏览器仍在启动，请稍后重试")
        if self.settings.login_mode == "remote_browser":
            for other_id, other in self._runtimes().items():
                if other_id == account_id:
                    continue
                other_state = _dedicated_chrome_state(
                    other.cdp_port,
                    other.profile_dir,
                    other.pid_file,
                )
                if other_state != "stopped":
                    raise LoginControlError("已有其他账号正在使用服务器登录窗口")
        if runtime.provider == "claude":
            if state == "ready":
                _run_chrome(runtime, "--stop")
            _run_chrome(runtime, manual=True)
        else:
            if state == "manual":
                raise LoginControlError("登录浏览器运行方式异常，请先取消登录")
            _run_chrome(runtime)
        self._record(account_id, "login_opened")
        result = self.status(account_id)
        if self.settings.login_mode == "remote_browser":
            store = self.settings.remote_browser_session_store
            public_url = str(self.settings.remote_browser_public_url or "")
            if store is None or not public_url:
                raise LoginControlError("服务器登录服务尚未配置")
            try:
                token, expires_at = issue_pending_session(
                    store,
                    account_id=account_id,
                    ttl_seconds=self.settings.remote_browser_ttl_seconds,
                )
            except RemoteBrowserSessionError as exc:
                raise LoginControlError(str(exc)) from exc
            result["login_session_url"] = f"{public_url}#token={token}"
            result["login_session_expires_at"] = expires_at
        return result

    def cancel(self, account_id: str) -> dict[str, Any]:
        runtime = self._runtime(account_id)
        _run_chrome(runtime, "--stop")
        self._revoke_remote_sessions(account_id)
        self._record(account_id, "login_cancelled")
        return self.status(account_id)

    def prepare_capture(self, account_id: str) -> dict[str, Any]:
        """Switch a manual Claude window to CDP using the same browser profile."""

        runtime = self._runtime(account_id)
        state = _dedicated_chrome_state(
            runtime.cdp_port,
            runtime.profile_dir,
            runtime.pid_file,
        )
        if runtime.provider != "claude":
            if state != "ready":
                raise LoginControlError("专用登录浏览器未处于等待验证状态")
            return self.status(account_id)
        if state == "ready":
            return self.status(account_id)
        if state != "manual":
            raise LoginControlError("Claude 手工登录窗口未打开，请先发起重新登录")
        _run_chrome(runtime, "--stop")
        _run_chrome(runtime)
        result = self.status(account_id)
        if result.get("browser_state") != "ready":
            raise LoginControlError("Claude 登录窗口未能切换到安全捕获状态")
        self._record(account_id, "capture_ready")
        return self.status(account_id)

    def restart_worker(self, account_id: str) -> dict[str, Any]:
        runtime = self._runtime(account_id)
        if self.status(account_id)["browser_state"] != "ready":
            raise LoginControlError("专用登录浏览器未处于等待验证状态")
        _run_chrome(runtime, "--stop")
        self._revoke_remote_sessions(account_id)
        if self.settings.service_manager == "launchd":
            command = [
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/{runtime.worker_service_label}",
            ]
        elif self.settings.service_manager == "systemd-user":
            command = [
                "systemctl",
                "--user",
                "restart",
                f"{runtime.worker_service_label}.service",
            ]
        else:
            raise LoginControlError("账号 worker 服务管理方式无效")
        _service_command(command, "账号 worker 重启失败")
        if not _worker_ready(runtime.worker_port):
            raise LoginControlError("账号 worker 重启后未及时就绪")
        self._record(account_id, "worker_restarted")
        return self.status(account_id)

    def _record(self, account_id: str, action: str) -> None:
        with self._lock:
            self._events[account_id] = {
                "last_action": action,
                "updated_at": int(time.time()),
            }

    def _revoke_remote_sessions(self, account_id: str) -> None:
        store = self.settings.remote_browser_session_store
        if self.settings.login_mode != "remote_browser" or store is None:
            return
        try:
            revoke_account_sessions(store, account_id=account_id)
        except RemoteBrowserSessionError as exc:
            raise LoginControlError(str(exc)) from exc


def create_login_control_app(settings: ControlSettings | None = None) -> FastAPI:
    resolved = settings or ControlSettings.from_env()
    service = LoginControlService(resolved)
    application = FastAPI(title="Turtle Login Control", version="0.2.0")
    application.state.login_control = service

    async def require_secret(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        expected = _read_secret(resolved.secret_path, enforce_private_mode=True)
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid control credential",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def run(action, account_id: str):
        try:
            return action(account_id)
        except LoginRuntimeMissing as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except LoginControlError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @application.get("/healthz", dependencies=[Depends(require_secret)])
    async def health() -> dict[str, Any]:
        return {"ok": True, "runtime_count": len(service._runtimes())}

    @application.get(
        "/v1/accounts/{account_id}/status",
        dependencies=[Depends(require_secret)],
    )
    async def account_status(account_id: str, request: Request):
        return await asyncio.to_thread(run, request.app.state.login_control.status, account_id)

    @application.post(
        "/v1/accounts/{account_id}/provision",
        dependencies=[Depends(require_secret)],
    )
    async def provision_account(account_id: str, request: Request):
        provider = "gpt"
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                provider = str(payload.get("provider") or "gpt")
        except (ValueError, RuntimeError):
            pass
        return await asyncio.to_thread(
            lambda: run(
                lambda target: request.app.state.login_control.provision(
                    target,
                    provider=provider,
                ),
                account_id,
            )
        )

    @application.post(
        "/v1/accounts/{account_id}/rollback-provision",
        dependencies=[Depends(require_secret)],
    )
    async def rollback_account_provision(account_id: str, request: Request):
        return await asyncio.to_thread(
            run,
            request.app.state.login_control.rollback_provision,
            account_id,
        )

    @application.post(
        "/v1/accounts/{account_id}/open",
        dependencies=[Depends(require_secret)],
    )
    async def open_login(account_id: str, request: Request):
        return await asyncio.to_thread(run, request.app.state.login_control.open, account_id)

    @application.post(
        "/v1/accounts/{account_id}/cancel",
        dependencies=[Depends(require_secret)],
    )
    async def cancel_login(account_id: str, request: Request):
        return await asyncio.to_thread(run, request.app.state.login_control.cancel, account_id)

    @application.post(
        "/v1/accounts/{account_id}/capture",
        dependencies=[Depends(require_secret)],
    )
    async def prepare_capture(account_id: str, request: Request):
        return await asyncio.to_thread(
            run,
            request.app.state.login_control.prepare_capture,
            account_id,
        )

    @application.post(
        "/v1/accounts/{account_id}/restart",
        dependencies=[Depends(require_secret)],
    )
    async def restart_worker(account_id: str, request: Request):
        return await asyncio.to_thread(
            run,
            request.app.state.login_control.restart_worker,
            account_id,
        )

    return application


class LoginControlClient:
    """Gateway-side client; all responses remain sanitized account metadata."""

    def __init__(
        self,
        *,
        base_url: str | None,
        secret_path: str | Path | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.enabled = bool(str(base_url or "").strip() and str(secret_path or "").strip())
        self.secret_path = Path(str(secret_path or "")) if secret_path else None
        self._client: httpx.AsyncClient | None = None
        if not self.enabled:
            return
        parsed = urlsplit(str(base_url).strip().rstrip("/"))
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("GATEWAY_LOGIN_CONTROL_URL must target the approved host control service")
        self._client = httpx.AsyncClient(
            base_url=parsed.geturl().rstrip("/"),
            timeout=httpx.Timeout(20.0, connect=3.0),
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        account_id: str,
        action: str,
        *,
        timeout_seconds: float = 20.0,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled or self._client is None or self.secret_path is None:
            raise LoginControlUnavailable("登录控制服务尚未配置")
        if not ACCOUNT_ID_RE.fullmatch(str(account_id or "")):
            raise LoginRuntimeMissing("账号没有可用的独立登录运行时")
        secret = _read_secret(self.secret_path, enforce_private_mode=False)
        try:
            response = await self._client.request(
                method,
                f"/v1/accounts/{quote(account_id, safe='')}/{action}",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=timeout_seconds,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise LoginControlUnavailable("登录控制服务暂时不可达") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 404:
            raise LoginRuntimeMissing("账号没有可用的独立登录运行时")
        if not response.is_success:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise LoginControlError(str(detail or "登录控制操作失败"))
        if not isinstance(payload, dict):
            raise LoginControlUnavailable("登录控制服务返回了无效状态")
        login_mode = str(payload.get("login_mode") or "local_window").strip()
        if login_mode not in {"local_window", "remote_browser"}:
            raise LoginControlUnavailable("登录控制服务返回了无效登录方式")
        payload["login_mode"] = login_mode
        session_url = payload.pop("login_session_url", None)
        session_expires_at = payload.pop("login_session_expires_at", None)
        if action == "open" and login_mode == "remote_browser":
            parsed_session_url = urlsplit(str(session_url or ""))
            if (
                parsed_session_url.scheme != "https"
                or not parsed_session_url.hostname
                or parsed_session_url.username
                or parsed_session_url.password
                or parsed_session_url.query
                or not re.fullmatch(
                    r"token=[A-Za-z0-9_-]{32,256}",
                    parsed_session_url.fragment,
                )
            ):
                raise LoginControlUnavailable("服务器登录服务没有返回安全链接")
            try:
                expires_at = int(session_expires_at)
            except (TypeError, ValueError) as exc:
                raise LoginControlUnavailable("服务器登录链接缺少有效期") from exc
            now = int(time.time())
            if not now < expires_at <= now + 900:
                raise LoginControlUnavailable("服务器登录链接有效期无效")
            payload["login_session_url"] = parsed_session_url.geturl()
            payload["login_session_expires_at"] = expires_at
        return payload

    async def status(self, account_id: str) -> dict[str, Any]:
        if not self.enabled:
            return {
                "account_id": account_id,
                "configured": False,
                "control_state": "disabled",
            }
        try:
            return await self._request("GET", account_id, "status", timeout_seconds=3.0)
        except LoginRuntimeMissing:
            return {
                "account_id": account_id,
                "configured": False,
                "control_state": "not_configured",
            }
        except LoginControlError:
            return {
                "account_id": account_id,
                "configured": False,
                "control_state": "unavailable",
            }

    async def open(self, account_id: str) -> dict[str, Any]:
        return await self._request("POST", account_id, "open", timeout_seconds=50.0)

    async def provision(
        self,
        account_id: str,
        provider: str = "gpt",
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            account_id,
            "provision",
            timeout_seconds=45.0,
            payload={"provider": str(provider)},
        )

    async def rollback_provision(self, account_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            account_id,
            "rollback-provision",
            timeout_seconds=20.0,
        )

    async def cancel(self, account_id: str) -> dict[str, Any]:
        return await self._request("POST", account_id, "cancel", timeout_seconds=15.0)

    async def prepare_capture(self, account_id: str) -> dict[str, Any]:
        return await self._request("POST", account_id, "capture", timeout_seconds=60.0)

    async def restart(self, account_id: str) -> dict[str, Any]:
        return await self._request("POST", account_id, "restart", timeout_seconds=45.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Turtle's loopback login-control service")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    settings = ControlSettings.from_env()
    if args.host:
        if args.host not in {"127.0.0.1", "::1"}:
            raise SystemExit("--host must remain loopback-only")
        settings = replace(settings, bind_host=args.host)
    if args.port:
        if not 1 <= args.port <= 65535:
            raise SystemExit("--port must be between 1 and 65535")
        settings = replace(settings, port=args.port)
    import uvicorn

    uvicorn.run(
        create_login_control_app(settings),
        host=settings.bind_host,
        port=settings.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
