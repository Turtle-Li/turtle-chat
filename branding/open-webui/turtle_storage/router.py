"""Authenticated Turtle storage, direct-upload, quota, and admin APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from open_webui.internal.db import get_async_session
from open_webui.models.files import File, FileForm, FileModelResponse, Files
from open_webui.models.users import Users
from open_webui.utils.auth import get_admin_user, get_verified_user
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..turtle_chat.store import CHAT_STORE
from .core import CONFIG_STORE, StorageConfigurationError, safe_filename
from .provider import Storage
from .pump import MEDIA_PUMP, strict_media_mode
from .quota import (
    QuotaExceededError,
    ensure_upload_capacity,
    quota_http_exception,
    quota_summary,
    usage_bytes,
    user_lock,
)


log = logging.getLogger(__name__)
router = APIRouter()
THUMBNAIL_CONTENT_TYPE = "image/webp"
THUMBNAIL_MAX_BYTES = 2 * 1024**2
THUMBNAIL_MAX_DIMENSION = 480


class ThumbnailUploadForm(BaseModel):
    content_type: Literal["image/webp"] = THUMBNAIL_CONTENT_TYPE
    size: int = Field(gt=0, le=THUMBNAIL_MAX_BYTES)
    width: int = Field(gt=0, le=THUMBNAIL_MAX_DIMENSION)
    height: int = Field(gt=0, le=THUMBNAIL_MAX_DIMENSION)


class PresignUploadForm(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=160)
    size: int = Field(gt=0)
    file_hash: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    metadata: dict[str, Any] | None = None
    process: bool = False
    thumbnail: ThumbnailUploadForm | None = None


class CompleteUploadForm(BaseModel):
    file_id: str


class ThumbnailPresignForm(ThumbnailUploadForm):
    file_id: str


class AdminConfigForm(BaseModel):
    provider: Literal["local", "cos"] | None = None
    cos: dict[str, Any] | None = None
    media: dict[str, Any] | None = None
    quota: dict[str, Any] | None = None


class UserQuotaForm(BaseModel):
    tier: str
    quota_bytes: int | None = None


def _kind(content_type: str | None) -> str:
    content_type = str(content_type or "")
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "file"


def _thumbnail_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    value = (meta or {}).get("thumbnail")
    return value if isinstance(value, dict) else {}


def _thumbnail_ready(meta: dict[str, Any] | None) -> bool:
    thumbnail = _thumbnail_metadata(meta)
    try:
        size = int(thumbnail.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return bool(
        thumbnail.get("status") == "completed"
        and thumbnail.get("content_type") == THUMBNAIL_CONTENT_TYPE
        and size > 0
    )


def _thumbnail_reservation(form_data: ThumbnailUploadForm, *, reserved_at: int | None = None) -> dict[str, Any]:
    return {
        "status": "uploading",
        "content_type": THUMBNAIL_CONTENT_TYPE,
        "size": int(form_data.size),
        "expected_size": int(form_data.size),
        "width": int(form_data.width),
        "height": int(form_data.height),
        "reserved_at": int(reserved_at or time.time()),
    }


async def _file_for_actor(file_id: str, user, db: AsyncSession) -> File | None:
    result = await db.execute(
        select(File)
        .where(File.id == file_id)
        .execution_options(populate_existing=True)
    )
    file = result.scalar_one_or_none()
    if not file or (file.user_id != user.id and user.role != "admin"):
        return None
    return file


async def _file_owner_role(file: File, user, db: AsyncSession) -> str:
    if file.user_id == user.id:
        return str(user.role or "user")
    owner = await Users.get_user_by_id(file.user_id, db=db)
    return str(getattr(owner, "role", None) or "user")


def _file_payload(file: File) -> dict[str, Any]:
    meta = file.meta or {}
    data = file.data or {}
    content_type = meta.get("content_type")
    media_meta = meta.get("data") if isinstance(meta.get("data"), dict) else {}

    def positive_dimension(name: str) -> int | None:
        try:
            value = int(meta.get(name) or media_meta.get(name) or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    return {
        "id": file.id,
        "name": meta.get("name") or file.filename,
        "content_type": content_type,
        "kind": _kind(content_type),
        "size": int(meta.get("size") or 0),
        "status": data.get("status") or "completed",
        "created_at": file.created_at,
        "updated_at": file.updated_at,
        "width": positive_dimension("width"),
        "height": positive_dimension("height"),
        "thumbnail_ready": _thumbnail_ready(meta),
        "content_endpoint": f"/api/v1/files/{file.id}/content",
    }


def _encode_cursor(file: File) -> str:
    payload = json.dumps(
        [int(file.created_at), str(file.id)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            f"{cursor}{padding}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        created_at = int(payload[0])
        file_id = str(payload[1])
        if created_at < 0 or not file_id or len(file_id) > 255:
            raise ValueError
        return created_at, file_id
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="分页游标无效，请刷新我的空间后重试",
        ) from exc


async def _cleanup_stale_reservations(user_id: str, db: AsyncSession) -> None:
    cutoff = int(time.time()) - 3600
    result = await db.execute(
        select(File).where(File.user_id == user_id, File.created_at < cutoff)
    )
    for file in result.scalars().all():
        meta = file.meta or {}
        data = file.data or {}
        if not meta.get("turtle_direct_upload") or data.get("status") != "uploading":
            continue
        try:
            await asyncio.to_thread(Storage.delete_file, file.path)
        except Exception:
            log.warning("Failed to remove a stale direct-upload object (%s)", type(file.path).__name__)
        await Files.delete_file_by_id(file.id, db=db)

    thumbnail_status = File.meta["thumbnail"]["status"].as_string()
    thumbnail_reserved_at = cast(
        File.meta["thumbnail"]["reserved_at"].as_string(),
        BigInteger,
    )
    stale_thumbnails = await db.execute(
        select(File).where(
            File.user_id == user_id,
            thumbnail_status == "uploading",
            thumbnail_reserved_at < cutoff,
        )
    )
    for file in stale_thumbnails.scalars().all():
        try:
            await asyncio.to_thread(Storage.delete_thumbnail, file.path)
        except Exception:
            log.warning("Failed to remove a stale static thumbnail")
            continue
        await Files.update_file_metadata_by_id(
            file.id,
            {
                "thumbnail": {"status": "missing"},
                "storage_size": int((file.meta or {}).get("size") or 0),
            },
            db=db,
        )


@router.get("/capabilities")
async def get_capabilities(
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    config = CONFIG_STORE.public(admin=False)
    return {
        "provider": config["provider"],
        "direct_upload": Storage.direct_upload_available(),
        "strict_external_media": strict_media_mode(),
        "media_pump_configured": MEDIA_PUMP.configured(),
        "cos_configured": config["cos"]["configured"],
        "media": config["media"],
        "quota": await quota_summary(user.id, db, role=user.role),
        "is_admin": user.role == "admin",
    }


@router.get("/me")
async def get_my_space(
    kind: Literal["all", "image", "video", "file"] = "all",
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int | None = Query(default=None, ge=1, le=100),
    include_summary: bool = True,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _cleanup_stale_reservations(user.id, db)
    filters = [File.user_id == user.id]
    content_type = File.meta["content_type"].as_string()
    if kind == "image":
        filters.append(content_type.like("image/%"))
    elif kind == "video":
        filters.append(content_type.like("video/%"))
    elif kind == "file":
        filters.append(
            or_(
                content_type.is_(None),
                (~content_type.like("image/%")) & (~content_type.like("video/%")),
            )
        )

    cursor_mode = cursor is not None or limit is not None
    effective_limit = limit or page_size
    count = None
    quota = None
    if include_summary:
        count = (await db.execute(select(func.count(File.id)).where(*filters))).scalar() or 0
        quota = await quota_summary(user.id, db, role=user.role)

    query = select(File).where(*filters).order_by(File.created_at.desc(), File.id.desc())
    if cursor_mode:
        if cursor:
            cursor_created_at, cursor_id = _decode_cursor(cursor)
            query = query.where(
                or_(
                    File.created_at < cursor_created_at,
                    and_(File.created_at == cursor_created_at, File.id < cursor_id),
                )
            )
        result = await db.execute(query.limit(effective_limit + 1))
        files = list(result.scalars().all())
        has_more = len(files) > effective_limit
        files = files[:effective_limit]
    else:
        result = await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
        files = list(result.scalars().all())
        has_more = bool(count is not None and page * page_size < count)

    next_cursor = _encode_cursor(files[-1]) if cursor_mode and has_more and files else None
    return {
        "quota": quota,
        "items": [_file_payload(file) for file in files],
        "total": count,
        "page": page,
        "page_size": effective_limit,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/files/{file_id}/url")
async def get_file_url(
    file_id: str,
    variant: Literal["thumbnail", "preview", "original"] = "original",
    attachment: bool = False,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(file_id, db=db)
    if not file or (file.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    meta = file.meta or {}
    content_type = str(meta.get("content_type") or "")
    effective_variant = variant if content_type.startswith("image/") else "original"
    if attachment and variant != "original":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="下载只能使用原文件",
        )
    selected_path = file.path
    selected_name = meta.get("name") or file.filename
    if effective_variant == "thumbnail":
        if not _thumbnail_ready(meta):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "static_thumbnail_missing",
                    "message": "静态缩略图尚未生成",
                },
            )
        selected_path = Storage.thumbnail_path(file.path)
        selected_name = f"{Path(str(selected_name)).stem}.thumbnail.webp"
    try:
        direct_url = Storage.presign_download(
            selected_path,
            filename=selected_name,
            attachment=attachment,
            # Preview is deliberately the untouched original. Thumbnail points
            # at its own persisted object, so no COS image transform is used.
            variant="original",
        )
    except StorageConfigurationError as exc:
        log.warning("Managed media URL signing is unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="对象存储配置暂时不可用，请联系管理员检查存储设置",
        ) from exc
    if effective_variant == "thumbnail" and not direct_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="静态缩略图直读暂时不可用",
        )
    return {
        "url": direct_url or f"/api/v1/files/{file.id}/content",
        "direct": bool(direct_url),
        "variant": effective_variant,
        "expires_in": (
            Storage.download_url_ttl(selected_path, attachment=attachment)
            if direct_url
            else None
        ),
    }


@router.get("/files/{file_id}/thumbnail")
async def get_inline_thumbnail(
    file_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Redirect an inline ``img`` to the persisted small object only."""

    payload = await get_file_url(
        file_id=file_id,
        variant="thumbnail",
        attachment=False,
        user=user,
        db=db,
    )
    if not payload.get("direct") or not payload.get("url"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="静态缩略图直读暂时不可用",
        )
    ttl = max(30, min(300, int(payload.get("expires_in") or 300) - 60))
    return RedirectResponse(
        str(payload["url"]),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={
            "Cache-Control": f"private, max-age={ttl}",
            "Vary": "Authorization, Cookie",
        },
    )


@router.post("/uploads/presign")
async def presign_upload(
    form_data: PresignUploadForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if not Storage.direct_upload_available():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="COS 浏览器直传尚未启用")
    if form_data.process:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="需要服务器处理的媒体请使用标准上传接口",
        )
    content_type = form_data.content_type.lower().split(";", 1)[0].strip()
    if len(json.dumps(form_data.metadata or {}, ensure_ascii=False)) > 32 * 1024:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传 metadata 过大")
    if not content_type.startswith(("image/", "video/")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="直传仅用于图片和视频")
    if form_data.thumbnail and not content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只有图片可以附带缩略图")
    media = CONFIG_STORE.load()["media"]
    max_bytes = media["max_image_bytes"] if content_type.startswith("image/") else media["max_video_bytes"]
    if form_data.size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "media_too_large", "max_bytes": max_bytes},
        )

    await _cleanup_stale_reservations(user.id, db)
    reserved_size = int(form_data.size) + int(form_data.thumbnail.size if form_data.thumbnail else 0)
    async with user_lock(user.id):
        try:
            await ensure_upload_capacity(user.id, reserved_size, db, role=user.role)
        except QuotaExceededError as exc:
            raise quota_http_exception(exc) from exc

        file_id = str(uuid.uuid4())
        name = Path(form_data.filename).name
        stored_name = safe_filename(name)
        file_path = Storage.build_cloud_path(user.id, file_id, stored_name)
        thumbnail_path = Storage.thumbnail_path(file_path) if form_data.thumbnail else ""
        try:
            signed = Storage.presign_upload(file_path, content_type)
            thumbnail_signed = (
                Storage.presign_upload(thumbnail_path, THUMBNAIL_CONTENT_TYPE)
                if form_data.thumbnail
                else None
            )
        except Exception as exc:
            log.warning("COS upload signing failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="无法签发 COS 上传地址，请联系管理员检查存储配置",
            ) from exc

        record = await Files.insert_new_file(
            user.id,
            FileForm(
                id=file_id,
                hash=form_data.file_hash,
                filename=name,
                path=file_path,
                data={"status": "uploading"},
                meta={
                    "name": name,
                    "content_type": content_type,
                    "size": form_data.size,
                    "expected_size": form_data.size,
                    "storage_size": reserved_size,
                    "data": form_data.metadata or {},
                    "process": bool(form_data.process),
                    "storage": "cos",
                    "turtle_direct_upload": True,
                    **(
                        {"thumbnail": _thumbnail_reservation(form_data.thumbnail)}
                        if form_data.thumbnail
                        else {}
                    ),
                },
            ),
            db=db,
        )
        if not record:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法创建上传记录")
        return {
            "file_id": file_id,
            "upload_url": signed["url"],
            "method": "PUT",
            "headers": signed["headers"],
            "expires_in": signed["expires_in"],
            "thumbnail_upload": (
                {
                    "upload_url": thumbnail_signed["url"],
                    "method": "PUT",
                    "headers": thumbnail_signed["headers"],
                    "expires_in": thumbnail_signed["expires_in"],
                }
                if thumbnail_signed
                else None
            ),
        }


async def _discard_upload(file, db: AsyncSession) -> None:
    try:
        await asyncio.to_thread(Storage.delete_file, file.path)
    except Exception:
        log.warning("COS cleanup failed for rejected upload: %s", type(file.path).__name__)
    await Files.delete_file_by_id(file.id, db=db)


@router.post("/uploads/complete", response_model=FileModelResponse)
async def complete_upload(
    form_data: CompleteUploadForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id_and_user_id(form_data.file_id, user.id, db=db)
    if not file or not (file.meta or {}).get("turtle_direct_upload"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="上传记录不存在")
    if (file.data or {}).get("status") == "completed":
        return FileModelResponse.model_validate(file.model_dump())
    if (file.data or {}).get("status") != "uploading":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="上传记录状态无效")

    try:
        head = await asyncio.to_thread(Storage.head_file, file.path)
    except Exception as exc:
        log.warning("COS upload verification failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="COS 尚未确认收到文件，请检查 Bucket CORS 后重试",
        ) from exc

    meta = file.meta or {}
    actual_size = int(head.get("ContentLength") or 0)
    expected_size = int(meta.get("expected_size") or 0)
    if actual_size <= 0 or actual_size != expected_size:
        await _discard_upload(file, db)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "upload_size_mismatch",
                "message": "COS 对象大小与预留不一致，上传已取消",
            },
        )

    thumbnail = _thumbnail_metadata(meta)
    completed_thumbnail: dict[str, Any] | None = None
    if thumbnail.get("status") == "uploading":
        try:
            thumbnail_head = await asyncio.to_thread(
                Storage.head_file,
                Storage.thumbnail_path(file.path),
            )
        except Exception as exc:
            log.warning("COS static-thumbnail verification failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="COS 尚未确认收到静态缩略图，请检查 Bucket CORS 后重试",
            ) from exc
        thumbnail_size = int(thumbnail_head.get("ContentLength") or 0)
        expected_thumbnail_size = int(thumbnail.get("expected_size") or 0)
        thumbnail_type = str(thumbnail_head.get("ContentType") or "").split(";", 1)[0].lower()
        if (
            thumbnail_size <= 0
            or thumbnail_size != expected_thumbnail_size
            or thumbnail_type != THUMBNAIL_CONTENT_TYPE
        ):
            await _discard_upload(file, db)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "thumbnail_size_mismatch",
                    "message": "静态缩略图与预留不一致，上传已取消",
                },
            )
        completed_thumbnail = {
            **thumbnail,
            "status": "completed",
            "size": thumbnail_size,
            "content_type": THUMBNAIL_CONTENT_TYPE,
            "verified_at": int(time.time()),
        }

    await Files.update_file_metadata_by_id(
        file.id,
        {
            "size": actual_size,
            "storage_size": actual_size + int((completed_thumbnail or {}).get("size") or 0),
            "content_type": head.get("ContentType") or meta.get("content_type"),
            "verified_at": int(time.time()),
            **({"thumbnail": completed_thumbnail} if completed_thumbnail else {}),
        },
        db=db,
    )
    updated = await Files.update_file_data_by_id(file.id, {"status": "completed"}, db=db)
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法完成上传记录")
    return FileModelResponse.model_validate(updated.model_dump())


@router.get("/thumbnails/{file_id}")
async def get_thumbnail_status(
    file_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await _file_for_actor(file_id, user, db)
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    meta = file.meta or {}
    eligible = bool(
        str(meta.get("content_type") or "").startswith("image/")
        and (file.data or {}).get("status") == "completed"
        and Storage.is_cloud_path(file.path)
    )
    return {
        "file_id": file.id,
        "eligible": eligible,
        "ready": _thumbnail_ready(meta),
        "status": _thumbnail_metadata(meta).get("status") or "missing",
    }


@router.post("/thumbnails/presign")
async def presign_thumbnail(
    form_data: ThumbnailPresignForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if not Storage.direct_upload_available():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="COS 浏览器直传尚未启用")
    file = await _file_for_actor(form_data.file_id, user, db)
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    async with user_lock(file.user_id):
        # A chat tab and the administrator backfill can discover the same image
        # together. Re-read after taking the user lock so only one reservation
        # is issued and a newer reservation cannot be overwritten.
        file = await _file_for_actor(form_data.file_id, user, db)
        if not file:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
        meta = file.meta or {}
        if (
            not str(meta.get("content_type") or "").startswith("image/")
            or (file.data or {}).get("status") != "completed"
            or not Storage.is_cloud_path(file.path)
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前文件不能生成静态缩略图")
        if _thumbnail_ready(meta):
            return {"file_id": file.id, "ready": True, "thumbnail_upload": None}
        current_thumbnail = _thumbnail_metadata(meta)
        if (
            current_thumbnail.get("status") == "uploading"
            and int(current_thumbnail.get("reserved_at") or 0) >= int(time.time()) - 3600
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="静态缩略图正在由另一个页面生成",
            )

        thumbnail_path = Storage.thumbnail_path(file.path)
        try:
            signed = Storage.presign_upload(thumbnail_path, THUMBNAIL_CONTENT_TYPE)
        except Exception as exc:
            log.warning("COS static-thumbnail signing failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="无法签发静态缩略图上传地址",
            ) from exc

        original_size = int(meta.get("size") or 0)
        owner_role = await _file_owner_role(file, user, db)
        try:
            await ensure_upload_capacity(
                file.user_id,
                original_size + int(form_data.size),
                db,
                exclude_file_id=file.id,
                role=owner_role,
            )
        except QuotaExceededError as exc:
            raise quota_http_exception(exc) from exc
        await Files.update_file_metadata_by_id(
            file.id,
            {
                "thumbnail": _thumbnail_reservation(form_data),
                "storage_size": original_size + int(form_data.size),
            },
            db=db,
        )
    return {
        "file_id": file.id,
        "ready": False,
        "thumbnail_upload": {
            "upload_url": signed["url"],
            "method": "PUT",
            "headers": signed["headers"],
            "expires_in": signed["expires_in"],
        },
    }


@router.post("/thumbnails/complete")
async def complete_thumbnail(
    form_data: CompleteUploadForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await _file_for_actor(form_data.file_id, user, db)
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    async with user_lock(file.user_id):
        file = await _file_for_actor(form_data.file_id, user, db)
        if not file:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
        meta = file.meta or {}
        if _thumbnail_ready(meta):
            return {"ok": True, "file_id": file.id, "ready": True}
        thumbnail = _thumbnail_metadata(meta)
        if thumbnail.get("status") != "uploading":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="缩略图上传记录状态无效")
        try:
            head = await asyncio.to_thread(Storage.head_file, Storage.thumbnail_path(file.path))
        except Exception as exc:
            log.warning("COS static-thumbnail verification failed: %s", type(exc).__name__)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="COS 尚未确认收到静态缩略图") from exc

        actual_size = int(head.get("ContentLength") or 0)
        expected_size = int(thumbnail.get("expected_size") or 0)
        actual_type = str(head.get("ContentType") or "").split(";", 1)[0].lower()
        if actual_size <= 0 or actual_size != expected_size or actual_type != THUMBNAIL_CONTENT_TYPE:
            try:
                await asyncio.to_thread(Storage.delete_thumbnail, file.path)
            except Exception as exc:
                log.warning("Rejected static-thumbnail cleanup failed")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="无效缩略图清理失败，请稍后重试",
                ) from exc
            await Files.update_file_metadata_by_id(
                file.id,
                {
                    "thumbnail": {"status": "failed"},
                    "storage_size": int(meta.get("size") or 0),
                },
                db=db,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "thumbnail_size_mismatch",
                    "message": "静态缩略图与预留不一致",
                },
            )

        verified_at = int(time.time())
        completed = {
            **thumbnail,
            "status": "completed",
            "size": actual_size,
            "content_type": THUMBNAIL_CONTENT_TYPE,
            "verified_at": verified_at,
        }
        await Files.update_file_metadata_by_id(
            file.id,
            {
                "thumbnail": completed,
                "storage_size": int(meta.get("size") or 0) + actual_size,
            },
            db=db,
        )
        return {"ok": True, "file_id": file.id, "ready": True, "thumbnail": completed}


@router.delete("/thumbnails/{file_id}")
async def cancel_thumbnail(
    file_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await _file_for_actor(file_id, user, db)
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    async with user_lock(file.user_id):
        file = await _file_for_actor(file_id, user, db)
        if not file:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
        if _thumbnail_ready(file.meta or {}):
            return {"ok": True, "ready": True}
        try:
            await asyncio.to_thread(Storage.delete_thumbnail, file.path)
        except Exception as exc:
            log.warning("Static-thumbnail cancellation cleanup failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="缩略图清理失败，请稍后重试",
            ) from exc
        await Files.update_file_metadata_by_id(
            file.id,
            {
                "thumbnail": {"status": "missing"},
                "storage_size": int((file.meta or {}).get("size") or 0),
            },
            db=db,
        )
        return {"ok": True}


@router.delete("/uploads/{file_id}")
async def cancel_upload(
    file_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id_and_user_id(file_id, user.id, db=db)
    if not file or not (file.meta or {}).get("turtle_direct_upload"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="上传记录不存在")
    if (file.data or {}).get("status") == "completed":
        # A successful completion response can be lost on the network. Treat a
        # late cancellation as an idempotent no-op instead of deleting a valid
        # original and its thumbnail.
        return {"ok": True, "completed": True}
    await _discard_upload(file, db)
    return {"ok": True}


@router.get("/admin/thumbnails/missing")
async def get_missing_thumbnails(
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=24, ge=1, le=100),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    content_type = File.meta["content_type"].as_string()
    file_status = File.data["status"].as_string()
    thumbnail_status = File.meta["thumbnail"]["status"].as_string()
    thumbnail_reserved_at = cast(File.meta["thumbnail"]["reserved_at"].as_string(), BigInteger)
    stale_cutoff = int(time.time()) - 3600
    query = (
        select(File)
        .where(
            content_type.like("image/%"),
            File.path.like("s3://%"),
            or_(file_status.is_(None), file_status == "completed"),
            or_(
                thumbnail_status.is_(None),
                thumbnail_status.in_(("missing", "pending", "failed")),
                and_(thumbnail_status == "uploading", thumbnail_reserved_at < stale_cutoff),
            ),
        )
        .order_by(File.created_at.desc(), File.id.desc())
    )
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                File.created_at < cursor_created_at,
                and_(File.created_at == cursor_created_at, File.id < cursor_id),
            )
        )
    files = list((await db.execute(query.limit(limit + 1))).scalars().all())
    has_more = len(files) > limit
    files = files[:limit]
    return {
        "items": [
            {
                "id": file.id,
                "name": (file.meta or {}).get("name") or file.filename,
                "size": int((file.meta or {}).get("size") or 0),
            }
            for file in files
        ],
        "next_cursor": _encode_cursor(files[-1]) if has_more and files else None,
        "has_more": has_more,
    }


@router.get("/admin/config")
async def get_admin_config(user=Depends(get_admin_user)):
    return {
        **CONFIG_STORE.public(admin=True),
        "quota_managed_by": "chat_groups",
        "media_isolation": {
            "strict": strict_media_mode(),
            "pump_configured": MEDIA_PUMP.configured(),
            "server_uploads_disabled": strict_media_mode(),
        },
    }


@router.put("/admin/config")
async def update_admin_config(form_data: AdminConfigForm, user=Depends(get_admin_user)):
    try:
        return CONFIG_STORE.update_admin(form_data.model_dump(exclude_none=True))
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/admin/test")
async def test_admin_config(user=Depends(get_admin_user)):
    config = CONFIG_STORE.load()
    if config["provider"] == "local":
        return {"ok": True, "provider": "local", "message": "本地存储可用"}
    try:
        await asyncio.to_thread(Storage.test_connection)
        return {"ok": True, "provider": "cos", "message": "COS Bucket 连接成功"}
    except Exception as exc:
        log.warning("COS connection test failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="COS 连接失败，请检查地域、Endpoint、Bucket、密钥和权限",
        ) from exc


@router.get("/admin/users")
async def get_admin_users(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    result = await Users.get_users(skip=0, limit=None, db=db)
    items = []
    for target in result.get("users", []):
        assignment = await asyncio.to_thread(
            CHAT_STORE.storage_quota_for_user, target.id, target.role
        )
        items.append(
            {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "role": target.role,
                **assignment,
                "used_bytes": await usage_bytes(target.id, db),
            }
        )
    return {
        "items": items,
        "managed_by": "chat_groups",
    }


@router.put("/admin/users/{user_id}/quota")
async def update_user_quota(
    user_id: str,
    form_data: UserQuotaForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="用户空间额度已迁移到聊天分组，请在额度策略中编辑所属分组",
    )
