"""Authenticated project API self-service and administrator views."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from open_webui.internal.db import get_async_session
from open_webui.models.users import Users
from open_webui.turtle_admin.router import _gateway_internal_url, _gateway_json
from open_webui.utils.auth import get_admin_user, get_verified_user
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter()
proxy_router = APIRouter()


class ProjectKeyForm(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectPermissionForm(BaseModel):
    enabled: bool
    max_keys: int | None = Field(default=None, ge=1, le=100)


class ProjectPricingConfigForm(BaseModel):
    cost_multiplier: float = Field(ge=0, le=100)


class ProjectCreditGrantForm(BaseModel):
    amount_microusd: int = Field(ge=1, le=1_000_000_000_000)
    reason: str = Field(min_length=2, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)


async def _project_gateway_proxy(request: Request, path: str) -> Response:
    target = _gateway_internal_url(path)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目 API Gateway 尚未配置",
        )
    url, _internal_key = target
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少项目 API 密钥",
            headers={"WWW-Authenticate": "Bearer"},
        )
    body = await request.body()
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="项目 API 请求体不能超过 8 MiB",
        )
    headers = {
        "Authorization": authorization,
        "Accept": request.headers.get("accept", "application/json"),
    }
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    if request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = request.headers["idempotency-key"]
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=5.0),
        trust_env=False,
    )
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                url,
                headers=headers,
                content=body,
            ),
            stream=True,
        )
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="项目 API 上游超时",
        ) from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目 API Gateway 暂时不可达",
        ) from exc

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        in {
            "content-type",
            "cache-control",
            "x-accel-buffering",
            "www-authenticate",
        }
    }

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


@proxy_router.get("/models")
async def proxy_project_models(request: Request):
    return await _project_gateway_proxy(request, "/v1/models")


@proxy_router.post("/chat/completions")
async def proxy_project_chat_completions(request: Request):
    return await _project_gateway_proxy(request, "/v1/chat/completions")


def _query(path: str, **values) -> str:
    params = {
        key: value
        for key, value in values.items()
        if value is not None and value != ""
    }
    return f"{path}?{urlencode(params)}" if params else path


async def _permission(user_id: str) -> dict:
    return await _gateway_json(
        "GET",
        f"/internal/project-api/permissions/{user_id}",
    )


async def _require_project_access(user_id: str) -> dict:
    permission = await _permission(user_id)
    if not permission.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员尚未为该账号开通项目 API 权限",
        )
    return permission


@router.get("/me")
async def get_my_project_api(
    hours: int = Query(24),
    user=Depends(get_verified_user),
):
    permission = await _permission(user.id)
    if not permission.get("enabled"):
        return {
            "enabled": False,
            "viewer": {"id": user.id, "name": user.name, "role": user.role},
            "keys": [],
            "usage": None,
        }
    keys, usage = await asyncio.gather(
        _gateway_json(
            "GET",
            _query(
                "/internal/project-api/keys",
                owner_user_id=user.id,
            ),
        ),
        _gateway_json(
            "GET",
            _query(
                "/internal/project-api/usage",
                hours=hours,
                owner_user_id=user.id,
            ),
        ),
    )
    return {
        "enabled": True,
        "viewer": {"id": user.id, "name": user.name, "role": user.role},
        "max_keys": int(permission.get("max_keys") or 5),
        "balance_microusd": permission.get("balance_microusd"),
        "reserved_microusd": int(permission.get("reserved_microusd") or 0),
        "keys": keys.get("items", []),
        "usage": usage,
    }


@router.post("/keys")
async def create_my_project_key(
    form_data: ProjectKeyForm,
    user=Depends(get_verified_user),
):
    await _require_project_access(user.id)
    return await _gateway_json(
        "POST",
        "/internal/project-api/keys",
        {"owner_user_id": user.id, "name": form_data.name},
    )


@router.delete("/keys/{key_id}")
async def revoke_my_project_key(
    key_id: str,
    user=Depends(get_verified_user),
):
    await _require_project_access(user.id)
    return await _gateway_json(
        "DELETE",
        _query(
            f"/internal/project-api/keys/{key_id}",
            owner_user_id=user.id,
        ),
    )


@router.get("/usage")
async def get_my_project_usage(
    hours: int = Query(24),
    key_id: str | None = Query(None),
    model: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_verified_user),
):
    await _require_project_access(user.id)
    return await _gateway_json(
        "GET",
        _query(
            "/internal/project-api/usage",
            hours=hours,
            owner_user_id=user.id,
            key_id=key_id,
            model=model,
            outcome=outcome,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/admin/users")
async def get_project_api_users(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    result, permission_payload, key_payload = await asyncio.gather(
        Users.get_users(skip=0, limit=None, db=db),
        _gateway_json("GET", "/internal/project-api/permissions"),
        _gateway_json("GET", "/internal/project-api/keys"),
    )
    permissions = {
        str(item.get("user_id")): item
        for item in permission_payload.get("items", [])
    }
    keys_by_owner: dict[str, list[dict]] = {}
    for item in key_payload.get("items", []):
        keys_by_owner.setdefault(str(item.get("owner_user_id")), []).append(item)
    items = []
    for target in result.get("users", []):
        permission = permissions.get(str(target.id))
        if permission is None:
            continue
        keys = keys_by_owner.get(str(target.id), [])
        items.append(
            {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "role": target.role,
                "enabled": bool(permission.get("enabled")),
                "max_keys": int(permission.get("max_keys") or 5),
                "balance_microusd": permission.get("balance_microusd"),
                "reserved_microusd": int(permission.get("reserved_microusd") or 0),
                "active_keys": sum(item.get("status") == "active" for item in keys),
                "request_count": sum(int(item.get("request_count") or 0) for item in keys),
                "total_tokens": sum(int(item.get("total_tokens") or 0) for item in keys),
                "official_cost_microusd": sum(
                    int(item.get("total_official_cost_microusd") or 0)
                    for item in keys
                ),
                "actual_cost_microusd": sum(
                    int(item.get("total_actual_cost_microusd") or 0)
                    for item in keys
                ),
            }
        )
    return {
        "items": items,
        "directory": [
            {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "role": target.role,
            }
            for target in result.get("users", [])
        ],
    }


@router.get("/admin/users/search")
async def search_project_api_users(
    q: str = Query("", max_length=100),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    result, permission_payload = await asyncio.gather(
        Users.get_users(skip=0, limit=None, db=db),
        _gateway_json("GET", "/internal/project-api/permissions"),
    )
    configured_ids = {
        str(item.get("user_id"))
        for item in permission_payload.get("items", [])
    }
    needle = q.strip().casefold()
    items = []
    for target in result.get("users", []):
        if str(target.id) in configured_ids or target.role == "pending":
            continue
        haystack = f"{target.name or ''} {target.email or ''}".casefold()
        if needle and needle not in haystack:
            continue
        items.append(
            {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "role": target.role,
            }
        )
        if len(items) >= 20:
            break
    return {"items": items}


@router.put("/admin/users/{user_id}/permission")
async def update_project_api_permission(
    user_id: str,
    form_data: ProjectPermissionForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await Users.get_user_by_id(user_id, db=db)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    result = await _gateway_json(
        "PUT",
        f"/internal/project-api/permissions/{user_id}",
        {
            "enabled": form_data.enabled,
            "updated_by": user.id,
            "max_keys": form_data.max_keys,
        },
    )
    return {
        **result,
        "name": target.name,
        "email": target.email,
    }


@router.delete("/admin/users/{user_id}/permission")
async def delete_project_api_permission(
    user_id: str,
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "DELETE",
        f"/internal/project-api/permissions/{user_id}",
    )


@router.post("/admin/users/{user_id}/credits")
async def grant_project_api_credit(
    user_id: str,
    form_data: ProjectCreditGrantForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await Users.get_user_by_id(user_id, db=db)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    return await _gateway_json(
        "POST",
        f"/internal/project-api/permissions/{user_id}/credits",
        {
            "amount_microusd": form_data.amount_microusd,
            "reason": form_data.reason,
            "idempotency_key": form_data.idempotency_key,
            "updated_by": user.id,
        },
    )


@router.get("/admin/users/{user_id}/credits")
async def get_project_api_credit_ledger(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "GET",
        _query(
            f"/internal/project-api/permissions/{user_id}/credits",
            limit=limit,
        ),
    )


@router.get("/admin/keys")
async def get_all_project_keys(
    owner_user_id: str | None = Query(None),
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "GET",
        _query(
            "/internal/project-api/keys",
            owner_user_id=owner_user_id,
        ),
    )


@router.get("/admin/config")
async def get_project_pricing_config(
    user=Depends(get_admin_user),
):
    return await _gateway_json("GET", "/internal/project-api/config")


@router.put("/admin/config")
async def update_project_pricing_config(
    form_data: ProjectPricingConfigForm,
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "PUT",
        "/internal/project-api/config",
        {
            "cost_multiplier": form_data.cost_multiplier,
            "updated_by": user.id,
        },
    )


@router.delete("/admin/keys/{key_id}")
async def revoke_any_project_key(
    key_id: str,
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "DELETE",
        f"/internal/project-api/keys/{key_id}",
    )


@router.get("/admin/usage")
async def get_all_project_usage(
    hours: int = Query(24),
    owner_user_id: str | None = Query(None),
    key_id: str | None = Query(None),
    model: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_admin_user),
):
    return await _gateway_json(
        "GET",
        _query(
            "/internal/project-api/usage",
            hours=hours,
            owner_user_id=owner_user_id,
            key_id=key_id,
            model=model,
            outcome=outcome,
            limit=limit,
            offset=offset,
        ),
    )
