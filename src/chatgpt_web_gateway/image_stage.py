from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass(slots=True)
class ImageStage:
    token: str
    session_id: str
    conversation_id: str
    account_id: str
    pool_id: str
    user_id: str
    required_quota_profiles: tuple[str, ...]
    expires_at: int
    media_ids: set[str] = field(default_factory=set)
    claimed: bool = False


class ImageStageRegistry:
    """Short-lived routing hints for model-input files prepared before send."""

    def __init__(self, *, ttl_seconds: int = 30 * 60, max_entries: int = 2048):
        self.ttl_seconds = max(60, min(60 * 60, int(ttl_seconds)))
        self.max_entries = max(32, min(10_000, int(max_entries)))
        self._by_token: dict[str, ImageStage] = {}
        self._by_session: dict[tuple[str, str, str], ImageStage] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    @staticmethod
    def _session_key(user_id: str, pool_id: str, session_id: str) -> tuple[str, str, str]:
        return str(user_id), str(pool_id), str(session_id)

    def _discard(self, stage: ImageStage) -> None:
        self._by_token.pop(stage.token, None)
        key = self._session_key(stage.user_id, stage.pool_id, stage.session_id)
        if self._by_session.get(key) is stage:
            self._by_session.pop(key, None)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                self._locks.pop(key, None)

    def _prune(self, now: int | None = None) -> None:
        current = int(time.time()) if now is None else int(now)
        for stage in tuple(self._by_token.values()):
            if stage.expires_at <= current:
                self._discard(stage)
        while len(self._by_token) > self.max_entries:
            oldest = min(self._by_token.values(), key=lambda item: item.expires_at)
            self._discard(oldest)

    @asynccontextmanager
    async def session_lock(
        self,
        *,
        user_id: str,
        pool_id: str,
        session_id: str,
    ) -> AsyncIterator[None]:
        self._prune()
        key = self._session_key(user_id, pool_id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    def current(
        self,
        *,
        user_id: str,
        pool_id: str,
        session_id: str,
        required_quota_profiles: frozenset[str],
    ) -> ImageStage | None:
        self._prune()
        stage = self._by_session.get(
            self._session_key(user_id, pool_id, session_id)
        )
        if stage is None or stage.claimed:
            if stage is not None:
                self._discard(stage)
            return None
        if stage.required_quota_profiles != tuple(sorted(required_quota_profiles)):
            self._discard(stage)
            return None
        return stage

    def remember(
        self,
        *,
        user_id: str,
        pool_id: str,
        session_id: str,
        conversation_id: str,
        account_id: str,
        required_quota_profiles: frozenset[str],
        media_ids: set[str],
        existing: ImageStage | None = None,
    ) -> ImageStage:
        self._prune()
        key = self._session_key(user_id, pool_id, session_id)
        stage = existing
        if stage is None:
            previous = self._by_session.get(key)
            if previous is not None:
                self._discard(previous)
            stage = ImageStage(
                token=secrets.token_urlsafe(32),
                session_id=session_id,
                conversation_id=conversation_id,
                account_id=account_id,
                pool_id=pool_id,
                user_id=user_id,
                required_quota_profiles=tuple(sorted(required_quota_profiles)),
                expires_at=int(time.time()) + self.ttl_seconds,
            )
            self._by_session[key] = stage
            self._by_token[stage.token] = stage
        stage.media_ids.update(str(value) for value in media_ids)
        stage.expires_at = int(time.time()) + self.ttl_seconds
        self._prune()
        return stage

    def claim(
        self,
        *,
        token: str,
        user_id: str,
        pool_id: str,
        required_quota_profiles: frozenset[str],
        media_ids: set[str],
    ) -> ImageStage | None:
        self._prune()
        stage = self._by_token.get(str(token or ""))
        if (
            stage is None
            or stage.claimed
            or stage.user_id != str(user_id)
            or stage.pool_id != str(pool_id)
            or stage.required_quota_profiles != tuple(sorted(required_quota_profiles))
            or not media_ids
            or not set(media_ids).issubset(stage.media_ids)
        ):
            return None
        stage.claimed = True
        return stage
