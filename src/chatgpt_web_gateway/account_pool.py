"""Durable Provider account-pool routing without storing account credentials.

The pool owns only sanitized operational metadata. Each ChatGPT or Claude login
remains inside one isolated worker/auth directory; the Gateway stores only that
worker's internal endpoint and leases it for one request at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from claude_web_worker.models import CLAUDE_PUBLIC_MODEL, ROUTE_BY_KEY

from .account_quota import (
    ACCOUNT_QUOTA_PROFILES,
    normalize_quota_profile,
    quota_lane,
    quota_profiles_payload,
    selection_keys,
    selection_label,
)
from .capabilities import GPT_PUBLIC_MODEL, resolve_selection
from .upstream import (
    UpstreamClient,
    UpstreamFailure,
    extract_upstream_resource_metadata,
)


logger = logging.getLogger("uvicorn.error")

POOL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ACCOUNT_STATES = {"disabled", "ready", "cooldown", "reauth_required"}
SESSION_STATES = {"missing", "valid", "expired", "unknown"}
HEALTH_STATES = {"unknown", "healthy", "degraded", "unhealthy"}
PROVIDERS = {"gpt", "claude"}
FAIRNESS_WINDOW_SECONDS = 3 * 60 * 60
DEGRADED_PROBE_FAILURE_THRESHOLD = 3


class AccountPoolError(RuntimeError):
    """Base class for sanitized account-pool failures."""


class AccountUnavailable(AccountPoolError):
    """Raised when a pool has no account that can safely accept a request."""


class AccountPoolConflict(AccountPoolError):
    """Raised for invalid or conflicting administrator changes."""


@dataclass(frozen=True, slots=True)
class UpstreamAccount:
    id: str
    pool_id: str
    provider: str
    name: str
    worker_endpoint: str
    health_path: str | None
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class RateLimitRecoveryLease:
    request_id: str
    account_id: str
    selection_key: str


@dataclass(slots=True)
class AccountLease:
    router: "AccountPoolRouter"
    request_id: str
    account: UpstreamAccount
    released: bool = False
    heartbeat_task: asyncio.Task[None] | None = None

    def start_heartbeat(self) -> None:
        if self.heartbeat_task is not None:
            return
        self.heartbeat_task = asyncio.create_task(
            self._heartbeat(),
            name=f"account-lease-{self.request_id}",
        )

    async def _heartbeat(self) -> None:
        interval = max(1.0, min(60.0, self.router.lease_seconds / 3))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.router.renew(self.request_id, self.account.id)
                except Exception:
                    # PostgreSQL admission itself fails closed while the store
                    # is unavailable. Keep retrying inside the original lease
                    # window instead of abandoning renewal after one hiccup.
                    logger.warning(
                        "account_lease_renewal_failed id=%s account=%s",
                        self.request_id,
                        self.account.id,
                    )
                    continue
                if not renewed:
                    logger.warning(
                        "account_lease_renewal_stopped id=%s account=%s",
                        self.request_id,
                        self.account.id,
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def release(
        self,
        *,
        outcome: str,
        status_code: int | None = None,
        error_class: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        if self.released:
            return
        self.released = True
        heartbeat = self.heartbeat_task
        self.heartbeat_task = None
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self.router.release(
            self.request_id,
            self.account.id,
            outcome=outcome,
            status_code=status_code,
            error_class=error_class,
            retry_after_seconds=retry_after_seconds,
        )


class AccountStore(Protocol):
    def initialize(
        self,
        default_accounts: list[dict[str, Any]] | dict[str, Any],
    ) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def account(self, account_id: str) -> UpstreamAccount | None: ...

    def acquire(
        self,
        *,
        pool_id: str,
        request_id: str,
        user_id: str | None,
        chat_id: str | None,
        lease_seconds: int,
        selection_key: str = "latest:medium",
        excluded_account_ids: frozenset[str] = frozenset(),
        required_quota_profiles: frozenset[str] = frozenset(),
        migration_reason_hint: str | None = None,
    ) -> UpstreamAccount: ...

    def release(
        self,
        request_id: str,
        account_id: str,
        *,
        outcome: str,
        status_code: int | None,
        error_class: str | None,
        cooldown_seconds: int,
        retry_after_seconds: int | None = None,
    ) -> None: ...

    def renew(
        self,
        request_id: str,
        account_id: str,
        *,
        lease_seconds: int,
    ) -> bool: ...

    def claim_rate_limit_recoveries(
        self,
        *,
        limit: int,
        claim_seconds: int,
    ) -> list[RateLimitRecoveryLease]: ...

    def mark_probe(
        self,
        account_id: str,
        *,
        state: str,
        http_status: int | None,
        latency_ms: int,
        upstream_display_name: str | None = None,
        allow_reauth: bool = False,
    ) -> None: ...

    def begin_reauth(self, account_id: str) -> dict[str, Any]: ...

    def create_pool(
        self, *, provider: str, name: str, description: str
    ) -> dict[str, Any]: ...

    def update_pool(
        self, pool_id: str, *, name: str, description: str, enabled: bool
    ) -> dict[str, Any]: ...

    def delete_pool(self, pool_id: str) -> dict[str, Any]: ...

    def create_account(
        self,
        *,
        account_id: str | None,
        pool_id: str,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]: ...

    def update_account(
        self,
        account_id: str,
        *,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        enabled: bool,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]: ...


def _now() -> int:
    return int(time.time())


def _stable_tiebreak(pool_id: str, chat_id: str | None, account_id: str) -> int:
    material = f"{pool_id}:{chat_id or 'no-chat'}:{account_id}".encode()
    return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")


def _quota_usage(
    leases: list[dict[str, Any]],
    *,
    account_id: str,
    selection_key: str,
    window_seconds: int,
    now: int,
) -> dict[str, int | None]:
    since = now - (window_seconds or FAIRNESS_WINDOW_SECONDS)
    successes = [
        int(item.get("completed_at") or 0)
        for item in leases
        if item.get("account_id") == account_id
        and item.get("selection_key", "latest:medium") == selection_key
        and item.get("state") == "completed"
        and item.get("outcome") == "success"
        and int(item.get("completed_at") or 0) > since
    ]
    active = sum(
        1
        for item in leases
        if item.get("account_id") == account_id
        and item.get("selection_key", "latest:medium") == selection_key
        and item.get("state") == "active"
        and int(item.get("expires_at") or 0) > now
    )
    return {
        "used_count": len(successes),
        "active_count": active,
        "oldest_success_at": min(successes) if successes else None,
    }


def _lane_rate_limit_cooldown_seconds(
    account: dict[str, Any],
    selection_key: str,
    *,
    failures: int,
    cooldown_seconds: int,
    elapsed_seconds: int = 0,
    retry_after_seconds: int | None = None,
) -> int:
    """Back off dynamic upstream limits without using users as frequent probes."""
    if retry_after_seconds is not None:
        # Trust only the already-sanitized numeric hint and keep a hard bound
        # against malformed or unexpectedly distant upstream values.
        return min(24 * 60 * 60, max(60, int(retry_after_seconds)))
    lane = quota_lane(
        account.get("quota_profile"),
        selection_key,
        str(account.get("provider") or "gpt"),
    )
    if lane.get("source") in {"official_dynamic", "official_multiplier"}:
        published_window = max(
            15 * 60,
            int(lane.get("published_window_seconds") or 5 * 60 * 60),
        )
        cap = min(5 * 60 * 60, published_window)
        exponential = 15 * 60 * (2 ** min(max(0, failures - 1), 5))
        remaining_in_window = max(0, cap - max(0, int(elapsed_seconds)))
        if remaining_in_window:
            # Do not let exponential backoff jump past the absolute published
            # reset window measured from the first observed 429.
            return min(
                remaining_in_window,
                max(cooldown_seconds, exponential),
            )
        # Dynamic/anti-abuse restrictions can outlive the nominal window.
        # Once the published boundary has passed, probe every 30 minutes
        # instead of starting another multi-hour exponential cycle.
        return 30 * 60
    return min(
        15 * 60,
        max(cooldown_seconds, 15 * (2 ** min(failures, 5))),
    )


def _soft_concurrency_limit(account: dict[str, Any]) -> int:
    """Leave headroom before spilling new work into idle broader accounts."""
    hard_limit = max(1, int(account.get("max_concurrency") or 1))
    return max(1, (hard_limit * 3 + 3) // 4)


def _rate_limit_recovery_payload(
    provider: str,
    selection_key: str,
) -> dict[str, Any]:
    version, separator, level = selection_key.partition(":")
    if not separator or not version or not level:
        raise AccountPoolConflict("账号恢复探测档位无效")
    if provider == "claude":
        route = ROUTE_BY_KEY.get(selection_key)
        if route is None:
            raise AccountPoolConflict("Claude 恢复探测档位无效")
        return {
            "model": CLAUDE_PUBLIC_MODEL,
            "messages": [{"role": "user", "content": "Reply only: OK"}],
            "stream": False,
            "web_search": False,
            "turtle_claude_model": route.version,
            "turtle_claude_thinking": route.level,
        }
    upstream_model, reasoning_effort = resolve_selection(
        public_model_name=GPT_PUBLIC_MODEL,
        default_upstream_model="auto",
        version=version,
        thinking_level=level,
    )
    return {
        "model": upstream_model,
        "messages": [{"role": "user", "content": "Reply only: OK"}],
        "stream": False,
        "temporary": True,
        **(
            {"reasoning_effort": reasoning_effort}
            if reasoning_effort is not None
            else {}
        ),
    }


def _completion_has_effective_content(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in content
        )
    return False


def _lane_snapshot(
    account: dict[str, Any],
    selection_key: str,
    *,
    usage: dict[str, int | None],
    blocked_until: int | None,
    first_rate_limit_at: int | None = None,
    last_rate_limit_at: int | None = None,
    consecutive_failures: int = 0,
    now: int,
) -> dict[str, Any]:
    provider = str(account.get("provider") or "gpt")
    profile_id = str(account.get("quota_profile") or "untracked")
    lane = quota_lane(profile_id, selection_key, provider)
    used = max(0, int(usage.get("used_count") or 0))
    active = max(0, int(usage.get("active_count") or 0))
    budget = lane.get("dispatch_budget_count")
    reserve = max(0, int(lane.get("reserve_count") or 0))
    enabled = bool(lane.get("enabled"))
    # A lane-level 429 remains excluded until a background probe explicitly
    # clears its durable row. ``blocked_until`` is the next probe time, not an
    # automatic user-scheduling recovery time.
    blocked = blocked_until is not None
    remaining = None if budget is None else max(0, int(budget) - used - active)
    safe_remaining = None if remaining is None else max(0, remaining - reserve)
    reset_at = None
    if lane.get("window_seconds") and usage.get("oldest_success_at"):
        reset_at = int(usage["oldest_success_at"]) + int(lane["window_seconds"])
    if not enabled:
        state = "disabled"
    elif blocked:
        state = "cooldown"
    elif remaining == 0:
        state = "exhausted"
    elif remaining is not None and remaining <= reserve:
        state = "reserve"
    elif budget is None:
        state = (
            "dynamic"
            if lane.get("source") in {"official_dynamic", "official_multiplier"}
            else "untracked"
        )
    else:
        state = "available"
    pressure = (
        (used + active) / max(1, int(budget))
        if budget is not None
        else None
    )
    return {
        "selection_key": selection_key,
        "label": selection_label(provider, selection_key),
        **lane,
        "used_count": used,
        "active_count": active,
        "remaining_count": remaining,
        "safe_remaining_count": safe_remaining,
        "reset_at": reset_at,
        "blocked_until": int(blocked_until) if blocked else None,
        "first_rate_limit_at": (
            int(first_rate_limit_at)
            if first_rate_limit_at is not None
            else None
        ),
        "last_rate_limit_at": (
            int(last_rate_limit_at)
            if last_rate_limit_at is not None
            else None
        ),
        "consecutive_rate_limit_failures": max(
            0,
            int(consecutive_failures),
        ),
        "state": state,
        "admission_available": enabled
        and not blocked
        and (budget is None or used < int(budget)),
        "available": enabled and not blocked and remaining != 0,
        "pressure": pressure,
    }


def _account_quota_snapshot(
    account: dict[str, Any],
    *,
    leases: list[dict[str, Any]],
    lane_blocks: dict[tuple[str, str], dict[str, Any]],
    now: int,
) -> dict[str, Any]:
    provider = str(account.get("provider") or "gpt")
    profile_id = str(account.get("quota_profile") or "untracked")
    normalized_profile = normalize_quota_profile(profile_id, provider)
    profile = (
        ACCOUNT_QUOTA_PROFILES[normalized_profile]
        if provider == "gpt"
        else quota_profiles_payload("claude")[0]
    )
    lanes = []
    for selection_key in selection_keys(provider):
        lane = quota_lane(profile_id, selection_key, provider)
        usage = _quota_usage(
            leases,
            account_id=str(account["id"]),
            selection_key=selection_key,
            window_seconds=int(lane.get("window_seconds") or FAIRNESS_WINDOW_SECONDS),
            now=now,
        )
        block = lane_blocks.get((str(account["id"]), selection_key)) or {}
        lanes.append(
            _lane_snapshot(
                account,
                selection_key,
                usage=usage,
                blocked_until=block.get("blocked_until"),
                first_rate_limit_at=block.get("first_rate_limit_at"),
                last_rate_limit_at=block.get("last_rate_limit_at"),
                consecutive_failures=int(
                    block.get("consecutive_failures") or 0
                ),
                now=now,
            )
        )
    enabled_lanes = [item for item in lanes if item["enabled"]]
    available_lanes = [item for item in enabled_lanes if item["available"]]
    tracked = [item for item in enabled_lanes if item["dispatch_budget_count"] is not None]
    tightest = min(
        tracked,
        key=lambda item: (
            item["safe_remaining_count"] / max(1, int(item["dispatch_budget_count"])),
            item["selection_key"],
        ),
        default=None,
    )
    return {
        "profile_id": profile_id,
        "profile_label": str(profile["label"]),
        "tracked": profile_id != "untracked",
        "enabled_lane_count": len(enabled_lanes),
        "available_lane_count": len(available_lanes),
        "tightest_lane": dict(tightest) if tightest else None,
        "lanes": lanes,
    }


def _routing_capability_width(account: dict[str, Any]) -> int:
    """Return how many published lanes this account can serve.

    New chats should consume the narrowest account that can satisfy the
    selected lane.  This preserves broader Pro capacity for Pro-only routes
    without coupling routing to a user's overall model permissions.
    """

    provider = _normalize_provider(account.get("provider"))
    return sum(
        1
        for selection_key in selection_keys(provider)
        if quota_lane(
            account.get("quota_profile"),
            selection_key,
            provider,
        ).get("enabled")
    )


def _routing_plan_rank(account: dict[str, Any]) -> int:
    """Prefer the smallest same-capability subscription before broader plans."""

    return {
        "free": 0,
        "go": 1,
        "plus": 2,
        "pro": 3,
        "pro-5x": 4,
        "max-5x": 4,
        "pro-20x": 5,
        "max-20x": 5,
        "untracked": 99,
    }.get(str(account.get("quota_profile") or "untracked"), 50)


def _choose_account(
    *,
    pool_id: str,
    chat_id: str | None,
    preferred_account_id: str | None,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str | None]:
    """Choose an account without moving a healthy sticky chat for mere load."""

    preferred = next(
        (item for item in candidates if item["id"] == preferred_account_id),
        None,
    )
    migration_reason: str | None = None
    available = [
        item
        for item in candidates
        if item.get("base_eligible")
        and int(item.get("active") or 0) < int(item.get("max_concurrency") or 1)
        and item["selected_lane"]["available"]
    ]

    if preferred is not None and preferred.get("base_eligible"):
        preferred_lane = preferred["selected_lane"]
        if preferred_lane["available"]:
            if int(preferred.get("active") or 0) >= int(
                preferred.get("max_concurrency") or 1
            ):
                overflow = [
                    item
                    for item in available
                    if item["id"] != preferred["id"]
                ]
                if not overflow:
                    raise AccountUnavailable("会话绑定账号正忙，请稍后重试")
                # The same-chat active-lease guard has already proved that no
                # turn is running. Full Turtle history is authoritative, so a
                # hard-cap spillover can safely rebuild the upstream
                # conversation on an idle account instead of returning a local
                # busy error while pool capacity exists.
                migration_reason = "concurrency_overflow"
                available = overflow
            else:
                healthier_budget = [
                    item
                    for item in available
                    if item["id"] != preferred["id"]
                    and preferred_lane["state"] == "reserve"
                    and item["selected_lane"]["state"] in {
                        "available",
                        "dynamic",
                        "untracked",
                    }
                ]
                if not healthier_budget:
                    return preferred, None
                migration_reason = "quota_reserve"
                available = healthier_budget
        else:
            migration_reason = (
                "quota_disabled"
                if preferred_lane["state"] == "disabled"
                else "quota_cooldown"
                if preferred_lane["state"] == "cooldown"
                else "quota_exhausted"
            )
    elif preferred_account_id:
        migration_reason = "account_unavailable"

    if not available:
        raise AccountUnavailable("当前账号组没有适合该档位的可调度账号")

    known_budgets = [
        int(item["selected_lane"]["dispatch_budget_count"])
        for item in available
        if item["selected_lane"]["dispatch_budget_count"] is not None
    ]
    unknown_weight = max(1, min(known_budgets, default=1))
    preferred_class = min(
        (
            _routing_capability_width(item),
            _routing_plan_rank(item),
        )
        for item in available
    )
    preferred_class_has_headroom = any(
        (
            _routing_capability_width(item),
            _routing_plan_rank(item),
        )
        == preferred_class
        and int(item.get("active") or 0) < _soft_concurrency_limit(item)
        for item in available
    )

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        lane = item["selected_lane"]
        pressure = lane.get("pressure")
        dispatch_budget = lane.get("dispatch_budget_count")
        if pressure is None:
            pressure = (int(lane["used_count"]) + int(lane["active_count"])) / unknown_weight
        account_class = (
            _routing_capability_width(item),
            _routing_plan_rank(item),
        )
        elastic_overflow_rank = (
            0
            if preferred_class_has_headroom or account_class == preferred_class
            else (
                0
                if int(item.get("active") or 0) < _soft_concurrency_limit(item)
                else 1
            )
        )
        return (
            1 if lane["state"] == "reserve" else 0,
            elastic_overflow_rank,
            (
                0
                if preferred_class_has_headroom
                else int(item.get("active") or 0)
                / max(1, int(item.get("max_concurrency") or 1))
            ),
            _routing_capability_width(item),
            _routing_plan_rank(item),
            int(dispatch_budget) if dispatch_budget is not None else 2**63 - 1,
            float(pressure),
            int(item.get("active") or 0) / max(1, int(item.get("max_concurrency") or 1)),
            -int(item.get("priority") or 0),
            _stable_tiebreak(pool_id, chat_id, str(item["id"])),
            int(item.get("last_used_at") or 0),
            str(item["id"]),
        )

    return min(available, key=rank), migration_reason


def _account_from_mapping(value: dict[str, Any]) -> UpstreamAccount:
    return UpstreamAccount(
        id=str(value["id"]),
        pool_id=str(value["pool_id"]),
        provider=_normalize_provider(value.get("provider")),
        name=str(value["name"]),
        worker_endpoint=str(value["worker_endpoint"]),
        health_path=str(value["health_path"]) if value.get("health_path") else None,
        max_concurrency=max(1, int(value.get("max_concurrency") or 1)),
    )


class MemoryAccountStore:
    """Single-process store used by compatibility mode and deterministic tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.pools: dict[str, dict[str, Any]] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.affinity: dict[tuple[str, str], dict[str, Any]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.lane_blocks: dict[tuple[str, str], dict[str, Any]] = {}

    def initialize(
        self,
        default_accounts: list[dict[str, Any]] | dict[str, Any],
    ) -> None:
        defaults = (
            [default_accounts]
            if isinstance(default_accounts, dict)
            else default_accounts
        )
        with self._lock:
            for default_account in defaults:
                provider = _normalize_provider(default_account.get("provider"))
                pool_id = str(default_account["pool_id"])
                self.pools.setdefault(
                    pool_id,
                    {
                        "id": pool_id,
                        "name": (
                            "ChatGPT 默认账号池"
                            if provider == "gpt"
                            else "Claude 默认账号池"
                        ),
                        "description": "部署兼容账号池",
                        "provider": provider,
                        "enabled": True,
                        "created_at": _now(),
                        "updated_at": _now(),
                    },
                )
                account_id = str(default_account["id"])
                self.accounts.setdefault(account_id, dict(default_account))

    def _expire(self, now: int) -> None:
        for lease in self.leases.values():
            if lease["state"] == "active" and int(lease["expires_at"]) <= now:
                lease["state"] = "expired"
                lease["completed_at"] = now
                lease["outcome"] = "lease_expired"
        for account in self.accounts.values():
            cooldown_until = int(account.get("cooldown_until") or 0)
            if account.get("status") == "cooldown" and cooldown_until <= now:
                account["status"] = (
                    "ready"
                    if account.get("enabled") and account.get("session_state") == "valid"
                    else "disabled"
                )
                account["health_status"] = "unknown"
                account["cooldown_until"] = None
                account["updated_at"] = now
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = _now()
            self._expire(now)
            accounts: list[dict[str, Any]] = []
            for source in self.accounts.values():
                active = sum(
                    1
                    for lease in self.leases.values()
                    if lease["account_id"] == source["id"] and lease["state"] == "active"
                )
                item = dict(source)
                item["active"] = active
                affinities = [
                    value
                    for value in self.affinity.values()
                    if value.get("preferred_account_id") == source["id"]
                ]
                item["sticky_chat_count"] = len(affinities)
                item["affinity_migration_count"] = sum(
                    int(value.get("migration_count") or 0) for value in affinities
                )
                item["available"] = self._schedulable(item, active, now)
                item["quota"] = _account_quota_snapshot(
                    item,
                    leases=list(self.leases.values()),
                    lane_blocks=self.lane_blocks,
                    now=now,
                )
                accounts.append(item)
            pools = []
            for source in self.pools.values():
                members = [item for item in accounts if item["pool_id"] == source["id"]]
                pool = dict(source)
                admission_members = [
                    item
                    for item in members
                    if source.get("enabled") and self._admission_eligible(item, now)
                ]
                pool.update(
                    {
                        "account_count": len(members),
                        "ready_count": sum(1 for item in members if item["available"]),
                        "active": sum(int(item["active"]) for item in members),
                        "capacity": sum(
                            int(item["max_concurrency"])
                            for item in members
                            if item.get("enabled")
                        ),
                        "admission_capacity": sum(
                            int(item["max_concurrency"])
                            for item in admission_members
                        ),
                        "available_slots": sum(
                            max(0, int(item["max_concurrency"]) - int(item["active"]))
                            for item in admission_members
                        ),
                        "sticky_chat_count": sum(
                            int(item.get("sticky_chat_count") or 0) for item in members
                        ),
                        "affinity_migration_count": sum(
                            int(item.get("affinity_migration_count") or 0)
                            for item in members
                        ),
                    }
                )
                pools.append(pool)
            return {
                "enabled": False,
                "backend": "memory",
                "quota_profiles": quota_profiles_payload(),
                "quota_profiles_by_provider": {
                    provider: quota_profiles_payload(provider)
                    for provider in sorted(PROVIDERS)
                },
                "pools": sorted(pools, key=lambda item: (item["name"], item["id"])),
                "accounts": sorted(accounts, key=lambda item: (item["pool_id"], -int(item["priority"]), item["name"])),
            }

    @staticmethod
    def _admission_eligible(account: dict[str, Any], now: int) -> bool:
        return (
            bool(account.get("enabled"))
            and account.get("status") == "ready"
            and account.get("session_state") == "valid"
            and account.get("health_status") in {"unknown", "healthy"}
            and (not account.get("cooldown_until") or int(account["cooldown_until"]) <= now)
        )

    @classmethod
    def _schedulable(cls, account: dict[str, Any], active: int, now: int) -> bool:
        return cls._admission_eligible(account, now) and active < int(
            account.get("max_concurrency") or 1
        )

    def account(self, account_id: str) -> UpstreamAccount | None:
        with self._lock:
            value = self.accounts.get(str(account_id))
            return _account_from_mapping(value) if value else None

    def acquire(
        self,
        *,
        pool_id: str,
        request_id: str,
        user_id: str | None,
        chat_id: str | None,
        lease_seconds: int,
        selection_key: str = "latest:medium",
        excluded_account_ids: frozenset[str] = frozenset(),
        required_quota_profiles: frozenset[str] = frozenset(),
        migration_reason_hint: str | None = None,
    ) -> UpstreamAccount:
        with self._lock:
            now = _now()
            self._expire(now)
            if request_id in self.leases:
                raise AccountUnavailable("请求标识已使用")
            pool = self.pools.get(pool_id)
            if not pool or not pool.get("enabled"):
                raise AccountUnavailable("账号池不存在或已停用")
            provider = _normalize_provider(pool.get("provider"))
            if selection_key not in selection_keys(provider):
                raise AccountUnavailable("请求档位无效")
            if chat_id and any(
                lease.get("pool_id") == pool_id
                and lease.get("chat_id") == chat_id
                and lease.get("state") == "active"
                and int(lease.get("expires_at") or 0) > now
                for lease in self.leases.values()
            ):
                raise AccountUnavailable("同一会话已有请求正在生成")
            active_by_account = {
                account_id: sum(
                    1
                    for lease in self.leases.values()
                    if lease["account_id"] == account_id and lease["state"] == "active"
                )
                for account_id in self.accounts
            }
            affinity = self.affinity.get((pool_id, str(chat_id))) if chat_id else None
            preferred = str(affinity["preferred_account_id"]) if affinity else None
            leases = list(self.leases.values())
            candidates: list[dict[str, Any]] = []
            for value in self.accounts.values():
                if value["pool_id"] != pool_id:
                    continue
                item = dict(value)
                item["active"] = active_by_account[value["id"]]
                item["base_eligible"] = (
                    str(item["id"]) not in excluded_account_ids
                    and (
                        not required_quota_profiles
                        or str(item.get("quota_profile") or "untracked")
                        in required_quota_profiles
                    )
                    and self._admission_eligible(item, now)
                )
                item["provider"] = provider
                lane = quota_lane(
                    item.get("quota_profile"),
                    selection_key,
                    provider,
                )
                usage = _quota_usage(
                    leases,
                    account_id=str(item["id"]),
                    selection_key=selection_key,
                    window_seconds=int(lane.get("window_seconds") or FAIRNESS_WINDOW_SECONDS),
                    now=now,
                )
                block = self.lane_blocks.get((str(item["id"]), selection_key)) or {}
                item["selected_lane"] = _lane_snapshot(
                    item,
                    selection_key,
                    usage=usage,
                    blocked_until=block.get("blocked_until"),
                    now=now,
                )
                candidates.append(item)
            selected, migration_reason = _choose_account(
                pool_id=pool_id,
                chat_id=chat_id,
                preferred_account_id=preferred,
                candidates=candidates,
            )
            if (
                migration_reason_hint
                and preferred
                and str(selected["id"]) != preferred
            ):
                migration_reason = migration_reason_hint
            selected_source = self.accounts[str(selected["id"])]
            selected_source["last_used_at"] = now
            self.leases[request_id] = {
                "request_id": request_id,
                "account_id": selected["id"],
                "pool_id": pool_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "selection_key": selection_key,
                "state": "active",
                "leased_at": now,
                "expires_at": now + lease_seconds,
                "completed_at": None,
                "outcome": None,
                "error_class": None,
            }
            if chat_id:
                key = (pool_id, str(chat_id))
                existing = self.affinity.get(key)
                migrated = bool(existing and existing["preferred_account_id"] != selected["id"])
                self.affinity[key] = {
                    "pool_id": pool_id,
                    "chat_id": str(chat_id),
                    "user_id": user_id,
                    "preferred_account_id": selected["id"],
                    "created_at": int(existing.get("created_at") or now) if existing else now,
                    "updated_at": now,
                    "last_routed_at": now,
                    "migration_count": int(existing.get("migration_count") or 0) + (1 if migrated else 0) if existing else 0,
                    "last_migrated_at": now if migrated else existing.get("last_migrated_at") if existing else None,
                    "last_migration_reason": migration_reason if migrated else existing.get("last_migration_reason") if existing else None,
                }
            return _account_from_mapping(selected_source)

    def release(
        self,
        request_id: str,
        account_id: str,
        *,
        outcome: str,
        status_code: int | None,
        error_class: str | None,
        cooldown_seconds: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        with self._lock:
            now = _now()
            lease = self.leases.get(request_id)
            if not lease or lease["account_id"] != account_id or lease["state"] != "active":
                return
            lease.update(
                state=(
                    "completed"
                    if outcome == "success"
                    else "cancelled"
                    if outcome == "cancelled"
                    else "failed"
                ),
                completed_at=now,
                outcome=outcome,
                error_class=error_class,
            )
            account = self.accounts.get(account_id)
            if not account:
                return
            if status_code == 429:
                selection_key = str(lease.get("selection_key") or "latest:medium")
                block_key = (account_id, selection_key)
                previous = self.lane_blocks.get(block_key) or {}
                failures = int(previous.get("consecutive_failures") or 0) + 1
                first_rate_limit_at = int(
                    previous.get("first_rate_limit_at")
                    or previous.get("last_rate_limit_at")
                    or now
                )
                self.lane_blocks[block_key] = {
                    "account_id": account_id,
                    "selection_key": selection_key,
                    "blocked_until": now
                    + _lane_rate_limit_cooldown_seconds(
                        account,
                        selection_key,
                        failures=failures,
                        cooldown_seconds=cooldown_seconds,
                        elapsed_seconds=now - first_rate_limit_at,
                        retry_after_seconds=retry_after_seconds,
                    ),
                    "first_rate_limit_at": first_rate_limit_at,
                    "last_rate_limit_at": now,
                    "consecutive_failures": failures,
                }
            elif outcome == "success":
                selection_key = str(lease.get("selection_key") or "latest:medium")
                self.lane_blocks.pop((account_id, selection_key), None)
            _apply_account_result(
                account,
                outcome=outcome,
                status_code=status_code,
                error_class=error_class,
                cooldown_seconds=cooldown_seconds,
                now=now,
            )

    def renew(
        self,
        request_id: str,
        account_id: str,
        *,
        lease_seconds: int,
    ) -> bool:
        with self._lock:
            now = _now()
            self._expire(now)
            lease = self.leases.get(request_id)
            if (
                not lease
                or lease["account_id"] != account_id
                or lease["state"] != "active"
            ):
                return False
            lease["expires_at"] = now + lease_seconds
            return True

    def claim_rate_limit_recoveries(
        self,
        *,
        limit: int,
        claim_seconds: int,
    ) -> list[RateLimitRecoveryLease]:
        with self._lock:
            now = _now()
            self._expire(now)
            claimed: list[RateLimitRecoveryLease] = []
            due = sorted(
                (
                    (key, block)
                    for key, block in self.lane_blocks.items()
                    if int(block.get("blocked_until") or 0) <= now
                ),
                key=lambda item: (
                    int(item[1].get("blocked_until") or 0),
                    item[0],
                ),
            )
            for (account_id, selection_key), block in due:
                if len(claimed) >= max(1, int(limit)):
                    break
                account = self.accounts.get(account_id)
                if (
                    not account
                    or not bool(account.get("enabled"))
                    or account.get("status") != "ready"
                    or account.get("session_state") != "valid"
                    or account.get("health_status") not in {"unknown", "healthy"}
                ):
                    continue
                active = sum(
                    1
                    for lease in self.leases.values()
                    if lease["account_id"] == account_id
                    and lease["state"] == "active"
                    and int(lease["expires_at"]) > now
                )
                if active >= int(account.get("max_concurrency") or 1):
                    continue
                request_id = f"recovery-{uuid.uuid4().hex[:24]}"
                self.leases[request_id] = {
                    "request_id": request_id,
                    "account_id": account_id,
                    "pool_id": account["pool_id"],
                    "user_id": None,
                    "chat_id": None,
                    "selection_key": selection_key,
                    "state": "active",
                    "leased_at": now,
                    "expires_at": now + max(1, int(claim_seconds)),
                    "completed_at": None,
                    "outcome": None,
                    "error_class": None,
                }
                block["blocked_until"] = now + max(1, int(claim_seconds))
                block["updated_at"] = now
                claimed.append(
                    RateLimitRecoveryLease(
                        request_id=request_id,
                        account_id=account_id,
                        selection_key=selection_key,
                    )
                )
            return claimed

    def mark_probe(
        self,
        account_id: str,
        *,
        state: str,
        http_status: int | None,
        latency_ms: int,
        upstream_display_name: str | None = None,
        allow_reauth: bool = False,
    ) -> None:
        with self._lock:
            account = self.accounts.get(account_id)
            if not account:
                raise AccountPoolConflict("账号不存在")
            _apply_probe(
                account,
                state=state,
                http_status=http_status,
                latency_ms=latency_ms,
                upstream_display_name=upstream_display_name,
                allow_reauth=allow_reauth,
            )

    def begin_reauth(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            now = _now()
            self._expire(now)
            account = self.accounts.get(account_id)
            if not account:
                raise AccountPoolConflict("账号不存在")
            active = sum(
                1
                for lease in self.leases.values()
                if lease["account_id"] == account_id and lease["state"] == "active"
            )
            if active:
                raise AccountPoolConflict("账号仍有活动请求，无法开始重新登录")
            account.update(
                status="reauth_required",
                session_state="expired",
                health_status="unhealthy",
                last_error_class="manual_reauth",
                cooldown_until=None,
                updated_at=now,
            )
            result = dict(account)
            result["active"] = 0
            result["available"] = False
            return result

    def create_pool(
        self, *, provider: str, name: str, description: str
    ) -> dict[str, Any]:
        with self._lock:
            if any(item["name"] == name for item in self.pools.values()):
                raise AccountPoolConflict("账号池名称已存在")
            pool_id = f"pool-{uuid.uuid4().hex[:12]}"
            value = {
                "id": pool_id,
                "name": name,
                "description": description,
                "provider": _normalize_provider(provider),
                "enabled": True,
                "created_at": _now(),
                "updated_at": _now(),
            }
            self.pools[pool_id] = value
            return dict(value)

    def update_pool(
        self, pool_id: str, *, name: str, description: str, enabled: bool
    ) -> dict[str, Any]:
        with self._lock:
            value = self.pools.get(pool_id)
            if not value:
                raise AccountPoolConflict("账号池不存在")
            if any(item["id"] != pool_id and item["name"] == name for item in self.pools.values()):
                raise AccountPoolConflict("账号池名称已存在")
            value.update(name=name, description=description, enabled=bool(enabled), updated_at=_now())
            return dict(value)

    def delete_pool(self, pool_id: str) -> dict[str, Any]:
        with self._lock:
            value = self.pools.get(pool_id)
            if value is None:
                raise AccountPoolConflict("账号池不存在")
            if any(item["pool_id"] == pool_id for item in self.accounts.values()):
                raise AccountPoolConflict("账号池仍有账号，不能删除")
            deleted = dict(value)
            del self.pools[pool_id]
            deleted["deleted"] = True
            return deleted

    def create_account(
        self,
        *,
        account_id: str | None = None,
        pool_id: str,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        with self._lock:
            pool = self.pools.get(pool_id)
            if pool is None:
                raise AccountPoolConflict("账号池不存在")
            provider = _normalize_provider(pool.get("provider"))
            if any(item["worker_endpoint"] == worker_endpoint for item in self.accounts.values()):
                raise AccountPoolConflict("该 worker 地址已被其他账号使用")
            resolved_account_id = account_id or f"acct-{uuid.uuid4().hex[:12]}"
            if resolved_account_id in self.accounts:
                raise AccountPoolConflict("账号标识已存在")
            value = _new_account(
                resolved_account_id,
                pool_id=pool_id,
                provider=provider,
                name=name,
                worker_endpoint=worker_endpoint,
                health_path=health_path,
                max_concurrency=max_concurrency,
                priority=priority,
                quota_profile=quota_profile,
            )
            self.accounts[resolved_account_id] = value
            return dict(value)

    def update_account(
        self,
        account_id: str,
        *,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        enabled: bool,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        with self._lock:
            value = self.accounts.get(account_id)
            if not value:
                raise AccountPoolConflict("账号不存在")
            provider = _normalize_provider(
                (self.pools.get(str(value["pool_id"])) or {}).get("provider")
            )
            if any(
                item["id"] != account_id and item["worker_endpoint"] == worker_endpoint
                for item in self.accounts.values()
            ):
                raise AccountPoolConflict("该 worker 地址已被其他账号使用")
            endpoint_changed = (
                value.get("worker_endpoint") != worker_endpoint
                or value.get("health_path") != health_path
            )
            value.update(
                name=name,
                worker_endpoint=worker_endpoint,
                health_path=health_path,
                max_concurrency=max_concurrency,
                priority=priority,
                quota_profile=normalize_quota_profile(quota_profile, provider),
                enabled=bool(enabled),
                updated_at=_now(),
            )
            if endpoint_changed:
                value.update(
                    status="disabled",
                    session_state="unknown",
                    health_status="unknown",
                    last_error_class=None,
                    consecutive_failures=0,
                    cooldown_until=None,
                )
            elif not enabled:
                value["status"] = "disabled"
            elif value["session_state"] == "valid" and value["health_status"] == "healthy":
                value["status"] = "ready"
            return dict(value)


def _new_account(
    account_id: str,
    *,
    pool_id: str,
    provider: str = "gpt",
    name: str,
    worker_endpoint: str,
    health_path: str | None,
    max_concurrency: int,
    priority: int,
    quota_profile: str = "untracked",
    deployment_managed: bool = False,
) -> dict[str, Any]:
    now = _now()
    normalized_provider = _normalize_provider(provider)
    return {
        "id": account_id,
        "pool_id": pool_id,
        "provider": normalized_provider,
        "name": name,
        "worker_endpoint": worker_endpoint,
        "health_path": health_path,
        "enabled": bool(deployment_managed),
        "status": "ready" if deployment_managed else "disabled",
        "session_state": "valid" if deployment_managed else "missing",
        "health_status": "unknown",
        "max_concurrency": max_concurrency,
        "priority": priority,
        "quota_profile": normalize_quota_profile(
            quota_profile,
            normalized_provider,
        ),
        "last_used_at": None,
        "last_success_at": None,
        "last_health_at": None,
        "last_error_class": None,
        "last_probe_latency_ms": None,
        "upstream_display_name": None,
        "upstream_identity_updated_at": None,
        "consecutive_failures": 0,
        "cooldown_until": None,
        "deployment_managed": bool(deployment_managed),
        "created_at": now,
        "updated_at": now,
    }


def _apply_probe(
    account: dict[str, Any],
    *,
    state: str,
    http_status: int | None,
    latency_ms: int,
    upstream_display_name: str | None = None,
    allow_reauth: bool = False,
) -> None:
    now = _now()
    account["last_health_at"] = now
    account["last_probe_latency_ms"] = max(0, int(latency_ms))
    identity_name = _safe_upstream_display_name(upstream_display_name)
    if state == "ready" and identity_name:
        account["upstream_display_name"] = identity_name
        account["upstream_identity_updated_at"] = now
    if state == "ready":
        if account.get("status") == "reauth_required" and not allow_reauth:
            account.update(
                health_status="healthy",
                last_success_at=now,
                consecutive_failures=0,
                cooldown_until=None,
                updated_at=now,
            )
            return
        account.update(
            status="ready" if account.get("enabled") else "disabled",
            session_state="valid",
            health_status="healthy",
            last_success_at=now,
            last_error_class=None,
            consecutive_failures=0,
            cooldown_until=None,
        )
    elif state == "auth_required" or http_status in {401, 403}:
        account.update(
            status="reauth_required",
            session_state="expired",
            health_status="unhealthy",
            last_error_class="authentication",
        )
    elif state == "offline":
        account.update(
            health_status="unhealthy",
            last_error_class="offline",
            consecutive_failures=int(account.get("consecutive_failures") or 0) + 1,
        )
    else:
        failures = int(account.get("consecutive_failures") or 0) + 1
        keep_scheduling = (
            bool(account.get("enabled"))
            and account.get("status") == "ready"
            and account.get("session_state") == "valid"
            and account.get("health_status") == "healthy"
            and failures < DEGRADED_PROBE_FAILURE_THRESHOLD
        )
        account.update(
            health_status="healthy" if keep_scheduling else "degraded",
            last_error_class="health_probe",
            consecutive_failures=failures,
        )
    account["updated_at"] = now


def _apply_account_result(
    account: dict[str, Any],
    *,
    outcome: str,
    status_code: int | None,
    error_class: str | None,
    cooldown_seconds: int,
    now: int,
) -> None:
    if outcome in {"success", "cancelled"}:
        if outcome == "success":
            account.update(
                status="ready" if account.get("enabled") else "disabled",
                session_state="valid",
                health_status="healthy",
                last_success_at=now,
                last_error_class=None,
                consecutive_failures=0,
                cooldown_until=None,
            )
        account["updated_at"] = now
        return
    failures = int(account.get("consecutive_failures") or 0) + 1
    account["consecutive_failures"] = failures
    account["last_error_class"] = error_class or "upstream"
    if status_code in {401, 403}:
        account.update(
            status="reauth_required",
            session_state="expired",
            health_status="unhealthy",
        )
    elif status_code == 429:
        account.update(
            status="ready" if account.get("enabled") else "disabled",
            session_state="valid",
            health_status="healthy",
            cooldown_until=None,
        )
    else:
        account.update(
            status="cooldown" if account.get("enabled") else "disabled",
            health_status="unhealthy" if failures >= 3 else "degraded",
            cooldown_until=now
            + min(cooldown_seconds, 15 * (2 ** min(failures, 4))),
        )
    account["updated_at"] = now


class PostgresAccountStore:
    """PostgreSQL implementation used by the production Gateway."""

    def __init__(self, database_url: str):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency gate
            raise RuntimeError(
                "psycopg and psycopg_pool are required for the account pool"
            ) from exc

        self.database_url = database_url
        # Account admission performs several very short transactions per chat.
        # Opening a fresh PostgreSQL connection for each one added tens to
        # hundreds of milliseconds before every upstream request. Keep a
        # bounded pool aligned with the published GPT concurrency ceiling.
        self._connection_pool = ConnectionPool(
            conninfo=self.database_url,
            kwargs={"row_factory": dict_row},
            min_size=2,
            max_size=24,
            timeout=5.0,
            max_idle=300.0,
            max_lifetime=1800.0,
            check=ConnectionPool.check_connection,
            name="turtle-account-store",
            open=True,
        )

    def _connect(self):
        return self._connection_pool.connection()

    @property
    def connection_pool(self) -> Any:
        """Expose the Gateway-owned pool to short same-database stores."""
        return self._connection_pool

    def close(self) -> None:
        self._connection_pool.close(timeout=5.0)

    def initialize(
        self,
        default_accounts: list[dict[str, Any]] | dict[str, Any],
    ) -> None:
        defaults = (
            [default_accounts]
            if isinstance(default_accounts, dict)
            else default_accounts
        )
        statements = (
            """
            CREATE TABLE IF NOT EXISTS chat_account_pool (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'gpt'
                    CHECK (provider IN ('gpt', 'claude')),
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """,
            """
            ALTER TABLE chat_account_pool
                DROP CONSTRAINT IF EXISTS chat_account_pool_provider_check
            """,
            """
            ALTER TABLE chat_account_pool
                ADD CONSTRAINT chat_account_pool_provider_check
                CHECK (provider IN ('gpt', 'claude'))
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_account (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL REFERENCES chat_account_pool(id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                worker_endpoint TEXT NOT NULL UNIQUE,
                health_path TEXT,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'disabled',
                session_state TEXT NOT NULL DEFAULT 'missing',
                health_status TEXT NOT NULL DEFAULT 'unknown',
                max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrency > 0),
                priority INTEGER NOT NULL DEFAULT 0,
                quota_profile TEXT NOT NULL DEFAULT 'untracked',
                last_used_at BIGINT,
                last_success_at BIGINT,
                last_health_at BIGINT,
                last_error_class TEXT,
                last_probe_latency_ms INTEGER,
                upstream_display_name TEXT,
                upstream_identity_updated_at BIGINT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                cooldown_until BIGINT,
                deployment_managed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                UNIQUE(pool_id, name)
            )
            """,
            """
            ALTER TABLE chat_account
                ADD COLUMN IF NOT EXISTS upstream_display_name TEXT
            """,
            """
            ALTER TABLE chat_account
                ADD COLUMN IF NOT EXISTS upstream_identity_updated_at BIGINT
            """,
            """
            ALTER TABLE chat_account
                ADD COLUMN IF NOT EXISTS quota_profile TEXT NOT NULL DEFAULT 'untracked'
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_account_affinity (
                pool_id TEXT NOT NULL REFERENCES chat_account_pool(id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                preferred_account_id TEXT NOT NULL REFERENCES chat_account(id) ON DELETE CASCADE,
                created_at BIGINT NOT NULL DEFAULT 0,
                updated_at BIGINT NOT NULL,
                last_routed_at BIGINT,
                migration_count INTEGER NOT NULL DEFAULT 0,
                last_migrated_at BIGINT,
                last_migration_reason TEXT,
                PRIMARY KEY (pool_id, chat_id)
            )
            """,
            """
            ALTER TABLE chat_account_affinity
                ADD COLUMN IF NOT EXISTS created_at BIGINT NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE chat_account_affinity
                ADD COLUMN IF NOT EXISTS last_routed_at BIGINT
            """,
            """
            ALTER TABLE chat_account_affinity
                ADD COLUMN IF NOT EXISTS migration_count INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE chat_account_affinity
                ADD COLUMN IF NOT EXISTS last_migrated_at BIGINT
            """,
            """
            ALTER TABLE chat_account_affinity
                ADD COLUMN IF NOT EXISTS last_migration_reason TEXT
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_account_lease (
                request_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES chat_account(id) ON DELETE RESTRICT,
                pool_id TEXT NOT NULL REFERENCES chat_account_pool(id) ON DELETE RESTRICT,
                user_id TEXT,
                chat_id TEXT,
                selection_key TEXT NOT NULL DEFAULT 'latest:medium',
                state TEXT NOT NULL,
                leased_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                completed_at BIGINT,
                outcome TEXT,
                error_class TEXT
            )
            """,
            """
            ALTER TABLE chat_account_lease
                ADD COLUMN IF NOT EXISTS selection_key TEXT NOT NULL DEFAULT 'latest:medium'
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_account_lane_state (
                account_id TEXT NOT NULL REFERENCES chat_account(id) ON DELETE CASCADE,
                selection_key TEXT NOT NULL,
                blocked_until BIGINT,
                first_rate_limit_at BIGINT,
                last_rate_limit_at BIGINT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (account_id, selection_key)
            )
            """,
            """
            ALTER TABLE chat_account_lane_state
                ADD COLUMN IF NOT EXISTS first_rate_limit_at BIGINT
            """,
            """
            UPDATE chat_account_lane_state AS state
               SET first_rate_limit_at = COALESCE(
                       (
                           SELECT MIN(failed.completed_at)
                             FROM chat_account_lease AS failed
                            WHERE failed.account_id = state.account_id
                              AND failed.selection_key = state.selection_key
                              AND failed.completed_at IS NOT NULL
                              AND failed.error_class IN (
                                  'failover_rate_limit',
                                  'rate_limit_recovery'
                              )
                              AND failed.completed_at > COALESCE(
                                  (
                                      SELECT MAX(succeeded.completed_at)
                                        FROM chat_account_lease AS succeeded
                                       WHERE succeeded.account_id = state.account_id
                                         AND succeeded.selection_key = state.selection_key
                                         AND succeeded.outcome = 'success'
                                  ),
                                  0
                              )
                       ),
                       state.last_rate_limit_at,
                       state.updated_at
                   )
             WHERE state.first_rate_limit_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_account_schedulable_idx
                ON chat_account (pool_id, enabled, status, health_status, cooldown_until, priority)
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_account_lease_active_idx
                ON chat_account_lease (account_id, expires_at) WHERE state = 'active'
            """,
        )
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                for default_account in defaults:
                    provider = _normalize_provider(default_account.get("provider"))
                    cursor.execute(
                        """
                        INSERT INTO chat_account_pool
                            (id, name, description, provider, enabled, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, TRUE, %s, %s)
                        ON CONFLICT(id) DO NOTHING
                        """,
                        (
                            default_account["pool_id"],
                            (
                                "ChatGPT 默认账号池"
                                if provider == "gpt"
                                else "Claude 默认账号池"
                            ),
                            "部署兼容账号池",
                            provider,
                            now,
                            now,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO chat_account
                            (id, pool_id, name, worker_endpoint, health_path, enabled,
                             status, session_state, health_status, max_concurrency,
                             priority, quota_profile, deployment_managed, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, TRUE, 'ready', 'valid', 'unknown',
                                %s, %s, %s, TRUE, %s, %s)
                        ON CONFLICT(id) DO UPDATE SET
                            worker_endpoint = excluded.worker_endpoint,
                            health_path = excluded.health_path,
                            updated_at = excluded.updated_at
                        WHERE chat_account.deployment_managed = TRUE
                        """,
                        (
                            default_account["id"],
                            default_account["pool_id"],
                            default_account["name"],
                            default_account["worker_endpoint"],
                            default_account.get("health_path"),
                            int(default_account["max_concurrency"]),
                            int(default_account["priority"]),
                            str(default_account.get("quota_profile") or "untracked"),
                            now,
                            now,
                        ),
                    )
            connection.commit()

    @staticmethod
    def _expire(cursor, now: int) -> None:
        cursor.execute(
            """
            UPDATE chat_account_lease
               SET state = 'expired', completed_at = %s, outcome = 'lease_expired'
             WHERE state = 'active' AND expires_at <= %s
            """,
            (now, now),
        )
        cursor.execute(
            """
            UPDATE chat_account
               SET status = CASE
                       WHEN enabled AND session_state = 'valid' THEN 'ready'
                       ELSE 'disabled'
                   END,
                   health_status = 'unknown', cooldown_until = NULL,
                   updated_at = %s
             WHERE status = 'cooldown'
               AND cooldown_until IS NOT NULL
               AND cooldown_until <= %s
            """,
            (now, now),
        )

    def snapshot(self) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._expire(cursor, now)
                cursor.execute(
                    """
                    SELECT p.*,
                           COUNT(a.id)::INTEGER AS account_count,
                           COUNT(a.id) FILTER (
                               WHERE a.enabled AND a.status = 'ready'
                                 AND a.session_state = 'valid'
                                 AND a.health_status IN ('unknown', 'healthy')
                                 AND (a.cooldown_until IS NULL OR a.cooldown_until <= %s)
                                 AND COALESCE(active.count, 0) < a.max_concurrency
                           )::INTEGER AS ready_count,
                           COALESCE(SUM(active.count), 0)::INTEGER AS active,
                           COALESCE(
                               SUM(a.max_concurrency) FILTER (WHERE a.enabled),
                               0
                           )::INTEGER AS capacity,
                           COALESCE(
                               SUM(a.max_concurrency) FILTER (
                                   WHERE a.enabled AND a.status = 'ready'
                                     AND a.session_state = 'valid'
                                     AND a.health_status IN ('unknown', 'healthy')
                                     AND (a.cooldown_until IS NULL OR a.cooldown_until <= %s)
                               ),
                               0
                           )::INTEGER AS admission_capacity,
                           COALESCE(
                               SUM(GREATEST(a.max_concurrency - COALESCE(active.count, 0), 0))
                                   FILTER (
                                       WHERE a.enabled AND a.status = 'ready'
                                         AND a.session_state = 'valid'
                                         AND a.health_status IN ('unknown', 'healthy')
                                         AND (a.cooldown_until IS NULL OR a.cooldown_until <= %s)
                                   ),
                               0
                           )::INTEGER AS available_slots
                      FROM chat_account_pool p
                      LEFT JOIN chat_account a ON a.pool_id = p.id
                      LEFT JOIN (
                          SELECT account_id, COUNT(*)::INTEGER AS count
                            FROM chat_account_lease
                           WHERE state = 'active' AND expires_at > %s
                           GROUP BY account_id
                      ) active ON active.account_id = a.id
                     GROUP BY p.id
                     ORDER BY p.name, p.id
                    """,
                    (now, now, now, now),
                )
                pools = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT a.*, p.provider,
                           COALESCE(active.count, 0)::INTEGER AS active,
                           COALESCE(affinity.count, 0)::INTEGER AS sticky_chat_count,
                           COALESCE(affinity.migrations, 0)::INTEGER AS affinity_migration_count
                      FROM chat_account a
                      JOIN chat_account_pool p ON p.id = a.pool_id
                      LEFT JOIN (
                          SELECT account_id, COUNT(*)::INTEGER AS count
                            FROM chat_account_lease
                           WHERE state = 'active' AND expires_at > %s
                           GROUP BY account_id
                      ) active ON active.account_id = a.id
                      LEFT JOIN (
                          SELECT preferred_account_id,
                                 COUNT(*)::INTEGER AS count,
                                 COALESCE(SUM(migration_count), 0)::INTEGER AS migrations
                            FROM chat_account_affinity
                           GROUP BY preferred_account_id
                      ) affinity ON affinity.preferred_account_id = a.id
                     ORDER BY a.pool_id, a.priority DESC, a.name, a.id
                    """,
                    (now,),
                )
                accounts = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT account_id, selection_key, state, expires_at,
                           completed_at, outcome
                      FROM chat_account_lease
                     WHERE (state = 'active' AND expires_at > %s)
                        OR (state = 'completed' AND outcome = 'success'
                            AND completed_at > %s)
                    """,
                    (now, now - 7 * 24 * 60 * 60),
                )
                quota_leases = [dict(row) for row in cursor.fetchall()]
                cursor.execute("SELECT * FROM chat_account_lane_state")
                lane_blocks = {
                    (str(row["account_id"]), str(row["selection_key"])): dict(row)
                    for row in cursor.fetchall()
                }
            connection.commit()
        for pool in pools:
            if not bool(pool.get("enabled")):
                pool["admission_capacity"] = 0
                pool["available_slots"] = 0
            members = [item for item in accounts if item.get("pool_id") == pool.get("id")]
            pool["sticky_chat_count"] = sum(
                int(item.get("sticky_chat_count") or 0) for item in members
            )
            pool["affinity_migration_count"] = sum(
                int(item.get("affinity_migration_count") or 0) for item in members
            )
        for item in accounts:
            item["available"] = (
                bool(item["enabled"])
                and item["status"] == "ready"
                and item["session_state"] == "valid"
                and item["health_status"] in {"unknown", "healthy"}
                and (not item["cooldown_until"] or int(item["cooldown_until"]) <= now)
                and int(item["active"]) < int(item["max_concurrency"])
            )
            item["quota"] = _account_quota_snapshot(
                item,
                leases=quota_leases,
                lane_blocks=lane_blocks,
                now=now,
            )
        return {
            "enabled": True,
            "backend": "postgresql",
            "quota_profiles": quota_profiles_payload(),
            "quota_profiles_by_provider": {
                provider: quota_profiles_payload(provider)
                for provider in sorted(PROVIDERS)
            },
            "pools": pools,
            "accounts": accounts,
        }

    def account(self, account_id: str) -> UpstreamAccount | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.*, p.provider
                  FROM chat_account a
                  JOIN chat_account_pool p ON p.id = a.pool_id
                 WHERE a.id = %s
                """,
                (str(account_id),),
            )
            row = cursor.fetchone()
        return _account_from_mapping(dict(row)) if row else None

    def acquire(
        self,
        *,
        pool_id: str,
        request_id: str,
        user_id: str | None,
        chat_id: str | None,
        lease_seconds: int,
        selection_key: str = "latest:medium",
        excluded_account_ids: frozenset[str] = frozenset(),
        required_quota_profiles: frozenset[str] = frozenset(),
        migration_reason_hint: str | None = None,
    ) -> UpstreamAccount:
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._expire(cursor, now)
                # Selection is a short transaction. Serializing only this pool
                # makes quota reservation and first-chat affinity deterministic
                # across multiple Gateway processes without holding a lock for
                # the lifetime of the upstream request.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"account-pool:{pool_id}",),
                )
                cursor.execute(
                    """
                    SELECT enabled, provider
                      FROM chat_account_pool
                     WHERE id = %s
                     FOR UPDATE
                    """,
                    (pool_id,),
                )
                pool = cursor.fetchone()
                if not pool or not bool(pool["enabled"]):
                    raise AccountUnavailable("账号池不存在或已停用")
                provider = _normalize_provider(pool["provider"])
                if selection_key not in selection_keys(provider):
                    raise AccountUnavailable("请求档位无效")
                cursor.execute(
                    "SELECT 1 FROM chat_account_lease WHERE request_id = %s",
                    (request_id,),
                )
                if cursor.fetchone() is not None:
                    raise AccountUnavailable("请求标识已使用")
                preferred: str | None = None
                affinity: dict[str, Any] | None = None
                if chat_id:
                    cursor.execute(
                        """
                        SELECT * FROM chat_account_affinity
                         WHERE pool_id = %s AND chat_id = %s
                         FOR UPDATE
                        """,
                        (pool_id, str(chat_id)),
                    )
                    affinity_row = cursor.fetchone()
                    affinity = dict(affinity_row) if affinity_row else None
                    preferred = str(affinity["preferred_account_id"]) if affinity else None
                    cursor.execute(
                        """
                        SELECT 1 FROM chat_account_lease
                         WHERE pool_id = %s AND chat_id = %s
                           AND state = 'active' AND expires_at > %s
                         LIMIT 1
                        """,
                        (pool_id, str(chat_id), now),
                    )
                    if cursor.fetchone() is not None:
                        raise AccountUnavailable("同一会话已有请求正在生成")
                cursor.execute(
                    """
                    SELECT a.*, COALESCE(active.count, 0)::INTEGER AS active
                      FROM chat_account a
                      LEFT JOIN (
                          SELECT account_id, COUNT(*)::INTEGER AS count
                            FROM chat_account_lease
                           WHERE state = 'active' AND expires_at > %s
                           GROUP BY account_id
                      ) active ON active.account_id = a.id
                     WHERE a.pool_id = %s
                     ORDER BY a.id
                     FOR UPDATE OF a
                    """,
                    (now, pool_id),
                )
                account_rows = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT account_id, selection_key, state, expires_at,
                           completed_at, outcome
                      FROM chat_account_lease
                     WHERE pool_id = %s AND selection_key = %s
                       AND ((state = 'active' AND expires_at > %s)
                         OR (state = 'completed' AND outcome = 'success'
                             AND completed_at > %s))
                    """,
                    (pool_id, selection_key, now, now - 7 * 24 * 60 * 60),
                )
                quota_leases = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT * FROM chat_account_lane_state WHERE selection_key = %s",
                    (selection_key,),
                )
                lane_blocks = {
                    str(row["account_id"]): dict(row) for row in cursor.fetchall()
                }
                candidates: list[dict[str, Any]] = []
                for item in account_rows:
                    item["provider"] = provider
                    item["base_eligible"] = (
                        str(item["id"]) not in excluded_account_ids
                        and (
                            not required_quota_profiles
                            or str(item.get("quota_profile") or "untracked")
                            in required_quota_profiles
                        )
                        and bool(item.get("enabled"))
                        and item.get("status") == "ready"
                        and item.get("session_state") == "valid"
                        and item.get("health_status") in {"unknown", "healthy"}
                        and (
                            not item.get("cooldown_until")
                            or int(item["cooldown_until"]) <= now
                        )
                    )
                    lane = quota_lane(
                        item.get("quota_profile"),
                        selection_key,
                        provider,
                    )
                    usage = _quota_usage(
                        quota_leases,
                        account_id=str(item["id"]),
                        selection_key=selection_key,
                        window_seconds=int(
                            lane.get("window_seconds") or FAIRNESS_WINDOW_SECONDS
                        ),
                        now=now,
                    )
                    item["selected_lane"] = _lane_snapshot(
                        item,
                        selection_key,
                        usage=usage,
                        blocked_until=(lane_blocks.get(str(item["id"])) or {}).get(
                            "blocked_until"
                        ),
                        now=now,
                    )
                    candidates.append(item)
                selected, migration_reason = _choose_account(
                    pool_id=pool_id,
                    chat_id=chat_id,
                    preferred_account_id=preferred,
                    candidates=candidates,
                )
                if (
                    migration_reason_hint
                    and preferred
                    and str(selected["id"]) != preferred
                ):
                    migration_reason = migration_reason_hint
                cursor.execute(
                    """
                    INSERT INTO chat_account_lease
                        (request_id, account_id, pool_id, user_id, chat_id,
                         selection_key, state, leased_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s)
                    """,
                    (
                        request_id,
                        selected["id"],
                        pool_id,
                        user_id,
                        chat_id,
                        selection_key,
                        now,
                        now + lease_seconds,
                    ),
                )
                cursor.execute(
                    "UPDATE chat_account SET last_used_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, selected["id"]),
                )
                if chat_id:
                    if affinity is None:
                        cursor.execute(
                            """
                            INSERT INTO chat_account_affinity
                                (pool_id, chat_id, user_id, preferred_account_id,
                                 created_at, updated_at, last_routed_at,
                                 migration_count)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
                            """,
                            (pool_id, chat_id, user_id, selected["id"], now, now, now),
                        )
                    else:
                        migrated = str(affinity["preferred_account_id"]) != str(
                            selected["id"]
                        )
                        cursor.execute(
                            """
                            UPDATE chat_account_affinity
                               SET user_id = %s, preferred_account_id = %s,
                                   updated_at = %s, last_routed_at = %s,
                                   migration_count = migration_count + %s,
                                   last_migrated_at = CASE WHEN %s THEN %s ELSE last_migrated_at END,
                                   last_migration_reason = CASE WHEN %s THEN %s ELSE last_migration_reason END
                             WHERE pool_id = %s AND chat_id = %s
                            """,
                            (
                                user_id,
                                selected["id"],
                                now,
                                now,
                                1 if migrated else 0,
                                migrated,
                                now,
                                migrated,
                                migration_reason,
                                pool_id,
                                chat_id,
                            ),
                        )
            connection.commit()
        return _account_from_mapping(dict(selected))

    def release(
        self,
        request_id: str,
        account_id: str,
        *,
        outcome: str,
        status_code: int | None,
        error_class: str | None,
        cooldown_seconds: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE chat_account_lease
                       SET state = %s, completed_at = %s, outcome = %s, error_class = %s
                     WHERE request_id = %s AND account_id = %s AND state = 'active'
                     RETURNING selection_key
                    """,
                    (
                        (
                            "completed"
                            if outcome == "success"
                            else "cancelled"
                            if outcome == "cancelled"
                            else "failed"
                        ),
                        now,
                        outcome,
                        error_class,
                        request_id,
                        account_id,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.commit()
                    return
                lease_row = cursor.fetchone()
                if status_code == 429:
                    selection_key = str(
                        (lease_row or {}).get("selection_key") or "latest:medium"
                    )
                    cursor.execute(
                        """
                        SELECT a.*, p.provider
                          FROM chat_account a
                          JOIN chat_account_pool p ON p.id = a.pool_id
                         WHERE a.id = %s
                         FOR UPDATE OF a
                        """,
                        (account_id,),
                    )
                    account_row = cursor.fetchone()
                    account_for_cooldown = dict(account_row) if account_row else {}
                    cursor.execute(
                        """
                        SELECT consecutive_failures, first_rate_limit_at,
                               last_rate_limit_at
                          FROM chat_account_lane_state
                         WHERE account_id = %s AND selection_key = %s
                         FOR UPDATE
                        """,
                        (account_id, selection_key),
                    )
                    previous = cursor.fetchone()
                    failures = int(previous["consecutive_failures"] or 0) + 1 if previous else 1
                    first_rate_limit_at = int(
                        (previous or {}).get("first_rate_limit_at")
                        or (previous or {}).get("last_rate_limit_at")
                        or now
                    )
                    blocked_until = now + _lane_rate_limit_cooldown_seconds(
                        account_for_cooldown,
                        selection_key,
                        failures=failures,
                        cooldown_seconds=cooldown_seconds,
                        elapsed_seconds=now - first_rate_limit_at,
                        retry_after_seconds=retry_after_seconds,
                    )
                    cursor.execute(
                        """
                        INSERT INTO chat_account_lane_state
                            (account_id, selection_key, blocked_until,
                             first_rate_limit_at,
                             last_rate_limit_at, consecutive_failures, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(account_id, selection_key) DO UPDATE SET
                            blocked_until = excluded.blocked_until,
                            first_rate_limit_at = excluded.first_rate_limit_at,
                            last_rate_limit_at = excluded.last_rate_limit_at,
                            consecutive_failures = excluded.consecutive_failures,
                            updated_at = excluded.updated_at
                        """,
                        (
                            account_id,
                            selection_key,
                            blocked_until,
                            first_rate_limit_at,
                            now,
                            failures,
                            now,
                        ),
                    )
                elif outcome == "success":
                    selection_key = str(
                        (lease_row or {}).get("selection_key") or "latest:medium"
                    )
                    cursor.execute(
                        """
                        DELETE FROM chat_account_lane_state
                         WHERE account_id = %s AND selection_key = %s
                        """,
                        (account_id, selection_key),
                    )
                cursor.execute("SELECT * FROM chat_account WHERE id = %s FOR UPDATE", (account_id,))
                row = cursor.fetchone()
                if row:
                    updated = dict(row)
                    _apply_account_result(
                        updated,
                        outcome=outcome,
                        status_code=status_code,
                        error_class=error_class,
                        cooldown_seconds=cooldown_seconds,
                        now=now,
                    )
                    cursor.execute(
                        """
                        UPDATE chat_account
                           SET status = %s, session_state = %s, health_status = %s,
                               last_success_at = %s, last_error_class = %s,
                               consecutive_failures = %s, cooldown_until = %s,
                               updated_at = %s
                         WHERE id = %s
                        """,
                        (
                            updated["status"],
                            updated["session_state"],
                            updated["health_status"],
                            updated["last_success_at"],
                            updated["last_error_class"],
                            updated["consecutive_failures"],
                            updated["cooldown_until"],
                            updated["updated_at"],
                            account_id,
                        ),
                    )
            connection.commit()

    def renew(
        self,
        request_id: str,
        account_id: str,
        *,
        lease_seconds: int,
    ) -> bool:
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._expire(cursor, now)
                cursor.execute(
                    """
                    UPDATE chat_account_lease
                       SET expires_at = %s
                     WHERE request_id = %s AND account_id = %s
                       AND state = 'active' AND expires_at > %s
                    """,
                    (now + lease_seconds, request_id, account_id, now),
                )
                renewed = cursor.rowcount == 1
            connection.commit()
        return renewed

    def claim_rate_limit_recoveries(
        self,
        *,
        limit: int,
        claim_seconds: int,
    ) -> list[RateLimitRecoveryLease]:
        now = _now()
        claimed: list[RateLimitRecoveryLease] = []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._expire(cursor, now)
                cursor.execute(
                    """
                    SELECT s.account_id, s.selection_key, a.pool_id
                      FROM chat_account_lane_state s
                      JOIN chat_account a ON a.id = s.account_id
                     WHERE s.blocked_until IS NOT NULL
                       AND s.blocked_until <= %s
                       AND a.enabled
                       AND a.status = 'ready'
                       AND a.session_state = 'valid'
                       AND a.health_status IN ('unknown', 'healthy')
                     ORDER BY s.blocked_until, s.account_id, s.selection_key
                     FOR UPDATE OF s SKIP LOCKED
                     LIMIT %s
                    """,
                    (now, max(1, int(limit)) * 2),
                )
                due = [dict(row) for row in cursor.fetchall()]
                for item in due:
                    if len(claimed) >= max(1, int(limit)):
                        break
                    pool_id = str(item["pool_id"])
                    account_id = str(item["account_id"])
                    selection_key = str(item["selection_key"])
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"account-pool:{pool_id}",),
                    )
                    cursor.execute(
                        """
                        SELECT a.max_concurrency,
                               COUNT(l.request_id)::INTEGER AS active
                          FROM chat_account a
                          LEFT JOIN chat_account_lease l
                            ON l.account_id = a.id
                           AND l.state = 'active'
                           AND l.expires_at > %s
                         WHERE a.id = %s
                           AND a.enabled
                           AND a.status = 'ready'
                           AND a.session_state = 'valid'
                           AND a.health_status IN ('unknown', 'healthy')
                         GROUP BY a.max_concurrency
                        """,
                        (now, account_id),
                    )
                    capacity = cursor.fetchone()
                    if (
                        not capacity
                        or int(capacity["active"]) >= int(capacity["max_concurrency"])
                    ):
                        continue
                    request_id = f"recovery-{uuid.uuid4().hex[:24]}"
                    expires_at = now + max(1, int(claim_seconds))
                    cursor.execute(
                        """
                        INSERT INTO chat_account_lease
                            (request_id, account_id, pool_id, user_id, chat_id,
                             selection_key, state, leased_at, expires_at,
                             completed_at, outcome, error_class)
                        VALUES (%s, %s, %s, NULL, NULL, %s, 'active', %s, %s,
                                NULL, NULL, NULL)
                        """,
                        (
                            request_id,
                            account_id,
                            pool_id,
                            selection_key,
                            now,
                            expires_at,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE chat_account_lane_state
                           SET blocked_until = %s, updated_at = %s
                         WHERE account_id = %s AND selection_key = %s
                        """,
                        (expires_at, now, account_id, selection_key),
                    )
                    claimed.append(
                        RateLimitRecoveryLease(
                            request_id=request_id,
                            account_id=account_id,
                            selection_key=selection_key,
                        )
                    )
            connection.commit()
        return claimed

    def mark_probe(
        self,
        account_id: str,
        *,
        state: str,
        http_status: int | None,
        latency_ms: int,
        upstream_display_name: str | None = None,
        allow_reauth: bool = False,
    ) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM chat_account WHERE id = %s FOR UPDATE", (account_id,))
                row = cursor.fetchone()
                if not row:
                    raise AccountPoolConflict("账号不存在")
                updated = dict(row)
                _apply_probe(
                    updated,
                    state=state,
                    http_status=http_status,
                    latency_ms=latency_ms,
                    upstream_display_name=upstream_display_name,
                    allow_reauth=allow_reauth,
                )
                cursor.execute(
                    """
                    UPDATE chat_account
                       SET enabled = %s, status = %s, session_state = %s,
                           health_status = %s, last_success_at = %s,
                           last_health_at = %s, last_error_class = %s,
                           last_probe_latency_ms = %s, consecutive_failures = %s,
                           cooldown_until = %s, upstream_display_name = %s,
                           upstream_identity_updated_at = %s, updated_at = %s
                     WHERE id = %s
                    """,
                    (
                        updated["enabled"],
                        updated["status"],
                        updated["session_state"],
                        updated["health_status"],
                        updated["last_success_at"],
                        updated["last_health_at"],
                        updated["last_error_class"],
                        updated["last_probe_latency_ms"],
                        updated["consecutive_failures"],
                        updated["cooldown_until"],
                        updated.get("upstream_display_name"),
                        updated.get("upstream_identity_updated_at"),
                        updated["updated_at"],
                        account_id,
                    ),
                )
            connection.commit()

    def begin_reauth(self, account_id: str) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._expire(cursor, now)
                cursor.execute(
                    "SELECT * FROM chat_account WHERE id = %s FOR UPDATE",
                    (account_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise AccountPoolConflict("账号不存在")
                cursor.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS count
                      FROM chat_account_lease
                     WHERE account_id = %s AND state = 'active' AND expires_at > %s
                    """,
                    (account_id, now),
                )
                active = int(cursor.fetchone()["count"])
                if active:
                    raise AccountPoolConflict("账号仍有活动请求，无法开始重新登录")
                cursor.execute(
                    """
                    UPDATE chat_account
                       SET status = 'reauth_required', session_state = 'expired',
                           health_status = 'unhealthy', last_error_class = 'manual_reauth',
                           cooldown_until = NULL, updated_at = %s
                     WHERE id = %s
                     RETURNING *
                    """,
                    (now, account_id),
                )
                result = dict(cursor.fetchone())
            connection.commit()
        result["active"] = 0
        result["available"] = False
        return result

    def create_pool(
        self, *, provider: str, name: str, description: str
    ) -> dict[str, Any]:
        now = _now()
        pool_id = f"pool-{uuid.uuid4().hex[:12]}"
        normalized_provider = _normalize_provider(provider)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_account_pool
                        (id, name, description, provider, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, TRUE, %s, %s)
                    RETURNING *
                    """,
                    (
                        pool_id,
                        name,
                        description,
                        normalized_provider,
                        now,
                        now,
                    ),
                )
                result = dict(cursor.fetchone())
                connection.commit()
            return result
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise AccountPoolConflict("账号池名称已存在") from exc
            raise

    def update_pool(
        self, pool_id: str, *, name: str, description: str, enabled: bool
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE chat_account_pool
                       SET name = %s, description = %s, enabled = %s, updated_at = %s
                     WHERE id = %s
                     RETURNING *
                    """,
                    (name, description, bool(enabled), _now(), pool_id),
                )
                row = cursor.fetchone()
                if not row:
                    raise AccountPoolConflict("账号池不存在")
                result = dict(row)
                connection.commit()
            return result
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise AccountPoolConflict("账号池名称已存在") from exc
            raise

    def delete_pool(self, pool_id: str) -> dict[str, Any]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM chat_account_pool WHERE id = %s FOR UPDATE",
                    (pool_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AccountPoolConflict("账号池不存在")

                cursor.execute(
                    "SELECT COUNT(*)::INTEGER AS count FROM chat_account WHERE pool_id = %s",
                    (pool_id,),
                )
                if int(cursor.fetchone()["count"] or 0) > 0:
                    raise AccountPoolConflict("账号池仍有账号，不能删除")

                cursor.execute(
                    """
                    SELECT to_regclass('chat_model_group') IS NOT NULL AS model_groups,
                           to_regclass('chat_group') IS NOT NULL AS resource_groups
                    """
                )
                reference_tables = cursor.fetchone()
                reference_count = 0
                if reference_tables["model_groups"]:
                    cursor.execute(
                        """
                        SELECT COUNT(*)::INTEGER AS count
                          FROM chat_model_group
                         WHERE account_pool_id = %s
                        """,
                        (pool_id,),
                    )
                    reference_count += int(cursor.fetchone()["count"] or 0)
                if reference_tables["resource_groups"]:
                    cursor.execute(
                        """
                        SELECT COUNT(*)::INTEGER AS count
                          FROM chat_group
                         WHERE gpt_account_pool_id = %s
                        """,
                        (pool_id,),
                    )
                    reference_count += int(cursor.fetchone()["count"] or 0)
                if reference_count:
                    raise AccountPoolConflict(
                        "账号池仍被模型或资源分组引用，请先更换分组绑定"
                    )

                cursor.execute(
                    "DELETE FROM chat_account_pool WHERE id = %s",
                    (pool_id,),
                )
                connection.commit()
                deleted = dict(row)
                deleted["deleted"] = True
                return deleted
        except AccountPoolConflict:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23503":
                raise AccountPoolConflict("账号池仍被其他配置引用，不能删除") from exc
            raise

    def create_account(
        self,
        *,
        account_id: str | None = None,
        pool_id: str,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT provider FROM chat_account_pool WHERE id = %s",
                (pool_id,),
            )
            pool = cursor.fetchone()
        if pool is None:
            raise AccountPoolConflict("账号池不存在")
        provider = _normalize_provider(pool["provider"])
        value = _new_account(
            account_id or f"acct-{uuid.uuid4().hex[:12]}",
            pool_id=pool_id,
            provider=provider,
            name=name,
            worker_endpoint=worker_endpoint,
            health_path=health_path,
            max_concurrency=max_concurrency,
            priority=priority,
            quota_profile=quota_profile,
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_account
                        (id, pool_id, name, worker_endpoint, health_path, enabled,
                         status, session_state, health_status, max_concurrency,
                         priority, quota_profile, deployment_managed, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, FALSE, 'disabled', 'missing',
                            'unknown', %s, %s, %s, FALSE, %s, %s)
                    RETURNING *
                    """,
                    (
                        value["id"],
                        pool_id,
                        name,
                        worker_endpoint,
                        health_path,
                        max_concurrency,
                        priority,
                        value["quota_profile"],
                        value["created_at"],
                        value["updated_at"],
                    ),
                )
                result = dict(cursor.fetchone())
                connection.commit()
            return result
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23503":
                raise AccountPoolConflict("账号池不存在") from exc
            if getattr(exc, "sqlstate", None) == "23505":
                raise AccountPoolConflict("账号别名或 worker 地址已存在") from exc
            raise

    def update_account(
        self,
        account_id: str,
        *,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        enabled: bool,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT a.*, p.provider
                      FROM chat_account a
                      JOIN chat_account_pool p ON p.id = a.pool_id
                     WHERE a.id = %s
                     FOR UPDATE OF a
                    """,
                    (account_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise AccountPoolConflict("账号不存在")
                updated = dict(row)
                endpoint_changed = (
                    updated.get("worker_endpoint") != worker_endpoint
                    or updated.get("health_path") != health_path
                )
                updated.update(
                    name=name,
                    worker_endpoint=worker_endpoint,
                    health_path=health_path,
                    max_concurrency=max_concurrency,
                    priority=priority,
                    quota_profile=normalize_quota_profile(
                        quota_profile,
                        _normalize_provider(updated.get("provider")),
                    ),
                    enabled=bool(enabled),
                    updated_at=_now(),
                )
                if endpoint_changed:
                    updated.update(
                        status="disabled",
                        session_state="unknown",
                        health_status="unknown",
                        last_error_class=None,
                        consecutive_failures=0,
                        cooldown_until=None,
                    )
                elif not enabled:
                    updated["status"] = "disabled"
                elif (
                    updated.get("session_state") == "valid"
                    and updated.get("health_status") == "healthy"
                ):
                    updated["status"] = "ready"
                cursor.execute(
                    """
                    UPDATE chat_account
                       SET name = %s, worker_endpoint = %s, health_path = %s,
                           max_concurrency = %s, priority = %s, quota_profile = %s,
                           enabled = %s,
                           status = %s, session_state = %s, health_status = %s,
                           last_error_class = %s, consecutive_failures = %s,
                           cooldown_until = %s, updated_at = %s
                     WHERE id = %s
                     RETURNING *
                    """,
                    (
                        updated["name"],
                        updated["worker_endpoint"],
                        updated["health_path"],
                        updated["max_concurrency"],
                        updated["priority"],
                        updated["quota_profile"],
                        updated["enabled"],
                        updated["status"],
                        updated["session_state"],
                        updated["health_status"],
                        updated["last_error_class"],
                        updated["consecutive_failures"],
                        updated["cooldown_until"],
                        updated["updated_at"],
                        account_id,
                    ),
                )
                result = dict(cursor.fetchone())
                connection.commit()
            return result
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise AccountPoolConflict("账号别名或 worker 地址已存在") from exc
            raise


class AccountPoolRouter:
    def __init__(
        self,
        *,
        store: AccountStore,
        upstream_api_key: str,
        upstream_timeout_seconds: float,
        lease_seconds: int,
        cooldown_seconds: int,
        allowed_hosts: tuple[str, ...],
        default_accounts: list[dict[str, Any]] | None = None,
        default_account: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        recovery_poll_seconds: float = 0.0,
        recovery_probe_concurrency: int = 2,
    ) -> None:
        self.store = store
        self.upstream_api_key = upstream_api_key
        self.upstream_timeout_seconds = upstream_timeout_seconds
        self.lease_seconds = lease_seconds
        self.cooldown_seconds = cooldown_seconds
        self.allowed_hosts = {value.lower() for value in allowed_hosts}
        self.default_accounts = list(
            default_accounts
            if default_accounts is not None
            else [default_account]
            if default_account is not None
            else []
        )
        if not self.default_accounts:
            raise ValueError("at least one default Provider account is required")
        self.protected_pool_ids = frozenset(
            str(item["pool_id"]) for item in self.default_accounts
        )
        self.transport = transport
        self.recovery_poll_seconds = max(0.0, float(recovery_poll_seconds))
        self.recovery_probe_concurrency = max(
            1,
            min(4, int(recovery_probe_concurrency)),
        )
        self._clients: dict[str, tuple[str, str | None, UpstreamClient]] = {}
        self._client_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize, self.default_accounts)
        if self.recovery_poll_seconds > 0 and self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._rate_limit_recovery_loop()
            )

    async def close(self) -> None:
        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None:
            recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery_task
        clients = [value[2] for value in self._clients.values()]
        self._clients.clear()
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        close_store = getattr(self.store, "close", None)
        if callable(close_store):
            await asyncio.to_thread(close_store)

    async def client_for(self, account: UpstreamAccount) -> UpstreamClient:
        async with self._client_lock:
            cached = self._clients.get(account.id)
            signature = (account.worker_endpoint, account.health_path)
            if cached and cached[:2] == signature:
                return cached[2]
            if cached:
                await cached[2].close()
            client = UpstreamClient(
                base_url=account.worker_endpoint,
                health_path=account.health_path,
                auth_capture_path=(
                    "/api/ClaudeWeb/auth/capture"
                    if account.provider == "claude"
                    else "/api/OpenaiAccount/auth/capture"
                ),
                auth_capture_timeout_seconds=(
                    900.0 if account.provider == "claude" else 45.0
                ),
                api_key=self.upstream_api_key,
                timeout_seconds=self.upstream_timeout_seconds,
                transport=self.transport,
            )
            self._clients[account.id] = (signature[0], signature[1], client)
            return client

    async def _probe_rate_limit_recovery(
        self,
        claim: RateLimitRecoveryLease,
    ) -> None:
        started = time.monotonic()
        status_code: int | None = None
        retry_after_seconds: int | None = None
        outcome = "error"
        error_class = "rate_limit_recovery"
        account = await asyncio.to_thread(self.store.account, claim.account_id)
        if account is None:
            await asyncio.to_thread(
                self.store.release,
                claim.request_id,
                claim.account_id,
                outcome="error",
                status_code=502,
                error_class="rate_limit_recovery_missing_account",
                cooldown_seconds=self.cooldown_seconds,
            )
            return
        try:
            client = await self.client_for(account)
            result = await client.completion(
                _rate_limit_recovery_payload(
                    account.provider,
                    claim.selection_key,
                )
            )
            if not _completion_has_effective_content(result):
                status_code = 502
                error_class = "rate_limit_recovery_empty"
            else:
                status_code = 200
                outcome = "success"
                error_class = None
                if account.provider == "gpt":
                    metadata = extract_upstream_resource_metadata(result)
                    if metadata.conversation_id:
                        try:
                            await client.cleanup_resource(
                                resource_type="conversation",
                                resource_id=metadata.conversation_id,
                                dry_run=False,
                                conversation_action="delete",
                            )
                        except Exception:
                            logger.warning(
                                "account_recovery_cleanup_failed account=%s lane=%s",
                                account.id,
                                claim.selection_key,
                            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.release,
                claim.request_id,
                claim.account_id,
                outcome="cancelled",
                status_code=499,
                error_class="rate_limit_recovery_cancelled",
                cooldown_seconds=self.cooldown_seconds,
            )
            raise
        except UpstreamFailure as exc:
            status_code = exc.status_code
            retry_after_seconds = exc.retry_after_seconds
        except Exception:
            status_code = 502
        await asyncio.to_thread(
            self.store.release,
            claim.request_id,
            claim.account_id,
            outcome=outcome,
            status_code=status_code,
            error_class=error_class,
            cooldown_seconds=self.cooldown_seconds,
            retry_after_seconds=retry_after_seconds,
        )
        logger.info(
            "account_recovery_probe account=%s lane=%s recovered=%s status=%s latency_ms=%d",
            account.id,
            claim.selection_key,
            outcome == "success",
            status_code,
            int((time.monotonic() - started) * 1000),
        )

    async def _rate_limit_recovery_loop(self) -> None:
        semaphore = asyncio.Semaphore(self.recovery_probe_concurrency)

        async def bounded_probe(claim: RateLimitRecoveryLease) -> None:
            async with semaphore:
                await self._probe_rate_limit_recovery(claim)

        claim_seconds = max(60, int(self.upstream_timeout_seconds) + 60)
        while True:
            try:
                claims = await asyncio.to_thread(
                    self.store.claim_rate_limit_recoveries,
                    limit=self.recovery_probe_concurrency,
                    claim_seconds=claim_seconds,
                )
                if claims:
                    await asyncio.gather(
                        *(bounded_probe(claim) for claim in claims)
                    )
                await asyncio.sleep(self.recovery_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("account_recovery_monitor_failed")
                await asyncio.sleep(self.recovery_poll_seconds)

    async def acquire(
        self,
        *,
        pool_id: str,
        request_id: str,
        user_id: str | None,
        chat_id: str | None,
        selection_key: str = "latest:medium",
        excluded_account_ids: frozenset[str] = frozenset(),
        required_quota_profiles: frozenset[str] = frozenset(),
        migration_reason_hint: str | None = None,
    ) -> AccountLease:
        normalized_request_id = request_id if REQUEST_ID_RE.fullmatch(request_id) else uuid.uuid4().hex[:12]
        account = await asyncio.to_thread(
            self.store.acquire,
            pool_id=pool_id,
            request_id=normalized_request_id,
            user_id=user_id,
            chat_id=chat_id,
            lease_seconds=self.lease_seconds,
            selection_key=selection_key,
            excluded_account_ids=excluded_account_ids,
            required_quota_profiles=required_quota_profiles,
            migration_reason_hint=migration_reason_hint,
        )
        lease = AccountLease(self, normalized_request_id, account)
        lease.start_heartbeat()
        return lease

    async def renew(self, request_id: str, account_id: str) -> bool:
        return await asyncio.to_thread(
            self.store.renew,
            request_id,
            account_id,
            lease_seconds=self.lease_seconds,
        )

    async def release(
        self,
        request_id: str,
        account_id: str,
        *,
        outcome: str,
        status_code: int | None,
        error_class: str | None,
        retry_after_seconds: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self.store.release,
            request_id,
            account_id,
            outcome=outcome,
            status_code=status_code,
            error_class=error_class,
            cooldown_seconds=self.cooldown_seconds,
            retry_after_seconds=retry_after_seconds,
        )

    async def snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.snapshot)

    async def account(self, account_id: str) -> UpstreamAccount | None:
        return await asyncio.to_thread(self.store.account, account_id)

    async def capacity(
        self,
        pool_id: str,
        selection_key: str | None = None,
    ) -> dict[str, Any]:
        if not POOL_ID_RE.fullmatch(str(pool_id or "")):
            raise AccountPoolConflict("账号组 ID 无效")
        snapshot = await self.snapshot()
        pool = next(
            (item for item in snapshot.get("pools", []) if item.get("id") == pool_id),
            None,
        )
        if pool is None:
            raise AccountPoolConflict("账号池不存在")

        provider = _normalize_provider(pool.get("provider"))
        if selection_key is not None and selection_key not in selection_keys(provider):
            raise AccountPoolConflict("请求档位无效")

        pools = list(snapshot.get("pools", []))
        accounts = list(snapshot.get("accounts", []))

        def resolved_capacity(
            candidate_pool: dict[str, Any],
            lane_key: str | None,
        ) -> tuple[int, int]:
            if not bool(candidate_pool.get("enabled")):
                return 0, 0
            if lane_key is None:
                return (
                    max(0, int(candidate_pool.get("admission_capacity") or 0)),
                    max(0, int(candidate_pool.get("available_slots") or 0)),
                )
            members = [
                item
                for item in accounts
                if item.get("pool_id") == candidate_pool.get("id")
            ]
            eligible = []
            for item in members:
                lane = next(
                    (
                        value
                        for value in (item.get("quota") or {}).get("lanes", [])
                        if value.get("selection_key") == lane_key
                    ),
                    None,
                )
                base_eligible = (
                    bool(item.get("enabled"))
                    and item.get("status") == "ready"
                    and item.get("session_state") == "valid"
                    and item.get("health_status") in {"unknown", "healthy"}
                    and not item.get("cooldown_until")
                )
                if base_eligible and lane and lane.get("admission_available"):
                    eligible.append(item)
            return (
                sum(
                    max(1, int(item.get("max_concurrency") or 1))
                    for item in eligible
                ),
                sum(
                    max(
                        0,
                        int(item.get("max_concurrency") or 1)
                        - int(item.get("active") or 0),
                    )
                    for item in eligible
                ),
            )

        admission_capacity, available_slots = resolved_capacity(pool, selection_key)
        provider_pools = [
            candidate
            for candidate in pools
            if _normalize_provider(candidate.get("provider")) == provider
        ]
        provider_admission_capacity = sum(
            resolved_capacity(candidate, selection_key)[0]
            for candidate in provider_pools
        )
        provider_available_slots = sum(
            resolved_capacity(candidate, selection_key)[1]
            for candidate in provider_pools
        )
        global_admission_capacity = sum(
            resolved_capacity(candidate, None)[0] for candidate in pools
        )
        global_available_slots = sum(
            resolved_capacity(candidate, None)[1] for candidate in pools
        )
        return {
            "id": str(pool["id"]),
            "name": str(pool.get("name") or pool["id"]),
            "provider": provider,
            "enabled": bool(pool.get("enabled")),
            "capacity": max(0, int(pool.get("capacity") or 0)),
            "selection_key": selection_key,
            "admission_capacity": admission_capacity,
            "available_slots": available_slots,
            "provider_admission_capacity": provider_admission_capacity,
            "provider_available_slots": provider_available_slots,
            "global_admission_capacity": global_admission_capacity,
            "global_available_slots": global_available_slots,
            "active": max(0, int(pool.get("active") or 0)),
        }

    async def begin_reauth(self, account_id: str) -> dict[str, Any]:
        if not ACCOUNT_ID_RE.fullmatch(str(account_id or "")):
            raise AccountPoolConflict("账号 ID 无效")
        return await asyncio.to_thread(self.store.begin_reauth, account_id)

    async def probe_account(
        self,
        account_id: str,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        account = await asyncio.to_thread(self.store.account, account_id)
        if account is None:
            raise AccountPoolConflict("账号不存在")
        client = await self.client_for(account)
        result = await client.probe()
        if persist:
            await asyncio.to_thread(
                self.store.mark_probe,
                account.id,
                state=result["state"],
                http_status=result.get("http_status"),
                latency_ms=int(result.get("latency_ms") or 0),
                upstream_display_name=result.get("upstream_display_name"),
            )
        return {"account_id": account.id, **result}

    async def capture_account_auth(self, account_id: str) -> dict[str, Any]:
        account = await asyncio.to_thread(self.store.account, account_id)
        if account is None:
            raise AccountPoolConflict("账号不存在")
        client = await self.client_for(account)
        result = await client.capture_auth()
        return {"account_id": account.id, **result}

    async def probe_pool(self, pool_id: str) -> dict[str, Any]:
        snapshot = await self.snapshot()
        if not any(item["id"] == pool_id for item in snapshot["pools"]):
            raise AccountPoolConflict("账号组不存在")
        account_ids = [item["id"] for item in snapshot["accounts"] if item["pool_id"] == pool_id]
        results = await asyncio.gather(
            *(self.probe_account(account_id) for account_id in account_ids),
            return_exceptions=True,
        )
        sanitized = []
        for account_id, value in zip(account_ids, results, strict=True):
            if isinstance(value, Exception):
                sanitized.append(
                    {
                        "account_id": account_id,
                        "state": "offline",
                        "ok": False,
                        "latency_ms": 0,
                    }
                )
            else:
                sanitized.append(value)
        return {
            "pool_id": pool_id,
            "ok": any(item.get("ok") for item in sanitized),
            "items": sanitized,
        }

    async def create_pool(
        self, *, provider: str, name: str, description: str
    ) -> dict[str, Any]:
        normalized_name, normalized_description = _normalize_pool(name, description)
        return await asyncio.to_thread(
            self.store.create_pool,
            provider=_normalize_provider(provider),
            name=normalized_name,
            description=normalized_description,
        )

    async def update_pool(
        self, pool_id: str, *, name: str, description: str, enabled: bool
    ) -> dict[str, Any]:
        if not POOL_ID_RE.fullmatch(pool_id):
            raise AccountPoolConflict("账号组 ID 无效")
        normalized_name, normalized_description = _normalize_pool(name, description)
        return await asyncio.to_thread(
            self.store.update_pool,
            pool_id,
            name=normalized_name,
            description=normalized_description,
            enabled=bool(enabled),
        )

    async def delete_pool(self, pool_id: str) -> dict[str, Any]:
        if not POOL_ID_RE.fullmatch(pool_id):
            raise AccountPoolConflict("账号组 ID 无效")
        if pool_id in self.protected_pool_ids:
            raise AccountPoolConflict("部署默认账号池不能删除")
        return await asyncio.to_thread(self.store.delete_pool, pool_id)

    async def create_account(
        self,
        *,
        account_id: str | None = None,
        pool_id: str,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        if account_id is not None and not ACCOUNT_ID_RE.fullmatch(str(account_id)):
            raise AccountPoolConflict("账号 ID 无效")
        snapshot = await self.snapshot()
        pool = next(
            (
                item
                for item in snapshot.get("pools", [])
                if str(item.get("id")) == pool_id
            ),
            None,
        )
        if pool is None:
            raise AccountPoolConflict("账号池不存在")
        provider = _normalize_provider(pool.get("provider"))
        return await asyncio.to_thread(
            self.store.create_account,
            account_id=account_id,
            pool_id=pool_id,
            name=_normalize_account_name(name),
            worker_endpoint=self.validate_endpoint(worker_endpoint),
            health_path=_normalize_health_path(health_path),
            max_concurrency=_normalize_concurrency(max_concurrency),
            priority=_normalize_priority(priority),
            quota_profile=_normalize_quota_profile(quota_profile, provider),
        )

    async def update_account(
        self,
        account_id: str,
        *,
        name: str,
        worker_endpoint: str,
        health_path: str | None,
        max_concurrency: int,
        priority: int,
        enabled: bool,
        quota_profile: str = "untracked",
    ) -> dict[str, Any]:
        account = await asyncio.to_thread(self.store.account, account_id)
        if account is None:
            raise AccountPoolConflict("账号不存在")
        return await asyncio.to_thread(
            self.store.update_account,
            account_id,
            name=_normalize_account_name(name),
            worker_endpoint=self.validate_endpoint(worker_endpoint),
            health_path=_normalize_health_path(health_path),
            max_concurrency=_normalize_concurrency(max_concurrency),
            priority=_normalize_priority(priority),
            enabled=bool(enabled),
            quota_profile=_normalize_quota_profile(
                quota_profile,
                account.provider,
            ),
        )

    def validate_endpoint(self, value: str) -> str:
        normalized = str(value or "").strip().rstrip("/")
        try:
            parsed = urlsplit(normalized)
        except ValueError as exc:
            raise AccountPoolConflict("worker 地址无效") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AccountPoolConflict("worker 地址必须是不含凭据和查询参数的 HTTP(S) 地址")
        if parsed.hostname.lower() not in self.allowed_hosts:
            raise AccountPoolConflict("worker 主机不在部署允许列表中")
        if parsed.path.rstrip("/") != "/v1":
            raise AccountPoolConflict("worker 地址必须以 /v1 结尾")
        return normalized


def _normalize_pool(name: str, description: str) -> tuple[str, str]:
    normalized_name = str(name or "").strip()
    normalized_description = str(description or "").strip()
    if not 1 <= len(normalized_name) <= 60:
        raise AccountPoolConflict("账号池名称必须为 1–60 字")
    if len(normalized_description) > 200:
        raise AccountPoolConflict("账号池说明不能超过 200 字")
    return normalized_name, normalized_description


def _normalize_account_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 60:
        raise AccountPoolConflict("账号别名必须为 1–60 字")
    if "@" in normalized:
        raise AccountPoolConflict("账号别名不能使用真实邮箱，请填写内部别名")
    return normalized


def _safe_upstream_display_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(
        "".join(character for character in value if character.isprintable()).split()
    )
    return normalized[:120] or None


def _normalize_health_path(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if not normalized.startswith("/") or "://" in normalized or len(normalized) > 160:
        raise AccountPoolConflict("健康检查路径无效")
    return normalized


def _normalize_concurrency(value: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AccountPoolConflict("账号并发必须是整数") from exc
    if not 1 <= normalized <= 20:
        raise AccountPoolConflict("账号并发必须在 1–20 之间")
    return normalized


def _normalize_priority(value: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AccountPoolConflict("账号优先级必须是整数") from exc
    if not -100 <= normalized <= 100:
        raise AccountPoolConflict("账号优先级必须在 -100–100 之间")
    return normalized


def _normalize_provider(value: Any) -> str:
    provider = str(value or "gpt").strip().lower()
    if provider not in PROVIDERS:
        raise AccountPoolConflict("账号 Provider 无效")
    return provider


def _normalize_quota_profile(value: str | None, provider: str = "gpt") -> str:
    try:
        return normalize_quota_profile(value, provider)
    except ValueError as exc:
        raise AccountPoolConflict(str(exc)) from exc


def build_account_pool(
    *,
    enabled: bool,
    claude_enabled: bool,
    database_url: str | None,
    default_pool_id: str,
    default_claude_pool_id: str,
    upstream_base_url: str,
    upstream_health_path: str | None,
    claude_upstream_base_url: str,
    claude_upstream_health_path: str | None,
    upstream_api_key: str,
    upstream_timeout_seconds: float,
    lease_seconds: int,
    cooldown_seconds: int,
    recovery_poll_seconds: float,
    allowed_hosts: tuple[str, ...],
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountPoolRouter:
    if not POOL_ID_RE.fullmatch(default_pool_id):
        raise ValueError("GATEWAY_DEFAULT_ACCOUNT_POOL_ID is invalid")
    if not POOL_ID_RE.fullmatch(default_claude_pool_id):
        raise ValueError("GATEWAY_DEFAULT_CLAUDE_ACCOUNT_POOL_ID is invalid")
    default_accounts = [
        _new_account(
            "legacy-primary",
            pool_id=default_pool_id,
            provider="gpt",
            name="主账号",
            worker_endpoint=upstream_base_url,
            health_path=upstream_health_path,
            max_concurrency=1,
            priority=100,
            deployment_managed=True,
        ),
    ]
    if claude_enabled:
        default_accounts.append(
            _new_account(
            "legacy-claude-primary",
            pool_id=default_claude_pool_id,
            provider="claude",
            name="Claude 主账号",
            worker_endpoint=claude_upstream_base_url,
            health_path=claude_upstream_health_path,
            max_concurrency=1,
            priority=100,
            deployment_managed=True,
            )
        )
    store: AccountStore = (
        PostgresAccountStore(str(database_url))
        if enabled and database_url
        else MemoryAccountStore()
    )
    return AccountPoolRouter(
        store=store,
        upstream_api_key=upstream_api_key,
        upstream_timeout_seconds=upstream_timeout_seconds,
        lease_seconds=lease_seconds,
        cooldown_seconds=cooldown_seconds,
        allowed_hosts=allowed_hosts,
        default_accounts=default_accounts,
        transport=transport,
        recovery_poll_seconds=recovery_poll_seconds,
    )
