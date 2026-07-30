"""Managed model media URLs and response-media persistence."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlparse

from open_webui.internal.db import get_async_db_context
from open_webui.models.chats import Chats
from open_webui.models.files import File, FileForm, FileModel, Files
from sqlalchemy import select

from .core import CONFIG_STORE, safe_filename
from .provider import Storage
from .pump import (
    MEDIA_PUMP,
    MODEL_INPUT_MAX_BYTES,
    MODEL_MEDIA_MAX_DIMENSION,
    MODEL_MEDIA_TYPE_RE,
    MediaPumpError,
    strict_media_mode,
)
from .quota import (
    MediaTooLargeError,
    QuotaExceededError,
    ensure_media_size,
    ensure_upload_capacity,
    user_lock,
)


log = logging.getLogger(__name__)
FILE_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,40}$")
FILE_URL_RE = re.compile(r"/api/v1/files/([0-9a-fA-F-]{32,40})/content(?:/[^?#]+)?")
MEDIA_REFERENCE_RE = re.compile(
    r"(?P<prefix>!\[[^\]]*\]\(|<(?:img|video|source)\b[^>]*?\bsrc=[\"'])"
    r"(?P<url>https?://[^\s\"'<>\)]+)",
    re.IGNORECASE,
)
FILE_REFERENCE_RE = re.compile(
    r"(?<!!)\[(?P<label>[^\]\r\n]{1,255})\]\("
    r"(?P<url>https?://[^\s\"'<>\)]+)\)",
    re.IGNORECASE,
)
BASE64_MEDIA_REFERENCE_RE = re.compile(
    r"(?P<prefix>!\[[^\]]*\]\(|<img\b[^>]*?\bsrc=[\"'])"
    r"(?P<url>data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+)",
    re.IGNORECASE,
)
ZIP_CONTENT_TYPES = {"application/zip", "application/x-zip-compressed"}
ZIP_NAME_RE = re.compile(r"([^\s/\\()[\]<>]{1,180}\.zip)\b", re.IGNORECASE)
STREAM_MEDIA_PLACEHOLDER = "图片已生成，正在整理并保存…"


def requested_image_count(prompt: str) -> int:
    """Return an explicit 2–4 image request; ordinary prompts stay at one."""
    text = str(prompt or "")
    chinese = re.search(
        r"(?<!第)([2-4]|[两二三四])\s*(?:张|幅)"
        r"(?:[^。！？!?\n]{0,16})?(?:图片|图像|图)",
        text,
    )
    if chinese:
        token = chinese.group(1)
        names = {"两": 2, "二": 2, "三": 3, "四": 4}
        return names[token] if token in names else int(token)
    english = re.search(
        r"\b(two|three|four|[2-4])\s+(?:[A-Za-z-]+\s+){0,3}images?\b",
        text,
        re.IGNORECASE,
    )
    if english:
        token = english.group(1).lower()
        names = {"two": 2, "three": 3, "four": 4}
        return names[token] if token in names else int(token)
    return 1


def _strip_stream_source_lines(text: str) -> tuple[str, bool]:
    """Hide short-lived Pump capabilities until the final managed URL exists."""
    base_url = str(getattr(MEDIA_PUMP, "base_url", "") or "").rstrip("/")
    if not base_url or base_url not in text:
        return text, False

    hidden = False
    cleaned: list[str] = []
    for line in text.splitlines(keepends=True):
        source_at = line.find(base_url)
        if source_at < 0:
            cleaned.append(line)
            continue

        hidden = True
        marker_at = max(
            line.rfind("![", 0, source_at),
            line.rfind("<img", 0, source_at),
            line.rfind("<video", 0, source_at),
            line.rfind("<source", 0, source_at),
        )
        if marker_at > 0 and line[marker_at - 1] == "[":
            marker_at -= 1
        if marker_at < 0:
            marker_at = line.rfind("[", 0, source_at)
        if marker_at < 0:
            marker_at = source_at

        prefix = line[:marker_at].rstrip()
        if prefix:
            cleaned.append(prefix)
            if line.endswith(("\n", "\r")):
                cleaned.append("\n")

    return "".join(cleaned).rstrip(), hidden


def visible_stream_output(output: list) -> list:
    """Return a UI-safe stream snapshot without transient media capability URLs.

    The original output remains untouched so the completion path can persist
    every source through the Media Pump and emit the final managed file URLs.
    """
    base_url = str(getattr(MEDIA_PUMP, "base_url", "") or "").rstrip("/")
    if not base_url:
        return output

    has_source = any(
        base_url in part["text"]
        for item in output or []
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    )
    if not has_source:
        return output

    visible = deepcopy(output)
    last_hidden_part: dict | None = None
    for item in visible:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            cleaned, hidden = _strip_stream_source_lines(text)
            if hidden:
                part["text"] = cleaned
                last_hidden_part = part

    if last_hidden_part is not None:
        text = str(last_hidden_part.get("text") or "").rstrip()
        last_hidden_part["text"] = (
            f"{text}\n\n{STREAM_MEDIA_PLACEHOLDER}"
            if text
            else STREAM_MEDIA_PLACEHOLDER
        )
    return visible


def carry_forward_message_images(messages: list[dict]) -> list[dict]:
    """Attach branch images to the latest user turn for OpenaiAccount.

    The web provider currently consumes media only from the final user message.
    Keep all text in its original turn, deduplicate image references, and move
    only the structured ``image_url`` parts to that final user message.
    """
    copied = deepcopy(messages)
    user_messages = [message for message in copied if message.get("role") == "user"]
    if not user_messages:
        return copied

    images: list[dict] = []
    seen: set[str] = set()
    for message in user_messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        retained = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                retained.append(item)
                continue
            image_url = item.get("image_url")
            value = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not isinstance(value, str) or not value:
                retained.append(item)
                continue
            if value not in seen:
                seen.add(value)
                images.append(item)
        message["content"] = retained

    if not images:
        return copied

    latest = user_messages[-1]
    content = latest.get("content")
    if isinstance(content, list):
        latest["content"] = [*content, *images]
    elif isinstance(content, str):
        latest["content"] = ([{"type": "text", "text": content}] if content else []) + images
    else:
        latest["content"] = images
    return copied


def bind_message_image_file_ids(messages: list[dict]) -> list[dict]:
    """Bind first-turn image parts to immutable managed file IDs.

    A newly composed message can carry a transient preview URL while its
    ``files`` metadata already contains the authoritative file ID. A DB-backed
    first turn can instead carry plain text plus the same file metadata.
    Replacing only validated, unambiguous image/file pairs before request
    processing lets the normal ownership check seal the CDN/COS model source
    on the first turn.
    """
    copied = deepcopy(messages)
    for message in copied:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        files = message.get("files")
        if not isinstance(files, list):
            continue
        image_file_ids = []
        for file in files:
            if not isinstance(file, dict):
                continue
            nested_file = file.get("file")
            nested_file = nested_file if isinstance(nested_file, dict) else {}
            file_type = file.get("type") or nested_file.get("type")
            content_type = (
                file.get("content_type")
                or nested_file.get("content_type")
                or ""
            )
            if (
                file_type != "image"
                and not str(content_type).startswith("image/")
            ):
                continue
            nested_id = _file_id_from_url(str(nested_file.get("id") or ""))
            direct_id = _file_id_from_url(str(file.get("id") or ""))
            url_id = _file_id_from_url(
                str(file.get("url") or nested_file.get("url") or "")
            )
            if nested_id and direct_id and nested_id != direct_id:
                continue
            candidate = nested_id or direct_id or url_id
            if candidate:
                image_file_ids.append(candidate)
        if not image_file_ids:
            continue
        if isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content},
                *[
                    {
                        "type": "image_url",
                        "image_url": {"url": file_id},
                    }
                    for file_id in image_file_ids
                ],
            ]
            continue
        if not isinstance(content, list):
            continue
        image_parts = [
            part
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        if not image_parts or len(image_parts) != len(image_file_ids):
            continue
        for part, file_id in zip(image_parts, image_file_ids):
            current = part.get("image_url")
            detail = current.get("detail") if isinstance(current, dict) else None
            part["image_url"] = {
                "url": file_id,
                **({"detail": detail} if isinstance(detail, str) and detail else {}),
            }
    return copied


def _file_id_from_url(value: str) -> str | None:
    value = str(value or "")
    if FILE_ID_RE.fullmatch(value):
        return value
    match = FILE_URL_RE.search(value)
    return match.group(1) if match else None


def _cloud_path_from_files_cdn_url(value: str) -> str | None:
    """Map only the configured main-object CDN URL back to its COS path."""
    try:
        source = urlparse(str(value or ""))
        config = CONFIG_STORE.load()
        files_base = urlparse(str(config["cdn"].get("files_base_url") or ""))
        bucket = str(config["cos"].get("bucket") or "")
        prefix = str(config["cos"].get("prefix") or "").strip("/")
    except (KeyError, TypeError, ValueError):
        return None
    if (
        source.scheme != "https"
        or not source.hostname
        or source.username
        or source.password
        or source.fragment
        or source.netloc.lower() != files_base.netloc.lower()
        or not bucket
    ):
        return None
    key = unquote(source.path).lstrip("/")
    if (
        not key
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or any(ord(character) < 32 for character in key)
    ):
        return None
    namespaces = (
        f"{prefix}/files/users/" if prefix else "files/users/",
        f"{prefix}/users/" if prefix else "users/",
    )
    if not key.startswith(namespaces):
        return None
    return f"s3://{bucket}/{key}"


async def get_presigned_model_image_source(value: str, user) -> dict | None:
    """Return a signed CDN-first model source only for a caller-owned file."""
    file_id = _file_id_from_url(value)
    if user is None:
        return None
    file = await Files.get_file_by_id(file_id) if file_id else None
    if file is None and not file_id:
        cloud_path = _cloud_path_from_files_cdn_url(value)
        if cloud_path:
            async with get_async_db_context() as db:
                statement = select(File).where(File.path == cloud_path)
                if getattr(user, "role", "") != "admin":
                    statement = statement.where(File.user_id == str(user.id))
                result = await db.execute(statement)
                matches = result.scalars().all()
                if len(matches) == 1:
                    file = FileModel.model_validate(matches[0])
    return get_presigned_model_image_source_for_file(file, user)


def get_presigned_model_image_source_for_file(file, user) -> dict | None:
    """Seal a previously authorized file without repeating its database read."""

    if not file or (file.user_id != user.id and getattr(user, "role", "") != "admin"):
        return None
    meta = file.meta or {}
    primary_url = Storage.presign_download(
        file.path,
        filename=meta.get("name") or file.filename,
        attachment=False,
        use_cdn=True,
    )
    fallback_url = Storage.presign_download(
        file.path,
        filename=meta.get("name") or file.filename,
        attachment=False,
        use_cdn=False,
    )
    if not primary_url or not fallback_url:
        return None
    primary_parsed = urlparse(primary_url)
    fallback_parsed = urlparse(fallback_url)
    if (
        primary_parsed.hostname == fallback_parsed.hostname
        and primary_parsed.path == fallback_parsed.path
    ):
        fallback_url = None
    media_metadata: dict[str, int | str] = {}
    file_data = file.data if isinstance(getattr(file, "data", None), dict) else {}
    if file_data.get("status") == "completed":
        try:
            verified_size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            verified_size = 0
        verified_type = (
            str(meta.get("content_type") or "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if (
            0 < verified_size <= MODEL_INPUT_MAX_BYTES
            and MODEL_MEDIA_TYPE_RE.fullmatch(verified_type)
        ):
            media_metadata = {
                "expected_size": verified_size,
                "expected_content_type": verified_type,
            }
            nested_meta = meta.get("data") if isinstance(meta.get("data"), dict) else {}
            try:
                width = int(meta.get("width") or nested_meta.get("width") or 0)
                height = int(meta.get("height") or nested_meta.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
            if (
                0 < width <= MODEL_MEDIA_MAX_DIMENSION
                and 0 < height <= MODEL_MEDIA_MAX_DIMENSION
            ):
                media_metadata.update({"width": width, "height": height})
    token = MEDIA_PUMP.seal_model_source(
        primary_url=primary_url,
        fallback_url=fallback_url,
        source_key=f"{file.id}\0{file.path}",
        ttl_seconds=min(
            Storage.download_url_ttl(file.path, attachment=False, use_cdn=True),
            Storage.download_url_ttl(file.path, attachment=False, use_cdn=False),
            900,
        ),
        **media_metadata,
    )
    return {"url": primary_url, "turtle_source": token}


def _generated_name(url: str, content_type: str, filename_hint: str | None = None) -> str:
    if content_type == "application/zip":
        match = ZIP_NAME_RE.search(str(filename_hint or ""))
        return safe_filename(match.group(1) if match else "chatgpt-generated.zip")
    suffix = mimetypes.guess_extension(content_type) or Path(urlparse(url).path).suffix
    suffix = suffix if suffix and len(suffix) <= 10 else ""
    kind = "image" if content_type.startswith("image/") else "video"
    return safe_filename(f"chatgpt-generated-{kind}{suffix}")


def _is_pump_source_url(url: str) -> bool:
    base_url = str(getattr(MEDIA_PUMP, "base_url", "") or "").rstrip("/")
    return bool(base_url and str(url or "").startswith(f"{base_url}/v1/source/"))


def _normalized_generated_type(content_type: str, filename_hint: str | None) -> str:
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    if value in ZIP_CONTENT_TYPES:
        return "application/zip"
    if value == "application/octet-stream" and str(filename_hint or "").lower().endswith(".zip"):
        return "application/zip"
    return value


async def _delete_transfer_record(file_id: str, file_path: str) -> None:
    try:
        await asyncio.to_thread(Storage.delete_file, file_path)
    except Exception:
        log.warning("Generated media object cleanup failed")
    async with get_async_db_context() as db:
        await Files.delete_file_by_id(file_id, db=db)


async def persist_generated_media_url(
    request,
    url: str,
    metadata: dict,
    user,
    *,
    filename_hint: str | None = None,
) -> str:
    """Ask the external pump to mirror generated media/files without proxying bytes here."""
    if not str(url or "").startswith(("http://", "https://")):
        return url
    state = getattr(request, "state", None)
    cache = getattr(state, "turtle_persisted_media", None) if state is not None else None
    if cache is None:
        cache = {}
        if state is not None:
            state.turtle_persisted_media = cache
    if url in cache:
        return cache[url]
    if not MEDIA_PUMP.configured() or not Storage.direct_upload_available():
        log.warning("Generated media persistence skipped: external pump unavailable")
        cache[url] = url
        return url
    file_id = ""
    file_path = ""
    try:
        config = CONFIG_STORE.load()
        absolute_max = max(
            config["media"]["max_image_bytes"],
            config["media"]["max_video_bytes"],
            config["media"]["max_file_bytes"],
        )
        probe = await MEDIA_PUMP.probe(url, absolute_max)
        content_type = _normalized_generated_type(probe["content_type"], filename_hint)
        size = int(probe["size"])
        if not content_type.startswith(("image/", "video/")) and content_type != "application/zip":
            return url
        ensure_media_size(content_type, size)
        if content_type.startswith("image/"):
            limit = config["media"]["max_image_bytes"]
        elif content_type.startswith("video/"):
            limit = config["media"]["max_video_bytes"]
        else:
            limit = config["media"]["max_file_bytes"]
        file_id = str(uuid.uuid4())
        name = _generated_name(url, content_type, filename_hint)
        file_path = Storage.build_cloud_path(user.id, file_id, name)
        async with get_async_db_context() as db:
            async with user_lock(user.id):
                await ensure_upload_capacity(user.id, size, db, role=user.role)
                file_item = await Files.insert_new_file(
                    user.id,
                    FileForm(
                        id=file_id,
                        filename=name,
                        path=file_path,
                        data={"status": "copying"},
                        meta={
                            "name": name,
                            "content_type": content_type,
                            "size": size,
                            "expected_size": size,
                            "storage_size": size,
                            "storage": "cos",
                            "origin": "chatgpt-generated",
                            "turtle_pump_transfer": True,
                            **(
                                {
                                    "width": int(probe.get("width") or 0),
                                    "height": int(probe.get("height") or 0),
                                    "thumbnail": {"status": "pending"},
                                }
                                if content_type.startswith("image/")
                                else {}
                            ),
                            "data": {
                                "chat_id": metadata.get("chat_id"),
                                "message_id": metadata.get("message_id"),
                            },
                        },
                    ),
                    db=db,
                )
        if not file_item:
            raise MediaPumpError("generated media reservation failed")
        signed = Storage.presign_upload(file_path, content_type)
        transfer = await MEDIA_PUMP.transfer(
            source_url=url,
            destination_url=signed["url"],
            destination_headers=signed["headers"],
            expected_size=size,
            expected_content_type=content_type,
            max_bytes=limit,
        )
        head = await asyncio.to_thread(Storage.head_file, file_path)
        actual_size = int(head.get("ContentLength") or 0)
        if actual_size != size:
            raise MediaPumpError("COS size verification failed")
        actual_type = str(head.get("ContentType") or content_type).split(";", 1)[0].lower()
        async with get_async_db_context() as db:
            await Files.update_file_metadata_by_id(
                file_id,
                {
                    "size": actual_size,
                    "storage_size": actual_size,
                    "content_type": actual_type,
                    "sha256": transfer.get("sha256"),
                    "verified_at": int(time.time()),
                },
                db=db,
            )
            file_item = await Files.update_file_data_by_id(
                file_id,
                {"status": "completed"},
                db=db,
            )
        if not file_item:
            raise MediaPumpError("generated media completion failed")
        chat_id = metadata.get("chat_id")
        message_id = metadata.get("message_id")
        if chat_id and message_id and file_item:
            try:
                await Chats.insert_chat_files(
                    chat_id=chat_id,
                    message_id=message_id,
                    file_ids=[file_item.id],
                    user_id=user.id,
                )
            except Exception as exc:
                log.warning("Generated media chat linkage failed: %s", type(exc).__name__)
        managed_url = f"/api/v1/files/{file_item.id}/content"
        cache[url] = managed_url
        return managed_url
    except (MediaPumpError, MediaTooLargeError, QuotaExceededError) as exc:
        log.warning("Generated media persistence skipped: %s", type(exc).__name__)
        if file_id and file_path:
            await _delete_transfer_record(file_id, file_path)
        cache[url] = url
        return url
    except Exception as exc:
        log.warning("Generated media persistence failed: %s", type(exc).__name__)
        if file_id and file_path:
            await _delete_transfer_record(file_id, file_path)
        cache[url] = url
        return url


async def persist_output_media(request, output: list, metadata: dict, user) -> list:
    """Replace completed remote media and generated ZIP URLs with managed file URLs."""
    url_cache: dict[str, str] = {}
    base64_cache: dict[str, str] = {}
    for item in output or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            if "data:image/" in text and not strict_media_mode():
                for match in list(BASE64_MEDIA_REFERENCE_RE.finditer(text)):
                    value = match.group("url")
                    if value in base64_cache:
                        continue
                    try:
                        from open_webui.utils.files import get_image_url_from_base64

                        managed = await get_image_url_from_base64(request, value, metadata, user)
                        base64_cache[value] = str(managed or value)
                    except Exception as exc:
                        log.warning("Generated base64 image persistence skipped: %s", type(exc).__name__)
                        base64_cache[value] = value
                text = BASE64_MEDIA_REFERENCE_RE.sub(
                    lambda match: (
                        f"{match.group('prefix')}"
                        f"{base64_cache.get(match.group('url'), match.group('url'))}"
                    ),
                    text,
                )
                part["text"] = text
            if "http" not in text:
                continue
            matches = list(MEDIA_REFERENCE_RE.finditer(text))
            for match in matches:
                url = match.group("url")
                if url not in url_cache:
                    url_cache[url] = await persist_generated_media_url(request, url, metadata, user)
            if url_cache:
                part["text"] = MEDIA_REFERENCE_RE.sub(
                    lambda match: f"{match.group('prefix')}{url_cache.get(match.group('url'), match.group('url'))}",
                    text,
                )
                text = part["text"]
            file_matches = list(FILE_REFERENCE_RE.finditer(text))
            for match in file_matches:
                url = match.group("url")
                if url in url_cache or not _is_pump_source_url(url):
                    continue
                label = match.group("label")
                if not ZIP_NAME_RE.search(label):
                    continue
                url_cache[url] = await persist_generated_media_url(
                    request,
                    url,
                    metadata,
                    user,
                    filename_hint=label,
                )
            if file_matches:
                part["text"] = FILE_REFERENCE_RE.sub(
                    lambda match: (
                        f"[{match.group('label')}]"
                        f"({url_cache.get(match.group('url'), match.group('url'))})"
                    ),
                    text,
                )
    return output
