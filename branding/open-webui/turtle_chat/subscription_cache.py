"""Short-lived Redis read-through cache for subscription authorization.

PostgreSQL remains the source of truth. Redis stores only a compact,
non-PII authorization snapshot with a mandatory TTL, clamped to the next
subscription boundary. Local and distributed single-flight locks prevent a
cache miss from stampeding PostgreSQL. Redis failure falls back to PostgreSQL;
database failure never reuses stale authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import weakref
from typing import Any, Callable

from .concurrency import CHAT_CONCURRENCY
from .store import (
    CHAT_STORE,
    DEFAULT_SUBSCRIPTION_DAYS,
    DEFAULT_SUBSCRIPTION_TIMEZONE,
    ChatPolicyError,
    ChatSubscriptionError,
    ChatStore,
)


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class SubscriptionCache:
    """Cache-aside subscription resolver with stampede protection."""

    def __init__(self, store: ChatStore = CHAT_STORE) -> None:
        self.store = store
        self.prefix = str(
            os.getenv(
                "TURTLE_SUBSCRIPTION_CACHE_PREFIX",
                "turtle:chat:subscription:v1",
            )
        ).strip() or "turtle:chat:subscription:v1"
        self.max_ttl_seconds = _bounded_env(
            "TURTLE_SUBSCRIPTION_CACHE_TTL_SECONDS",
            120,
            10,
            600,
        )
        self.negative_ttl_seconds = _bounded_env(
            "TURTLE_SUBSCRIPTION_NEGATIVE_TTL_SECONDS",
            15,
            3,
            60,
        )
        self.lock_ttl_ms = _bounded_env(
            "TURTLE_SUBSCRIPTION_LOCK_TTL_MS",
            5_000,
            1_000,
            15_000,
        )
        self.lock_wait_ms = _bounded_env(
            "TURTLE_SUBSCRIPTION_LOCK_WAIT_MS",
            1_500,
            100,
            5_000,
        )
        self.command_timeout_ms = _bounded_env(
            "TURTLE_SUBSCRIPTION_REDIS_TIMEOUT_MS",
            350,
            50,
            2_000,
        )
        self._local_locks: weakref.WeakValueDictionary[
            tuple[int, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._db_limit = _bounded_env(
            "TURTLE_SUBSCRIPTION_DB_FALLBACK_CONCURRENCY",
            8,
            1,
            32,
        )
        self._db_semaphores: dict[int, asyncio.Semaphore] = {}
        self._redis_backoff_until = 0.0
        self._metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "database_reads": 0,
            "lock_waits": 0,
            "redis_errors": 0,
            "mutations": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self._metrics,
            "max_ttl_seconds": self.max_ttl_seconds,
            "negative_ttl_seconds": self.negative_ttl_seconds,
            "redis_configured": CHAT_CONCURRENCY.shared_redis_configured,
            "redis_backoff": time.monotonic() < self._redis_backoff_until,
        }

    def _redis_failed(self) -> None:
        self._metrics["redis_errors"] += 1
        self._redis_backoff_until = max(
            self._redis_backoff_until,
            time.monotonic() + 1.0,
        )

    def _redis_succeeded(self) -> None:
        self._redis_backoff_until = 0.0

    @staticmethod
    def _opaque_user_key(user_id: str) -> str:
        return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:32]

    def _cache_key(self, user_id: str, role: str) -> str:
        return f"{self.prefix}:value:{self._opaque_user_key(user_id)}:{role}"

    def _lock_key(self, user_id: str) -> str:
        return f"{self.prefix}:lock:{self._opaque_user_key(user_id)}"

    def _keys_for_user(self, user_id: str) -> list[str]:
        return [
            self._cache_key(user_id, role)
            for role in ("pending", "user", "admin")
        ]

    def _local_lock(self, cache_key: str) -> asyncio.Lock:
        loop_key = id(asyncio.get_running_loop())
        key = (loop_key, cache_key)
        lock = self._local_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._local_locks[key] = lock
        return lock

    def _db_semaphore(self) -> asyncio.Semaphore:
        loop_key = id(asyncio.get_running_loop())
        semaphore = self._db_semaphores.get(loop_key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._db_limit)
            self._db_semaphores[loop_key] = semaphore
        return semaphore

    async def _redis(self):
        if not CHAT_CONCURRENCY.shared_redis_configured:
            return None
        if time.monotonic() < self._redis_backoff_until:
            return None
        try:
            return await CHAT_CONCURRENCY.shared_redis_client()
        except Exception:
            self._redis_failed()
            return None

    async def _command(self, callback: Callable[[], Any]):
        async with CHAT_CONCURRENCY.shared_redis_command_semaphore:
            return await asyncio.wait_for(
                callback(),
                timeout=self.command_timeout_ms / 1000,
            )

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, str) or not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        required = {"status", "active", "configured", "server_time"}
        if not required.issubset(payload):
            return None
        allowed_statuses = {
            "active",
            "scheduled",
            "expired",
            "cancelled",
            "inactive",
            "pending",
        }
        if payload.get("status") not in allowed_statuses:
            return None
        if not isinstance(payload.get("active"), bool):
            return None
        if not isinstance(payload.get("configured"), bool):
            return None
        if payload["active"] != (payload["status"] == "active"):
            return None
        if payload.get("expires_at") is not None:
            try:
                payload["expires_at"] = int(payload["expires_at"])
            except (TypeError, ValueError):
                return None
        if payload.get("starts_at") is not None:
            try:
                payload["starts_at"] = int(payload["starts_at"])
            except (TypeError, ValueError):
                return None
        now = int(time.time())
        if payload.get("status") == "active" and payload.get("expires_at") is not None:
            if now > int(payload["expires_at"]):
                return None
            payload["remaining_seconds"] = max(
                0,
                int(payload["expires_at"]) - now,
            )
        elif payload.get("status") == "active":
            return None
        if payload.get("status") == "scheduled" and payload.get("starts_at") is not None:
            if now >= int(payload["starts_at"]):
                return None
        payload["server_time"] = now
        return payload

    def _ttl(self, payload: dict[str, Any], cache_key: str) -> int:
        status = str(payload.get("status") or "inactive")
        ttl = (
            self.max_ttl_seconds
            if status in {"active", "scheduled"}
            else self.negative_ttl_seconds
        )
        now = int(time.time())
        boundary = None
        if status == "active" and payload.get("expires_at") is not None:
            boundary = int(payload["expires_at"]) - now
        elif status == "scheduled" and payload.get("starts_at") is not None:
            boundary = int(payload["starts_at"]) - now
        if boundary is not None:
            ttl = min(ttl, max(1, boundary))
        # Deterministic 0–10% early expiry spreads recurring refreshes without
        # ever keeping authorization beyond the actual subscription boundary.
        jitter_ceiling = max(0, ttl // 10)
        if jitter_ceiling:
            digest = hashlib.sha256(
                f"{cache_key}:{now // max(1, ttl)}".encode("utf-8")
            ).digest()
            ttl -= int.from_bytes(digest[:2], "big") % (jitter_ceiling + 1)
        return max(1, ttl)

    async def _get_cached(
        self,
        redis,
        cache_key: str,
        *,
        count_miss: bool = True,
    ) -> dict[str, Any] | None:
        if redis is None:
            return None
        try:
            raw = await self._command(lambda: redis.get(cache_key))
        except Exception:
            self._redis_failed()
            return None
        self._redis_succeeded()
        payload = self._decode(raw)
        if payload is None and count_miss:
            self._metrics["cache_misses"] += 1
        else:
            if payload is not None:
                self._metrics["cache_hits"] += 1
        if payload is None and raw:
            try:
                await self._command(lambda: redis.delete(cache_key))
            except Exception:
                pass
        return payload

    async def _set_cached(
        self,
        redis,
        cache_key: str,
        payload: dict[str, Any],
    ) -> None:
        if redis is None:
            return
        ttl = self._ttl(payload, cache_key)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            await self._command(
                lambda: redis.set(cache_key, serialized, ex=ttl)
            )
        except Exception:
            self._redis_failed()
            return
        self._redis_succeeded()

    async def _acquire_lock(
        self,
        redis,
        user_id: str,
    ) -> tuple[str | None, bool]:
        if redis is None:
            return None, False
        token = secrets.token_urlsafe(18)
        try:
            acquired = await self._command(
                lambda: redis.set(
                    self._lock_key(user_id),
                    token,
                    nx=True,
                    px=self.lock_ttl_ms,
                )
            )
        except Exception:
            self._redis_failed()
            return None, False
        self._redis_succeeded()
        return (token if acquired else None), True

    async def _release_lock(self, redis, user_id: str, token: str | None) -> None:
        if redis is None or not token:
            return
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """
        try:
            await self._command(
                lambda: redis.eval(script, 1, self._lock_key(user_id), token)
            )
        except Exception:
            self._redis_failed()
            return
        self._redis_succeeded()

    async def _renew_lock(
        self,
        redis,
        user_id: str,
        token: str,
    ) -> None:
        interval = max(0.25, self.lock_ttl_ms / 3_000)
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('pexpire', KEYS[1], ARGV[2])
        end
        return 0
        """
        while True:
            try:
                await asyncio.sleep(interval)
                renewed = await self._command(
                    lambda: redis.eval(
                        script,
                        1,
                        self._lock_key(user_id),
                        token,
                        self.lock_ttl_ms,
                    )
                )
                if not renewed:
                    return
                self._redis_succeeded()
            except asyncio.CancelledError:
                return
            except Exception:
                self._redis_failed()
                return

    @staticmethod
    async def _stop_renewer(task: asyncio.Task | None) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _wait_for_fill(
        self,
        redis,
        cache_key: str,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.lock_wait_ms / 1000
        delay = 0.02
        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            payload = await self._get_cached(
                redis,
                cache_key,
                count_miss=False,
            )
            if payload is not None:
                return payload
            delay = min(0.12, delay * 1.7)
        return None

    async def _database_get(
        self,
        user_id: str,
        role: str,
        *,
        create_default: bool,
    ) -> dict[str, Any]:
        async with self._db_semaphore():
            self._metrics["database_reads"] += 1
            return await asyncio.to_thread(
                self.store.subscription_for_user,
                user_id,
                role,
                create_default=create_default,
            )

    async def get(
        self,
        user_id: str,
        role: str,
        *,
        create_default: bool = True,
    ) -> dict[str, Any]:
        normalized_role = str(role or "pending")
        if normalized_role not in {"pending", "user", "admin"}:
            normalized_role = "pending"
        if normalized_role == "admin":
            now = int(time.time())
            return {
                "status": "unlimited",
                "active": True,
                "configured": False,
                "starts_at": None,
                "expires_at": None,
                "remaining_seconds": None,
                "state": None,
                "timezone": DEFAULT_SUBSCRIPTION_TIMEZONE,
                "default_days": DEFAULT_SUBSCRIPTION_DAYS,
                "updated_at": None,
                "server_time": now,
            }
        cache_key = self._cache_key(user_id, normalized_role)
        redis = await self._redis()
        cached = await self._get_cached(redis, cache_key)
        if cached is not None:
            return cached
        if time.monotonic() < self._redis_backoff_until:
            redis = None

        local_lock = self._local_lock(cache_key)
        async with local_lock:
            cached = await self._get_cached(
                redis,
                cache_key,
                count_miss=False,
            )
            if cached is not None:
                return cached
            token, redis_healthy = await self._acquire_lock(redis, user_id)
            if not redis_healthy:
                redis = None
            if redis is not None and token is None:
                self._metrics["lock_waits"] += 1
                filled = await self._wait_for_fill(redis, cache_key)
                if filled is not None:
                    return filled
                token, redis_healthy = await self._acquire_lock(redis, user_id)
                if not redis_healthy:
                    redis = None
            renewer = (
                asyncio.create_task(
                    self._renew_lock(redis, user_id, token),
                    name="turtle-subscription-cache-lock",
                )
                if redis is not None and token is not None
                else None
            )
            try:
                payload = await self._database_get(
                    user_id,
                    normalized_role,
                    create_default=create_default,
                )
                await self._set_cached(redis, cache_key, payload)
                return payload
            finally:
                await self._stop_renewer(renewer)
                await self._release_lock(redis, user_id, token)

    async def get_many(
        self,
        users: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Resolve users in bounded batches without an unbounded task burst."""

        unique = list(
            dict.fromkeys((str(user_id), str(role)) for user_id, role in users)
        )
        resolved: dict[tuple[str, str], dict[str, Any]] = {}
        batch_size = max(1, min(32, self._db_limit * 4))
        for offset in range(0, len(unique), batch_size):
            batch = unique[offset : offset + batch_size]
            values = await asyncio.gather(
                *(self.get(user_id, role) for user_id, role in batch)
            )
            resolved.update(dict(zip(batch, values, strict=True)))
        return resolved

    async def require_active(
        self,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        payload = await self.get(user_id, role)
        if not payload.get("active"):
            raise ChatSubscriptionError(payload)
        return payload

    async def invalidate(self, user_id: str) -> None:
        redis = await self._redis()
        if redis is None:
            return
        keys = self._keys_for_user(user_id)
        for attempt in range(3):
            try:
                await self._command(lambda: redis.delete(*keys))
                self._redis_succeeded()
                return
            except Exception:
                self._redis_failed()
                if attempt < 2:
                    await asyncio.sleep(0.05 * (attempt + 1))

    async def _mutate(
        self,
        user_id: str,
        role: str,
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        redis = await self._redis()
        token = None
        if redis is not None:
            deadline = time.monotonic() + (self.lock_ttl_ms + self.lock_wait_ms) / 1000
            while token is None and time.monotonic() < deadline:
                token, redis_healthy = await self._acquire_lock(redis, user_id)
                if not redis_healthy:
                    redis = None
                    break
                if token is None:
                    await asyncio.sleep(0.05)
            if redis is not None and token is None:
                raise ChatPolicyError("订阅状态正在更新，请稍后重试")
        renewer = (
            asyncio.create_task(
                self._renew_lock(redis, user_id, token),
                name="turtle-subscription-mutation-lock",
            )
            if redis is not None and token is not None
            else None
        )
        try:
            async with self._db_semaphore():
                self._metrics["mutations"] += 1
                payload = await asyncio.to_thread(callback)
            await self.invalidate(user_id)
            await self._set_cached(
                redis,
                self._cache_key(user_id, str(role or "pending")),
                payload,
            )
            return payload
        finally:
            await self._stop_renewer(renewer)
            await self._release_lock(redis, user_id, token)

    async def set_subscription(
        self,
        user_id: str,
        role: str,
        *,
        starts_at: int | None = None,
        expires_at: int | None = None,
        duration_days: int = DEFAULT_SUBSCRIPTION_DAYS,
        updated_by: str,
    ) -> dict[str, Any]:
        return await self._mutate(
            user_id,
            role,
            lambda: self.store.set_subscription(
                user_id,
                role,
                starts_at=starts_at,
                expires_at=expires_at,
                duration_days=duration_days,
                updated_by=updated_by,
            ),
        )

    async def extend_subscription(
        self,
        user_id: str,
        role: str,
        *,
        days: int = DEFAULT_SUBSCRIPTION_DAYS,
        updated_by: str,
    ) -> dict[str, Any]:
        return await self._mutate(
            user_id,
            role,
            lambda: self.store.extend_subscription(
                user_id,
                role,
                days=days,
                updated_by=updated_by,
            ),
        )

    async def cancel_subscription(
        self,
        user_id: str,
        role: str,
        *,
        updated_by: str,
    ) -> dict[str, Any]:
        return await self._mutate(
            user_id,
            role,
            lambda: self.store.cancel_subscription(
                user_id,
                role,
                updated_by=updated_by,
            ),
        )


SUBSCRIPTION_CACHE = SubscriptionCache()
