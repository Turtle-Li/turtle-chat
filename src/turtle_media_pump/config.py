from __future__ import annotations

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None else default
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _host_list(name: str) -> tuple[str, ...]:
    values = []
    for raw in os.getenv(name, "").split(","):
        value = raw.strip().lower().removeprefix("*.").rstrip(".")
        if value and value not in values:
            values.append(value)
    if not values:
        raise ValueError(f"{name} must contain at least one host or domain suffix")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class PumpSettings:
    shared_secret: str
    source_hosts: tuple[str, ...]
    destination_hosts: tuple[str, ...]
    max_bytes: int = 5 * 1024**3
    timeout_seconds: int = 1800
    timestamp_skew_seconds: int = 90
    max_redirects: int = 3
    bind_host: str = "0.0.0.0"
    port: int = 8090

    @classmethod
    def from_env(cls) -> "PumpSettings":
        secret = os.getenv("MEDIA_PUMP_SHARED_SECRET", "").strip()
        if len(secret) < 32:
            raise ValueError("MEDIA_PUMP_SHARED_SECRET must contain at least 32 characters")
        port = _positive_int("MEDIA_PUMP_PORT", 8090)
        if port > 65535:
            raise ValueError("MEDIA_PUMP_PORT must be between 1 and 65535")
        redirects = _positive_int("MEDIA_PUMP_MAX_REDIRECTS", 3)
        if redirects > 10:
            raise ValueError("MEDIA_PUMP_MAX_REDIRECTS must not exceed 10")
        return cls(
            shared_secret=secret,
            source_hosts=_host_list("MEDIA_PUMP_SOURCE_HOSTS"),
            destination_hosts=_host_list("MEDIA_PUMP_DESTINATION_HOSTS"),
            max_bytes=_positive_int("MEDIA_PUMP_MAX_BYTES", 5 * 1024**3),
            timeout_seconds=_positive_int("MEDIA_PUMP_TIMEOUT_SECONDS", 1800),
            timestamp_skew_seconds=_positive_int("MEDIA_PUMP_TIMESTAMP_SKEW_SECONDS", 90),
            max_redirects=redirects,
            bind_host=os.getenv("MEDIA_PUMP_BIND_HOST", "0.0.0.0").strip(),
            port=port,
        )
