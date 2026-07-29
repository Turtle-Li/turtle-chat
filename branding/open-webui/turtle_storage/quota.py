"""Per-user media quota accounting backed by Open WebUI's file table."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import HTTPException, status
from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File
from sqlalchemy import BigInteger, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..turtle_chat.store import CHAT_STORE
from .core import CONFIG_STORE


_USER_LOCKS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class QuotaExceededError(ValueError):
    def __init__(self, used_bytes: int, quota_bytes: int, requested_bytes: int):
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        self.requested_bytes = requested_bytes
        super().__init__("用户存储空间不足")


class MediaTooLargeError(ValueError):
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        super().__init__("媒体文件超过管理员设置的大小上限")


def user_lock(user_id: str) -> asyncio.Lock:
    return _USER_LOCKS[user_id]


def ensure_media_size(content_type: str | None, requested_bytes: int) -> None:
    value = str(content_type or "").lower().split(";", 1)[0]
    media = CONFIG_STORE.load()["media"]
    limit = None
    if value.startswith("image/"):
        limit = int(media["max_image_bytes"])
    elif value.startswith("video/"):
        limit = int(media["max_video_bytes"])
    elif value in {"application/zip", "application/x-zip-compressed"}:
        limit = int(media["max_file_bytes"])
    if limit is not None and int(requested_bytes) > limit:
        raise MediaTooLargeError(limit)


async def usage_bytes(
    user_id: str,
    db: AsyncSession | None = None,
    exclude_file_id: str | None = None,
) -> int:
    async with get_async_db_context(db) as session:
        original_size = cast(File.meta["size"].as_string(), BigInteger)
        storage_size = cast(File.meta["storage_size"].as_string(), BigInteger)
        status_value = File.data["status"].as_string()
        counted_size = case(
            (storage_size > 0, storage_size),
            (original_size > 0, original_size),
            else_=0,
        )
        stmt = select(func.coalesce(func.sum(counted_size), 0)).where(
            File.user_id == user_id,
            or_(status_value.is_(None), ~status_value.in_(("failed", "cancelled"))),
        )
        if exclude_file_id:
            stmt = stmt.where(File.id != exclude_file_id)
        return max(0, int((await session.execute(stmt)).scalar() or 0))


async def quota_summary(
    user_id: str,
    db: AsyncSession | None = None,
    *,
    role: str = "user",
) -> dict:
    assignment = await asyncio.to_thread(CHAT_STORE.storage_quota_for_user, user_id, role)
    used = await usage_bytes(user_id, db)
    quota = assignment["quota_bytes"]
    return {
        **assignment,
        "used_bytes": used,
        "remaining_bytes": max(0, quota - used),
        "usage_ratio": (used / quota) if quota else (1.0 if used else 0.0),
    }


async def ensure_upload_capacity(
    user_id: str,
    requested_bytes: int,
    db: AsyncSession | None = None,
    *,
    exclude_file_id: str | None = None,
    role: str = "user",
) -> None:
    requested = max(0, int(requested_bytes))
    assignment = await asyncio.to_thread(CHAT_STORE.storage_quota_for_user, user_id, role)
    used = await usage_bytes(user_id, db, exclude_file_id=exclude_file_id)
    if used + requested > assignment["quota_bytes"]:
        raise QuotaExceededError(used, assignment["quota_bytes"], requested)


def quota_http_exception(error: QuotaExceededError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail={
            "code": "storage_quota_exceeded",
            "message": "存储空间不足，请删除旧文件或联系管理员调整额度",
            "used_bytes": error.used_bytes,
            "quota_bytes": error.quota_bytes,
            "requested_bytes": error.requested_bytes,
        },
    )


def media_size_http_exception(error: MediaTooLargeError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail={
            "code": "media_too_large",
            "message": "媒体文件超过管理员设置的大小上限",
            "max_bytes": error.max_bytes,
        },
    )
