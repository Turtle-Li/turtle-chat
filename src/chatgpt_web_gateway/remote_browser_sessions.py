"""Short-lived, one-time capabilities for the self-hosted login browser.

Only SHA-256 digests of URL and cookie capabilities are stored on disk.  The
raw values exist briefly in the administrator's browser and process memory,
never in PostgreSQL, the account runtime manifest, or application logs.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator


STORE_VERSION = 1


class RemoteBrowserSessionError(RuntimeError):
    """A sanitized session error safe to expose as an HTTP status."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "pending": {}, "connections": {}}


def _private_store_path(path: Path) -> None:
    if not path.exists():
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RemoteBrowserSessionError("服务器登录会话存储无效")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RemoteBrowserSessionError("服务器登录会话存储权限无效")


def _load_store(path: Path) -> dict[str, Any]:
    _private_store_path(path)
    if not path.exists():
        return _empty_store()
    try:
        if path.stat().st_size > 256 * 1024:
            raise RemoteBrowserSessionError("服务器登录会话存储过大")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RemoteBrowserSessionError("服务器登录会话存储无效") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != STORE_VERSION
        or not isinstance(payload.get("pending"), dict)
        or not isinstance(payload.get("connections"), dict)
    ):
        raise RemoteBrowserSessionError("服务器登录会话存储无效")
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _prune(payload: dict[str, Any], now: int) -> None:
    for section in ("pending", "connections"):
        current = payload[section]
        expired = [
            key
            for key, value in current.items()
            if not isinstance(value, dict) or int(value.get("expires_at") or 0) <= now
        ]
        for key in expired:
            current.pop(key, None)


@contextlib.contextmanager
def _locked_store(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            payload = _load_store(path)
            _prune(payload, int(time.time()))
            yield payload
            _atomic_write(path, payload)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def issue_pending_session(
    path: Path,
    *,
    account_id: str,
    ttl_seconds: int,
) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + max(30, min(int(ttl_seconds), 900))
    raw = secrets.token_urlsafe(48)
    with _locked_store(path) as payload:
        for section in ("pending", "connections"):
            for key, entry in list(payload[section].items()):
                if isinstance(entry, dict) and entry.get("account_id") == account_id:
                    payload[section].pop(key, None)
        payload["pending"][_digest(raw)] = {
            "account_id": account_id,
            "expires_at": expires_at,
        }
    return raw, expires_at


def exchange_pending_session(
    path: Path,
    *,
    raw_token: str,
) -> tuple[str, str, int]:
    token = str(raw_token or "")
    if not 32 <= len(token) <= 256 or any(character.isspace() for character in token):
        raise RemoteBrowserSessionError("服务器登录链接无效或已使用")
    now = int(time.time())
    with _locked_store(path) as payload:
        entry = payload["pending"].pop(_digest(token), None)
        if not isinstance(entry, dict) or int(entry.get("expires_at") or 0) <= now:
            raise RemoteBrowserSessionError("服务器登录链接无效或已使用")
        account_id = str(entry.get("account_id") or "")
        expires_at = int(entry["expires_at"])
        connection = secrets.token_urlsafe(48)
        payload["connections"][_digest(connection)] = {
            "account_id": account_id,
            "expires_at": expires_at,
        }
    return connection, account_id, expires_at


def consume_connection(path: Path, *, raw_cookie: str) -> tuple[str, int]:
    value = str(raw_cookie or "")
    if not 32 <= len(value) <= 256 or any(character.isspace() for character in value):
        raise RemoteBrowserSessionError("服务器登录会话无效或已使用")
    now = int(time.time())
    with _locked_store(path) as payload:
        entry = payload["connections"].pop(_digest(value), None)
        if not isinstance(entry, dict) or int(entry.get("expires_at") or 0) <= now:
            raise RemoteBrowserSessionError("服务器登录会话无效或已使用")
        return str(entry.get("account_id") or ""), int(entry["expires_at"])


def revoke_account_sessions(path: Path, *, account_id: str) -> None:
    with _locked_store(path) as payload:
        for section in ("pending", "connections"):
            for key, entry in list(payload[section].items()):
                if isinstance(entry, dict) and entry.get("account_id") == account_id:
                    payload[section].pop(key, None)
