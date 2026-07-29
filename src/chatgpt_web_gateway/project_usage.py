"""Project-scoped API keys and sanitized usage attribution.

The store never persists key plaintext, prompts, responses, or upstream error
bodies. Every priced row snapshots the official reference estimate and the
configured multiplier so later configuration changes never rewrite history.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .api_pricing import (
    OPENAI_STANDARD_PRICES,
    PRICE_CARD_VERSION,
    estimate_completion_tokens,
    price_for_route,
    simulated_cost,
)


KEY_PREFIX = "turtle_proj_"
DEFAULT_MAX_KEYS = 5
COST_MULTIPLIER_SCALE = 1_000_000
MAX_COST_MULTIPLIER_PPM = 100 * COST_MULTIPLIER_SCALE
REQUEST_RESERVATION_TTL_SECONDS = 1_200


class ProjectKeyConflict(ValueError):
    """Raised for duplicate or invalid project-key operations."""


class ProjectRequestConflict(ValueError):
    """Raised when a project request id has already been accepted."""


class ProjectBalanceInsufficient(ValueError):
    """Raised before upstream work when prepaid project credit is insufficient."""


@dataclass(frozen=True, slots=True)
class ProjectCaller:
    key_id: str | None
    owner_user_id: str | None
    project_name: str
    key_prefix: str
    is_master: bool = False


@dataclass(frozen=True, slots=True)
class UsageAmounts:
    prompt_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    source: str
    points: int
    pricing_profile: str | None
    price_card_version: str | None
    input_rate_nano_usd: int | None
    cached_input_rate_nano_usd: int | None
    cache_write_rate_nano_usd: int | None
    output_rate_nano_usd: int | None
    official_cost_microusd: int | None


def extract_usage(
    payload: Any,
    *,
    route: str | None = None,
    fallback_points: int = 1,
    estimated_prompt_tokens: int | None = None,
    estimated_completion_token_count: int | None = None,
) -> UsageAmounts:
    usage = payload.get("usage") if isinstance(payload, dict) else None

    def amount(*names: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        for name in names:
            value = usage.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return None

    def nested_amount(container_names: tuple[str, ...], *names: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        for container_name in container_names:
            container = usage.get(container_name)
            if not isinstance(container, dict):
                continue
            for name in names:
                value = container.get(name)
                if (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and value >= 0
                ):
                    return int(value)
        return None

    prompt = amount("prompt_tokens", "input_tokens")
    completion = amount("completion_tokens", "output_tokens")
    cached = nested_amount(
        ("prompt_tokens_details", "input_tokens_details"),
        "cached_tokens",
        "cached_input_tokens",
    )
    if cached is None:
        cached = amount("cached_tokens", "cached_input_tokens")
    cache_write = nested_amount(
        ("prompt_tokens_details", "input_tokens_details"),
        "cache_write_tokens",
    )
    if cache_write is None:
        cache_write = amount("cache_write_tokens")
    total = amount("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    source = "upstream_reported"
    if prompt is None or completion is None or total is None or total <= 0:
        estimated_completion = (
            int(estimated_completion_token_count)
            if estimated_completion_token_count is not None
            else estimate_completion_tokens(payload)
        )
        if (
            estimated_prompt_tokens is not None
            and int(estimated_prompt_tokens) > 0
            and estimated_completion > 0
        ):
            prompt = int(estimated_prompt_tokens)
            cached = 0
            cache_write = 0
            completion = estimated_completion
            total = prompt + completion
            source = "locally_estimated"
        else:
            return UsageAmounts(
                prompt,
                cached,
                cache_write,
                completion,
                total,
                "request_fallback" if fallback_points else "not_charged",
                max(0, int(fallback_points)),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )

    price = price_for_route(route)
    official_cost_microusd = simulated_cost(
        price=price,
        input_tokens=prompt,
        cached_input_tokens=cached or 0,
        cache_write_tokens=cache_write or 0,
        output_tokens=completion,
    )
    if not official_cost_microusd:
        return UsageAmounts(
            prompt,
            cached,
            cache_write,
            completion,
            total,
            "request_fallback" if fallback_points else "not_charged",
            max(0, int(fallback_points)),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    return UsageAmounts(
        prompt,
        cached or 0,
        cache_write or 0,
        completion,
        total,
        source,
        0,
        price.model,
        PRICE_CARD_VERSION,
        price.input_rate_nano_usd,
        price.cached_input_rate_nano_usd,
        price.cache_write_rate_nano_usd,
        price.output_rate_nano_usd,
        official_cost_microusd,
    )


def _digest(master_key: str, secret: str) -> str:
    return hmac.new(
        master_key.encode("utf-8"),
        secret.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _now() -> int:
    return int(time.time())


def _normalize_multiplier(value: float | int) -> int:
    try:
        ppm = round(float(value) * COST_MULTIPLIER_SCALE)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectKeyConflict("消耗倍率必须是 0 到 100 之间的数字") from exc
    if not 0 <= ppm <= MAX_COST_MULTIPLIER_PPM:
        raise ProjectKeyConflict("消耗倍率必须是 0 到 100 之间的数字")
    return int(ppm)


def _pricing_config(row: dict[str, Any]) -> dict[str, Any]:
    ppm = int(row.get("cost_multiplier_ppm") or 0)
    return {
        "cost_multiplier": ppm / COST_MULTIPLIER_SCALE,
        "cost_multiplier_ppm": ppm,
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


def _apply_cost_multiplier(entry: dict[str, Any], multiplier_ppm: int) -> dict[str, Any]:
    item = dict(entry)
    official = max(
        0,
        int(
            item.get("official_cost_microusd")
            or item.get("estimated_cost_microusd")
            or 0
        ),
    )
    actual = (
        (official * multiplier_ppm + COST_MULTIPLIER_SCALE - 1)
        // COST_MULTIPLIER_SCALE
        if official
        else 0
    )
    item.update(
        points=0,
        official_cost_microusd=official,
        cost_multiplier_ppm=int(multiplier_ppm),
        estimated_cost_microusd=actual,
    )
    return item


def _public_key(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "name": str(row["name"]),
        "key_prefix": str(row["key_prefix"]),
        "status": str(row["status"]),
        "created_at": int(row["created_at"]),
        "last_used_at": (
            int(row["last_used_at"]) if row.get("last_used_at") is not None else None
        ),
        "revoked_at": (
            int(row["revoked_at"]) if row.get("revoked_at") is not None else None
        ),
        "request_count": int(row.get("request_count") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "total_official_cost_microusd": int(
            row.get("total_official_cost_microusd") or 0
        ),
        "total_actual_cost_microusd": int(
            row.get("total_estimated_cost_microusd") or 0
        ),
    }


class MemoryProjectUsageStore:
    def __init__(self, master_key: str):
        self.master_key = master_key
        self.keys: dict[str, dict[str, Any]] = {}
        self.permissions: dict[str, dict[str, Any]] = {}
        self.usage: list[dict[str, Any]] = []
        self.requests: dict[tuple[str, str], dict[str, Any]] = {}
        self.credit_ledger: list[dict[str, Any]] = []
        self.config = {
            "cost_multiplier_ppm": COST_MULTIPLIER_SCALE,
            "updated_at": None,
            "updated_by": "system",
        }
        self._lock = threading.RLock()

    def initialize(self) -> None:
        return None

    def pricing_config(self) -> dict[str, Any]:
        with self._lock:
            return _pricing_config(self.config)

    def set_pricing_config(
        self, cost_multiplier: float, *, updated_by: str
    ) -> dict[str, Any]:
        with self._lock:
            self.config = {
                "cost_multiplier_ppm": _normalize_multiplier(cost_multiplier),
                "updated_at": _now(),
                "updated_by": str(updated_by or ""),
            }
            return _pricing_config(self.config)

    def set_permission(
        self,
        user_id: str,
        *,
        enabled: bool,
        updated_by: str,
        max_keys: int | None = None,
    ) -> dict[str, Any]:
        normalized = str(user_id or "").strip()
        if not normalized:
            raise ProjectKeyConflict("用户标识不能为空")
        if max_keys is not None and not 1 <= int(max_keys) <= 100:
            raise ProjectKeyConflict("密钥数量上限必须为 1 到 100")
        now = _now()
        with self._lock:
            row = self.permissions.get(
                normalized,
                {
                    "created_at": now,
                    "max_keys": DEFAULT_MAX_KEYS,
                    "balance_microusd": 0,
                    "reserved_microusd": 0,
                },
            )
            row.update(
                user_id=normalized,
                enabled=bool(enabled),
                updated_at=now,
                updated_by=str(updated_by or ""),
            )
            if max_keys is not None:
                row["max_keys"] = int(max_keys)
            self.permissions[normalized] = row
            return dict(row)

    def permission(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.permissions.get(str(user_id))
            return dict(
                row
                or {
                    "user_id": str(user_id),
                    "enabled": False,
                    "created_at": None,
                    "updated_at": None,
                    "updated_by": None,
                    "max_keys": DEFAULT_MAX_KEYS,
                    "balance_microusd": None,
                    "reserved_microusd": 0,
                }
            )

    def permissions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.permissions.values()]

    def grant_credit(
        self,
        user_id: str,
        amount_microusd: int,
        *,
        reason: str,
        idempotency_key: str,
        updated_by: str,
    ) -> dict[str, Any]:
        owner = str(user_id or "").strip()
        amount = int(amount_microusd)
        reason_text = str(reason or "").strip()
        idem = str(idempotency_key or "").strip()
        if amount <= 0:
            raise ProjectKeyConflict("增加额度必须大于 0")
        with self._lock:
            permission = self.permissions.get(owner)
            if permission is None:
                raise KeyError(owner)
            existing = next(
                (
                    row
                    for row in self.credit_ledger
                    if row["user_id"] == owner
                    and row["idempotency_key"] == idem
                ),
                None,
            )
            if existing is not None:
                if (
                    int(existing["amount_microusd"]) != amount
                    or existing["reason"] != reason_text
                ):
                    raise ProjectKeyConflict("幂等键已用于另一笔额度操作")
                return dict(existing)
            current = permission.get("balance_microusd")
            balance = (int(current) if current is not None else 0) + amount
            permission["balance_microusd"] = balance
            permission["updated_at"] = _now()
            permission["updated_by"] = str(updated_by or "")
            entry = {
                "id": uuid.uuid4().hex,
                "user_id": owner,
                "idempotency_key": idem,
                "entry_type": "grant",
                "amount_microusd": amount,
                "balance_after_microusd": balance,
                "request_id": None,
                "key_id": None,
                "reason": reason_text,
                "created_by": str(updated_by or ""),
                "created_at": _now(),
            }
            self.credit_ledger.append(entry)
            return dict(entry)

    def list_credit_ledger(
        self, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row)
                for row in self.credit_ledger
                if row["user_id"] == str(user_id)
            ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)[
            : max(1, min(int(limit), 200))
        ]

    def begin_request(
        self,
        key_id: str,
        request_id: str,
        *,
        authorization_microusd: int,
    ) -> dict[str, Any]:
        key = (str(key_id), str(request_id))
        now = _now()
        with self._lock:
            if key in self.requests:
                raise ProjectRequestConflict("该请求标识已被使用，请勿重复提交")
            api_key = self.keys.get(key[0])
            if api_key is None:
                raise KeyError(key_id)
            permission = self.permissions.get(str(api_key["owner_user_id"]))
            if permission is None or not permission.get("enabled"):
                raise PermissionError("该账号尚未开通项目 API 权限")
            expired = [
                request
                for request in self.requests.values()
                if request["owner_user_id"] == api_key["owner_user_id"]
                and request["state"] == "reserved"
                and int(request["expires_at"]) <= now
            ]
            for request in expired:
                permission["reserved_microusd"] = max(
                    0,
                    int(permission.get("reserved_microusd") or 0)
                    - int(request["reserved_microusd"]),
                )
                request["state"] = "released"
                request["updated_at"] = now
            multiplier_ppm = int(self.config["cost_multiplier_ppm"])
            official = max(0, int(authorization_microusd))
            reserved = (
                official * multiplier_ppm + COST_MULTIPLIER_SCALE - 1
            ) // COST_MULTIPLIER_SCALE
            balance = permission.get("balance_microusd")
            available = (
                None
                if balance is None
                else int(balance) - int(permission.get("reserved_microusd") or 0)
            )
            if available is not None and available < reserved:
                raise ProjectBalanceInsufficient(
                    f"API 可用额度不足，当前可用 {available} microUSD"
                )
            permission["reserved_microusd"] = (
                int(permission.get("reserved_microusd") or 0) + reserved
            )
            row = {
                "key_id": key[0],
                "request_id": key[1],
                "owner_user_id": str(api_key["owner_user_id"]),
                "state": "reserved",
                "reserved_microusd": reserved,
                "cost_multiplier_ppm": multiplier_ppm,
                "created_at": now,
                "updated_at": now,
                "expires_at": now + REQUEST_RESERVATION_TTL_SECONDS,
            }
            self.requests[key] = row
            return dict(row)

    def create_key(self, owner_user_id: str, name: str) -> dict[str, Any]:
        normalized = str(name or "").strip()
        owner = str(owner_user_id or "").strip()
        if not self.permission(owner)["enabled"]:
            raise PermissionError("该账号尚未开通项目 API 权限")
        if not normalized or len(normalized) > 80:
            raise ProjectKeyConflict("项目名称长度必须为 1 到 80 个字符")
        with self._lock:
            permission = self.permission(owner)
            active_count = sum(
                row["owner_user_id"] == owner and row["status"] == "active"
                for row in self.keys.values()
            )
            if active_count >= int(permission.get("max_keys") or DEFAULT_MAX_KEYS):
                raise ProjectKeyConflict(
                    f"该账号最多可创建 {int(permission.get('max_keys') or DEFAULT_MAX_KEYS)} 个有效 API 密钥"
                )
            if any(
                row["name"].casefold() == normalized.casefold()
                and row["owner_user_id"] == owner
                and row["status"] == "active"
                for row in self.keys.values()
            ):
                raise ProjectKeyConflict("同名项目已有有效密钥")
            key_id = uuid.uuid4().hex
            visible = secrets.token_hex(4)
            plaintext = f"{KEY_PREFIX}{visible}_{secrets.token_urlsafe(32)}"
            now = _now()
            row = {
                "id": key_id,
                "owner_user_id": owner,
                "name": normalized,
                "key_prefix": f"{KEY_PREFIX}{visible}",
                "key_digest": _digest(self.master_key, plaintext),
                "status": "active",
                "created_at": now,
                "last_used_at": None,
                "revoked_at": None,
                "request_count": 0,
                "total_tokens": 0,
                "total_points": 0,
                "total_estimated_cost_microusd": 0,
                "total_official_cost_microusd": 0,
            }
            self.keys[key_id] = row
            return {**_public_key(row), "api_key": plaintext}

    def delete_permission(self, user_id: str) -> dict[str, Any]:
        normalized = str(user_id or "").strip()
        with self._lock:
            now = _now()
            for request in self.requests.values():
                if (
                    request["owner_user_id"] == normalized
                    and request["state"] == "reserved"
                    and int(request["expires_at"]) <= now
                ):
                    request["state"] = "released"
                    request["updated_at"] = now
            if any(
                request["owner_user_id"] == normalized
                and request["state"] == "reserved"
                for request in self.requests.values()
            ):
                raise ProjectKeyConflict("该账号仍有 API 调用处理中，暂不能删除")
            row = self.permissions.pop(normalized, None)
            if row is None:
                raise KeyError(normalized)
            now = _now()
            for key in self.keys.values():
                if key["owner_user_id"] == normalized and key["status"] == "active":
                    key["status"] = "revoked"
                    key["revoked_at"] = now
            return dict(row)

    def list_keys(self, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(
                (
                    row
                    for row in self.keys.values()
                    if owner_user_id is None
                    or row["owner_user_id"] == owner_user_id
                ),
                key=lambda item: (item["created_at"], item["name"]),
                reverse=True,
            )
            return [_public_key(row) for row in rows]

    def authenticate(self, secret: str) -> ProjectCaller | None:
        supplied = _digest(self.master_key, secret)
        with self._lock:
            for row in self.keys.values():
                if (
                    row["status"] == "active"
                    and self.permissions.get(row["owner_user_id"], {}).get("enabled")
                    and hmac.compare_digest(
                    supplied, row["key_digest"]
                    )
                ):
                    return ProjectCaller(
                        key_id=row["id"],
                        owner_user_id=row["owner_user_id"],
                        project_name=row["name"],
                        key_prefix=row["key_prefix"],
                    )
        return None

    def revoke_key(
        self, key_id: str, owner_user_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            row = self.keys.get(key_id)
            if row is None:
                raise KeyError(key_id)
            if owner_user_id is not None and row["owner_user_id"] != owner_user_id:
                raise KeyError(key_id)
            if row["status"] != "revoked":
                row["status"] = "revoked"
                row["revoked_at"] = _now()
            return _public_key(row)

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        key_id = str(entry["key_id"])
        request_id = str(entry["request_id"])
        with self._lock:
            row = self.keys.get(key_id)
            if row is None:
                return {"recorded": False, "reason": "missing_key"}
            duplicate = next(
                (
                    item
                    for item in self.usage
                    if str(item["key_id"]) == key_id
                    and str(item["request_id"]) == request_id
                ),
                None,
            )
            if duplicate is not None:
                return {
                    "recorded": False,
                    "reason": "duplicate",
                    "usage_id": str(duplicate["id"]),
                }
            request_row = self.requests.get((key_id, request_id))
            multiplier_ppm = int(
                request_row["cost_multiplier_ppm"]
                if request_row is not None
                else self.config["cost_multiplier_ppm"]
            )
            item = _apply_cost_multiplier(
                entry, multiplier_ppm
            )
            item.setdefault("id", uuid.uuid4().hex)
            item.setdefault("created_at", _now())
            self.usage.append(item)
            row["last_used_at"] = item["created_at"]
            row["request_count"] += 1
            row["total_tokens"] += int(item.get("total_tokens") or 0)
            row["total_official_cost_microusd"] += int(
                item.get("official_cost_microusd") or 0
            )
            row["total_estimated_cost_microusd"] += int(
                item.get("estimated_cost_microusd") or 0
            )
            permission = self.permissions.get(str(row["owner_user_id"]))
            if request_row is not None and permission is not None:
                permission["reserved_microusd"] = max(
                    0,
                    int(permission.get("reserved_microusd") or 0)
                    - int(request_row["reserved_microusd"]),
                )
                request_row["state"] = "completed"
                request_row["updated_at"] = _now()
            actual = int(item.get("estimated_cost_microusd") or 0)
            if (
                permission is not None
                and permission.get("balance_microusd") is not None
                and actual > 0
            ):
                balance = int(permission["balance_microusd"]) - actual
                permission["balance_microusd"] = balance
                ledger = {
                    "id": uuid.uuid4().hex,
                    "user_id": str(row["owner_user_id"]),
                    "idempotency_key": f"usage:{key_id}:{request_id}",
                    "entry_type": "usage",
                    "amount_microusd": -actual,
                    "balance_after_microusd": balance,
                    "request_id": request_id,
                    "key_id": key_id,
                    "reason": f"项目 {row['name']} API 调用",
                    "created_by": "system",
                    "created_at": int(item["created_at"]),
                }
                self.credit_ledger.append(ledger)
            return {
                "recorded": True,
                "usage_id": str(item["id"]),
                "actual_cost_microusd": actual,
                "balance_microusd": (
                    permission.get("balance_microusd")
                    if permission is not None
                    else None
                ),
            }

    def summary(
        self,
        hours: int,
        *,
        owner_user_id: str | None = None,
        key_id: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        cutoff = _now() - int(hours) * 3600
        with self._lock:
            keys = {
                current_id: dict(row)
                for current_id, row in self.keys.items()
                if owner_user_id is None or row["owner_user_id"] == owner_user_id
            }
            rows = [
                dict(row)
                for row in self.usage
                if row["created_at"] >= cutoff
                and str(row["key_id"]) in keys
                and (key_id is None or str(row["key_id"]) == key_id)
                and (model is None or str(row["model"]) == model)
                and (outcome is None or str(row["outcome"]) == outcome)
            ]
        return _usage_summary(
            rows,
            keys,
            hours,
            limit=limit,
            offset=offset,
            pricing_config=self.pricing_config(),
        )


class PostgresProjectUsageStore:
    def __init__(self, database_url: str, master_key: str):
        self.database_url = database_url
        self.master_key = master_key

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - production dependency gate
            raise RuntimeError("psycopg is required for project API usage") from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_user (
                user_id TEXT PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                max_keys INTEGER NOT NULL DEFAULT 5
                    CHECK (max_keys BETWEEN 1 AND 100),
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """,
            """
            ALTER TABLE chat_project_api_user
                ADD COLUMN IF NOT EXISTS max_keys INTEGER NOT NULL DEFAULT 5
            """,
            """
            ALTER TABLE chat_project_api_user
                ADD COLUMN IF NOT EXISTS balance_microusd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_user
                ADD COLUMN IF NOT EXISTS reserved_microusd
                BIGINT NOT NULL DEFAULT 0
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_config (
                config_key TEXT PRIMARY KEY,
                cost_multiplier_ppm BIGINT NOT NULL DEFAULT 1000000
                    CHECK (
                        cost_multiplier_ppm BETWEEN 0 AND 100000000
                    ),
                updated_at BIGINT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """,
            f"""
            INSERT INTO chat_project_api_config
                (config_key, cost_multiplier_ppm, updated_at, updated_by)
            VALUES ('default', {COST_MULTIPLIER_SCALE}, {_now()}, 'system')
            ON CONFLICT (config_key) DO NOTHING
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_key (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL UNIQUE,
                key_digest TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'revoked')),
                created_at BIGINT NOT NULL,
                last_used_at BIGINT,
                revoked_at BIGINT,
                request_count BIGINT NOT NULL DEFAULT 0,
                total_tokens BIGINT NOT NULL DEFAULT 0,
                total_points BIGINT NOT NULL DEFAULT 0,
                total_estimated_cost_microusd BIGINT NOT NULL DEFAULT 0,
                total_official_cost_microusd BIGINT NOT NULL DEFAULT 0
            )
            """,
            """
            ALTER TABLE chat_project_api_key
                ADD COLUMN IF NOT EXISTS total_estimated_cost_microusd
                BIGINT NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE chat_project_api_key
                ADD COLUMN IF NOT EXISTS total_official_cost_microusd
                BIGINT NOT NULL DEFAULT 0
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS chat_project_api_key_active_name_idx
                ON chat_project_api_key (owner_user_id, lower(name))
                WHERE status = 'active'
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_usage (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                key_id TEXT NOT NULL
                    REFERENCES chat_project_api_key(id) ON DELETE RESTRICT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                route TEXT,
                stream BOOLEAN NOT NULL,
                outcome TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                prompt_tokens BIGINT,
                cached_tokens BIGINT,
                cache_write_tokens BIGINT,
                completion_tokens BIGINT,
                total_tokens BIGINT,
                usage_source TEXT NOT NULL,
                points INTEGER NOT NULL,
                pricing_profile TEXT,
                price_card_version TEXT,
                input_rate_nano_usd BIGINT,
                cached_input_rate_nano_usd BIGINT,
                cache_write_rate_nano_usd BIGINT,
                output_rate_nano_usd BIGINT,
                official_cost_microusd BIGINT,
                cost_multiplier_ppm BIGINT,
                estimated_cost_microusd BIGINT,
                latency_ms INTEGER NOT NULL,
                created_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_request (
                key_id TEXT NOT NULL
                    REFERENCES chat_project_api_key(id) ON DELETE RESTRICT,
                request_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                state TEXT NOT NULL
                    CHECK (state IN ('reserved', 'completed', 'released')),
                reserved_microusd BIGINT NOT NULL DEFAULT 0,
                cost_multiplier_ppm BIGINT NOT NULL,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                PRIMARY KEY (key_id, request_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_project_api_credit_ledger (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                entry_type TEXT NOT NULL
                    CHECK (entry_type IN ('grant', 'usage')),
                amount_microusd BIGINT NOT NULL,
                balance_after_microusd BIGINT NOT NULL,
                request_id TEXT,
                key_id TEXT,
                reason TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                UNIQUE (user_id, idempotency_key)
            )
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS cached_tokens BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS pricing_profile TEXT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS price_card_version TEXT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS input_rate_nano_usd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS cached_input_rate_nano_usd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS cache_write_rate_nano_usd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS output_rate_nano_usd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS estimated_cost_microusd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS official_cost_microusd BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS cost_multiplier_ppm BIGINT
            """,
            """
            ALTER TABLE chat_project_api_usage
                ADD COLUMN IF NOT EXISTS dedupe_key TEXT
            """,
            """
            UPDATE chat_project_api_usage
            SET dedupe_key = 'legacy:' || id
            WHERE dedupe_key IS NULL
            """,
            f"""
            UPDATE chat_project_api_usage
            SET official_cost_microusd = COALESCE(
                    official_cost_microusd,
                    estimated_cost_microusd,
                    0
                ),
                cost_multiplier_ppm = COALESCE(
                    cost_multiplier_ppm,
                    {COST_MULTIPLIER_SCALE}
                )
            WHERE official_cost_microusd IS NULL
               OR cost_multiplier_ppm IS NULL
            """,
            """
            UPDATE chat_project_api_key AS key
            SET total_official_cost_microusd = totals.amount
            FROM (
                SELECT key_id, COALESCE(SUM(official_cost_microusd), 0) AS amount
                FROM chat_project_api_usage
                GROUP BY key_id
            ) AS totals
            WHERE key.id = totals.key_id
              AND key.total_official_cost_microusd = 0
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_project_api_usage_time_idx
                ON chat_project_api_usage (created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_project_api_usage_key_time_idx
                ON chat_project_api_usage (key_id, created_at DESC)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS chat_project_api_usage_dedupe_idx
                ON chat_project_api_usage (dedupe_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_project_api_credit_user_time_idx
                ON chat_project_api_credit_ledger (user_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_project_api_request_owner_state_idx
                ON chat_project_api_request
                    (owner_user_id, state, expires_at)
            """,
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

    def pricing_config(self) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cost_multiplier_ppm, updated_at, updated_by
                    FROM chat_project_api_config
                    WHERE config_key = 'default'
                    """
                )
                row = cursor.fetchone()
        return _pricing_config(
            dict(
                row
                or {
                    "cost_multiplier_ppm": COST_MULTIPLIER_SCALE,
                    "updated_at": None,
                    "updated_by": "system",
                }
            )
        )

    def set_pricing_config(
        self, cost_multiplier: float, *, updated_by: str
    ) -> dict[str, Any]:
        ppm = _normalize_multiplier(cost_multiplier)
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_project_api_config
                        (config_key, cost_multiplier_ppm, updated_at, updated_by)
                    VALUES ('default', %s, %s, %s)
                    ON CONFLICT (config_key) DO UPDATE SET
                        cost_multiplier_ppm = excluded.cost_multiplier_ppm,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    RETURNING cost_multiplier_ppm, updated_at, updated_by
                    """,
                    (ppm, now, str(updated_by or "")),
                )
                return _pricing_config(dict(cursor.fetchone()))

    def set_permission(
        self,
        user_id: str,
        *,
        enabled: bool,
        updated_by: str,
        max_keys: int | None = None,
    ) -> dict[str, Any]:
        normalized = str(user_id or "").strip()
        if not normalized:
            raise ProjectKeyConflict("用户标识不能为空")
        if max_keys is not None and not 1 <= int(max_keys) <= 100:
            raise ProjectKeyConflict("密钥数量上限必须为 1 到 100")
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_project_api_user
                        (user_id, enabled, max_keys, balance_microusd,
                         created_at, updated_at, updated_by)
                    VALUES (%s, %s, COALESCE(%s, 5), 0, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        max_keys = COALESCE(%s, chat_project_api_user.max_keys),
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    RETURNING *
                    """,
                    (
                        normalized,
                        bool(enabled),
                        max_keys,
                        now,
                        now,
                        str(updated_by or ""),
                        max_keys,
                    ),
                )
                return dict(cursor.fetchone())

    def permission(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM chat_project_api_user WHERE user_id = %s",
                    (str(user_id),),
                )
                row = cursor.fetchone()
        return dict(
            row
            or {
                "user_id": str(user_id),
                "enabled": False,
                "created_at": None,
                "updated_at": None,
                "updated_by": None,
                "max_keys": DEFAULT_MAX_KEYS,
            }
        )

    def permissions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM chat_project_api_user ORDER BY updated_at DESC"
                )
                return [dict(row) for row in cursor.fetchall()]

    def grant_credit(
        self,
        user_id: str,
        amount_microusd: int,
        *,
        reason: str,
        idempotency_key: str,
        updated_by: str,
    ) -> dict[str, Any]:
        owner = str(user_id or "").strip()
        amount = int(amount_microusd)
        reason_text = str(reason or "").strip()
        idem = str(idempotency_key or "").strip()
        if amount <= 0:
            raise ProjectKeyConflict("增加额度必须大于 0")
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_credit_ledger
                    WHERE user_id = %s AND idempotency_key = %s
                    """,
                    (owner, idem),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if (
                        int(existing["amount_microusd"]) != amount
                        or str(existing["reason"]) != reason_text
                    ):
                        raise ProjectKeyConflict("幂等键已用于另一笔额度操作")
                    return dict(existing)
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_user
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (owner,),
                )
                permission = cursor.fetchone()
                if permission is None:
                    raise KeyError(owner)
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_credit_ledger
                    WHERE user_id = %s AND idempotency_key = %s
                    """,
                    (owner, idem),
                )
                concurrent_existing = cursor.fetchone()
                if concurrent_existing is not None:
                    if (
                        int(concurrent_existing["amount_microusd"]) != amount
                        or str(concurrent_existing["reason"]) != reason_text
                    ):
                        raise ProjectKeyConflict("幂等键已用于另一笔额度操作")
                    return dict(concurrent_existing)
                balance = int(permission["balance_microusd"] or 0) + amount
                cursor.execute(
                    """
                    UPDATE chat_project_api_user
                    SET balance_microusd = %s, updated_at = %s, updated_by = %s
                    WHERE user_id = %s
                    """,
                    (balance, now, str(updated_by or ""), owner),
                )
                entry = {
                    "id": uuid.uuid4().hex,
                    "user_id": owner,
                    "idempotency_key": idem,
                    "entry_type": "grant",
                    "amount_microusd": amount,
                    "balance_after_microusd": balance,
                    "request_id": None,
                    "key_id": None,
                    "reason": reason_text,
                    "created_by": str(updated_by or ""),
                    "created_at": now,
                }
                try:
                    cursor.execute(
                        """
                        INSERT INTO chat_project_api_credit_ledger
                            (id, user_id, idempotency_key, entry_type,
                             amount_microusd, balance_after_microusd,
                             request_id, key_id, reason, created_by, created_at)
                        VALUES
                            (%(id)s, %(user_id)s, %(idempotency_key)s,
                             %(entry_type)s, %(amount_microusd)s,
                             %(balance_after_microusd)s, %(request_id)s,
                             %(key_id)s, %(reason)s, %(created_by)s,
                             %(created_at)s)
                        """,
                        entry,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) != "23505":
                        raise
                    raise ProjectKeyConflict("额度操作发生并发冲突，请重试") from exc
                return entry

    def list_credit_ledger(
        self, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_credit_ledger
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (str(user_id), max(1, min(int(limit), 200))),
                )
                return [dict(row) for row in cursor.fetchall()]

    def begin_request(
        self,
        key_id: str,
        request_id: str,
        *,
        authorization_microusd: int,
    ) -> dict[str, Any]:
        normalized_key = str(key_id)
        normalized_request = str(request_id)
        now = _now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT key.owner_user_id, permission.enabled,
                           permission.balance_microusd,
                           permission.reserved_microusd
                    FROM chat_project_api_key AS key
                    JOIN chat_project_api_user AS permission
                      ON permission.user_id = key.owner_user_id
                    WHERE key.id = %s AND key.status = 'active'
                    FOR UPDATE OF permission
                    """,
                    (normalized_key,),
                )
                owner = cursor.fetchone()
                if owner is None:
                    raise KeyError(normalized_key)
                if not owner["enabled"]:
                    raise PermissionError("该账号尚未开通项目 API 权限")
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(reserved_microusd), 0) AS amount
                    FROM chat_project_api_request
                    WHERE owner_user_id = %s
                      AND state = 'reserved'
                      AND expires_at <= %s
                    """,
                    (owner["owner_user_id"], now),
                )
                expired_amount = int(cursor.fetchone()["amount"])
                if expired_amount:
                    cursor.execute(
                        """
                        UPDATE chat_project_api_request
                        SET state = 'released', updated_at = %s
                        WHERE owner_user_id = %s
                          AND state = 'reserved'
                          AND expires_at <= %s
                        """,
                        (now, owner["owner_user_id"], now),
                    )
                    cursor.execute(
                        """
                        UPDATE chat_project_api_user
                        SET reserved_microusd =
                            GREATEST(0, reserved_microusd - %s)
                        WHERE user_id = %s
                        """,
                        (expired_amount, owner["owner_user_id"]),
                    )
                cursor.execute(
                    """
                    SELECT cost_multiplier_ppm
                    FROM chat_project_api_config
                    WHERE config_key = 'default'
                    FOR SHARE
                    """
                )
                config = cursor.fetchone()
                multiplier_ppm = int(
                    config["cost_multiplier_ppm"]
                    if config is not None
                    else COST_MULTIPLIER_SCALE
                )
                official = max(0, int(authorization_microusd))
                reserved = (
                    official * multiplier_ppm + COST_MULTIPLIER_SCALE - 1
                ) // COST_MULTIPLIER_SCALE
                cursor.execute(
                    """
                    SELECT balance_microusd, reserved_microusd
                    FROM chat_project_api_user
                    WHERE user_id = %s
                    """,
                    (owner["owner_user_id"],),
                )
                balance_row = cursor.fetchone()
                balance = balance_row["balance_microusd"]
                available = (
                    None
                    if balance is None
                    else int(balance) - int(balance_row["reserved_microusd"])
                )
                if available is not None and available < reserved:
                    raise ProjectBalanceInsufficient(
                        f"API 可用额度不足，当前可用 {available} microUSD"
                    )
                request_row = {
                    "key_id": normalized_key,
                    "request_id": normalized_request,
                    "owner_user_id": str(owner["owner_user_id"]),
                    "state": "reserved",
                    "reserved_microusd": reserved,
                    "cost_multiplier_ppm": multiplier_ppm,
                    "created_at": now,
                    "updated_at": now,
                    "expires_at": now + REQUEST_RESERVATION_TTL_SECONDS,
                }
                try:
                    cursor.execute(
                        """
                        INSERT INTO chat_project_api_request
                            (key_id, request_id, owner_user_id, state,
                             reserved_microusd, cost_multiplier_ppm,
                             created_at, updated_at, expires_at)
                        VALUES
                            (%(key_id)s, %(request_id)s, %(owner_user_id)s,
                             %(state)s, %(reserved_microusd)s,
                             %(cost_multiplier_ppm)s, %(created_at)s,
                             %(updated_at)s, %(expires_at)s)
                        """,
                        request_row,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) == "23505":
                        raise ProjectRequestConflict(
                            "该请求标识已被使用，请勿重复提交"
                        ) from exc
                    raise
                cursor.execute(
                    """
                    UPDATE chat_project_api_user
                    SET reserved_microusd = reserved_microusd + %s
                    WHERE user_id = %s
                    """,
                    (reserved, owner["owner_user_id"]),
                )
                return request_row

    def create_key(self, owner_user_id: str, name: str) -> dict[str, Any]:
        normalized = str(name or "").strip()
        owner = str(owner_user_id or "").strip()
        if not normalized or len(normalized) > 80:
            raise ProjectKeyConflict("项目名称长度必须为 1 到 80 个字符")
        key_id = uuid.uuid4().hex
        visible = secrets.token_hex(4)
        plaintext = f"{KEY_PREFIX}{visible}_{secrets.token_urlsafe(32)}"
        now = _now()
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT enabled, max_keys
                        FROM chat_project_api_user
                        WHERE user_id = %s
                        FOR UPDATE
                        """,
                        (owner,),
                    )
                    permission = cursor.fetchone()
                    if permission is None or not permission["enabled"]:
                        raise PermissionError("该账号尚未开通项目 API 权限")
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS active_count
                        FROM chat_project_api_key
                        WHERE owner_user_id = %s AND status = 'active'
                        """,
                        (owner,),
                    )
                    active_count = int(cursor.fetchone()["active_count"])
                    max_keys = int(permission["max_keys"])
                    if active_count >= max_keys:
                        raise ProjectKeyConflict(
                            f"该账号最多可创建 {max_keys} 个有效 API 密钥"
                        )
                    cursor.execute(
                        """
                        INSERT INTO chat_project_api_key
                            (id, owner_user_id, name, key_prefix, key_digest,
                             status, created_at)
                        VALUES (%s, %s, %s, %s, %s, 'active', %s)
                        RETURNING *
                        """,
                        (
                            key_id,
                            owner,
                            normalized,
                            f"{KEY_PREFIX}{visible}",
                            _digest(self.master_key, plaintext),
                            now,
                        ),
                    )
                    row = dict(cursor.fetchone())
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ProjectKeyConflict("同名项目已有有效密钥") from exc
            raise
        return {**_public_key(row), "api_key": plaintext}

    def delete_permission(self, user_id: str) -> dict[str, Any]:
        normalized = str(user_id or "").strip()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_user
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (normalized,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(normalized)
                cursor.execute(
                    """
                    UPDATE chat_project_api_request
                    SET state = 'released', updated_at = %s
                    WHERE owner_user_id = %s
                      AND state = 'reserved'
                      AND expires_at <= %s
                    """,
                    (_now(), normalized, _now()),
                )
                cursor.execute(
                    """
                    SELECT 1
                    FROM chat_project_api_request
                    WHERE owner_user_id = %s AND state = 'reserved'
                    LIMIT 1
                    """,
                    (normalized,),
                )
                if cursor.fetchone() is not None:
                    raise ProjectKeyConflict(
                        "该账号仍有 API 调用处理中，暂不能删除"
                    )
                cursor.execute(
                    """
                    UPDATE chat_project_api_key
                    SET status = 'revoked', revoked_at = COALESCE(revoked_at, %s)
                    WHERE owner_user_id = %s AND status = 'active'
                    """,
                    (_now(), normalized),
                )
                cursor.execute(
                    "DELETE FROM chat_project_api_user WHERE user_id = %s",
                    (normalized,),
                )
                return dict(row)

    def list_keys(self, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if owner_user_id is None:
                    cursor.execute(
                        "SELECT * FROM chat_project_api_key ORDER BY created_at DESC, name"
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM chat_project_api_key
                        WHERE owner_user_id = %s
                        ORDER BY created_at DESC, name
                        """,
                        (owner_user_id,),
                    )
                return [_public_key(dict(row)) for row in cursor.fetchall()]

    def authenticate(self, secret: str) -> ProjectCaller | None:
        supplied = _digest(self.master_key, secret)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT key.id, key.owner_user_id, key.name,
                           key.key_prefix, key.key_digest
                    FROM chat_project_api_key AS key
                    JOIN chat_project_api_user AS permission
                      ON permission.user_id = key.owner_user_id
                    WHERE key.status = 'active'
                      AND permission.enabled = TRUE
                      AND key.key_digest = %s
                    """,
                    (supplied,),
                )
                row = cursor.fetchone()
        if row is None or not hmac.compare_digest(supplied, str(row["key_digest"])):
            return None
        return ProjectCaller(
            key_id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            project_name=str(row["name"]),
            key_prefix=str(row["key_prefix"]),
        )

    def revoke_key(
        self, key_id: str, owner_user_id: str | None = None
    ) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if owner_user_id is None:
                    cursor.execute(
                        """
                        UPDATE chat_project_api_key
                        SET status = 'revoked', revoked_at = COALESCE(revoked_at, %s)
                        WHERE id = %s
                        RETURNING *
                        """,
                        (_now(), key_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE chat_project_api_key
                        SET status = 'revoked', revoked_at = COALESCE(revoked_at, %s)
                        WHERE id = %s AND owner_user_id = %s
                        RETURNING *
                        """,
                        (_now(), key_id, owner_user_id),
                    )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(key_id)
        return _public_key(dict(row))

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                key_id = str(entry["key_id"])
                request_id = str(entry["request_id"])
                cursor.execute(
                    """
                    SELECT *
                    FROM chat_project_api_request
                    WHERE key_id = %s AND request_id = %s
                    FOR UPDATE
                    """,
                    (key_id, request_id),
                )
                request_row = cursor.fetchone()
                if request_row is None:
                    cursor.execute(
                        """
                        SELECT cost_multiplier_ppm
                        FROM chat_project_api_config
                        WHERE config_key = 'default'
                        FOR SHARE
                        """
                    )
                    config = cursor.fetchone()
                    multiplier_ppm = int(
                        config["cost_multiplier_ppm"]
                        if config is not None
                        else COST_MULTIPLIER_SCALE
                    )
                else:
                    multiplier_ppm = int(request_row["cost_multiplier_ppm"])
                item = _apply_cost_multiplier(entry, multiplier_ppm)
                item.setdefault("id", uuid.uuid4().hex)
                item.setdefault("created_at", _now())
                item["dedupe_key"] = f"request:{key_id}:{request_id}"
                cursor.execute(
                    """
                    INSERT INTO chat_project_api_usage
                        (id, dedupe_key, request_id, key_id, provider, model,
                         route, stream, outcome, status_code, prompt_tokens,
                         cached_tokens, cache_write_tokens, completion_tokens,
                         total_tokens, usage_source, points, pricing_profile,
                         price_card_version, input_rate_nano_usd,
                         cached_input_rate_nano_usd, cache_write_rate_nano_usd,
                         output_rate_nano_usd, official_cost_microusd,
                         cost_multiplier_ppm, estimated_cost_microusd,
                         latency_ms, created_at)
                    VALUES
                        (%(id)s, %(dedupe_key)s, %(request_id)s, %(key_id)s,
                         %(provider)s, %(model)s, %(route)s, %(stream)s,
                         %(outcome)s, %(status_code)s, %(prompt_tokens)s,
                         %(cached_tokens)s, %(cache_write_tokens)s,
                         %(completion_tokens)s, %(total_tokens)s,
                         %(usage_source)s, %(points)s, %(pricing_profile)s,
                         %(price_card_version)s, %(input_rate_nano_usd)s,
                         %(cached_input_rate_nano_usd)s,
                         %(cache_write_rate_nano_usd)s,
                         %(output_rate_nano_usd)s,
                         %(official_cost_microusd)s,
                         %(cost_multiplier_ppm)s,
                         %(estimated_cost_microusd)s, %(latency_ms)s,
                         %(created_at)s)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """,
                    item,
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        """
                        SELECT id
                        FROM chat_project_api_usage
                        WHERE dedupe_key = %s
                        """,
                        (item["dedupe_key"],),
                    )
                    duplicate = cursor.fetchone()
                    return {
                        "recorded": False,
                        "reason": "duplicate",
                        "usage_id": str(duplicate["id"]) if duplicate else None,
                    }
                cursor.execute(
                    """
                    UPDATE chat_project_api_key
                    SET last_used_at = %(created_at)s,
                        request_count = request_count + 1,
                        total_tokens = total_tokens + COALESCE(%(total_tokens)s, 0),
                        total_official_cost_microusd =
                            total_official_cost_microusd
                            + COALESCE(%(official_cost_microusd)s, 0),
                        total_estimated_cost_microusd =
                            total_estimated_cost_microusd
                            + COALESCE(%(estimated_cost_microusd)s, 0)
                    WHERE id = %(key_id)s
                    RETURNING owner_user_id, name
                    """,
                    item,
                )
                key_row = cursor.fetchone()
                if key_row is None:
                    raise KeyError(key_id)
                owner = str(key_row["owner_user_id"])
                if request_row is not None:
                    cursor.execute(
                        """
                        UPDATE chat_project_api_request
                        SET state = 'completed', updated_at = %s
                        WHERE key_id = %s AND request_id = %s
                        """,
                        (_now(), key_id, request_id),
                    )
                    cursor.execute(
                        """
                        UPDATE chat_project_api_user
                        SET reserved_microusd =
                            GREATEST(0, reserved_microusd - %s)
                        WHERE user_id = %s
                        """,
                        (int(request_row["reserved_microusd"]), owner),
                    )
                cursor.execute(
                    """
                    SELECT balance_microusd
                    FROM chat_project_api_user
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (owner,),
                )
                permission = cursor.fetchone()
                actual = int(item.get("estimated_cost_microusd") or 0)
                balance = (
                    permission["balance_microusd"]
                    if permission is not None
                    else None
                )
                if balance is not None and actual > 0:
                    balance_after = int(balance) - actual
                    cursor.execute(
                        """
                        UPDATE chat_project_api_user
                        SET balance_microusd = %s
                        WHERE user_id = %s
                        """,
                        (balance_after, owner),
                    )
                    cursor.execute(
                        """
                        INSERT INTO chat_project_api_credit_ledger
                            (id, user_id, idempotency_key, entry_type,
                             amount_microusd, balance_after_microusd,
                             request_id, key_id, reason, created_by, created_at)
                        VALUES (%s, %s, %s, 'usage', %s, %s, %s, %s, %s,
                                'system', %s)
                        ON CONFLICT (user_id, idempotency_key) DO NOTHING
                        """,
                        (
                            uuid.uuid4().hex,
                            owner,
                            f"usage:{key_id}:{request_id}",
                            -actual,
                            balance_after,
                            request_id,
                            key_id,
                            f"项目 {key_row['name']} API 调用",
                            int(item["created_at"]),
                        ),
                    )
                    balance = balance_after
                return {
                    "recorded": True,
                    "usage_id": str(item["id"]),
                    "actual_cost_microusd": actual,
                    "balance_microusd": balance,
                }

    def summary(
        self,
        hours: int,
        *,
        owner_user_id: str | None = None,
        key_id: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        cutoff = _now() - int(hours) * 3600
        conditions = ["usage.created_at >= %s"]
        parameters: list[Any] = [cutoff]
        if owner_user_id is not None:
            conditions.append("key.owner_user_id = %s")
            parameters.append(owner_user_id)
        if key_id is not None:
            conditions.append("usage.key_id = %s")
            parameters.append(key_id)
        if model is not None:
            conditions.append("usage.model = %s")
            parameters.append(model)
        if outcome is not None:
            conditions.append("usage.outcome = %s")
            parameters.append(outcome)
        where_clause = "\n                      AND ".join(conditions)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT usage.*
                    FROM chat_project_api_usage AS usage
                    JOIN chat_project_api_key AS key ON key.id = usage.key_id
                    WHERE {where_clause}
                    ORDER BY usage.created_at DESC
                    """,
                    tuple(parameters),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                if owner_user_id is None:
                    cursor.execute("SELECT * FROM chat_project_api_key")
                else:
                    cursor.execute(
                        """
                        SELECT * FROM chat_project_api_key
                        WHERE owner_user_id = %s
                        """,
                        (owner_user_id,),
                    )
                keys = {
                    str(row["id"]): dict(row)
                    for row in cursor.fetchall()
                }
        return _usage_summary(
            rows,
            keys,
            hours,
            limit=limit,
            offset=offset,
            pricing_config=self.pricing_config(),
        )


def _usage_summary(
    rows: list[dict[str, Any]],
    keys: dict[str, dict[str, Any]],
    hours: int,
    *,
    limit: int,
    offset: int,
    pricing_config: dict[str, Any],
) -> dict[str, Any]:
    projects: dict[str, dict[str, Any]] = {}
    for key_id, key in keys.items():
        projects[key_id] = {
            "key_id": key_id,
            "owner_user_id": str(key["owner_user_id"]),
            "name": str(key["name"]),
            "key_prefix": str(key["key_prefix"]),
            "status": str(key["status"]),
            "requests": 0,
            "errors": 0,
            "reported_tokens": 0,
            "total_tokens": 0,
            "official_cost_microusd": 0,
            "actual_cost_microusd": 0,
            "last_used_at": key.get("last_used_at"),
        }
    for row in rows:
        project = projects.get(str(row["key_id"]))
        if project is None:
            continue
        project["requests"] += 1
        project["errors"] += int(str(row.get("outcome")) != "success")
        project["total_tokens"] += int(row.get("total_tokens") or 0)
        if str(row.get("usage_source")) == "upstream_reported":
            project["reported_tokens"] += int(row.get("total_tokens") or 0)
        project["official_cost_microusd"] += int(
            row.get("official_cost_microusd")
            or row.get("estimated_cost_microusd")
            or 0
        )
        project["actual_cost_microusd"] += int(
            row.get("estimated_cost_microusd") or 0
        )
    ordered = sorted(
        projects.values(),
        key=lambda item: (
            item["actual_cost_microusd"],
            item["requests"],
            item["name"],
        ),
        reverse=True,
    )
    ordered_rows = sorted(rows, key=lambda item: item["created_at"], reverse=True)
    recent = [
        {
            "id": str(row["id"]),
            "request_id": str(row["request_id"]),
            "key_id": str(row["key_id"]),
            "project_name": str(keys.get(str(row["key_id"]), {}).get("name") or "未知项目"),
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "route": row.get("route"),
            "stream": bool(row["stream"]),
            "outcome": str(row["outcome"]),
            "status_code": int(row["status_code"]),
            "prompt_tokens": row.get("prompt_tokens"),
            "cached_tokens": row.get("cached_tokens"),
            "cache_write_tokens": row.get("cache_write_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "total_tokens": row.get("total_tokens"),
            "usage_source": str(row["usage_source"]),
            "pricing_profile": row.get("pricing_profile"),
            "price_card_version": row.get("price_card_version"),
            "input_rate_nano_usd": row.get("input_rate_nano_usd"),
            "cached_input_rate_nano_usd": row.get(
                "cached_input_rate_nano_usd"
            ),
            "cache_write_rate_nano_usd": row.get(
                "cache_write_rate_nano_usd"
            ),
            "output_rate_nano_usd": row.get("output_rate_nano_usd"),
            "official_cost_microusd": row.get("official_cost_microusd")
            or row.get("estimated_cost_microusd"),
            "cost_multiplier": int(
                row.get("cost_multiplier_ppm")
                if row.get("cost_multiplier_ppm") is not None
                else COST_MULTIPLIER_SCALE
            )
            / COST_MULTIPLIER_SCALE,
            "cost_multiplier_ppm": int(
                row.get("cost_multiplier_ppm")
                if row.get("cost_multiplier_ppm") is not None
                else COST_MULTIPLIER_SCALE
            ),
            "actual_cost_microusd": row.get("estimated_cost_microusd"),
            "latency_ms": int(row["latency_ms"]),
            "created_at": int(row["created_at"]),
        }
        for row in ordered_rows[offset : offset + limit]
    ]
    return {
        "hours": int(hours),
        "totals": {
            "requests": len(rows),
            "errors": sum(str(row.get("outcome")) != "success" for row in rows),
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in rows),
            "reported_tokens": sum(
                int(row.get("total_tokens") or 0)
                for row in rows
                if str(row.get("usage_source")) == "upstream_reported"
            ),
            "estimated_tokens": sum(
                int(row.get("total_tokens") or 0)
                for row in rows
                if str(row.get("usage_source")) == "locally_estimated"
            ),
            "official_cost_microusd": sum(
                int(
                    row.get("official_cost_microusd")
                    or row.get("estimated_cost_microusd")
                    or 0
                )
                for row in rows
            ),
            "actual_cost_microusd": sum(
                int(row.get("estimated_cost_microusd") or 0) for row in rows
            ),
            "upstream_reported_requests": sum(
                str(row.get("usage_source")) == "upstream_reported" for row in rows
            ),
            "locally_estimated_requests": sum(
                str(row.get("usage_source")) == "locally_estimated" for row in rows
            ),
            "fallback_requests": sum(
                str(row.get("usage_source")) == "request_fallback" for row in rows
            ),
            "not_charged_requests": sum(
                str(row.get("usage_source")) == "not_charged" for row in rows
            ),
        },
        "projects": ordered,
        "recent": recent,
        "pagination": {
            "total": len(rows),
            "limit": int(limit),
            "offset": int(offset),
            "has_more": offset + len(recent) < len(rows),
        },
        "pricing_config": pricing_config,
        "price_card_version": PRICE_CARD_VERSION,
        "price_profiles": {
            name: {
                "model": price.model,
                "input_usd_per_million": price.input_rate_nano_usd / 1000,
                "cached_input_usd_per_million": (
                    price.cached_input_rate_nano_usd / 1000
                    if price.cached_input_rate_nano_usd is not None
                    else None
                ),
                "cache_write_usd_per_million": (
                    price.cache_write_rate_nano_usd / 1000
                    if price.cache_write_rate_nano_usd is not None
                    else None
                ),
                "output_usd_per_million": price.output_rate_nano_usd / 1000,
            }
            for name, price in OPENAI_STANDARD_PRICES.items()
        },
        "usage_notice": (
            "This is a versioned simulation of standard OpenAI API token rates, "
            "then adjusted by the stored multiplier. It is not an OpenAI invoice "
            "or ChatGPT subscription balance. Token counts are upstream-reported "
            "when available and otherwise locally estimated."
        ),
    }


class ProjectUsageManager:
    def __init__(self, store: MemoryProjectUsageStore | PostgresProjectUsageStore):
        self.store = store

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize)

    async def pricing_config(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.pricing_config)

    async def set_pricing_config(
        self, cost_multiplier: float, *, updated_by: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.set_pricing_config,
            cost_multiplier,
            updated_by=updated_by,
        )

    async def set_permission(
        self,
        user_id: str,
        *,
        enabled: bool,
        updated_by: str,
        max_keys: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.set_permission,
            user_id,
            enabled=enabled,
            updated_by=updated_by,
            max_keys=max_keys,
        )

    async def permission(self, user_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.permission, user_id)

    async def permissions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.store.permissions)

    async def grant_credit(
        self,
        user_id: str,
        amount_microusd: int,
        *,
        reason: str,
        idempotency_key: str,
        updated_by: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.grant_credit,
            user_id,
            amount_microusd,
            reason=reason,
            idempotency_key=idempotency_key,
            updated_by=updated_by,
        )

    async def credit_ledger(
        self, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.store.list_credit_ledger,
            user_id,
            limit=limit,
        )

    async def begin_request(
        self,
        key_id: str,
        request_id: str,
        *,
        authorization_microusd: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.begin_request,
            key_id,
            request_id,
            authorization_microusd=authorization_microusd,
        )

    async def delete_permission(self, user_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.delete_permission, user_id)

    async def create_key(
        self, owner_user_id: str, name: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.create_key, owner_user_id, name
        )

    async def list_keys(
        self, owner_user_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.store.list_keys, owner_user_id)

    async def authenticate(self, secret: str) -> ProjectCaller | None:
        return await asyncio.to_thread(self.store.authenticate, secret)

    async def revoke_key(
        self, key_id: str, owner_user_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.revoke_key, key_id, owner_user_id
        )

    async def record(self, **entry: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.record, entry)

    async def summary(
        self,
        hours: int,
        *,
        owner_user_id: str | None = None,
        key_id: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.store.summary,
            hours,
            owner_user_id=owner_user_id,
            key_id=key_id,
            model=model,
            outcome=outcome,
            limit=limit,
            offset=offset,
        )


def build_project_usage(
    *, database_url: str | None, master_key: str
) -> ProjectUsageManager:
    store: MemoryProjectUsageStore | PostgresProjectUsageStore
    if database_url:
        store = PostgresProjectUsageStore(database_url, master_key)
    else:
        store = MemoryProjectUsageStore(master_key)
    return ProjectUsageManager(store)
