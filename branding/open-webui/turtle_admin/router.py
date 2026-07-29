"""Read-mostly operational APIs for the standalone administrator console.

The console intentionally exposes only sanitized state. Provider credentials,
browser sessions, cookies, and internal request content never leave their
deployment-managed stores.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from open_webui.internal.db import get_async_session
from open_webui.models.users import Users
from open_webui.turtle_chat.account_pool import ACCOUNT_POOL_ADMISSION
from open_webui.turtle_chat.concurrency import (
    CHAT_CONCURRENCY,
    ChatCoordinatorUnavailable,
)
from open_webui.turtle_chat.store import CHAT_STORE, ChatPolicyError
from open_webui.turtle_chat.subscription_cache import SUBSCRIPTION_CACHE
from open_webui.turtle_storage.core import CONFIG_STORE
from open_webui.turtle_storage.pump import MEDIA_PUMP, strict_media_mode
from open_webui.turtle_storage.provider import Storage
from open_webui.turtle_storage.quota import usage_bytes
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .monitoring import SYSTEM_MONITOR


router = APIRouter()


class UserRoleForm(BaseModel):
    role: Literal["user", "admin"]


class AccountPoolAdminForm(BaseModel):
    provider: Literal["gpt", "claude"] = "gpt"
    name: str
    description: str = ""
    enabled: bool = True


class AccountAdminForm(BaseModel):
    name: str
    worker_endpoint: str
    health_path: str | None = "/api/OpenaiAccount/quota"
    max_concurrency: int = 1
    priority: int = 0
    quota_profile: str = Field(default="untracked", min_length=1, max_length=24)
    enabled: bool = False


class AccountOnboardAdminForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class AccountSettingsAdminForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = False
    quota_profile: str | None = Field(default=None, min_length=1, max_length=24)
    max_concurrency: int | None = Field(default=None, ge=1, le=20)


class ProviderDisplayForm(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)


class UpstreamCleanupForm(BaseModel):
    retention_seconds: int = Field(ge=300, le=365 * 24 * 60 * 60)
    conversation_action: Literal["archive", "delete"] = "delete"


_PROVIDER_CACHE: list[dict[str, Any]] = []
_PROVIDER_CACHE_AT = 0.0
_PROVIDER_CACHE_SECONDS = 15.0
_PROVIDER_LOCK = asyncio.Lock()
_PROVIDER_TASK: asyncio.Task[list[dict[str, Any]]] | None = None


def _provider_health_snapshot() -> list[dict[str, Any]]:
    """Return the last sanitized probe result without waiting on an upstream."""

    return [dict(item) for item in _PROVIDER_CACHE]


def _provider_cache_stale() -> bool:
    return _PROVIDER_CACHE_AT <= 0 or time.monotonic() - _PROVIDER_CACHE_AT >= _PROVIDER_CACHE_SECONDS


def _consume_provider_task(task: asyncio.Task[list[dict[str, Any]]]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        # The next status request can retry. Provider details stay sanitized and
        # the rest of the management console remains independent of this task.
        pass


def _schedule_provider_probe(*, force: bool) -> asyncio.Task[list[dict[str, Any]]] | None:
    global _PROVIDER_TASK
    if _PROVIDER_TASK is not None and not _PROVIDER_TASK.done():
        return _PROVIDER_TASK
    if not force and not _provider_cache_stale():
        return None
    _PROVIDER_TASK = asyncio.create_task(_provider_health(force=True))
    _PROVIDER_TASK.add_done_callback(_consume_provider_task)
    return _PROVIDER_TASK


def _provider_identity(index: int, public_model: str) -> tuple[str, str, str]:
    normalized = str(public_model or "").strip().lower()
    if "claude" in normalized:
        return "claude", "Claude", "Claude Web"
    if normalized.startswith("gpt") or index == 0:
        return "gpt", "ChatGPT", "ChatGPT Web"
    return "claude", "Claude", "Claude Web"


def _health_url(base_url: str) -> str | None:
    try:
        parsed = urlsplit(str(base_url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return parsed._replace(path=f"{path}/healthz", query="", fragment="").geturl()


def _gateway_internal_url(path: str) -> tuple[str, str] | None:
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
    safe_path = "/" + str(path or "").lstrip("/")
    return parsed._replace(path=f"{base_path}{safe_path}", query="", fragment="").geturl(), keys[0]


async def _gateway_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    target = _gateway_internal_url(path)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway 管理连接尚未配置",
        )
    url, key = target
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                max(0.5, float(timeout_seconds)),
                connect=min(3.0, max(0.5, float(timeout_seconds))),
            ),
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Gateway 账号池操作超时",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway 账号池暂时不可达",
        ) from exc
    try:
        value = response.json()
    except ValueError:
        value = {}
    if not response.is_success:
        message = (
            value.get("error", {}).get("message")
            if isinstance(value, dict) and isinstance(value.get("error"), dict)
            else None
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=str(message or "Gateway 账号池操作失败"),
        )
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gateway 返回了无效的账号池数据",
        )
    return value


async def _probe_provider(
    index: int,
    base_url: str,
    forced_provider: Literal["gpt", "claude"] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    checked_at = int(time.time())
    url = _health_url(base_url)
    if not url:
        key, label, kind = _provider_identity(index, "")
        return {
            "key": key,
            "label": label,
            "kind": kind,
            "state": "misconfigured",
            "ok": False,
            "message": "连接地址无效",
            "checked_at": checked_at,
            "latency_ms": 0,
        }

    payload: dict[str, Any] = {}
    http_status: int | None = None
    try:
        timeout = httpx.Timeout(15.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url)
            http_status = response.status_code
            if response.headers.get("content-type", "").split(";", 1)[0] == "application/json":
                value = response.json()
                if isinstance(value, dict):
                    payload = value
    except httpx.TimeoutException:
        payload = {"status": "probe_timeout"}
    except (httpx.HTTPError, ValueError):
        payload = {}

    nested = payload.get("providers") if isinstance(payload, dict) else None
    if isinstance(nested, dict):
        provider_key = forced_provider or ("gpt" if index == 0 else "claude")
        provider_payload = nested.get(provider_key)
        if isinstance(provider_payload, dict):
            payload = {**payload, **provider_payload}

    public_model = str(payload.get("public_model") or "").strip()
    key, label, kind = _provider_identity(index, public_model)
    if forced_provider is not None:
        key, label, kind = (
            ("gpt", "ChatGPT", "ChatGPT Web")
            if forced_provider == "gpt"
            else ("claude", "Claude", "Claude Web")
        )
    reported_status = str(payload.get("status") or "").strip().lower()
    ok = bool(payload.get("ok")) and http_status is not None and 200 <= http_status < 300
    if ok:
        state = "ready"
        message = "连接正常"
    elif reported_status in {"login_required", "reauthentication_required"}:
        state = "auth_required"
        message = "需要登录或重新认证"
    elif reported_status == "model_verification_required":
        state = "verification_required"
        message = "等待真实模型验证"
    elif reported_status == "probe_timeout":
        state = "degraded"
        message = "健康检查超时"
    elif http_status is None:
        state = "offline"
        message = "服务暂时不可达"
    else:
        state = "degraded"
        message = "上游状态异常"

    result: dict[str, Any] = {
        "key": key,
        "label": label,
        "kind": kind,
        "state": state,
        "ok": ok,
        "message": message,
        "public_model": public_model or None,
        "checked_at": checked_at,
        "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }
    if isinstance(payload.get("verified_route_count"), int):
        result["verified_route_count"] = payload["verified_route_count"]
    if isinstance(payload.get("upstream_reachable"), bool):
        result["upstream_reachable"] = payload["upstream_reachable"]
    if isinstance(payload.get("text_only"), bool):
        result["text_only"] = payload["text_only"]
    return result


async def _provider_health(force: bool = False) -> list[dict[str, Any]]:
    global _PROVIDER_CACHE, _PROVIDER_CACHE_AT
    now = time.monotonic()
    if not force and _PROVIDER_CACHE and now - _PROVIDER_CACHE_AT < _PROVIDER_CACHE_SECONDS:
        return [dict(item) for item in _PROVIDER_CACHE]
    async with _PROVIDER_LOCK:
        now = time.monotonic()
        if not force and _PROVIDER_CACHE and now - _PROVIDER_CACHE_AT < _PROVIDER_CACHE_SECONDS:
            return [dict(item) for item in _PROVIDER_CACHE]
        base_urls = [
            value.strip()
            for value in os.getenv("OPENAI_API_BASE_URLS", "").split(";")
            if value.strip()
        ][:2]
        if not base_urls:
            _PROVIDER_CACHE = []
            _PROVIDER_CACHE_AT = now
            return []
        if len(base_urls) == 1:
            result = list(
                await asyncio.gather(
                    _probe_provider(0, base_urls[0], "gpt"),
                    _probe_provider(1, base_urls[0], "claude"),
                )
            )
        else:
            result = list(
                await asyncio.gather(
                    *(
                        _probe_provider(index, base_url)
                        for index, base_url in enumerate(base_urls)
                    )
                )
            )
        _PROVIDER_CACHE = [dict(item) for item in result]
        _PROVIDER_CACHE_AT = time.monotonic()
        return result


@router.get("/overview")
async def get_overview(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    result = await Users.get_users(skip=0, limit=None, db=db)
    users = list(result.get("users", []))
    role_counts = {"admin": 0, "user": 0, "pending": 0}
    total_storage_bytes = 0
    total_storage_quota_bytes = 0
    for target in users:
        role = str(target.role or "pending")
        role_counts[role] = role_counts.get(role, 0) + 1
        total_storage_bytes += int(await usage_bytes(target.id, db))
        assignment = await asyncio.to_thread(
            CHAT_STORE.storage_quota_for_user, target.id, target.role
        )
        total_storage_quota_bytes += int(assignment.get("quota_bytes") or 0)

    storage = CONFIG_STORE.public(admin=True)
    return {
        "generated_at": int(time.time()),
        "viewer": {"id": user.id, "name": user.name, "role": user.role},
        "users": {
            "total": len(users),
            "active_today": int(await Users.get_num_users_active_today(db=db) or 0),
            "roles": role_counts,
        },
        "chat": await asyncio.to_thread(CHAT_STORE.admin_summary),
        "storage": {
            "provider": storage["provider"],
            "cos_configured": bool(storage["cos"]["configured"]),
            "direct_upload": bool(Storage.direct_upload_available()),
            "strict_external_media": bool(strict_media_mode()),
            "media_pump_configured": bool(MEDIA_PUMP.configured()),
            "used_bytes": total_storage_bytes,
            "assigned_quota_bytes": total_storage_quota_bytes,
        },
        # Provider probes can legitimately take up to 15 seconds. The overview
        # must remain useful while an upstream is slow or unavailable, so it
        # only returns the most recent sanitized snapshot. The browser refreshes
        # Provider state independently after rendering the core dashboard.
        "providers": _provider_health_snapshot(),
        "configuration": {
            "provider_secrets": "deployment",
            "database": "postgresql" if os.getenv("DATABASE_HOST") else "embedded",
            "redis_configured": bool(os.getenv("REDIS_URL") or os.getenv("REDIS_HOST")),
            "subscription_cache": SUBSCRIPTION_CACHE.snapshot(),
        },
    }


@router.get("/providers")
async def get_providers(
    force: bool = Query(False),
    user=Depends(get_admin_user),
):
    probe_task = _schedule_provider_probe(force=force)
    if force and probe_task is not None:
        providers = await probe_task
    else:
        providers = _provider_health_snapshot()
    return {
        "items": providers if isinstance(providers, list) else [],
        "display": await asyncio.to_thread(CHAT_STORE.provider_display_settings),
        "probing": bool(probe_task is not None and not probe_task.done()),
        "secrets_managed_by": "deployment",
        "note": "连接凭据由部署 Secret 管理；控制台不会读取或回显密钥。",
    }


@router.put("/providers/{provider_family}/display")
async def update_provider_display(
    provider_family: Literal["gpt", "claude"],
    form_data: ProviderDisplayForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.set_provider_display_name,
            provider_family,
            form_data.display_name,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/upstream-cleanup")
async def get_upstream_cleanup(user=Depends(get_admin_user)):
    return await _gateway_json(
        "GET",
        "/internal/upstream-cleanup/status",
        timeout_seconds=5.0,
    )


@router.put("/upstream-cleanup")
async def update_upstream_cleanup(
    form_data: UpstreamCleanupForm,
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "PUT",
        "/internal/upstream-cleanup/config",
        form_data.model_dump(),
        timeout_seconds=10.0,
    )


@router.post("/account-pools")
async def create_account_pool(
    form_data: AccountPoolAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        "/internal/account-pools",
        form_data.model_dump(),
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.get("/account-pools")
async def get_account_pools(user=Depends(get_admin_user)):
    return await _gateway_json("GET", "/internal/account-pools")


@router.put("/account-pools/{pool_id}")
async def update_account_pool(
    pool_id: str,
    form_data: AccountPoolAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "PUT",
        f"/internal/account-pools/{pool_id}",
        form_data.model_dump(),
    )
    ACCOUNT_POOL_ADMISSION.invalidate(pool_id)
    return result


@router.delete("/account-pools/{pool_id}")
async def delete_account_pool(
    pool_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "DELETE",
        f"/internal/account-pools/{pool_id}",
    )
    ACCOUNT_POOL_ADMISSION.invalidate(pool_id)
    return result


@router.post("/account-pools/{pool_id}/accounts")
async def create_provider_account(
    pool_id: str,
    form_data: AccountAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/account-pools/{pool_id}/accounts",
        form_data.model_dump(),
    )
    ACCOUNT_POOL_ADMISSION.invalidate(pool_id)
    return result


@router.post("/account-pools/{pool_id}/accounts/onboard")
async def onboard_provider_account(
    pool_id: str,
    form_data: AccountOnboardAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/account-pools/{pool_id}/accounts/onboard",
        form_data.model_dump(),
        timeout_seconds=50.0,
    )
    ACCOUNT_POOL_ADMISSION.invalidate(pool_id)
    return result


@router.put("/accounts/{account_id}")
async def update_provider_account(
    account_id: str,
    form_data: AccountAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "PUT",
        f"/internal/accounts/{account_id}",
        form_data.model_dump(),
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.put("/accounts/{account_id}/settings")
async def update_provider_account_settings(
    account_id: str,
    form_data: AccountSettingsAdminForm,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "PUT",
        f"/internal/accounts/{account_id}/settings",
        form_data.model_dump(),
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/accounts/{account_id}/runtime/prepare")
async def prepare_provider_account_runtime(
    account_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/accounts/{account_id}/runtime/prepare",
        timeout_seconds=50.0,
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/accounts/{account_id}/probe")
async def probe_provider_account(
    account_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json("POST", f"/internal/accounts/{account_id}/probe")
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/accounts/{account_id}/reauth/start")
async def start_provider_account_reauth(
    account_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/accounts/{account_id}/reauth/start",
        timeout_seconds=55.0,
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/accounts/{account_id}/reauth/cancel")
async def cancel_provider_account_reauth(
    account_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/accounts/{account_id}/reauth/cancel",
        timeout_seconds=20.0,
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/accounts/{account_id}/reauth/verify")
async def verify_provider_account_reauth(
    account_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json(
        "POST",
        f"/internal/accounts/{account_id}/reauth/verify",
        timeout_seconds=950.0,
    )
    ACCOUNT_POOL_ADMISSION.invalidate()
    return result


@router.post("/account-pools/{pool_id}/probe")
async def probe_provider_account_pool(
    pool_id: str,
    user=Depends(get_admin_user),
):
    result = await _gateway_json("POST", f"/internal/account-pools/{pool_id}/probe")
    ACCOUNT_POOL_ADMISSION.invalidate(pool_id)
    return result


@router.get("/operations")
async def get_operations(
    hours: int = Query(1),
    user=Depends(get_admin_user),
):
    if int(hours) not in {1, 6, 24}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="监控时间范围只支持 1、6 或 24 小时",
        )
    groups = await asyncio.to_thread(CHAT_STORE.list_groups)
    account_pool_task = asyncio.create_task(
        _gateway_json(
            "GET",
            "/internal/account-pools",
            timeout_seconds=3.0,
        )
    )
    request_metrics, resources = await asyncio.gather(
        asyncio.to_thread(CHAT_STORE.operations_summary, int(hours)),
        asyncio.to_thread(SYSTEM_MONITOR.snapshot, int(hours)),
    )
    try:
        account_pool_snapshot = await account_pool_task
    except Exception:
        account_pool_snapshot = {"pools": []}
    account_pools = [
        {
            "id": str(pool.get("id") or ""),
            "name": str(pool.get("name") or pool.get("id") or "未知账号组"),
            "limit": max(0, int(pool.get("admission_capacity") or 0)),
        }
        for pool in account_pool_snapshot.get("pools", [])
        if str(pool.get("id") or "")
    ]
    try:
        concurrency = await CHAT_CONCURRENCY.snapshot(
            [str(group["id"]) for group in groups],
            account_pools,
        )
    except ChatCoordinatorUnavailable as exc:
        concurrency = {
            "backend": "redis",
            "state": "unavailable",
            "message": str(exc),
            "global": {"active": 0, "queued": 0, "limit": None},
            "providers": [],
            "groups": [],
            "account_pools": [],
        }
    group_names = {str(group["id"]): str(group["name"]) for group in groups}
    for item in concurrency.get("groups", []):
        item["group_name"] = group_names.get(str(item.get("group_id")), "未知分组")
        group = next(
            (value for value in groups if str(value["id"]) == str(item.get("group_id"))),
            None,
        )
        item["limit"] = int(group["max_concurrency"]) if group else None
    for item in request_metrics.get("groups", []):
        item["group_name"] = group_names.get(str(item.get("group_id")), "旧版/未知")
    return {
        "generated_at": int(time.time()),
        "hours": int(hours),
        "requests": request_metrics,
        "concurrency": concurrency,
        "resources": resources,
    }


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    form_data: UserRoleForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await Users.get_user_by_id(user_id, db=db)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if target.id == user.id and form_data.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能在当前会话中移除自己的管理员权限",
        )
    if target.role == "pending":
        if form_data.role != "user":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="待审批账号需先在订阅管理中激活为普通用户，再调整管理员角色",
            )
        subscription = await SUBSCRIPTION_CACHE.get(
            target.id,
            target.role,
            create_default=False,
        )
        if (
            not subscription.get("configured")
            or subscription.get("state") != "active"
            or int(subscription.get("expires_at") or 0) <= int(time.time())
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="请先在订阅管理中配置分组、并发和有效期，再激活用户",
            )
    if target.role == "admin" and form_data.role != "admin":
        admins = await Users.get_users(
            filter={"roles": ["admin"]}, skip=0, limit=None, db=db
        )
        if int(admins.get("total") or len(admins.get("users", []))) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="必须至少保留一位管理员",
            )
    updated = await Users.update_user_role_by_id(target.id, form_data.role, db=db)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="用户角色更新失败")
    await SUBSCRIPTION_CACHE.invalidate(target.id)
    return {
        "id": updated.id,
        "name": updated.name,
        "email": updated.email,
        "role": updated.role,
        "updated": True,
    }
