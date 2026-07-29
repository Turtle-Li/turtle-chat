"""Server-side quota, concurrency, queueing, and sanitized request timing."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from .account_pool import (
    ACCOUNT_POOL_ADMISSION,
    AccountPoolCapacityUnavailable,
)
from .concurrency import (
    CHAT_CONCURRENCY,
    ChatConcurrencyError,
    ChatCoordinatorUnavailable,
    ChatQueueTimeout,
    ConcurrencyLease,
)
from .provider import provider_for_chat
from .subscription_cache import SUBSCRIPTION_CACHE
from .store import (
    CHAT_STORE,
    SELECTION_BY_KEY,
    ChatModelQuotaError,
    ChatPolicyError,
    ChatSubscriptionError,
    Reservation,
)


MODEL_FIELDS = {
    "gpt-5-web": ("turtle_model_version", "turtle_thinking_level"),
    "claude-web": ("turtle_claude_model", "turtle_claude_thinking"),
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _has_effective_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_effective_value(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key in ("content", "text", "output_text", "tool_calls", "function_call", "image_url"):
        if key in value and _has_effective_value(value[key]):
            return True
    for key in ("choices", "message", "delta", "output"):
        if key in value and _has_effective_value(value[key]):
            return True
    return False


def response_has_effective_content(payload: Any) -> bool:
    return _has_effective_value(payload)


def _chunk_events(chunk: bytes | str) -> tuple[bool, bool]:
    text = chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else str(chunk)
    has_content = False
    has_done = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            has_done = True
            continue
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if response_has_effective_content(payload):
            has_content = True
    return has_content, has_done


def _chunk_has_effective_content(chunk: bytes | str) -> bool:
    return _chunk_events(chunk)[0]


@dataclass(slots=True)
class ChatRequestContext:
    lease: ConcurrencyLease
    reservation: Reservation | None = None
    internal_task: bool = False
    upstream_started_at_ms: int | None = None
    connected_at_ms: int | None = None
    http_status: int | None = None
    first_content_at_ms: int | None = None
    usage_committed: bool = False
    completed: bool = False

    @property
    def id(self) -> str | None:
        return self.reservation.id if self.reservation is not None else None

    @property
    def selection_key(self) -> str | None:
        return self.reservation.selection_key if self.reservation is not None else None


def _request_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ChatQueueTimeout):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "chat_queue_timeout",
                "message": str(exc),
                "retry_after_seconds": 10,
            },
            headers={"Retry-After": "10"},
        )
    if isinstance(exc, ChatCoordinatorUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "chat_concurrency_unavailable", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "chat_queue_conflict", "message": str(exc)},
    )


async def prepare_chat_request(
    user,
    payload: dict[str, Any],
    *,
    internal_task: bool = False,
    chat_id: str | None = None,
) -> ChatRequestContext | None:
    model_id = str(payload.get("model") or "").strip()
    try:
        if SUBSCRIPTION_CACHE.store is CHAT_STORE:
            await SUBSCRIPTION_CACHE.require_active(user.id, user.role)
        else:
            # Focused unit tests and one-time migration tools may inject an
            # isolated store. Production always uses the shared Redis cache.
            await asyncio.to_thread(
                CHAT_STORE.require_active_subscription,
                user.id,
                user.role,
            )
    except ChatSubscriptionError as exc:
        subscription = exc.subscription
        subscription_status = str(subscription.get("status") or "inactive")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": f"chat_subscription_{subscription_status}",
                "message": str(exc),
                "subscription": subscription,
            },
        ) from exc
    fields = MODEL_FIELDS.get(model_id)
    if fields is None:
        return None
    version_field, level_field = fields
    queue_request_id = CHAT_CONCURRENCY.normalize_request_id(
        str(payload.pop("turtle_queue_request_id", "") or "")
    )

    provider = "claude" if model_id == "claude-web" else "gpt"
    if chat_id:
        from open_webui.models.chats import Chats

        existing_chat = await Chats.get_chat_by_id(str(chat_id))
        if existing_chat is not None and str(existing_chat.user_id) == str(user.id):
            existing_provider = provider_for_chat(existing_chat.chat, existing_chat.meta)
            if existing_provider != provider:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "chat_provider_mismatch",
                        "message": "当前对话已固定到另一 Provider，请新建对话后切换",
                        "chat_provider": existing_provider,
                        "requested_provider": provider,
                    },
                )

    version = str(payload.get(version_field) or "").strip()
    level = str(payload.get(level_field) or "").strip()
    if internal_task:
        if model_id != "claude-web":
            fallback = await asyncio.to_thread(
                CHAT_STORE.task_selection,
                user.id,
                user.role,
                model_id,
            )
            version = fallback["version"]
            level = fallback["level"]
            payload[version_field] = version
            payload[level_field] = level
    elif not version or not level:
        fallback = await asyncio.to_thread(
            CHAT_STORE.default_selection,
            user.id,
            user.role,
            model_id,
        )
        version = fallback["version"]
        level = fallback["level"]
        payload[version_field] = version
        payload[level_field] = level

    concurrency = await asyncio.to_thread(
        CHAT_STORE.concurrency_for_user,
        user.id,
        user.role,
    )
    account_pool_id = str(
        (concurrency.get("account_pool_ids") or {}).get(provider)
        or concurrency.get(f"{provider}_account_pool_id")
        or f"{provider}-default"
    )
    account_pool_limit: int | None = None
    provider_limit: int | None = None
    global_limit: int | None = None
    account_pool_limit_resolver = None
    if ACCOUNT_POOL_ADMISSION.enabled:
        account_selection_key = f"{version}:{level}"
        try:
            admission_limits = await ACCOUNT_POOL_ADMISSION.limits(
                account_pool_id,
                account_selection_key,
            )
        except AccountPoolCapacityUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "chat_account_pool_unavailable",
                    "message": str(exc),
                },
            ) from exc
        account_pool_limit = int(admission_limits["account_pool"])
        provider_limit = int(admission_limits["provider"])
        global_limit = int(admission_limits["global"])
        if account_pool_limit <= 0:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "chat_account_pool_not_ready",
                    "message": f"当前 {provider.upper()} 账号池没有可调度的健康账号",
                },
            )

        async def resolve_account_pool_limit() -> dict[str, int]:
            try:
                return await ACCOUNT_POOL_ADMISSION.limits(
                    account_pool_id,
                    account_selection_key,
                )
            except AccountPoolCapacityUnavailable as exc:
                raise ChatCoordinatorUnavailable(
                    f"{provider.upper()} 账号池容量暂时不可用"
                ) from exc

        account_pool_limit_resolver = resolve_account_pool_limit

    lease: ConcurrencyLease | None = None
    try:
        lease = await CHAT_CONCURRENCY.acquire(
            request_id=queue_request_id,
            user_id=user.id,
            group_id=str(concurrency["group_id"]),
            provider=provider,
            user_limit=int(concurrency["user_max_concurrency"]),
            group_limit=int(concurrency["group_max_concurrency"]),
            account_pool_id=account_pool_id,
            account_pool_limit=account_pool_limit,
            provider_limit=provider_limit,
            global_limit=global_limit,
            account_pool_limit_resolver=account_pool_limit_resolver,
        )
    except ChatConcurrencyError as exc:
        raise _request_http_error(exc) from exc

    context = ChatRequestContext(lease=lease, internal_task=internal_task)
    # These opaque routing hints stop at the Gateway. They let the selected
    # Provider pool keep soft per-conversation affinity without forwarding
    # local user/chat identifiers to the upstream site.
    payload["turtle_account_pool_id"] = account_pool_id
    payload["turtle_user_id"] = str(user.id)
    payload["turtle_request_id"] = str(lease.request_id)
    if chat_id and not internal_task:
        payload["turtle_chat_id"] = str(chat_id)
    elif internal_task:
        # Title, tag, suggestion and memory jobs may run just before or after
        # the foreground turn. They must consume normal Provider capacity but
        # must not claim the user's upstream conversation affinity/serial lock,
        # otherwise the next real turn is rejected as an overlapping request.
        payload.pop("turtle_chat_id", None)
    if internal_task:
        return context

    try:
        reservation = await asyncio.to_thread(
            CHAT_STORE.reserve,
            user.id,
            user.role,
            version=version,
            level=level,
            model_id=model_id,
            request_id=lease.request_id,
            queued_at_ms=lease.queued_at_ms,
            admitted_at_ms=lease.admitted_at_ms,
        )
        context.reservation = reservation
        if reservation.selection_key != f"{version}:{level}":
            fallback = SELECTION_BY_KEY[reservation.selection_key]
            payload[version_field] = fallback["version"]
            payload[level_field] = fallback["level"]
        return context
    except ChatModelQuotaError as exc:
        await lease.release("quota_exceeded")
        retry_after = max(1, int(exc.reset_at - time.time())) if exc.reset_at else 60
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "chat_model_quota_exceeded",
                "message": str(exc),
                "selection_key": exc.selection_key,
                "reset_at": exc.reset_at,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        ) from exc
    except ChatPolicyError as exc:
        await lease.release("policy_denied")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "chat_selection_forbidden", "message": str(exc)},
        ) from exc
    except BaseException:
        await lease.release("prepare_failed")
        raise


async def mark_chat_upstream_started(context: ChatRequestContext | None) -> None:
    if context is not None:
        context.upstream_started_at_ms = _now_ms()


async def mark_chat_connected(
    context: ChatRequestContext | None,
    http_status: int | None,
) -> None:
    if context is None:
        return
    connected_at = _now_ms()
    context.connected_at_ms = connected_at
    context.http_status = int(http_status) if http_status is not None else None
    if context.reservation is not None:
        started = context.upstream_started_at_ms or context.lease.admitted_at_ms
        await asyncio.to_thread(
            CHAT_STORE.record_transport,
            context.reservation.id,
            connected_at_ms=connected_at,
            connect_ms=max(0, connected_at - started),
            http_status=context.http_status,
        )


async def _commit_first_content(context: ChatRequestContext) -> None:
    if context.first_content_at_ms is not None:
        return
    first_content_at = _now_ms()
    context.first_content_at_ms = first_content_at
    started = context.upstream_started_at_ms or context.lease.admitted_at_ms
    if context.reservation is not None:
        await asyncio.to_thread(
            CHAT_STORE.record_first_content,
            context.reservation.id,
            first_content_at_ms=first_content_at,
            ttft_ms=max(0, first_content_at - started),
        )
        await asyncio.to_thread(CHAT_STORE.finalize, context.reservation.id, "committed")
        context.usage_committed = True


async def _complete_context(
    context: ChatRequestContext | None,
    *,
    outcome: str,
    error_type: str | None = None,
    error_phase: str | None = None,
    http_status: int | None = None,
) -> None:
    if context is None or context.completed:
        return
    context.completed = True
    completed_at = _now_ms()
    try:
        if context.reservation is not None:
            if not context.usage_committed:
                await asyncio.to_thread(CHAT_STORE.finalize, context.reservation.id, "released")
            await asyncio.to_thread(
                CHAT_STORE.record_completion,
                context.reservation.id,
                completed_at_ms=completed_at,
                total_ms=max(0, completed_at - context.lease.admitted_at_ms),
                outcome=outcome,
                http_status=http_status if http_status is not None else context.http_status,
                error_type=error_type,
                error_phase=error_phase,
            )
    finally:
        await context.lease.release(outcome)


async def fail_chat_request(
    context: ChatRequestContext | None,
    *,
    error_type: str,
    error_phase: str,
    http_status: int | None = None,
) -> None:
    await _complete_context(
        context,
        outcome="error",
        error_type=error_type,
        error_phase=error_phase,
        http_status=http_status,
    )


async def commit_chat_request(context: ChatRequestContext | Reservation | None) -> None:
    """Compatibility wrapper used by unit tests and older patch call sites."""
    if isinstance(context, Reservation):
        await asyncio.to_thread(CHAT_STORE.finalize, context.id, "committed")
    elif context is not None:
        await _commit_first_content(context)


async def release_chat_request(
    context: ChatRequestContext | Reservation | None,
    *,
    outcome: str = "cancelled",
) -> None:
    if isinstance(context, Reservation):
        await asyncio.to_thread(CHAT_STORE.finalize, context.id, "released")
    else:
        await _complete_context(
            context,
            outcome=outcome,
            error_type="request_cancelled" if outcome == "cancelled" else outcome,
            error_phase="application",
        )


async def finalize_chat_response(
    context: ChatRequestContext | Reservation | None,
    payload: Any,
    http_status: int | None = None,
) -> None:
    if isinstance(context, Reservation):
        if response_has_effective_content(payload):
            await commit_chat_request(context)
        else:
            await release_chat_request(context)
        return
    if context is None:
        return
    if response_has_effective_content(payload):
        await _commit_first_content(context)
        await _complete_context(context, outcome="success", http_status=http_status)
    else:
        await _complete_context(
            context,
            outcome="error",
            error_type="empty_response",
            error_phase="response_body",
            http_status=http_status,
        )


async def tracked_chat_stream(
    source: AsyncIterator[bytes],
    context: ChatRequestContext | Reservation | None,
) -> AsyncIterator[bytes]:
    if isinstance(context, Reservation):
        committed = False
        try:
            async for chunk in source:
                if not committed and _chunk_has_effective_content(chunk):
                    await commit_chat_request(context)
                    committed = True
                yield chunk
        finally:
            if not committed:
                await release_chat_request(context)
        return

    if context is None:
        async for chunk in source:
            yield chunk
        return

    saw_content = False
    saw_done = False
    stream_error: BaseException | None = None
    try:
        async for chunk in source:
            has_content, has_done = _chunk_events(chunk)
            if has_content and not saw_content:
                saw_content = True
                await _commit_first_content(context)
            saw_done = saw_done or has_done
            yield chunk
    except BaseException as exc:
        stream_error = exc
        raise
    finally:
        if stream_error is not None:
            await _complete_context(
                context,
                outcome="cancelled" if isinstance(stream_error, asyncio.CancelledError) else "error",
                error_type=(
                    "client_cancelled"
                    if isinstance(stream_error, asyncio.CancelledError)
                    else "stream_exception"
                ),
                error_phase="stream",
            )
        elif saw_content and saw_done:
            await _complete_context(context, outcome="success")
        elif saw_content:
            await _complete_context(
                context,
                outcome="error",
                error_type="stream_incomplete",
                error_phase="stream",
            )
        else:
            await _complete_context(
                context,
                outcome="error",
                error_type="empty_stream",
                error_phase="stream",
            )
