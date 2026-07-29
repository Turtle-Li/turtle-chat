from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw is not None else default
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _port(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None else default
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    worker_api_key: str
    auth_path: Path = Path(".runtime/claude-auth/session.json")
    verified_models_path: Path = Path(".runtime/claude-auth/verified-models.json")
    base_url: str = "https://claude.ai"
    timeout_seconds: float = 900.0
    health_cache_seconds: float = 30.0
    bind_host: str = "127.0.0.1"
    port: int = 8330
    browser_port: int = 9225
    public_model_name: str = "claude-web"
    timezone: str = "Asia/Shanghai"
    # Claude's completion endpoint validates this against its supported UI
    # locales. Chinese prompts remain untouched; zh-CN is not currently an
    # accepted protocol value.
    locale: str = "en-US"
    proxy_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        worker_api_key = (
            os.getenv("CLAUDE_WORKER_API_KEY", "").strip()
            or os.getenv("UPSTREAM_API_KEY", "").strip()
            or os.getenv("GATEWAY_API_KEY", "").strip()
        )
        if not worker_api_key:
            raise ValueError(
                "CLAUDE_WORKER_API_KEY, UPSTREAM_API_KEY or GATEWAY_API_KEY is required"
            )

        base_url = os.getenv("CLAUDE_BASE_URL", "https://claude.ai").strip().rstrip("/")
        parsed = urlparse(base_url)
        # Authentication must never be redirected to a community proxy or a
        # third-party worker.  Tests inject an in-memory transport instead.
        if parsed.scheme != "https" or parsed.hostname != "claude.ai" or parsed.path:
            raise ValueError("CLAUDE_BASE_URL must be exactly https://claude.ai")

        proxy_url = os.getenv("CLAUDE_PROXY_URL", "").strip() or None
        if proxy_url:
            proxy = urlparse(proxy_url)
            if proxy.scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
                raise ValueError("CLAUDE_PROXY_URL uses an unsupported scheme")

        auth_path = Path(
            os.path.abspath(
                Path(
                    os.getenv("CLAUDE_AUTH_PATH", ".runtime/claude-auth/session.json")
                ).expanduser()
            )
        )
        verified_path = Path(
            os.path.abspath(
                Path(
                    os.getenv(
                        "CLAUDE_VERIFIED_MODELS_PATH",
                        str(auth_path.parent / "verified-models.json"),
                    )
                ).expanduser()
            )
        )
        if verified_path.parent != auth_path.parent:
            raise ValueError("Claude auth and verified-model files must share one private directory")

        public_model = os.getenv("CLAUDE_PUBLIC_MODEL_NAME", "claude-web").strip()
        if not public_model:
            raise ValueError("CLAUDE_PUBLIC_MODEL_NAME must not be empty")

        return cls(
            worker_api_key=worker_api_key,
            auth_path=auth_path,
            verified_models_path=verified_path,
            base_url=base_url,
            timeout_seconds=_positive_float("CLAUDE_TIMEOUT_SECONDS", 900.0),
            health_cache_seconds=_positive_float("CLAUDE_HEALTH_CACHE_SECONDS", 30.0),
            bind_host=os.getenv("CLAUDE_BIND_HOST", "127.0.0.1").strip(),
            port=_port("CLAUDE_PORT", 8330),
            browser_port=_port("CLAUDE_BROWSER_PORT", 9225),
            public_model_name=public_model,
            timezone=os.getenv("CLAUDE_TIMEZONE", "Asia/Shanghai").strip(),
            locale=os.getenv("CLAUDE_LOCALE", "en-US").strip(),
            proxy_url=proxy_url,
        )
