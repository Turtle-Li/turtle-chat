"""Configuration and policy primitives for Turtle's Chat media storage.

Production keeps shared configuration and normalized per-user quota assignments
in PostgreSQL. Explicit file paths retain the encrypted JSON backend for unit
tests, rollback, and one-time migration input. COS credentials remain Fernet-
encrypted and are never returned by public/admin API responses.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from ..turtle_database import connect_postgres, is_postgres_url, runtime_database_url


GIB = 1024**3
MIB = 1024**2
MAX_QUOTA_BYTES = 20 * 1024**4
MAIN_OBJECT_NAMESPACE = "files"
THUMBNAIL_OBJECT_NAMESPACE = "thumbnails"
USER_OBJECT_NAMESPACE = "users"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "provider": "local",
    "cos": {
        "endpoint_url": "",
        "region": "",
        "bucket": "",
        "prefix": "turtle-gpt",
        "addressing_style": "virtual",
        "direct_upload_enabled": True,
        "put_ttl_seconds": 900,
        "get_ttl_seconds": 900,
        "access_key_id_ciphertext": "",
        "secret_access_key_ciphertext": "",
    },
    "cdn": {
        "enabled": False,
        "files_base_url": "https://files.chat.totools.cn",
        "images_base_url": "https://img.chat.totools.cn",
        "auth_type": "type_a",
        "auth_parameter": "sign",
        "auth_ttl_seconds": 900,
        "files_auth_key_ciphertext": "",
        "images_auth_key_ciphertext": "",
    },
    "media": {
        "max_image_dimension": 2048,
        "image_quality": 0.82,
        "image_format": "image/webp",
        "max_image_bytes": 20 * MIB,
        "max_video_bytes": 500 * MIB,
        "max_file_bytes": 200 * MIB,
    },
    "quota": {
        "default_bytes": 2 * GIB,
        "tiers": {
            "free": 2 * GIB,
            "friend": 10 * GIB,
            "pro": 50 * GIB,
        },
        "users": {},
    },
}


class StorageConfigurationError(ValueError):
    """Raised for invalid or unusable storage configuration."""


def _merge_defaults(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if key not in result:
            continue
        if isinstance(result[key], dict) and isinstance(item, dict):
            result[key] = _merge_defaults(result[key], item)
        else:
            result[key] = item
    return result


def safe_component(value: str, fallback: str = "item") -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return normalized[:180] or fallback


def safe_filename(value: str) -> str:
    name = Path(str(value or "file")).name
    stem = safe_component(Path(name).stem, "file")
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(name).suffix.lower())[:16]
    return f"{stem}{suffix}"[:200]


def object_key(prefix: str, user_id: str, file_id: str, filename: str) -> str:
    parts = [safe_component(part) for part in str(prefix or "").split("/") if part.strip()]
    parts.extend(
        [
            MAIN_OBJECT_NAMESPACE,
            USER_OBJECT_NAMESPACE,
            safe_component(user_id, "unknown-user"),
            f"{safe_component(file_id, 'file')}_{safe_filename(filename)}",
        ]
    )
    return "/".join(parts)


def _bounded_int(value: Any, minimum: int, maximum: int, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StorageConfigurationError(f"{field} 必须是整数") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise StorageConfigurationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _bounded_float(value: Any, minimum: float, maximum: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise StorageConfigurationError(f"{field} 必须是数字") from exc
    if parsed < minimum or parsed > maximum:
        raise StorageConfigurationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


class ConfigStore:
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
                    "TURTLE_STORAGE_CONFIG_PATH",
                    "/app/backend/data/turtle-storage.json",
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
                CREATE TABLE IF NOT EXISTS turtle_storage_config (
                    id          SMALLINT PRIMARY KEY CHECK (id = 1),
                    payload     JSONB NOT NULL,
                    updated_at  BIGINT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turtle_storage_user_quota (
                    user_id      TEXT PRIMARY KEY,
                    tier         TEXT NOT NULL,
                    quota_bytes  BIGINT,
                    updated_at   BIGINT NOT NULL,
                    CHECK (quota_bytes IS NULL OR quota_bytes >= 0)
                );
                CREATE INDEX IF NOT EXISTS turtle_storage_user_quota_tier_idx
                    ON turtle_storage_user_quota (tier);
                """
            )
            # A dedicated integration-test schema intentionally contains only
            # Turtle's storage tables. Create the optional Open WebUI indexes
            # only when the corresponding application table is visible on the
            # active PostgreSQL search_path.
            optional_indexes = (
                (
                    "file",
                    "CREATE INDEX IF NOT EXISTS turtle_file_user_created_idx "
                    "ON file (user_id, created_at DESC, id DESC)",
                ),
                (
                    "file",
                    "CREATE INDEX IF NOT EXISTS turtle_file_user_kind_created_idx "
                    "ON file (user_id, ((meta ->> 'content_type')), created_at DESC, id DESC)",
                ),
                (
                    "chat",
                    "CREATE INDEX IF NOT EXISTS turtle_chat_user_archived_updated_idx "
                    "ON chat (user_id, archived, updated_at DESC)",
                ),
            )
            for table, statement in optional_indexes:
                if connection.execute("SELECT to_regclass(?)", (table,)).fetchone()[0] is not None:
                    connection.execute(statement)

    def _fernet(self) -> Fernet:
        secret = (
            self._master_secret
            or os.getenv("TURTLE_STORAGE_MASTER_KEY")
            or os.getenv("WEBUI_SECRET_KEY")
        )
        if not secret:
            raise StorageConfigurationError(
                "缺少 TURTLE_STORAGE_MASTER_KEY 或 WEBUI_SECRET_KEY，无法加密存储密钥"
            )
        derived = hashlib.sha256(f"turtle-storage-v1:{secret}".encode("utf-8")).digest()
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
            raise StorageConfigurationError("存储密钥无法解密，请由管理员重新填写") from exc

    @staticmethod
    def _merged_payload(payload: Any, *, include_users: bool) -> dict[str, Any]:
        merged = _merge_defaults(DEFAULT_CONFIG, payload)
        raw_quota = payload.get("quota", {}) if isinstance(payload, dict) else {}
        if isinstance(raw_quota.get("tiers"), dict):
            merged["quota"]["tiers"] = copy.deepcopy(raw_quota["tiers"])
        merged["quota"]["users"] = (
            copy.deepcopy(raw_quota.get("users", {}))
            if include_users and isinstance(raw_quota.get("users"), dict)
            else {}
        )
        return merged

    def _load_postgres(self, connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload FROM turtle_storage_config WHERE id = 1"
        ).fetchone()
        if row is None:
            return copy.deepcopy(DEFAULT_CONFIG)
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise StorageConfigurationError("数据库中的对象存储配置损坏") from exc
        return self._merged_payload(payload, include_users=False)

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
                raise StorageConfigurationError("对象存储配置文件损坏或不可读取") from exc
            return self._merged_payload(payload, include_users=True)

    def _write(self, payload: dict[str, Any], *, connection=None) -> None:
        if self.database_url:
            persisted = copy.deepcopy(payload)
            persisted.setdefault("quota", {})["users"] = {}
            serialized = json.dumps(
                persisted,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if connection is None:
                with self._connect() as owned:
                    self._write(persisted, connection=owned)
                return
            connection.execute(
                """
                INSERT INTO turtle_storage_config (id, payload, updated_at)
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
            prefix=".turtle-storage-", suffix=".json", dir=str(self.path.parent)
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
                    connection.begin(lock_key="turtle-storage-config")
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
        cos = config["cos"]
        cdn = config["cdn"]
        result = {
            "provider": config["provider"],
            "cos": {
                "configured": self.cos_ready(config),
                "endpoint_url": cos["endpoint_url"],
                "region": cos["region"],
                "bucket": cos["bucket"],
                "prefix": cos["prefix"],
                "addressing_style": cos["addressing_style"],
                "direct_upload_enabled": bool(cos["direct_upload_enabled"]),
                "put_ttl_seconds": cos["put_ttl_seconds"],
                "get_ttl_seconds": cos["get_ttl_seconds"],
                "secret_id_configured": bool(cos["access_key_id_ciphertext"]),
                "secret_key_configured": bool(cos["secret_access_key_ciphertext"]),
            },
            "cdn": {
                "enabled": bool(cdn["enabled"]),
                "files_base_url": cdn["files_base_url"],
                "images_base_url": cdn["images_base_url"],
                "auth_type": cdn["auth_type"],
                "auth_parameter": cdn["auth_parameter"],
                "auth_ttl_seconds": cdn["auth_ttl_seconds"],
                "files_auth_key_configured": bool(cdn["files_auth_key_ciphertext"]),
                "images_auth_key_configured": bool(cdn["images_auth_key_ciphertext"]),
                "files_ready": self.cdn_ready(config, "files"),
                "images_ready": self.cdn_ready(config, "images"),
            },
            "media": copy.deepcopy(config["media"]),
        }
        if admin:
            result["quota"] = copy.deepcopy(config["quota"])
            result["quota"]["users"] = {}
        return result

    def credentials(self, config: dict[str, Any] | None = None) -> tuple[str, str]:
        config = config or self.load()
        cos = config["cos"]
        return (
            self.decrypt(cos.get("access_key_id_ciphertext", "")),
            self.decrypt(cos.get("secret_access_key_ciphertext", "")),
        )

    def cdn_auth_key(
        self,
        kind: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        if kind not in {"files", "images"}:
            raise StorageConfigurationError("未知 CDN 类型")
        config = config or self.load()
        return self.decrypt(
            config["cdn"].get(f"{kind}_auth_key_ciphertext", "")
        )

    @staticmethod
    def cos_ready(config: dict[str, Any]) -> bool:
        cos = config.get("cos", {})
        return all(
            [
                cos.get("endpoint_url"),
                cos.get("region"),
                cos.get("bucket"),
                cos.get("access_key_id_ciphertext"),
                cos.get("secret_access_key_ciphertext"),
            ]
        )

    @staticmethod
    def cdn_ready(config: dict[str, Any], kind: str) -> bool:
        if kind not in {"files", "images"}:
            return False
        cdn = config.get("cdn", {})
        return bool(
            cdn.get("enabled")
            and cdn.get("auth_type") == "type_a"
            and cdn.get("auth_parameter") == "sign"
            and cdn.get(f"{kind}_base_url")
            and cdn.get(f"{kind}_auth_key_ciphertext")
        )

    def update_admin(self, update: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(update, dict):
            raise StorageConfigurationError("配置格式无效")
        with self._edit_config() as config:
            provider = update.get("provider", config["provider"])
            if provider not in {"local", "cos"}:
                raise StorageConfigurationError("provider 只支持 local 或 cos")
            config["provider"] = provider

            incoming_cos = update.get("cos") or {}
            cos = config["cos"]
            for field in ("endpoint_url", "region", "bucket", "prefix"):
                if field in incoming_cos:
                    cos[field] = str(incoming_cos[field] or "").strip()
            if "addressing_style" in incoming_cos:
                style = str(incoming_cos["addressing_style"] or "virtual")
                if style not in {"virtual", "path"}:
                    raise StorageConfigurationError("寻址方式只支持 virtual 或 path")
                cos["addressing_style"] = style
            if "direct_upload_enabled" in incoming_cos:
                cos["direct_upload_enabled"] = bool(incoming_cos["direct_upload_enabled"])
            if "put_ttl_seconds" in incoming_cos:
                cos["put_ttl_seconds"] = _bounded_int(
                    incoming_cos["put_ttl_seconds"], 60, 3600, "PUT URL 有效期"
                )
            if "get_ttl_seconds" in incoming_cos:
                cos["get_ttl_seconds"] = _bounded_int(
                    incoming_cos["get_ttl_seconds"], 60, 86400, "GET URL 有效期"
                )

            if incoming_cos.get("clear_credentials"):
                cos["access_key_id_ciphertext"] = ""
                cos["secret_access_key_ciphertext"] = ""
            if str(incoming_cos.get("secret_id") or "").strip():
                cos["access_key_id_ciphertext"] = self.encrypt(str(incoming_cos["secret_id"]).strip())
            if str(incoming_cos.get("secret_key") or "").strip():
                cos["secret_access_key_ciphertext"] = self.encrypt(str(incoming_cos["secret_key"]).strip())

            endpoint = cos.get("endpoint_url", "")
            if endpoint:
                parsed = urlparse(endpoint)
                local_hosts = {"localhost", "127.0.0.1", "::1"}
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise StorageConfigurationError("COS Endpoint 必须是完整的 http(s) URL")
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise StorageConfigurationError("COS Endpoint 不能包含账号、密码、查询参数或片段")
                if parsed.path not in {"", "/"}:
                    raise StorageConfigurationError("COS Endpoint 只填写服务域名，不要附加对象路径")
                if parsed.scheme != "https" and parsed.hostname not in local_hosts:
                    raise StorageConfigurationError("公网 COS Endpoint 必须使用 HTTPS")
            if cos.get("bucket") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,254}", cos["bucket"]):
                raise StorageConfigurationError("Bucket 名称格式无效")

            incoming_cdn = update.get("cdn") or {}
            cdn = config["cdn"]
            for field in ("files_base_url", "images_base_url"):
                if field in incoming_cdn:
                    cdn[field] = str(incoming_cdn[field] or "").strip().rstrip("/")
            if "auth_ttl_seconds" in incoming_cdn:
                cdn["auth_ttl_seconds"] = _bounded_int(
                    incoming_cdn["auth_ttl_seconds"],
                    60,
                    86400,
                    "CDN 鉴权 URL 有效期",
                )
            if incoming_cdn.get("clear_auth_keys"):
                cdn["files_auth_key_ciphertext"] = ""
                cdn["images_auth_key_ciphertext"] = ""
            for kind in ("files", "images"):
                incoming_key = str(incoming_cdn.get(f"{kind}_auth_key") or "").strip()
                if incoming_key:
                    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", incoming_key):
                        raise StorageConfigurationError(
                            "腾讯 CDN Type A 鉴权密钥必须为 6 到 40 位字母或数字"
                        )
                    cdn[f"{kind}_auth_key_ciphertext"] = self.encrypt(incoming_key)
            if "enabled" in incoming_cdn:
                cdn["enabled"] = bool(incoming_cdn["enabled"])

            cdn_hosts: list[str] = []
            for field, label in (
                ("files_base_url", "文件 CDN"),
                ("images_base_url", "图片 CDN"),
            ):
                value = str(cdn.get(field) or "")
                if not value:
                    continue
                parsed = urlparse(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                ):
                    raise StorageConfigurationError(
                        f"{label} 必须是仅包含 HTTPS 域名的完整 URL"
                    )
                cdn_hosts.append(parsed.hostname.lower())
            if bool(cdn.get("enabled")):
                if len(cdn_hosts) != 2:
                    raise StorageConfigurationError("启用 CDN 前请完整填写文件与图片 CDN 域名")
                if cdn_hosts[0] == cdn_hosts[1]:
                    raise StorageConfigurationError("文件 CDN 与图片 CDN 必须使用不同域名")
                if not self.cdn_ready(config, "files") or not self.cdn_ready(config, "images"):
                    raise StorageConfigurationError(
                        "启用 CDN 前请分别填写两个域名的 Type A 鉴权密钥"
                    )

            incoming_media = update.get("media") or {}
            media = config["media"]
            if "max_image_dimension" in incoming_media:
                media["max_image_dimension"] = _bounded_int(
                    incoming_media["max_image_dimension"], 512, 8192, "图片最长边"
                )
            if "image_quality" in incoming_media:
                media["image_quality"] = _bounded_float(
                    incoming_media["image_quality"], 0.4, 0.98, "图片质量"
                )
            if "max_image_bytes" in incoming_media:
                media["max_image_bytes"] = _bounded_int(
                    incoming_media["max_image_bytes"], MIB, 200 * MIB, "图片大小上限"
                )
            if "max_video_bytes" in incoming_media:
                media["max_video_bytes"] = _bounded_int(
                    incoming_media["max_video_bytes"], MIB, 5 * 1024**3, "视频大小上限"
                )
            if "max_file_bytes" in incoming_media:
                media["max_file_bytes"] = _bounded_int(
                    incoming_media["max_file_bytes"], MIB, 1024**3, "文件大小上限"
                )

            incoming_quota = update.get("quota") or {}
            quota = config["quota"]
            if "default_bytes" in incoming_quota:
                quota["default_bytes"] = _bounded_int(
                    incoming_quota["default_bytes"], 0, MAX_QUOTA_BYTES, "默认空间额度"
                )
            if "tiers" in incoming_quota:
                tiers = incoming_quota["tiers"]
                if not isinstance(tiers, dict) or not tiers:
                    raise StorageConfigurationError("会员等级不能为空")
                normalized_tiers = {}
                for name, size in tiers.items():
                    tier = safe_component(str(name).lower(), "")[:32]
                    if not tier:
                        raise StorageConfigurationError("会员等级名称无效")
                    normalized_tiers[tier] = _bounded_int(size, 0, MAX_QUOTA_BYTES, f"{tier} 额度")
                quota["tiers"] = normalized_tiers

            if provider == "cos" and not self.cos_ready(config):
                raise StorageConfigurationError("启用 COS 前请完整填写 Endpoint、地域、Bucket 和两项密钥")
            if provider == "cos" and not str(cos.get("prefix") or "").strip("/"):
                raise StorageConfigurationError("启用 COS 必须设置非空对象前缀，避免影响 Bucket 其他对象")

        return self.public(admin=True)

    def quota_for_user(self, user_id: str) -> dict[str, Any]:
        config = self.load()
        quota = config["quota"]
        if self.database_url:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT tier, quota_bytes
                      FROM turtle_storage_user_quota
                     WHERE user_id = ?
                    """,
                    (str(user_id),),
                ).fetchone()
            assignment = (
                {"tier": row["tier"], "quota_bytes": row["quota_bytes"]}
                if row is not None
                else {}
            )
        else:
            assignment = quota.get("users", {}).get(user_id, {})
        tier = assignment.get("tier") or "free"
        override = assignment.get("quota_bytes")
        if isinstance(override, int):
            limit = override
        else:
            limit = quota.get("tiers", {}).get(tier, quota["default_bytes"])
        return {"tier": tier, "quota_bytes": int(limit)}

    def set_user_quota(self, user_id: str, tier: str, quota_bytes: int | None) -> dict[str, Any]:
        if self.database_url:
            with self._lock, self._connect() as connection:
                connection.begin(lock_key="turtle-storage-config")
                try:
                    config = self._load_postgres(connection)
                    tiers = config["quota"]["tiers"]
                    if tier not in tiers:
                        raise StorageConfigurationError("未知会员等级")
                    if quota_bytes is not None:
                        quota_bytes = _bounded_int(
                            quota_bytes,
                            0,
                            MAX_QUOTA_BYTES,
                            "用户空间额度",
                        )
                    connection.execute(
                        """
                        INSERT INTO turtle_storage_user_quota
                            (user_id, tier, quota_bytes, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            tier = excluded.tier,
                            quota_bytes = excluded.quota_bytes,
                            updated_at = excluded.updated_at
                        """,
                        (str(user_id), str(tier), quota_bytes, int(time.time())),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            return self.quota_for_user(user_id)

        with self._lock:
            config = self.load()
            tiers = config["quota"]["tiers"]
            if tier not in tiers:
                raise StorageConfigurationError("未知会员等级")
            if quota_bytes is not None:
                quota_bytes = _bounded_int(quota_bytes, 0, MAX_QUOTA_BYTES, "用户空间额度")
            config["quota"].setdefault("users", {})[user_id] = {
                "tier": tier,
                "quota_bytes": quota_bytes,
            }
            self._write(config)
            return self.quota_for_user(user_id)


CONFIG_STORE = ConfigStore()
