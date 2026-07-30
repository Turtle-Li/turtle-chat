"""Authenticated project API self-service and administrator views."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from open_webui.internal.db import get_async_db_context, get_async_session
from open_webui.models.files import File, Files
from open_webui.models.users import Users
from open_webui.turtle_admin.router import _gateway_internal_url, _gateway_json
from open_webui.turtle_storage.media import (
    get_presigned_model_image_source_for_file,
)
from open_webui.turtle_storage.provider import Storage
from open_webui.turtle_storage.quota import user_lock
from open_webui.turtle_storage.router import (
    CompleteUploadForm,
    PresignUploadForm,
    _discard_upload,
    complete_upload as storage_complete_upload,
    presign_upload as storage_presign_upload,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .media import (
    PROJECT_FILE_PURPOSE,
    PROJECT_IMAGE_CONTENT_TYPES,
    ProjectMediaReferenceError,
    has_image_inputs,
    internal_file_id,
    project_file_object,
    project_file_scope,
    public_file_id,
    rewrite_image_inputs,
)


router = APIRouter()
proxy_router = APIRouter()
_project_proxy_client: httpx.AsyncClient | None = None
_project_proxy_client_lock = asyncio.Lock()
log = logging.getLogger(__name__)


async def _project_gateway_client() -> httpx.AsyncClient:
    """Reuse internal Gateway connections across Project API requests."""
    global _project_proxy_client
    if _project_proxy_client is not None and not _project_proxy_client.is_closed:
        return _project_proxy_client
    async with _project_proxy_client_lock:
        if _project_proxy_client is None or _project_proxy_client.is_closed:
            _project_proxy_client = httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=64,
                    max_keepalive_connections=32,
                    keepalive_expiry=30.0,
                ),
                trust_env=False,
            )
        return _project_proxy_client


@proxy_router.on_event("shutdown")
async def _close_project_gateway_client() -> None:
    global _project_proxy_client
    client = _project_proxy_client
    _project_proxy_client = None
    if client is not None and not client.is_closed:
        await client.aclose()


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


class ProjectFileCreateForm(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(alias="bytes", gt=0)
    purpose: Literal["vision"] = PROJECT_FILE_PURPOSE
    content_type: str = Field(
        min_length=3,
        max_length=160,
        validation_alias=AliasChoices("content_type", "mime_type"),
    )
    file_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        validation_alias=AliasChoices("sha256", "file_hash"),
    )


class ProjectActor(NamedTuple):
    user: Any
    key_id: str
    owner_user_id: str


def _project_error(
    status_code: int,
    message: str,
    *,
    code: str | None = None,
    error_type: str = "invalid_request_error",
    authenticate: bool = False,
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if authenticate else None
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": str(message),
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _project_api_external_prefix(request: Request) -> str:
    """Return the allowlisted public prefix selected by the trusted edge."""
    if request.headers.get("x-turtle-project-external-prefix") == "/v1":
        return "/v1"
    return "/api/project/v1"


def _http_error_response(exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "请求失败")
        code = str(detail.get("code") or "") or None
    else:
        message = str(detail or "请求失败")
        code = None
    return _project_error(
        int(exc.status_code),
        message,
        code=code,
        authenticate=int(exc.status_code) == status.HTTP_401_UNAUTHORIZED,
    )


async def _project_actor(
    request: Request,
    db: AsyncSession,
) -> tuple[ProjectActor | None, JSONResponse | None]:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None, _project_error(
            status.HTTP_401_UNAUTHORIZED,
            "缺少项目 API 密钥",
            code="invalid_api_key",
            error_type="authentication_error",
            authenticate=True,
        )
    target = _gateway_internal_url("/v1/project/context")
    if target is None:
        return None, _project_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "项目 API Gateway 尚未配置",
            code="gateway_unavailable",
            error_type="server_error",
        )
    url, _internal_key = target
    client = await _project_gateway_client()
    try:
        response = await client.get(
            url,
            headers={"Authorization": authorization, "Accept": "application/json"},
            timeout=5.0,
        )
    except httpx.TimeoutException:
        return None, _project_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "项目 API 鉴权超时",
            code="gateway_timeout",
            error_type="server_error",
        )
    except httpx.HTTPError:
        return None, _project_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "项目 API Gateway 暂时不可达",
            code="gateway_unavailable",
            error_type="server_error",
        )
    if response.status_code != status.HTTP_200_OK:
        message = "项目 API 密钥无效"
        try:
            payload = response.json()
            upstream_error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(upstream_error, dict) and upstream_error.get("message"):
                message = str(upstream_error["message"])
        except (TypeError, ValueError):
            pass
        return None, _project_error(
            int(response.status_code),
            message,
            code="invalid_api_key",
            error_type="authentication_error",
            authenticate=int(response.status_code) == status.HTTP_401_UNAUTHORIZED,
        )
    try:
        context = response.json()
    except (TypeError, ValueError):
        return None, _project_error(
            status.HTTP_502_BAD_GATEWAY,
            "项目 API Gateway 返回了无效鉴权结果",
            code="invalid_gateway_response",
            error_type="server_error",
        )
    key_id = str(context.get("project_key_id") or "").strip()
    owner_user_id = str(context.get("owner_user_id") or "").strip()
    if context.get("is_master") or not key_id or not owner_user_id:
        return None, _project_error(
            status.HTTP_403_FORBIDDEN,
            "内部 Gateway 密钥不能访问项目媒体",
            code="project_media_forbidden",
            error_type="permission_error",
        )
    user = await Users.get_user_by_id(owner_user_id, db=db)
    if user is None:
        return None, _project_error(
            status.HTTP_403_FORBIDDEN,
            "项目 API 所属账号不可用",
            code="project_owner_unavailable",
            error_type="permission_error",
        )
    return ProjectActor(user=user, key_id=key_id, owner_user_id=owner_user_id), None


async def _project_gateway_proxy(
    request: Request,
    path: str,
    *,
    body_override: bytes | None = None,
) -> Response:
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
    body = body_override if body_override is not None else await request.body()
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="项目 API 请求体不能超过 8 MiB",
        )
    headers = {
        "Authorization": authorization,
        "Accept": request.headers.get("accept", "application/json"),
    }
    if body_override is not None:
        headers["Content-Type"] = "application/json"
    elif request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    if request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = request.headers["idempotency-key"]
    client = await _project_gateway_client()
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
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="项目 API 上游超时",
        ) from exc
    except httpx.HTTPError as exc:
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

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


def _project_file_filters(actor: ProjectActor):
    return (
        File.user_id == actor.owner_user_id,
        File.meta["data"]["project_api_key_id"].as_string() == actor.key_id,
        File.meta["data"]["project_api_purpose"].as_string() == PROJECT_FILE_PURPOSE,
    )


async def _project_file_for_actor(
    value: str,
    actor: ProjectActor,
    db: AsyncSession,
) -> File | None:
    file_id = internal_file_id(value)
    if file_id is None:
        return None
    result = await db.execute(
        select(File)
        .where(File.id == file_id, *_project_file_filters(actor))
        .execution_options(populate_existing=True)
    )
    file = result.scalar_one_or_none()
    if file is None:
        return None
    key_id, purpose = project_file_scope(file.meta or {})
    if key_id != actor.key_id or purpose != PROJECT_FILE_PURPOSE:
        return None
    return file


def _completed_project_image(file: File) -> bool:
    meta = file.meta or {}
    data = file.data or {}
    content_type = str(meta.get("content_type") or "").split(";", 1)[0].lower()
    return (
        data.get("status") == "completed"
        and content_type in PROJECT_IMAGE_CONTENT_TYPES
    )


def _external_https_url(value: str, request: Request) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.hostname.lower() != str(request.url.hostname or "").lower()
    )


@proxy_router.post("/files")
async def create_project_file(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    content_type = str(request.headers.get("content-type") or "").lower()
    if not content_type.startswith("application/json"):
        return _project_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            (
                "为保证图片正文不经过日本服务器，POST /files 只接受 JSON 元数据；"
                "请使用返回的 upload.url 将图片直接 PUT 到 COS"
            ),
            code="direct_upload_required",
        )
    try:
        declared_length = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared_length = 0
    if declared_length > 64 * 1024:
        return _project_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "文件预留元数据不能超过 64 KiB",
            code="metadata_too_large",
        )
    body = await request.body()
    if len(body) > 64 * 1024:
        return _project_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "文件预留元数据不能超过 64 KiB",
            code="metadata_too_large",
        )
    try:
        form = ProjectFileCreateForm.model_validate_json(body)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg") if exc.errors() else None
        return _project_error(
            status.HTTP_400_BAD_REQUEST,
            str(message or "文件预留参数无效"),
            code="invalid_file_metadata",
        )
    normalized_type = form.content_type.lower().split(";", 1)[0].strip()
    if normalized_type not in PROJECT_IMAGE_CONTENT_TYPES:
        return _project_error(
            status.HTTP_400_BAD_REQUEST,
            "Project API 当前只支持 PNG、JPEG、WEBP 和非动画 GIF 图片",
            code="unsupported_image_type",
        )
    filename = Path(form.filename).name
    if not filename:
        return _project_error(
            status.HTTP_400_BAD_REQUEST,
            "filename 必须包含有效文件名",
            code="invalid_file_metadata",
        )
    try:
        ticket = await storage_presign_upload(
            PresignUploadForm(
                filename=filename,
                content_type=normalized_type,
                size=form.size,
                file_hash=form.file_hash,
                metadata={
                    "project_api_key_id": actor.key_id,
                    "project_api_purpose": form.purpose,
                },
                process=False,
            ),
            user=actor.user,
            db=db,
        )
    except HTTPException as exc:
        return _http_error_response(exc)
    file = await _project_file_for_actor(
        public_file_id(ticket["file_id"]),
        actor,
        db,
    )
    if file is None:
        return _project_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "文件预留记录创建失败",
            code="file_reservation_failed",
            error_type="server_error",
        )
    if not _external_https_url(str(ticket.get("upload_url") or ""), request):
        await _discard_upload(file, db)
        return _project_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "COS 直传地址配置无效",
            code="direct_upload_unavailable",
            error_type="server_error",
        )
    payload = project_file_object(file)
    payload.update(
        {
            "upload": {
                "url": ticket["upload_url"],
                "method": ticket["method"],
                "headers": ticket["headers"],
                "expires_in": ticket["expires_in"],
            },
            "complete_url": (
                f"{_project_api_external_prefix(request)}/files/"
                f"{public_file_id(file.id)}/complete"
            ),
        }
    )
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@proxy_router.post("/files/{file_id}/complete")
async def complete_project_file(
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    file = await _project_file_for_actor(file_id, actor, db)
    if file is None:
        return _project_error(
            status.HTTP_404_NOT_FOUND,
            "文件不存在",
            code="file_not_found",
        )
    try:
        await storage_complete_upload(
            CompleteUploadForm(file_id=file.id),
            user=actor.user,
            db=db,
        )
    except HTTPException as exc:
        return _http_error_response(exc)
    completed = await _project_file_for_actor(file_id, actor, db)
    if completed is None or not _completed_project_image(completed):
        return _project_error(
            status.HTTP_409_CONFLICT,
            "COS 文件尚未完成校验",
            code="file_not_ready",
        )
    return JSONResponse(
        project_file_object(completed),
        headers={"Cache-Control": "no-store"},
    )


@proxy_router.get("/files")
async def list_project_files(
    request: Request,
    purpose: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=1000),
    order: Literal["asc", "desc"] = Query(default="desc"),
    after: str | None = Query(default=None, min_length=1, max_length=80),
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    if purpose is not None and purpose != PROJECT_FILE_PURPOSE:
        return {
            "object": "list",
            "data": [],
            "first_id": None,
            "last_id": None,
            "has_more": False,
        }
    filters = list(_project_file_filters(actor))
    if after:
        cursor = await _project_file_for_actor(after, actor, db)
        if cursor is None:
            return _project_error(
                status.HTTP_400_BAD_REQUEST,
                "after 必须是当前项目中的有效 file_id",
                code="invalid_cursor",
            )
        if order == "desc":
            filters.append(
                or_(
                    File.created_at < cursor.created_at,
                    and_(File.created_at == cursor.created_at, File.id < cursor.id),
                )
            )
        else:
            filters.append(
                or_(
                    File.created_at > cursor.created_at,
                    and_(File.created_at == cursor.created_at, File.id > cursor.id),
                )
            )
    ordering = (
        (File.created_at.desc(), File.id.desc())
        if order == "desc"
        else (File.created_at.asc(), File.id.asc())
    )
    result = await db.execute(
        select(File).where(*filters).order_by(*ordering).limit(limit + 1)
    )
    files = list(result.scalars().all())
    has_more = len(files) > limit
    files = files[:limit]
    data = [project_file_object(file) for file in files]
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": has_more,
    }


@proxy_router.get("/files/{file_id}")
async def retrieve_project_file(
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    file = await _project_file_for_actor(file_id, actor, db)
    if file is None:
        return _project_error(
            status.HTTP_404_NOT_FOUND,
            "文件不存在",
            code="file_not_found",
        )
    return project_file_object(file)


@proxy_router.get("/files/{file_id}/content")
@proxy_router.head("/files/{file_id}/content")
async def retrieve_project_file_content(
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    file = await _project_file_for_actor(file_id, actor, db)
    if file is None:
        return _project_error(
            status.HTTP_404_NOT_FOUND,
            "文件不存在",
            code="file_not_found",
        )
    if not _completed_project_image(file):
        return _project_error(
            status.HTTP_409_CONFLICT,
            "文件尚未完成上传校验",
            code="file_not_ready",
        )
    meta = file.meta or {}
    try:
        direct_url = Storage.presign_download(
            file.path,
            filename=str(meta.get("name") or file.filename),
            attachment=False,
            use_cdn=True,
        )
    except Exception:
        log.warning("Project API CDN/COS URL signing failed")
        direct_url = None
    if not direct_url or not _external_https_url(direct_url, request):
        return _project_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CDN/COS 文件获取暂时不可用",
            code="media_route_unavailable",
            error_type="server_error",
        )
    return RedirectResponse(
        direct_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization",
        },
    )


@proxy_router.delete("/files/{file_id}")
async def delete_project_file(
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    actor, actor_error = await _project_actor(request, db)
    if actor_error is not None:
        return actor_error
    assert actor is not None
    async with user_lock(actor.owner_user_id):
        file = await _project_file_for_actor(file_id, actor, db)
        if file is None:
            return _project_error(
                status.HTTP_404_NOT_FOUND,
                "文件不存在",
                code="file_not_found",
            )
        try:
            await asyncio.to_thread(Storage.delete_file, file.path)
        except Exception:
            log.warning("Project API COS object deletion failed")
            return _project_error(
                status.HTTP_502_BAD_GATEWAY,
                "COS 文件删除失败，请稍后重试",
                code="file_delete_failed",
                error_type="server_error",
            )
        await Files.delete_file_by_id(file.id, db=db)
    return {
        "id": public_file_id(file.id),
        "object": "file",
        "deleted": True,
    }


@proxy_router.get("/models")
async def proxy_project_models(request: Request):
    return await _project_gateway_proxy(request, "/v1/models")


@proxy_router.post("/chat/completions")
async def proxy_project_chat_completions(request: Request):
    try:
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            return _project_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "项目 API 请求体不能超过 8 MiB",
                code="request_too_large",
            )
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return await _project_gateway_proxy(request, "/v1/chat/completions")
    if not isinstance(payload, dict) or not has_image_inputs(payload):
        return await _project_gateway_proxy(request, "/v1/chat/completions")

    async with get_async_db_context() as db:
        actor, actor_error = await _project_actor(request, db)
        if actor_error is not None:
            return actor_error
        assert actor is not None

        async def resolve(file_id: str) -> dict[str, str]:
            file = await _project_file_for_actor(public_file_id(file_id), actor, db)
            if file is None or not _completed_project_image(file):
                raise ProjectMediaReferenceError(
                    "file_id 不存在、尚未完成，或不属于当前项目"
                )
            source = get_presigned_model_image_source_for_file(
                file,
                actor.user,
            )
            if not source:
                raise RuntimeError("managed model source unavailable")
            return source

        try:
            rewritten, _image_count = await rewrite_image_inputs(payload, resolve)
        except ProjectMediaReferenceError as exc:
            return _project_error(
                status.HTTP_400_BAD_REQUEST,
                str(exc),
                code="invalid_image_reference",
            )
        except Exception:
            log.warning("Project API managed image source creation failed")
            return _project_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "托管图片来源暂时不可用",
                code="managed_media_unavailable",
                error_type="server_error",
            )
    encoded = json.dumps(
        rewritten,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return await _project_gateway_proxy(
        request,
        "/v1/chat/completions",
        body_override=encoded,
    )


@proxy_router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def unsupported_project_api_endpoint(path: str):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": {
                "message": f"unsupported Project API endpoint: /{path}",
                "type": "invalid_request_error",
                "code": "unsupported_endpoint",
            }
        },
    )


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
