"""Best-effort ChatGPT model-input preparation after a managed upload."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from open_webui.utils.session_pool import get_session

from ..turtle_chat.store import CHAT_STORE
from .media import get_presigned_model_image_source_for_file


STAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ModelStageUnavailable(RuntimeError):
    pass


def _target() -> tuple[str, str] | None:
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
    return (
        parsed._replace(
            path=f"{base_path}/internal/image-media/stage",
            query="",
            fragment="",
        ).geturl(),
        keys[0],
    )


async def stage_model_input(
    *,
    file,
    user,
    stage_session_id: str,
    chat_id: str | None = None,
) -> dict[str, Any]:
    target = _target()
    source = get_presigned_model_image_source_for_file(file, user)
    if target is None or source is None:
        raise ModelStageUnavailable("model-input prefetch is unavailable")
    routing = await asyncio.to_thread(
        CHAT_STORE.image_routing_for_user,
        str(user.id),
        str(user.role or "user"),
    )
    url, key = target
    payload = {
        "turtle_stage_session_id": stage_session_id,
        "turtle_account_pool_id": routing["account_pool_id"],
        "turtle_user_id": str(user.id),
        "turtle_required_quota_profiles": routing["required_quota_profiles"],
        "turtle_media": [
            {
                **source,
                "name": Path((file.meta or {}).get("name") or file.filename).name,
                "turtle_media_id": str(file.id),
            }
        ],
        **({"turtle_chat_id": str(chat_id)} if chat_id else {}),
    }
    try:
        session = await get_session()
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=240, connect=2),
        ) as response:
            if not 200 <= response.status < 300:
                raise ModelStageUnavailable("model-input prefetch was rejected")
            result = await response.json()
    except ModelStageUnavailable:
        raise
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
        raise ModelStageUnavailable("model-input prefetch failed") from exc
    token = result.get("stage_token") if isinstance(result, dict) else None
    expires_at = result.get("expires_at") if isinstance(result, dict) else None
    if (
        not STAGE_TOKEN_RE.fullmatch(str(token or ""))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= int(time.time())
        or expires_at > int(time.time()) + 60 * 60
    ):
        raise ModelStageUnavailable("model-input prefetch response is invalid")
    return {
        "v": 1,
        "token": token,
        "media_id": str(file.id),
        "expires_at": expires_at,
        "prepared_at": int(time.time()),
    }
