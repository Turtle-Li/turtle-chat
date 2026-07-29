"""Fast, sanitized account-pool capacity lookup for outer chat admission.

The Gateway remains the owner of account health and leases. Open WebUI reads
only a compact capacity number so its Redis FIFO can hold a request before it
reaches a saturated ChatGPT account group. No credential, endpoint, user, or
conversation data is cached here.
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx


class AccountPoolCapacityUnavailable(RuntimeError):
    """Raised when the outer queue cannot safely resolve account capacity."""


def _boolean(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return value in {"1", "true", "yes", "on"}


class AccountPoolAdmission:
    def __init__(self) -> None:
        self.cache_seconds = 0.75
        self._cache: dict[str, tuple[float, dict[str, int]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return _boolean("TURTLE_ACCOUNT_POOL_ADMISSION_ENABLED", False)

    @staticmethod
    def _target(pool_id: str, selection_key: str | None = None) -> tuple[str, str] | None:
        base_urls = [
            value.strip()
            for value in os.getenv("OPENAI_API_BASE_URLS", "").split(";")
            if value.strip()
        ]
        keys = [
            value.strip()
            for value in os.getenv("OPENAI_API_KEYS", "").split(";")
            if value.strip()
        ]
        if not base_urls or not keys:
            return None
        try:
            parsed = urlsplit(base_urls[0])
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/v1"):
            base_path = base_path[:-3]
        path = f"{base_path}/internal/account-pools/{quote(pool_id, safe='')}/capacity"
        query = urlencode({"selection_key": selection_key}) if selection_key else ""
        return parsed._replace(path=path, query=query, fragment="").geturl(), keys[0]

    async def limits(
        self,
        pool_id: str,
        selection_key: str | None = None,
    ) -> dict[str, int]:
        normalized = str(pool_id or "").strip()
        if not normalized:
            raise AccountPoolCapacityUnavailable("Provider 账号池未配置")
        now = time.monotonic()
        cache_key = f"{normalized}:{selection_key or '*'}"
        cached = self._cache.get(cache_key)
        if cached is not None and now - cached[0] < self.cache_seconds:
            return cached[1]

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._cache.get(cache_key)
            if cached is not None and now - cached[0] < self.cache_seconds:
                return cached[1]
            target = self._target(normalized, selection_key)
            if target is None:
                raise AccountPoolCapacityUnavailable("Gateway 账号组容量连接未配置")
            url, key = target
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(1.2, connect=0.5),
                    trust_env=False,
                ) as client:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {key}"},
                    )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                raise AccountPoolCapacityUnavailable(
                    "Gateway 账号组容量暂时不可读取"
                ) from exc
            if not response.is_success:
                raise AccountPoolCapacityUnavailable("Provider 账号池不存在或不可用")
            try:
                payload = response.json()
                limits = {
                    "account_pool": max(
                        0, int(payload.get("admission_capacity") or 0)
                    ),
                    "provider": max(
                        0,
                        int(
                            payload.get("provider_admission_capacity")
                            or payload.get("admission_capacity")
                            or 0
                        ),
                    ),
                    "global": max(
                        0,
                        int(
                            payload.get("global_admission_capacity")
                            or payload.get("provider_admission_capacity")
                            or payload.get("admission_capacity")
                            or 0
                        ),
                    ),
                }
            except (AttributeError, TypeError, ValueError) as exc:
                raise AccountPoolCapacityUnavailable(
                    "Gateway 返回了无效的账号组容量"
                ) from exc
            self._cache[cache_key] = (time.monotonic(), limits)
            return limits

    async def capacity(self, pool_id: str, selection_key: str | None = None) -> int:
        return int((await self.limits(pool_id, selection_key))["account_pool"])

    def invalidate(self, pool_id: str | None = None) -> None:
        if pool_id is None:
            self._cache.clear()
        else:
            prefix = f"{str(pool_id)}:"
            for key in list(self._cache):
                if key.startswith(prefix):
                    self._cache.pop(key, None)


ACCOUNT_POOL_ADMISSION = AccountPoolAdmission()
