"""Best-effort notification after a local Open WebUI chat is deleted."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx


log = logging.getLogger(__name__)


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
            path=f"{base_path}/internal/upstream-cleanup/schedule",
            query="",
            fragment="",
        ).geturl(),
        keys[0],
    )


async def _notify(payload: dict[str, Any]) -> None:
    target = _target()
    if target is None:
        return
    url, key = target
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0),
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPError:
        # Local deletion is authoritative and must never be rolled back by a
        # cleanup notification failure. The Gateway's orphan scan will find it.
        log.warning("Unable to schedule upstream chat cleanup")


def _consume(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def schedule_upstream_cleanup(
    *,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> None:
    payload = {
        key: value
        for key, value in {
            "chat_id": str(chat_id) if chat_id else None,
            "user_id": str(user_id) if user_id else None,
        }.items()
        if value
    }
    if not payload:
        return
    task = asyncio.create_task(
        _notify(payload),
        name="turtle-upstream-cleanup-notify",
    )
    task.add_done_callback(_consume)
