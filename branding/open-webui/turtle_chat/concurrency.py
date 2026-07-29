"""Distributed FIFO concurrency coordination for Turtle chat requests.

Redis is the production coordinator and is intentionally ephemeral. Unit tests
and explicit single-process development can use the in-memory implementation.
No prompt, response, credential, or upstream error body is stored here.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote


class ChatConcurrencyError(RuntimeError):
    """Base class for queue/coordinator failures."""


class ChatQueueTimeout(ChatConcurrencyError):
    """Raised when a request cannot obtain a slot before the queue deadline."""


class ChatCoordinatorUnavailable(ChatConcurrencyError):
    """Raised when production Redis coordination is unavailable."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _positive_env(name: str, default: int, maximum: int = 86_400) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(1, value))


def _safe_request_id(value: str | None) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (TypeError, ValueError, AttributeError):
        return str(uuid.uuid4())


@dataclass(slots=True)
class ConcurrencyLease:
    coordinator: "ChatConcurrencyCoordinator"
    request_id: str
    user_id: str
    group_id: str
    provider: str
    account_pool_id: str
    user_limit: int
    group_limit: int
    account_pool_limit: int
    provider_limit: int
    global_limit: int
    queued_at_ms: int
    admitted_at_ms: int
    lease_expires_at_ms: int
    _released: bool = False
    _heartbeat_task: asyncio.Task | None = field(default=None, repr=False)

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(
                self.coordinator._heartbeat_loop(self),
                name=f"turtle-chat-lease-{self.request_id[:8]}",
            )

    async def release(self, outcome: str = "completed") -> None:
        if self._released:
            return
        self._released = True
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self.coordinator.release(self, outcome=outcome)


class ChatConcurrencyCoordinator:
    _ENQUEUE_SCRIPT = r"""
local existing = redis.call('HGET', KEYS[2], 'state')
if existing then
  return -2
end
local sequence = redis.call('INCR', KEYS[1])
redis.call('HSET', KEYS[2],
  'request_id', ARGV[1], 'user_id', ARGV[2], 'group_id', ARGV[3],
  'provider', ARGV[4], 'account_pool_id', ARGV[5],
  'user_limit', ARGV[6], 'group_limit', ARGV[7],
  'account_pool_limit', ARGV[8], 'provider_limit', ARGV[9],
  'global_limit', ARGV[10], 'queued_at_ms', ARGV[11], 'state', 'queued',
  'user_scope', ARGV[13], 'group_scope', ARGV[14], 'account_pool_scope', ARGV[15])
redis.call('EXPIRE', KEYS[2], ARGV[12])
for index = 3, 7 do
  redis.call('ZADD', KEYS[index], sequence, ARGV[1])
  redis.call('EXPIRE', KEYS[index], ARGV[12])
end
return sequence
"""

    _TRY_ADMIT_SCRIPT = r"""
local function clean_wait(key)
  for _ = 1, 64 do
    local head = redis.call('ZRANGE', key, 0, 0)
    if #head == 0 then return end
    local state = redis.call('HGET', ARGV[5] .. head[1], 'state')
    if state == 'queued' then return end
    redis.call('ZREM', key, head[1])
  end
end
for index = 7, 11 do redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', ARGV[2]) end
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'admitted' then return 1 end
if state ~= 'queued' then return -1 end
redis.call('HSET', KEYS[1],
  'account_pool_limit', ARGV[11],
  'provider_limit', ARGV[10],
  'global_limit', ARGV[7])
for index = 2, 6 do clean_wait(KEYS[index]) end
for index = 3, 4 do
  local rank = redis.call('ZRANK', KEYS[index], ARGV[1])
  if not rank or rank ~= 0 then return 0 end
end
local pool_rank = redis.call('ZRANK', KEYS[6], ARGV[1])
if not pool_rank or pool_rank ~= 0 then return 0 end
if redis.call('ZCARD', KEYS[7]) >= tonumber(ARGV[7]) then return 0 end
if redis.call('ZCARD', KEYS[8]) >= tonumber(ARGV[8]) then return 0 end
if redis.call('ZCARD', KEYS[9]) >= tonumber(ARGV[9]) then return 0 end
if redis.call('ZCARD', KEYS[10]) >= tonumber(ARGV[10]) then return 0 end
if redis.call('ZCARD', KEYS[11]) >= tonumber(ARGV[11]) then return 0 end
for index = 2, 6 do redis.call('ZREM', KEYS[index], ARGV[1]) end
for index = 7, 11 do
  redis.call('ZADD', KEYS[index], ARGV[3], ARGV[1])
  redis.call('PEXPIRE', KEYS[index], ARGV[6] * 1000)
end
redis.call('HSET', KEYS[1], 'state', 'admitted', 'admitted_at_ms', ARGV[2], 'lease_expires_at_ms', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[6])
redis.call('PUBLISH', KEYS[12], 'capacity')
return 1
"""

    _RELEASE_SCRIPT = r"""
for index = 2, 11 do redis.call('ZREM', KEYS[index], ARGV[1]) end
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], 'state', ARGV[2], 'outcome', ARGV[3], 'completed_at_ms', ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
end
if ARGV[6] == '1' then redis.call('PUBLISH', KEYS[12], 'capacity') end
return 1
"""

    _HEARTBEAT_SCRIPT = r"""
if redis.call('HGET', KEYS[1], 'state') ~= 'admitted' then return 0 end
for index = 2, 6 do redis.call('ZADD', KEYS[index], ARGV[2], ARGV[1]) end
redis.call('HSET', KEYS[1], 'lease_expires_at_ms', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

    def __init__(self) -> None:
        # v2 adds an account-pool dimension. Keeping a new prefix prevents
        # pre-upgrade wait/lease keys from being interpreted with the new Lua
        # key layout during a rolling rebuild.
        self.prefix = f"{os.getenv('REDIS_KEY_PREFIX', 'turtle-gpt')}:chat-concurrency:v2"
        self.queue_timeout_seconds = _positive_env("TURTLE_CHAT_QUEUE_TIMEOUT_SECONDS", 120, 3_600)
        self.lease_seconds = _positive_env("TURTLE_CHAT_LEASE_SECONDS", 1_200, 7_200)
        self.status_ttl_seconds = _positive_env("TURTLE_CHAT_STATUS_TTL_SECONDS", 300, 3_600)
        self.global_limit = _positive_env("TURTLE_CHAT_MAX_CONCURRENCY", 32, 1_000)
        self.redis_max_connections = _positive_env(
            "TURTLE_REDIS_MAX_CONNECTIONS",
            100,
            1_000,
        )
        command_concurrency = min(
            _positive_env(
                "TURTLE_REDIS_COMMAND_CONCURRENCY",
                64,
                1_000,
            ),
            max(1, self.redis_max_connections - 2),
        )
        maintenance_concurrency = min(
            _positive_env(
                "TURTLE_REDIS_MAINTENANCE_CONCURRENCY",
                16,
                1_000,
            ),
            max(1, self.redis_max_connections - command_concurrency - 1),
        )
        self._redis_client = None
        self._redis_lock = asyncio.Lock()
        self._redis_command_semaphore = asyncio.Semaphore(command_concurrency)
        self._redis_maintenance_semaphore = asyncio.Semaphore(
            maintenance_concurrency
        )
        self._redis_waiter_events: dict[
            str,
            tuple[asyncio.Event, str],
        ] = {}
        self._redis_wake_heads_per_pool = _positive_env(
            "TURTLE_REDIS_WAKE_HEADS_PER_POOL",
            32,
            1_000,
        )
        self._redis_event_ready = asyncio.Event()
        self._redis_event_listener_task: asyncio.Task | None = None
        self._redis_deferred_releases: list[tuple[Any, ...]] = []
        self._redis_deferred_release_task: asyncio.Task | None = None
        self._redis_event_retry_seconds = 5.0
        self._redis_event_coalesce_seconds = 0.01
        self._memory_condition = asyncio.Condition()
        self._memory_sequence = 0
        self._memory_entries: dict[str, dict[str, Any]] = {}

    @staticmethod
    def normalize_request_id(value: str | None) -> str:
        return _safe_request_id(value)

    @property
    def backend(self) -> str:
        configured = str(os.getenv("TURTLE_CONCURRENCY_BACKEND", "auto")).strip().lower()
        if configured in {"memory", "redis"}:
            return configured
        return "redis" if self._redis_url() else "memory"

    @staticmethod
    def _redis_url() -> str:
        try:
            from open_webui.env import REDIS_URL  # type: ignore

            if REDIS_URL:
                return str(REDIS_URL)
        except (ImportError, AttributeError):
            pass
        value = str(os.getenv("REDIS_URL", "")).strip()
        if value:
            return value
        host = str(os.getenv("REDIS_HOST", "")).strip()
        if not host:
            return ""
        password = ""
        password_file = str(os.getenv("REDIS_PASSWORD_FILE", "")).strip()
        if password_file:
            try:
                with open(password_file, encoding="utf-8") as handle:
                    password = handle.read().strip()
            except OSError:
                return ""
        auth = f":{quote(password, safe='')}@" if password else ""
        port = str(os.getenv("REDIS_PORT", "6379"))
        database = str(os.getenv("REDIS_DATABASE", "0"))
        return f"redis://{auth}{host}:{port}/{database}"

    async def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        async with self._redis_lock:
            if self._redis_client is not None:
                return self._redis_client
            url = self._redis_url()
            if not url:
                raise ChatCoordinatorUnavailable("并发协调 Redis 未配置")
            try:
                from redis.asyncio import ConnectionPool, Redis  # type: ignore

                pool = ConnectionPool.from_url(
                    url,
                    max_connections=self.redis_max_connections,
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=5,
                    health_check_interval=30,
                )
                client = Redis.from_pool(pool)
                await client.ping()
            except Exception as exc:
                if "client" in locals():
                    try:
                        await client.aclose()
                    except Exception:
                        pass
                raise ChatCoordinatorUnavailable("并发协调服务暂时不可用") from exc
            self._redis_client = client
            return client

    async def shared_redis_client(self):
        """Return the process-wide Redis client used by Turtle coordination.

        Short-lived read-through caches share this bounded pool instead of
        creating extra connection pools that could exceed the deployment's
        Redis safety ceiling.
        """

        return await self._redis()

    @property
    def shared_redis_configured(self) -> bool:
        return bool(self._redis_url())

    @property
    def shared_redis_command_semaphore(self) -> asyncio.Semaphore:
        return self._redis_command_semaphore

    @property
    def _redis_event_channel(self) -> str:
        return f"{self.prefix}:events"

    async def _redis_event_listener(self) -> None:
        while True:
            pubsub = None
            try:
                redis = await self._redis()
                pubsub = redis.pubsub()
                await pubsub.subscribe(self._redis_event_channel)
                self._redis_event_ready.set()
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is None:
                        continue
                    # Capacity releases often arrive in a tight burst. Drain
                    # them into one generation so queued coroutines do not
                    # create a Redis thundering herd for every release.
                    await asyncio.sleep(self._redis_event_coalesce_seconds)
                    while (
                        await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=0.0,
                        )
                        is not None
                    ):
                        pass
                    await self._wake_redis_waiter_heads()
            except asyncio.CancelledError:
                return
            except Exception:
                self._redis_event_ready.set()
                await asyncio.sleep(1)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass

    async def _ensure_redis_event_listener(self) -> None:
        task = self._redis_event_listener_task
        if task is None or task.done():
            self._redis_event_ready = asyncio.Event()
            self._redis_event_listener_task = asyncio.create_task(
                self._redis_event_listener(),
                name="turtle-chat-capacity-events",
            )
        try:
            await asyncio.wait_for(
                self._redis_event_ready.wait(),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            # The timed retry below remains the fail-safe if Pub/Sub is
            # temporarily unavailable. Redis admission itself still fails
            # closed through the normal request path.
            return

    async def _wake_redis_waiter_heads(self) -> None:
        waiters = dict(self._redis_waiter_events)
        if not waiters:
            return
        pool_scopes = sorted({pool_scope for _event, pool_scope in waiters.values()})
        redis = await self._redis()
        pipeline = redis.pipeline(transaction=False)
        for pool_scope in pool_scopes:
            pipeline.zrange(
                f"{self.prefix}:wait:account-pool:{pool_scope}",
                0,
                self._redis_wake_heads_per_pool - 1,
            )
        async with self._redis_command_semaphore:
            heads_by_pool = await pipeline.execute()
        for request_id in {
            request_id
            for pool_heads in heads_by_pool
            for request_id in pool_heads
        }:
            waiter = self._redis_waiter_events.get(request_id)
            if waiter is not None:
                waiter[0].set()

    async def _wait_for_redis_event(
        self,
        waiter_event: asyncio.Event,
        timeout: float,
    ) -> None:
        await self._ensure_redis_event_listener()
        try:
            await asyncio.wait_for(
                waiter_event.wait(),
                timeout=max(0.001, timeout),
            )
        except asyncio.TimeoutError:
            return

    async def close(self) -> None:
        deferred = self._redis_deferred_release_task
        if deferred is not None and not deferred.done():
            try:
                await asyncio.wait_for(asyncio.shield(deferred), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                deferred.cancel()
                await asyncio.gather(deferred, return_exceptions=True)
        self._redis_deferred_release_task = None
        task = self._redis_event_listener_task
        self._redis_event_listener_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        redis = self._redis_client
        self._redis_client = None
        if redis is not None:
            await redis.aclose()

    @staticmethod
    def _scope(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]

    def _keys(
        self,
        request_id: str,
        user_id: str,
        group_id: str,
        provider: str,
        account_pool_id: str,
    ) -> dict[str, str]:
        user_scope = self._scope(user_id)
        group_scope = self._scope(group_id)
        account_pool_scope = self._scope(account_pool_id)
        return {
            "sequence": f"{self.prefix}:sequence",
            "meta": f"{self.prefix}:meta:{request_id}",
            "meta_prefix": f"{self.prefix}:meta:",
            "wait_global": f"{self.prefix}:wait:global",
            "wait_user": f"{self.prefix}:wait:user:{user_scope}",
            "wait_group": f"{self.prefix}:wait:group:{group_scope}",
            "wait_provider": f"{self.prefix}:wait:provider:{provider}",
            "wait_account_pool": f"{self.prefix}:wait:account-pool:{account_pool_scope}",
            "active_global": f"{self.prefix}:active:global",
            "active_user": f"{self.prefix}:active:user:{user_scope}",
            "active_group": f"{self.prefix}:active:group:{group_scope}",
            "active_provider": f"{self.prefix}:active:provider:{provider}",
            "active_account_pool": f"{self.prefix}:active:account-pool:{account_pool_scope}",
            "user_scope": user_scope,
            "group_scope": group_scope,
            "account_pool_scope": account_pool_scope,
        }

    @staticmethod
    def _provider_limit(provider: str) -> int:
        name, default = {
            "gpt": ("TURTLE_GPT_MAX_CONCURRENCY", 24),
            "claude": ("TURTLE_CLAUDE_MAX_CONCURRENCY", 4),
        }.get(provider, ("TURTLE_OTHER_MAX_CONCURRENCY", 2))
        return _positive_env(name, default, 1_000)

    async def acquire(
        self,
        *,
        request_id: str | None,
        user_id: str,
        group_id: str,
        provider: str,
        user_limit: int,
        group_limit: int,
        account_pool_id: str | None = None,
        account_pool_limit: int | None = None,
        provider_limit: int | None = None,
        global_limit: int | None = None,
        account_pool_limit_resolver: Callable[
            [], Awaitable[int | dict[str, int]]
        ]
        | None = None,
    ) -> ConcurrencyLease:
        normalized_id = _safe_request_id(request_id)
        provider_ceiling = self._provider_limit(provider)
        normalized_pool_id = str(account_pool_id or f"{provider}-default")
        limits = {
            "user": max(1, int(user_limit)),
            "group": max(1, int(group_limit)),
            "account_pool": (
                provider_ceiling
                if account_pool_limit is None
                else max(0, int(account_pool_limit))
            ),
            "provider": min(
                provider_ceiling,
                provider_ceiling
                if provider_limit is None
                else max(0, int(provider_limit)),
            ),
            "global": min(
                self.global_limit,
                self.global_limit
                if global_limit is None
                else max(0, int(global_limit)),
            ),
        }
        if self.backend == "memory":
            lease = await self._acquire_memory(
                normalized_id,
                str(user_id),
                str(group_id),
                str(provider),
                normalized_pool_id,
                limits,
                account_pool_limit_resolver,
            )
        else:
            lease = await self._acquire_redis(
                normalized_id,
                str(user_id),
                str(group_id),
                str(provider),
                normalized_pool_id,
                limits,
                account_pool_limit_resolver,
            )
        lease.start_heartbeat()
        return lease

    async def _refresh_dynamic_limits(
        self,
        limits: dict[str, int],
        provider: str,
        resolver: Callable[[], Awaitable[int | dict[str, int]]] | None,
    ) -> None:
        if resolver is None:
            return
        resolved = await resolver()
        if isinstance(resolved, dict):
            if "account_pool" in resolved:
                limits["account_pool"] = max(
                    0, int(resolved.get("account_pool") or 0)
                )
            if "provider" in resolved:
                limits["provider"] = min(
                    self._provider_limit(provider),
                    max(0, int(resolved.get("provider") or 0)),
                )
            if "global" in resolved:
                limits["global"] = min(
                    self.global_limit,
                    max(0, int(resolved.get("global") or 0)),
                )
            return
        limits["account_pool"] = max(0, int(resolved))

    def _memory_cleanup(self, now_ms: int) -> None:
        expired_before = now_ms - self.status_ttl_seconds * 1000
        for request_id, entry in list(self._memory_entries.items()):
            if entry["state"] == "admitted" and int(entry.get("lease_expires_at_ms") or 0) <= now_ms:
                entry.update(
                    state="failed",
                    outcome="lease_expired",
                    completed_at_ms=now_ms,
                )
            if entry["state"] not in {"queued", "admitted"} and int(entry.get("completed_at_ms") or 0) < expired_before:
                self._memory_entries.pop(request_id, None)

    def _memory_active(self, field: str, value: str) -> int:
        return sum(
            1
            for entry in self._memory_entries.values()
            if entry["state"] == "admitted" and str(entry[field]) == value
        )

    async def _acquire_memory(
        self,
        request_id: str,
        user_id: str,
        group_id: str,
        provider: str,
        account_pool_id: str,
        limits: dict[str, int],
        account_pool_limit_resolver: Callable[
            [], Awaitable[int | dict[str, int]]
        ]
        | None,
    ) -> ConcurrencyLease:
        queued_at = _now_ms()
        deadline = time.monotonic() + self.queue_timeout_seconds
        async with self._memory_condition:
            existing = self._memory_entries.get(request_id)
            if existing is not None:
                raise ChatConcurrencyError("排队请求编号已使用")
            self._memory_sequence += 1
            entry = {
                "request_id": request_id,
                "user_id": user_id,
                "group_id": group_id,
                "provider": provider,
                "account_pool_id": account_pool_id,
                "user_limit": limits["user"],
                "group_limit": limits["group"],
                "account_pool_limit": limits["account_pool"],
                "provider_limit": limits["provider"],
                "global_limit": limits["global"],
                "queued_at_ms": queued_at,
                "sequence": self._memory_sequence,
                "state": "queued",
            }
            self._memory_entries[request_id] = entry
            try:
                while True:
                    await self._refresh_dynamic_limits(
                        limits,
                        provider,
                        account_pool_limit_resolver,
                    )
                    entry.update(
                        account_pool_limit=limits["account_pool"],
                        provider_limit=limits["provider"],
                        global_limit=limits["global"],
                    )
                    now_ms = _now_ms()
                    self._memory_cleanup(now_ms)
                    waiting = sorted(
                        (item for item in self._memory_entries.values() if item["state"] == "queued"),
                        key=lambda item: item["sequence"],
                    )
                    earlier = [item for item in waiting if item["sequence"] < entry["sequence"]]
                    can_admit = (
                        not any(
                            item["user_id"] == user_id
                            or item["group_id"] == group_id
                            or item["account_pool_id"] == account_pool_id
                            for item in earlier
                        )
                        and self._memory_active("user_id", user_id) < limits["user"]
                        and self._memory_active("group_id", group_id) < limits["group"]
                        and self._memory_active("account_pool_id", account_pool_id)
                        < limits["account_pool"]
                        and self._memory_active("provider", provider) < limits["provider"]
                        and sum(1 for item in self._memory_entries.values() if item["state"] == "admitted")
                        < limits["global"]
                    )
                    if can_admit:
                        admitted_at = _now_ms()
                        expires_at = admitted_at + self.lease_seconds * 1000
                        entry.update(
                            state="admitted",
                            admitted_at_ms=admitted_at,
                            lease_expires_at_ms=expires_at,
                        )
                        self._memory_condition.notify_all()
                        return ConcurrencyLease(
                            coordinator=self,
                            request_id=request_id,
                            user_id=user_id,
                            group_id=group_id,
                            provider=provider,
                            account_pool_id=account_pool_id,
                            user_limit=limits["user"],
                            group_limit=limits["group"],
                            account_pool_limit=limits["account_pool"],
                            provider_limit=limits["provider"],
                            global_limit=limits["global"],
                            queued_at_ms=queued_at,
                            admitted_at_ms=admitted_at,
                            lease_expires_at_ms=expires_at,
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        entry.update(
                            state="timed_out",
                            outcome="queue_timeout",
                            completed_at_ms=_now_ms(),
                        )
                        self._memory_condition.notify_all()
                        raise ChatQueueTimeout("排队等待超时，请稍后重试")
                    try:
                        await asyncio.wait_for(
                            self._memory_condition.wait(), timeout=min(0.25, remaining)
                        )
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                entry.update(
                    state="cancelled",
                    outcome="queue_cancelled",
                    completed_at_ms=_now_ms(),
                )
                self._memory_condition.notify_all()
                raise

    async def _acquire_redis(
        self,
        request_id: str,
        user_id: str,
        group_id: str,
        provider: str,
        account_pool_id: str,
        limits: dict[str, int],
        account_pool_limit_resolver: Callable[
            [], Awaitable[int | dict[str, int]]
        ]
        | None,
    ) -> ConcurrencyLease:
        redis = await self._redis()
        keys = self._keys(request_id, user_id, group_id, provider, account_pool_id)
        queued_at = _now_ms()
        meta_ttl = max(
            1,
            math.ceil(
                self.queue_timeout_seconds
                + self.lease_seconds
                + self.status_ttl_seconds
            ),
        )
        waiter_event = asyncio.Event()
        self._redis_waiter_events[request_id] = (
            waiter_event,
            keys["account_pool_scope"],
        )
        try:
            async with self._redis_command_semaphore:
                result = await redis.eval(
                    self._ENQUEUE_SCRIPT,
                    7,
                    keys["sequence"],
                    keys["meta"],
                    keys["wait_global"],
                    keys["wait_user"],
                    keys["wait_group"],
                    keys["wait_provider"],
                    keys["wait_account_pool"],
                    request_id,
                    user_id,
                    group_id,
                    provider,
                    account_pool_id,
                    limits["user"],
                    limits["group"],
                    limits["account_pool"],
                    limits["provider"],
                    limits["global"],
                    queued_at,
                    meta_ttl,
                    keys["user_scope"],
                    keys["group_scope"],
                    keys["account_pool_scope"],
                )
            if int(result) < 0:
                raise ChatConcurrencyError("排队请求编号已使用")
            await self._ensure_redis_event_listener()
            deadline = time.monotonic() + self.queue_timeout_seconds
            while True:
                waiter_event.clear()
                await self._refresh_dynamic_limits(
                    limits,
                    provider,
                    account_pool_limit_resolver,
                )
                now_ms = _now_ms()
                expires_at = now_ms + self.lease_seconds * 1000
                async with self._redis_command_semaphore:
                    admitted = await redis.eval(
                        self._TRY_ADMIT_SCRIPT,
                        12,
                        keys["meta"],
                        keys["wait_global"],
                        keys["wait_user"],
                        keys["wait_group"],
                        keys["wait_provider"],
                        keys["wait_account_pool"],
                        keys["active_global"],
                        keys["active_user"],
                        keys["active_group"],
                        keys["active_provider"],
                        keys["active_account_pool"],
                        self._redis_event_channel,
                        request_id,
                        now_ms,
                        expires_at,
                        self.lease_seconds,
                        keys["meta_prefix"],
                        self.lease_seconds + self.status_ttl_seconds,
                        limits["global"],
                        limits["user"],
                        limits["group"],
                        limits["provider"],
                        limits["account_pool"],
                    )
                if int(admitted) == 1:
                    return ConcurrencyLease(
                        coordinator=self,
                        request_id=request_id,
                        user_id=user_id,
                        group_id=group_id,
                        provider=provider,
                        account_pool_id=account_pool_id,
                        user_limit=limits["user"],
                        group_limit=limits["group"],
                        account_pool_limit=limits["account_pool"],
                        provider_limit=limits["provider"],
                        global_limit=limits["global"],
                        queued_at_ms=queued_at,
                        admitted_at_ms=now_ms,
                        lease_expires_at_ms=expires_at,
                    )
                if time.monotonic() >= deadline:
                    cleanup = self._queue_redis_release(
                        request_id,
                        user_id,
                        group_id,
                        provider,
                        account_pool_id,
                        "timed_out",
                        "queue_timeout",
                    )
                    try:
                        await cleanup
                    except Exception:
                        pass
                    raise ChatQueueTimeout("排队等待超时，请稍后重试")
                remaining = max(0.001, deadline - time.monotonic())
                await self._wait_for_redis_event(
                    waiter_event,
                    min(self._redis_event_retry_seconds, remaining),
                )
        except asyncio.CancelledError:
            cleanup = self._queue_redis_release(
                request_id,
                user_id,
                group_id,
                provider,
                account_pool_id,
                "cancelled",
                "queue_cancelled",
            )
            try:
                await asyncio.shield(cleanup)
            except Exception:
                pass
            raise
        except (ChatConcurrencyError, ChatQueueTimeout):
            raise
        except Exception as exc:
            try:
                cleanup = self._queue_redis_release(
                    request_id,
                    user_id,
                    group_id,
                    provider,
                    account_pool_id,
                    "failed",
                    "coordinator_error",
                )
                await cleanup
            except Exception:
                pass
            raise ChatCoordinatorUnavailable("并发协调服务暂时不可用") from exc
        finally:
            self._redis_waiter_events.pop(request_id, None)

    @staticmethod
    def _state_for_outcome(outcome: str) -> str:
        if outcome in {"success", "completed"}:
            return "completed"
        if "cancel" in outcome:
            return "cancelled"
        if "timeout" in outcome:
            return "timed_out"
        return "failed"

    async def release(self, lease: ConcurrencyLease, *, outcome: str) -> None:
        if self.backend == "memory":
            async with self._memory_condition:
                entry = self._memory_entries.get(lease.request_id)
                if entry is not None:
                    entry.update(
                        state=self._state_for_outcome(outcome),
                        outcome=str(outcome)[:64],
                        completed_at_ms=_now_ms(),
                    )
                self._memory_condition.notify_all()
            return
        await self._release_redis_values(
            lease.request_id,
            lease.user_id,
            lease.group_id,
            lease.provider,
            lease.account_pool_id,
            self._state_for_outcome(outcome),
            outcome,
        )

    async def _release_redis_values(
        self,
        request_id: str,
        user_id: str,
        group_id: str,
        provider: str,
        account_pool_id: str,
        state: str,
        outcome: str,
    ) -> None:
        redis = await self._redis()
        keys = self._keys(request_id, user_id, group_id, provider, account_pool_id)
        async with self._redis_maintenance_semaphore:
            await redis.eval(
                self._RELEASE_SCRIPT,
                12,
                keys["meta"],
                keys["wait_global"],
                keys["wait_user"],
                keys["wait_group"],
                keys["wait_provider"],
                keys["wait_account_pool"],
                keys["active_global"],
                keys["active_user"],
                keys["active_group"],
                keys["active_provider"],
                keys["active_account_pool"],
                self._redis_event_channel,
                request_id,
                state,
                str(outcome)[:64],
                _now_ms(),
                self.status_ttl_seconds,
                "1",
            )

    def _queue_redis_release(
        self,
        request_id: str,
        user_id: str,
        group_id: str,
        provider: str,
        account_pool_id: str,
        state: str,
        outcome: str,
    ) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._redis_deferred_releases.append(
            (
                request_id,
                user_id,
                group_id,
                provider,
                account_pool_id,
                state,
                outcome,
                future,
            )
        )
        task = self._redis_deferred_release_task
        if task is None or task.done():
            self._redis_deferred_release_task = asyncio.create_task(
                self._flush_redis_releases(),
                name="turtle-chat-release-batcher",
            )
        return future

    async def _flush_redis_releases(self) -> None:
        while True:
            await asyncio.sleep(0.01)
            batch = self._redis_deferred_releases
            self._redis_deferred_releases = []
            if not batch:
                return
            futures = [item[-1] for item in batch]
            try:
                redis = await self._redis()
                pipeline = redis.pipeline(transaction=False)
                for (
                    request_id,
                    user_id,
                    group_id,
                    provider,
                    account_pool_id,
                    state,
                    outcome,
                    _future,
                ) in batch:
                    keys = self._keys(
                        request_id,
                        user_id,
                        group_id,
                        provider,
                        account_pool_id,
                    )
                    pipeline.eval(
                        self._RELEASE_SCRIPT,
                        12,
                        keys["meta"],
                        keys["wait_global"],
                        keys["wait_user"],
                        keys["wait_group"],
                        keys["wait_provider"],
                        keys["wait_account_pool"],
                        keys["active_global"],
                        keys["active_user"],
                        keys["active_group"],
                        keys["active_provider"],
                        keys["active_account_pool"],
                        self._redis_event_channel,
                        request_id,
                        state,
                        str(outcome)[:64],
                        _now_ms(),
                        self.status_ttl_seconds,
                        "0",
                    )
                pipeline.publish(self._redis_event_channel, "capacity")
                async with self._redis_maintenance_semaphore:
                    await pipeline.execute()
            except Exception as exc:
                for future in futures:
                    if not future.done():
                        future.set_exception(exc)
            else:
                for future in futures:
                    if not future.done():
                        future.set_result(None)

    async def _heartbeat_loop(self, lease: ConcurrencyLease) -> None:
        interval = max(5, min(60, self.lease_seconds // 3))
        try:
            while not lease._released:
                await asyncio.sleep(interval)
                expires_at = _now_ms() + self.lease_seconds * 1000
                if self.backend == "memory":
                    async with self._memory_condition:
                        entry = self._memory_entries.get(lease.request_id)
                        if entry is None or entry["state"] != "admitted":
                            return
                        entry["lease_expires_at_ms"] = expires_at
                else:
                    redis = await self._redis()
                    keys = self._keys(
                        lease.request_id,
                        lease.user_id,
                        lease.group_id,
                        lease.provider,
                        lease.account_pool_id,
                    )
                    async with self._redis_maintenance_semaphore:
                        renewed = await redis.eval(
                            self._HEARTBEAT_SCRIPT,
                            6,
                            keys["meta"],
                            keys["active_global"],
                            keys["active_user"],
                            keys["active_group"],
                            keys["active_provider"],
                            keys["active_account_pool"],
                            lease.request_id,
                            expires_at,
                            self.lease_seconds + self.status_ttl_seconds,
                        )
                    if not int(renewed):
                        return
                lease.lease_expires_at_ms = expires_at
        except (asyncio.CancelledError, ChatCoordinatorUnavailable):
            return
        except Exception:
            # The lease TTL remains the final safety net if a heartbeat fails.
            return

    async def status(self, request_id: str, user_id: str) -> dict[str, Any]:
        normalized_id = _safe_request_id(request_id)
        if normalized_id != str(request_id):
            return {"request_id": str(request_id), "state": "unknown"}
        if self.backend == "memory":
            async with self._memory_condition:
                self._memory_cleanup(_now_ms())
                entry = self._memory_entries.get(normalized_id)
                if entry is None or entry["user_id"] != str(user_id):
                    return {"request_id": normalized_id, "state": "unknown"}
                waiting = sorted(
                    (item for item in self._memory_entries.values() if item["state"] == "queued"),
                    key=lambda item: item["sequence"],
                )
                position = next(
                    (index + 1 for index, item in enumerate(waiting) if item["request_id"] == normalized_id),
                    None,
                )
                return self._public_status(
                    entry,
                    position,
                    self._memory_active("user_id", entry["user_id"]),
                    self._memory_active("group_id", entry["group_id"]),
                    self._memory_active(
                        "account_pool_id", entry["account_pool_id"]
                    ),
                    self._memory_active("provider", entry["provider"]),
                    sum(1 for item in self._memory_entries.values() if item["state"] == "admitted"),
                )

        redis = await self._redis()
        meta_key = f"{self.prefix}:meta:{normalized_id}"
        async with self._redis_command_semaphore:
            meta = await redis.hgetall(meta_key)
        if not meta or meta.get("user_id") != str(user_id):
            return {"request_id": normalized_id, "state": "unknown"}
        keys = self._keys(
            normalized_id,
            meta["user_id"],
            meta["group_id"],
            meta["provider"],
            meta.get("account_pool_id") or f"{meta['provider']}-default",
        )
        now_ms = _now_ms()
        pipeline = redis.pipeline(transaction=False)
        for key in (
            keys["active_global"],
            keys["active_user"],
            keys["active_group"],
            keys["active_account_pool"],
            keys["active_provider"],
        ):
            pipeline.zremrangebyscore(key, "-inf", now_ms)
        pipeline.zrank(keys["wait_global"], normalized_id)
        pipeline.zcard(keys["active_user"])
        pipeline.zcard(keys["active_group"])
        pipeline.zcard(keys["active_account_pool"])
        pipeline.zcard(keys["active_provider"])
        pipeline.zcard(keys["active_global"])
        async with self._redis_command_semaphore:
            values = await pipeline.execute()
        rank, user_active, group_active, account_pool_active, provider_active, global_active = values[5:]
        position = int(rank) + 1 if rank is not None else None
        return self._public_status(
            meta,
            position,
            int(user_active),
            int(group_active),
            int(account_pool_active),
            int(provider_active),
            int(global_active),
        )

    @staticmethod
    def _public_status(
        entry: dict[str, Any],
        position: int | None,
        user_active: int,
        group_active: int,
        account_pool_active: int,
        provider_active: int,
        global_active: int,
    ) -> dict[str, Any]:
        def integer(name: str) -> int | None:
            value = entry.get(name)
            return int(value) if value not in (None, "") else None

        return {
            "request_id": entry.get("request_id"),
            "state": entry.get("state") or "unknown",
            "position": position,
            "provider": entry.get("provider"),
            "account_pool_id": entry.get("account_pool_id"),
            "group_id": entry.get("group_id"),
            "queued_at_ms": integer("queued_at_ms"),
            "admitted_at_ms": integer("admitted_at_ms"),
            "completed_at_ms": integer("completed_at_ms"),
            "outcome": entry.get("outcome"),
            "concurrency": {
                "user": {"active": user_active, "limit": integer("user_limit")},
                "group": {"active": group_active, "limit": integer("group_limit")},
                "account_pool": {
                    "active": account_pool_active,
                    "limit": integer("account_pool_limit"),
                },
                "provider": {"active": provider_active, "limit": integer("provider_limit")},
                "global": {"active": global_active, "limit": integer("global_limit")},
            },
        }

    async def _valid_wait_count(self, redis, key: str) -> int:
        async with self._redis_command_semaphore:
            members = await redis.zrange(key, 0, -1)
        if not members:
            return 0
        pipeline = redis.pipeline(transaction=False)
        for member in members:
            pipeline.hget(f"{self.prefix}:meta:{member}", "state")
        async with self._redis_command_semaphore:
            states = await pipeline.execute()
        stale = [member for member, state in zip(members, states) if state != "queued"]
        if stale:
            async with self._redis_command_semaphore:
                await redis.zrem(key, *stale)
        return len(members) - len(stale)

    async def snapshot(
        self,
        group_ids: list[str] | None = None,
        account_pools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        providers = ("gpt", "claude")
        group_ids = list(group_ids or [])
        account_pools = list(account_pools or [])
        if self.backend == "memory":
            async with self._memory_condition:
                self._memory_cleanup(_now_ms())
                entries = list(self._memory_entries.values())
                active = [entry for entry in entries if entry["state"] == "admitted"]
                queued = [entry for entry in entries if entry["state"] == "queued"]
                return {
                    "backend": "memory",
                    "global": {
                        "active": len(active),
                        "queued": len(queued),
                        "limit": self.global_limit,
                    },
                    "providers": [
                        {
                            "family": provider,
                            "active": sum(1 for item in active if item["provider"] == provider),
                            "queued": sum(1 for item in queued if item["provider"] == provider),
                            "limit": self._provider_limit(provider),
                        }
                        for provider in providers
                    ],
                    "groups": [
                        {
                            "group_id": group_id,
                            "active": sum(1 for item in active if item["group_id"] == group_id),
                            "queued": sum(1 for item in queued if item["group_id"] == group_id),
                        }
                        for group_id in group_ids
                    ],
                    "account_pools": [
                        {
                            "pool_id": str(pool.get("id") or ""),
                            "pool_name": str(pool.get("name") or pool.get("id") or "未知账号组"),
                            "active": sum(
                                1
                                for item in active
                                if item["account_pool_id"] == str(pool.get("id") or "")
                            ),
                            "queued": sum(
                                1
                                for item in queued
                                if item["account_pool_id"] == str(pool.get("id") or "")
                            ),
                            "limit": max(0, int(pool.get("limit") or 0)),
                        }
                        for pool in account_pools
                        if str(pool.get("id") or "")
                    ],
                }

        redis = await self._redis()
        now_ms = _now_ms()

        async def active_count(key: str) -> int:
            async with self._redis_command_semaphore:
                await redis.zremrangebyscore(key, "-inf", now_ms)
                return int(await redis.zcard(key))

        global_key = f"{self.prefix}:active:global"
        global_wait = f"{self.prefix}:wait:global"
        provider_items = []
        for provider in providers:
            provider_items.append(
                {
                    "family": provider,
                    "active": await active_count(f"{self.prefix}:active:provider:{provider}"),
                    "queued": await self._valid_wait_count(
                        redis, f"{self.prefix}:wait:provider:{provider}"
                    ),
                    "limit": self._provider_limit(provider),
                }
            )
        group_items = []
        for group_id in group_ids:
            scope = self._scope(group_id)
            group_items.append(
                {
                    "group_id": group_id,
                    "active": await active_count(f"{self.prefix}:active:group:{scope}"),
                    "queued": await self._valid_wait_count(
                        redis, f"{self.prefix}:wait:group:{scope}"
                    ),
                }
            )
        account_pool_items = []
        for pool in account_pools:
            pool_id = str(pool.get("id") or "")
            if not pool_id:
                continue
            scope = self._scope(pool_id)
            account_pool_items.append(
                {
                    "pool_id": pool_id,
                    "pool_name": str(pool.get("name") or pool_id),
                    "active": await active_count(
                        f"{self.prefix}:active:account-pool:{scope}"
                    ),
                    "queued": await self._valid_wait_count(
                        redis, f"{self.prefix}:wait:account-pool:{scope}"
                    ),
                    "limit": max(0, int(pool.get("limit") or 0)),
                }
            )
        return {
            "backend": "redis",
            "global": {
                "active": await active_count(global_key),
                "queued": await self._valid_wait_count(redis, global_wait),
                "limit": self.global_limit,
            },
            "providers": provider_items,
            "groups": group_items,
            "account_pools": account_pool_items,
        }


CHAT_CONCURRENCY = ChatConcurrencyCoordinator()
