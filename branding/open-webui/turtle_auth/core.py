"""Durable, redacted authentication-security configuration.

The active deployment keeps this state in Turtle's dedicated PostgreSQL
database even though Open WebUI's generic persistent configuration is disabled.
Turnstile secrets are encrypted before persistence and are never returned by
the public or administrator APIs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..turtle_database import connect_postgres, is_postgres_url, runtime_database_url


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 2,
    "registration_enabled": True,
    "maintenance": {
        "enabled": False,
        "message": "系统正在维护，请稍后再试。",
    },
    "turnstile": {
        "enabled": False,
        "site_key": "",
        "secret_key_ciphertext": "",
    },
}

_TURNSTILE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AuthSecurityConfigurationError(ValueError):
    """Raised when the registration security configuration is invalid."""


def _merge_defaults(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(value, dict):
        return result
    if "registration_enabled" in value:
        result["registration_enabled"] = bool(value["registration_enabled"])
    maintenance = value.get("maintenance")
    if isinstance(maintenance, dict):
        if "enabled" in maintenance:
            result["maintenance"]["enabled"] = bool(maintenance["enabled"])
        if "message" in maintenance:
            message = str(maintenance["message"] or "").strip()
            if message:
                result["maintenance"]["message"] = message[:800]
    turnstile = value.get("turnstile")
    if isinstance(turnstile, dict):
        for key in ("enabled", "site_key", "secret_key_ciphertext"):
            if key in turnstile:
                result["turnstile"][key] = turnstile[key]
    return result


def _validated_key(value: str, *, label: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) > maximum or not _TURNSTILE_KEY_RE.fullmatch(normalized):
        raise AuthSecurityConfigurationError(f"{label} 格式无效")
    return normalized


def validate_turnstile_secret_key(value: str) -> str:
    """Validate a candidate secret without ever including it in an error."""

    return _validated_key(
        value,
        label="Turnstile Secret Key",
        maximum=512,
    )


class AuthSecurityStore:
    def __init__(
        self,
        path: str | Path | None = None,
        master_secret: str | None = None,
        *,
        database_url: str | None = None,
    ):
        resolved_url = str(database_url or runtime_database_url()).strip()
        self.database_url = resolved_url if path is None and is_postgres_url(resolved_url) else ""
        self.backend = "postgresql" if self.database_url else "file"
        self.path = (
            None
            if self.database_url
            else Path(
                path
                or os.getenv(
                    "TURTLE_AUTH_SECURITY_CONFIG_PATH",
                    "/app/backend/data/turtle-auth-security.json",
                )
            )
        )
        self._master_secret = master_secret
        self._lock = threading.RLock()
        if self.database_url:
            self._initialize_database()

    def _connect(self):
        return connect_postgres(self.database_url)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS turtle_auth_config (
                    id          SMALLINT PRIMARY KEY CHECK (id = 1),
                    payload     JSONB NOT NULL,
                    updated_at  BIGINT NOT NULL
                );
                """
            )

    def _fernet(self) -> Fernet:
        secret = (
            self._master_secret
            or os.getenv("TURTLE_AUTH_MASTER_KEY")
            or os.getenv("TURTLE_STORAGE_MASTER_KEY")
            or os.getenv("WEBUI_SECRET_KEY")
        )
        if not secret:
            raise AuthSecurityConfigurationError(
                "缺少稳定的 Turtle 或 WebUI 主密钥，无法加密 Turnstile Secret Key"
            )
        derived = hashlib.sha256(f"turtle-auth-v1:{secret}".encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise AuthSecurityConfigurationError(
                "Turnstile Secret Key 无法解密，请由管理员重新填写"
            ) from exc

    def _load_postgres(self, connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload FROM turtle_auth_config WHERE id = 1"
        ).fetchone()
        if row is None:
            return copy.deepcopy(DEFAULT_CONFIG)
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise AuthSecurityConfigurationError("数据库中的认证安全配置损坏") from exc
        return _merge_defaults(payload)

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.database_url:
                with self._connect() as connection:
                    return self._load_postgres(connection)
            assert self.path is not None
            if not self.path.exists():
                return copy.deepcopy(DEFAULT_CONFIG)
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AuthSecurityConfigurationError("认证安全配置文件损坏或不可读取") from exc
            return _merge_defaults(payload)

    def _write(self, payload: dict[str, Any], *, connection=None) -> None:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if self.database_url:
            if connection is None:
                with self._connect() as owned:
                    self._write(payload, connection=owned)
                return
            connection.execute(
                """
                INSERT INTO turtle_auth_config (id, payload, updated_at)
                VALUES (1, ?::jsonb, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (serialized, int(time.time())),
            )
            return

        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".turtle-auth-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @contextmanager
    def _edit_config(self):
        with self._lock:
            if self.database_url:
                with self._connect() as connection:
                    connection.begin(lock_key="turtle-auth-config")
                    try:
                        config = self._load_postgres(connection)
                        yield config
                        self._write(config, connection=connection)
                        connection.execute("COMMIT")
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
                return
            config = self.load()
            yield config
            self._write(config)

    def public(self, *, admin: bool = False) -> dict[str, Any]:
        config = self.load()
        turnstile = config["turnstile"]
        result = {
            "registration_enabled": bool(config["registration_enabled"]),
            "maintenance_enabled": bool(config["maintenance"]["enabled"]),
            "maintenance_message": str(config["maintenance"]["message"] or ""),
            "turnstile_enabled": bool(turnstile["enabled"]),
            "turnstile_site_key": str(turnstile["site_key"] or ""),
        }
        if admin:
            result["turnstile_secret_key_configured"] = bool(
                turnstile["secret_key_ciphertext"]
            )
            result["storage_backend"] = self.backend
        return result

    def turnstile_secret(self, config: dict[str, Any] | None = None) -> str:
        value = (config or self.load())["turnstile"].get("secret_key_ciphertext", "")
        return self.decrypt(str(value or ""))

    def update_admin(self, update: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(update, dict):
            raise AuthSecurityConfigurationError("配置格式无效")
        with self._edit_config() as config:
            if "registration_enabled" in update:
                config["registration_enabled"] = bool(update["registration_enabled"])

            maintenance = config["maintenance"]
            if "maintenance_message" in update:
                message = str(update["maintenance_message"] or "").strip()
                if not message:
                    raise AuthSecurityConfigurationError("请填写维护提示信息")
                if len(message) > 800:
                    raise AuthSecurityConfigurationError("维护提示信息不能超过 800 个字符")
                maintenance["message"] = message
            if "maintenance_enabled" in update:
                maintenance["enabled"] = bool(update["maintenance_enabled"])

            turnstile = config["turnstile"]
            if "turnstile_site_key" in update:
                turnstile["site_key"] = _validated_key(
                    update["turnstile_site_key"],
                    label="Turnstile Site Key",
                    maximum=256,
                )
            if str(update.get("turnstile_secret_key") or "").strip():
                secret = validate_turnstile_secret_key(
                    update["turnstile_secret_key"]
                )
                turnstile["secret_key_ciphertext"] = self.encrypt(secret)
            if update.get("clear_turnstile_secret"):
                turnstile["secret_key_ciphertext"] = ""
            if "turnstile_enabled" in update:
                turnstile["enabled"] = bool(update["turnstile_enabled"])

            if turnstile["enabled"] and not turnstile["site_key"]:
                raise AuthSecurityConfigurationError(
                    "启用 Turnstile 前必须填写 Site Key"
                )
            if turnstile["enabled"] and not turnstile["secret_key_ciphertext"]:
                raise AuthSecurityConfigurationError(
                    "启用 Turnstile 前必须填写 Secret Key"
                )
        return self.public(admin=True)

    def set_registration_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._edit_config() as config:
            config["registration_enabled"] = bool(enabled)
        return self.public(admin=True)


AUTH_SECURITY = AuthSecurityStore()
