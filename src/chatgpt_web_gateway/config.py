from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None else default
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None else default
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _port(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None else default
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _database_url() -> str | None:
    explicit = os.getenv("GATEWAY_DATABASE_URL", "").strip()
    if explicit:
        if not explicit.startswith(("postgresql://", "postgres://")):
            raise ValueError("GATEWAY_DATABASE_URL must be a PostgreSQL URL")
        return explicit

    host = os.getenv("DATABASE_HOST", "").strip()
    if not host:
        return None
    user = os.getenv("DATABASE_USER", "turtle").strip()
    database = os.getenv("DATABASE_NAME", "turtle").strip()
    port = _port("DATABASE_PORT", 5432)
    password = os.getenv("DATABASE_PASSWORD", "")
    password_file = os.getenv("DATABASE_PASSWORD_FILE", "").strip()
    if not password and password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("unable to read DATABASE_PASSWORD_FILE") from exc
    if not password:
        raise ValueError("a database password is required when DATABASE_HOST is configured")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _csv(name: str, default: str) -> tuple[str, ...]:
    values = tuple(value.strip().lower() for value in os.getenv(name, default).split(",") if value.strip())
    if not values:
        raise ValueError(f"{name} must contain at least one value")
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    gateway_api_key: str
    backend: str = "upstream"
    public_model_name: str = "gpt-5-web"
    claude_public_model_name: str = "claude-web"
    upstream_model: str = "auto"
    upstream_base_url: str = "http://127.0.0.1:8320/v1"
    upstream_health_path: str | None = "/api/OpenaiAccount/quota"
    claude_upstream_base_url: str = "http://127.0.0.1:8330/v1"
    claude_upstream_health_path: str | None = "/healthz"
    upstream_api_key: str = ""
    upstream_timeout_seconds: float = 180.0
    bind_host: str = "127.0.0.1"
    port: int = 8000
    strict_external_media: bool = True
    stream_chunk_chars: int = 8
    stream_chunk_delay_ms: float = 18.0
    account_pool_enabled: bool = False
    claude_pool_enabled: bool = False
    account_pool_database_url: str | None = None
    default_account_pool_id: str = "gpt-default"
    default_claude_account_pool_id: str = "claude-default"
    account_lease_seconds: int = 1200
    account_cooldown_seconds: int = 300
    account_recovery_poll_seconds: int = 30
    account_failover_max_attempts: int = 3
    upstream_cleanup_enabled: bool = False
    upstream_cleanup_execute: bool = False
    upstream_cleanup_ttl_seconds: int = 30 * 24 * 60 * 60
    upstream_cleanup_conversation_action: str = "delete"
    upstream_cleanup_interval_seconds: int = 300
    upstream_cleanup_batch_size: int = 20
    account_allowed_hosts: tuple[str, ...] = (
        "127.0.0.1",
        "localhost",
        "host.docker.internal",
    )
    login_control_url: str | None = None
    login_control_secret_file: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.getenv("GATEWAY_BACKEND", "upstream").strip().lower()
        if backend not in {"upstream", "mock"}:
            raise ValueError("GATEWAY_BACKEND must be 'upstream' or 'mock'")

        gateway_api_key = os.getenv("GATEWAY_API_KEY", "").strip()
        if not gateway_api_key:
            raise ValueError("GATEWAY_API_KEY is required")

        upstream_api_key = os.getenv("UPSTREAM_API_KEY", "").strip()
        if backend == "upstream" and not upstream_api_key:
            raise ValueError("UPSTREAM_API_KEY is required for the upstream backend")

        base_url = os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8320/v1").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("UPSTREAM_BASE_URL must be an http(s) URL")
        claude_base_url = os.getenv(
            "CLAUDE_UPSTREAM_BASE_URL",
            "http://127.0.0.1:8330/v1",
        ).rstrip("/")
        if not claude_base_url.startswith(("http://", "https://")):
            raise ValueError("CLAUDE_UPSTREAM_BASE_URL must be an http(s) URL")

        health_path = os.getenv(
            "UPSTREAM_HEALTH_PATH", "/api/OpenaiAccount/quota"
        ).strip() or None
        if health_path is not None and not health_path.startswith("/"):
            raise ValueError("UPSTREAM_HEALTH_PATH must be an absolute URL path")
        claude_health_path = os.getenv(
            "CLAUDE_UPSTREAM_HEALTH_PATH",
            "/healthz",
        ).strip() or None
        if claude_health_path is not None and not claude_health_path.startswith("/"):
            raise ValueError(
                "CLAUDE_UPSTREAM_HEALTH_PATH must be an absolute URL path"
            )

        account_pool_enabled = _boolean("GATEWAY_ACCOUNT_POOL_ENABLED", False)
        account_pool_database_url = _database_url() if account_pool_enabled else None
        if account_pool_enabled and account_pool_database_url is None:
            raise ValueError(
                "GATEWAY_ACCOUNT_POOL_ENABLED requires GATEWAY_DATABASE_URL or DATABASE_HOST"
            )
        upstream_cleanup_enabled = _boolean(
            "GATEWAY_UPSTREAM_CLEANUP_ENABLED",
            False,
        )
        if upstream_cleanup_enabled and account_pool_database_url is None:
            raise ValueError(
                "GATEWAY_UPSTREAM_CLEANUP_ENABLED requires the account-pool database"
            )
        upstream_cleanup_execute = _boolean(
            "GATEWAY_UPSTREAM_CLEANUP_EXECUTE",
            False,
        )
        if upstream_cleanup_execute and not upstream_cleanup_enabled:
            raise ValueError(
                "GATEWAY_UPSTREAM_CLEANUP_EXECUTE requires GATEWAY_UPSTREAM_CLEANUP_ENABLED"
            )
        upstream_cleanup_conversation_action = os.getenv(
            "GATEWAY_UPSTREAM_CLEANUP_CONVERSATION_ACTION",
            "delete",
        ).strip().lower()
        if upstream_cleanup_conversation_action not in {"archive", "delete"}:
            raise ValueError(
                "GATEWAY_UPSTREAM_CLEANUP_CONVERSATION_ACTION must be "
                "'archive' or 'delete'"
            )

        return cls(
            gateway_api_key=gateway_api_key,
            backend=backend,
            public_model_name=os.getenv("PUBLIC_MODEL_NAME", "gpt-5-web").strip(),
            claude_public_model_name=os.getenv(
                "CLAUDE_PUBLIC_MODEL_NAME",
                "claude-web",
            ).strip(),
            upstream_model=os.getenv("UPSTREAM_MODEL", "auto").strip(),
            upstream_base_url=base_url,
            upstream_health_path=health_path,
            claude_upstream_base_url=claude_base_url,
            claude_upstream_health_path=claude_health_path,
            upstream_api_key=upstream_api_key,
            upstream_timeout_seconds=_positive_float("UPSTREAM_TIMEOUT_SECONDS", 180.0),
            bind_host=os.getenv("GATEWAY_BIND_HOST", "127.0.0.1").strip(),
            port=_port("GATEWAY_PORT", 8000),
            strict_external_media=_boolean("TURTLE_MEDIA_STRICT", True),
            stream_chunk_chars=_non_negative_int("GATEWAY_STREAM_CHUNK_CHARS", 8),
            stream_chunk_delay_ms=_non_negative_float("GATEWAY_STREAM_CHUNK_DELAY_MS", 18.0),
            account_pool_enabled=account_pool_enabled,
            claude_pool_enabled=_boolean("GATEWAY_CLAUDE_POOL_ENABLED", False),
            account_pool_database_url=account_pool_database_url,
            default_account_pool_id=os.getenv(
                "GATEWAY_DEFAULT_ACCOUNT_POOL_ID", "gpt-default"
            ).strip(),
            default_claude_account_pool_id=os.getenv(
                "GATEWAY_DEFAULT_CLAUDE_ACCOUNT_POOL_ID",
                "claude-default",
            ).strip(),
            account_lease_seconds=int(
                _positive_float("GATEWAY_ACCOUNT_LEASE_SECONDS", 1200.0)
            ),
            account_cooldown_seconds=int(
                _positive_float("GATEWAY_ACCOUNT_COOLDOWN_SECONDS", 300.0)
            ),
            account_recovery_poll_seconds=_bounded_int(
                "GATEWAY_ACCOUNT_RECOVERY_POLL_SECONDS",
                30,
                minimum=5,
                maximum=300,
            ),
            account_failover_max_attempts=_bounded_int(
                "GATEWAY_ACCOUNT_FAILOVER_MAX_ATTEMPTS",
                3,
                minimum=1,
                maximum=8,
            ),
            upstream_cleanup_enabled=upstream_cleanup_enabled,
            upstream_cleanup_execute=upstream_cleanup_execute,
            upstream_cleanup_ttl_seconds=int(
                _positive_float(
                    "GATEWAY_UPSTREAM_CLEANUP_TTL_SECONDS",
                    30 * 24 * 60 * 60,
                )
            ),
            upstream_cleanup_conversation_action=(
                upstream_cleanup_conversation_action
            ),
            upstream_cleanup_interval_seconds=int(
                _positive_float(
                    "GATEWAY_UPSTREAM_CLEANUP_INTERVAL_SECONDS",
                    300,
                )
            ),
            upstream_cleanup_batch_size=_bounded_int(
                "GATEWAY_UPSTREAM_CLEANUP_BATCH_SIZE",
                20,
                minimum=1,
                maximum=100,
            ),
            account_allowed_hosts=_csv(
                "GATEWAY_ACCOUNT_ALLOWED_HOSTS",
                "127.0.0.1,localhost,host.docker.internal",
            ),
            login_control_url=os.getenv("GATEWAY_LOGIN_CONTROL_URL", "").strip()
            or None,
            login_control_secret_file=os.getenv(
                "GATEWAY_LOGIN_CONTROL_SECRET_FILE", ""
            ).strip()
            or None,
        )
