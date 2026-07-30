"""Pure helpers for Project API managed image references.

The public API intentionally exposes OpenAI-shaped file objects while using a
Turtle direct-upload transport.  Media bytes never pass through this module or
the Japan application host.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable


PROJECT_FILE_PREFIX = "file-"
PROJECT_FILE_PURPOSE = "vision"
PROJECT_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class ProjectMediaReferenceError(ValueError):
    """Raised when a Project API message contains an unsafe media reference."""


def public_file_id(file_id: str) -> str:
    value = str(file_id or "").strip()
    return value if value.startswith(PROJECT_FILE_PREFIX) else f"{PROJECT_FILE_PREFIX}{value}"


def internal_file_id(value: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate.startswith(PROJECT_FILE_PREFIX):
        return None
    candidate = candidate[len(PROJECT_FILE_PREFIX) :]
    if len(candidate) != 36:
        return None
    parts = candidate.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return None
    if any(character not in "0123456789abcdefABCDEF" for part in parts for character in part):
        return None
    return candidate.lower()


def project_file_scope(meta: dict[str, Any] | None) -> tuple[str | None, str | None]:
    data = (meta or {}).get("data")
    data = data if isinstance(data, dict) else {}
    key_id = str(data.get("project_api_key_id") or "").strip() or None
    purpose = str(data.get("project_api_purpose") or "").strip() or None
    return key_id, purpose


def project_file_object(file: Any) -> dict[str, Any]:
    meta = file.meta or {}
    data = file.data or {}
    _key_id, purpose = project_file_scope(meta)
    return {
        "id": public_file_id(file.id),
        "object": "file",
        "bytes": max(0, int(meta.get("size") or 0)),
        "created_at": int(file.created_at),
        "filename": str(meta.get("name") or file.filename),
        "purpose": purpose or PROJECT_FILE_PURPOSE,
        # Turtle reserves the object before the direct COS PUT. This extension
        # is needed to distinguish a reservation from a verified file.
        "status": str(data.get("status") or "completed"),
    }


def has_image_inputs(payload: dict[str, Any]) -> bool:
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict)
            and part.get("type") in {"image_url", "input_image"}
            for part in content
        ):
            return True
    return False


def _detail(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in {"auto", "low", "high"}:
        raise ProjectMediaReferenceError("image detail 只支持 auto、low 或 high")
    return normalized


def _reference_from_part(part: dict[str, Any]) -> tuple[str, str | None]:
    part_type = part.get("type")
    if part_type == "input_image":
        if any(key not in {"type", "file_id", "detail"} for key in part):
            raise ProjectMediaReferenceError("input_image 包含不支持的字段")
        file_id = internal_file_id(str(part.get("file_id") or ""))
        if file_id is None:
            raise ProjectMediaReferenceError(
                "input_image 必须使用 POST /files 返回的 file_id"
            )
        return file_id, _detail(part.get("detail"))

    image_url = part.get("image_url")
    if isinstance(image_url, str):
        reference = image_url
        detail = None
    elif isinstance(image_url, dict):
        if "turtle_source" in image_url:
            raise ProjectMediaReferenceError("客户端不能提交 turtle_source")
        if any(key not in {"url", "file_id", "detail"} for key in image_url):
            raise ProjectMediaReferenceError("image_url 包含不支持的字段")
        reference = image_url.get("file_id") or image_url.get("url")
        detail = _detail(image_url.get("detail"))
    else:
        raise ProjectMediaReferenceError("image_url 必须引用已上传的 file_id")

    file_id = internal_file_id(str(reference or ""))
    if file_id is None:
        raise ProjectMediaReferenceError(
            "Project API 图片必须使用 POST /files 返回的 file_id；"
            "不接受任意公网 URL 或 base64"
        )
    return file_id, detail


async def rewrite_image_inputs(
    payload: dict[str, Any],
    resolver: Callable[[str], Awaitable[dict[str, str]]],
) -> tuple[dict[str, Any], int]:
    """Resolve public file IDs into sealed internal CDN-first image sources."""

    copied = deepcopy(payload)
    resolved_count = 0
    cache: dict[str, dict[str, str]] = {}
    for message in copied.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        rewritten: list[Any] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {
                "image_url",
                "input_image",
            }:
                rewritten.append(part)
                continue
            file_id, detail = _reference_from_part(part)
            source = cache.get(file_id)
            if source is None:
                source = await resolver(file_id)
                cache[file_id] = source
            image_url = {
                "url": source["url"],
                "turtle_source": source["turtle_source"],
            }
            if detail:
                image_url["detail"] = detail
            rewritten.append({"type": "image_url", "image_url": image_url})
            resolved_count += 1
        message["content"] = rewritten
    return copied, resolved_count
