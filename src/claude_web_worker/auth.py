from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable


COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class AuthError(ValueError):
    """Raised when the private Claude browser session cannot be used safely."""


@dataclass(frozen=True, slots=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    secure: bool = True
    http_only: bool = False


@dataclass(frozen=True, slots=True)
class AuthSession:
    cookies: tuple[BrowserCookie, ...]
    captured_at: int
    organization_uuid: str | None = None

    @property
    def session_key(self) -> str:
        for cookie in self.cookies:
            if cookie.name == "sessionKey":
                return cookie.value
        return ""

    @property
    def device_id(self) -> str:
        for cookie in self.cookies:
            if cookie.name == "anthropic-device-id" and cookie.value:
                return cookie.value
        digest = hashlib.sha256(b"turtle-claude-device\0" + self.session_key.encode()).digest()
        value = bytearray(digest[:16])
        value[6] = (value[6] & 0x0F) | 0x40
        value[8] = (value[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(value)))

    def with_organization(self, organization_uuid: str) -> "AuthSession":
        value = str(organization_uuid or "").strip()
        if not value:
            raise AuthError("Claude organization is missing")
        return replace(self, organization_uuid=value)

    def cookie_header(self) -> str:
        now = time.time()
        values: list[str] = []
        for cookie in self.cookies:
            if cookie.expires and cookie.expires > 0 and cookie.expires <= now:
                continue
            if not COOKIE_NAME.fullmatch(cookie.name):
                continue
            if "\r" in cookie.value or "\n" in cookie.value:
                continue
            values.append(f"{cookie.name}={cookie.value}")
        if not any(value.startswith("sessionKey=") for value in values):
            raise AuthError("Claude login is missing or expired")
        return "; ".join(values)


def _is_claude_domain(domain: str) -> bool:
    normalized = str(domain or "").strip().lower().lstrip(".")
    return normalized == "claude.ai" or normalized.endswith(".claude.ai")


def _cookie_from_mapping(value: dict[str, Any]) -> BrowserCookie | None:
    name = str(value.get("name") or "").strip()
    raw_value = value.get("value")
    domain = str(value.get("domain") or "").strip().lower()
    if not name or not isinstance(raw_value, str) or not _is_claude_domain(domain):
        return None
    if not COOKIE_NAME.fullmatch(name) or "\r" in raw_value or "\n" in raw_value:
        return None
    raw_expires = value.get("expires")
    try:
        expires = float(raw_expires) if raw_expires not in (None, "") else None
    except (TypeError, ValueError):
        expires = None
    return BrowserCookie(
        name=name,
        value=raw_value,
        domain=domain,
        path=str(value.get("path") or "/"),
        expires=expires,
        secure=bool(value.get("secure", True)),
        http_only=bool(value.get("httpOnly", value.get("http_only", False))),
    )


def session_from_cdp_cookies(values: Iterable[dict[str, Any]]) -> AuthSession:
    by_identity: dict[tuple[str, str, str], BrowserCookie] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        cookie = _cookie_from_mapping(value)
        if cookie is not None:
            by_identity[(cookie.domain, cookie.path, cookie.name)] = cookie
    cookies = tuple(sorted(by_identity.values(), key=lambda item: (item.name, item.domain, item.path)))
    session = AuthSession(cookies=cookies, captured_at=int(time.time()))
    if not session.session_key:
        raise AuthError("Claude login has not completed in the dedicated browser")
    session.cookie_header()
    return session


def _reject_symlinks(path: Path) -> None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_symlink():
        raise AuthError("Claude authentication paths must not use symbolic links")
    if not path.exists():
        return
    if path.is_symlink():
        raise AuthError("Claude authentication paths must not use symbolic links")
    if path.is_dir():
        for root, directories, files in os.walk(path, followlinks=False):
            root_path = Path(root)
            for name in (*directories, *files):
                if (root_path / name).is_symlink():
                    raise AuthError("Claude authentication paths must not contain symbolic links")


def secure_auth_directory(directory: Path) -> None:
    directory = Path(os.path.abspath(Path(directory).expanduser()))
    _reject_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        os.chmod(root_path, 0o700)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise AuthError("Claude authentication paths must not contain symbolic links")
            os.chmod(child, 0o700)
        for name in files:
            child = root_path / name
            if child.is_symlink():
                raise AuthError("Claude authentication paths must not contain symbolic links")
            os.chmod(child, 0o600)


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    secure_auth_directory(path.parent)
    if path.exists() and path.is_symlink():
        raise AuthError("Claude authentication files must not be symbolic links")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def save_auth_session(path: Path, session: AuthSession) -> None:
    if not session.session_key:
        raise AuthError("Claude login is missing a session key")
    session.cookie_header()
    _atomic_json_write(
        Path(path),
        {
            "version": 1,
            "captured_at": int(session.captured_at),
            "organization_uuid": session.organization_uuid,
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                    "secure": cookie.secure,
                    "http_only": cookie.http_only,
                }
                for cookie in session.cookies
            ],
        },
    )


def load_auth_session(path: Path) -> AuthSession:
    path = Path(os.path.abspath(Path(path).expanduser()))
    secure_auth_directory(path.parent)
    if not path.is_file():
        raise AuthError("Claude login is required")
    if path.is_symlink():
        raise AuthError("Claude authentication files must not be symbolic links")
    os.chmod(path, 0o600)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError("Claude authentication file is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise AuthError("Claude authentication file has an unsupported format")
    raw_cookies = payload.get("cookies")
    if not isinstance(raw_cookies, list):
        raise AuthError("Claude authentication file is invalid")
    cookies = tuple(
        cookie
        for value in raw_cookies
        if isinstance(value, dict)
        and (cookie := _cookie_from_mapping(value)) is not None
    )
    try:
        captured_at = int(payload.get("captured_at") or 0)
    except (TypeError, ValueError) as exc:
        raise AuthError("Claude authentication timestamp is invalid") from exc
    organization = str(payload.get("organization_uuid") or "").strip() or None
    session = AuthSession(
        cookies=cookies,
        captured_at=captured_at,
        organization_uuid=organization,
    )
    if not session.session_key:
        raise AuthError("Claude login is missing or expired")
    session.cookie_header()
    return session


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Write non-secret Claude runtime metadata beside the private session."""

    _atomic_json_write(Path(path), payload)
