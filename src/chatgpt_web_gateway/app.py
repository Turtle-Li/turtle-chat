from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from copy import deepcopy
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from claude_web_worker.models import (
    CLAUDE_ROUTES,
    model_metadata as claude_model_metadata,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .account_pool import (
    AccountPoolConflict,
    AccountUnavailable,
    build_account_pool,
)
from .api_pricing import (
    estimate_chat_input_tokens,
    estimate_completion_tokens,
    price_for_route,
    simulated_cost,
)
from .capabilities import (
    SelectionError,
    model_metadata,
    resolve_selection,
    resolve_selection_key,
)
from .config import Settings
from .login_control import (
    LoginControlClient,
    LoginControlError,
    LoginControlUnavailable,
    LoginRuntimeMissing,
)
from .mock_backend import chunks, mock_answer
from .models import (
    AccountForm,
    AccountOnboardForm,
    AccountPoolForm,
    AccountSettingsForm,
    ChatCompletionRequest,
    ImageGenerationRequest,
    ProjectApiKeyForm,
    ProjectApiCreditGrantForm,
    ProjectApiPermissionForm,
    ProjectApiPricingConfigForm,
)
from .project_usage import (
    ProjectCaller,
    ProjectBalanceInsufficient,
    ProjectKeyConflict,
    ProjectRequestConflict,
    build_project_usage,
    extract_usage,
)
from .security import install_redaction_filter
from .upstream import (
    SearchPresentationBuffer,
    UpstreamFailure,
    UpstreamMediaMetrics,
    UpstreamResourceMetadata,
    UpstreamStageMetrics,
    extract_upstream_media_metrics,
    extract_upstream_resource_metadata,
    extract_upstream_stage_metrics,
    normalize_markdown_boundaries,
    normalize_sse_events,
)
from .upstream_cleanup import UpstreamCleanupManager


# Reuse Uvicorn's configured error logger so metric-only application events are
# visible in both local and container runs without adding a second log handler.
logger = logging.getLogger("uvicorn.error")
_SEARCH_INTENT_RE = re.compile(
    r"联网|搜索|检索|查找|浏览网页|打开.{0,12}(?:页面|官网)"
    r"|\b(?:browse|search|look up)\b",
    re.IGNORECASE,
)
_TURTLE_SOURCE_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9_-]{20,48000}\.[A-Za-z0-9_-]{43}$"
)


def _enforce_external_media(payload: dict[str, Any]) -> None:
    """Fail closed before gpt4free can decode or download inline media."""
    for field in ("image", "images", "media"):
        if payload.get(field) is not None:
            raise SelectionError(f"{field} is disabled; use an HTTPS image_url backed by managed storage")
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            value = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not isinstance(value, str) or not value.startswith("https://"):
                raise SelectionError(
                    "inline and non-HTTPS media is disabled; media must use a managed HTTPS URL"
                )
            if isinstance(image_url, dict):
                if any(
                    key not in {"url", "detail", "turtle_source"}
                    for key in image_url
                ):
                    raise SelectionError(
                        "image_url contains an unsupported field"
                    )
                turtle_source = image_url.get("turtle_source")
                if (
                    turtle_source is not None
                    and (
                        not isinstance(turtle_source, str)
                        or not _TURTLE_SOURCE_TOKEN_RE.fullmatch(turtle_source)
                    )
                ):
                    raise SelectionError(
                        "managed media source token is invalid"
                    )


def _image_url_count(payload: dict[str, Any]) -> int:
    return sum(
        1
        for message in payload.get("messages", [])
        if isinstance(message, dict)
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


def _has_unsealed_image_url(payload: dict[str, Any]) -> bool:
    return any(
        not isinstance(part.get("image_url"), dict)
        or not part["image_url"].get("turtle_source")
        for message in payload.get("messages", [])
        if isinstance(message, dict)
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


def _message_shape(payload: dict[str, Any]) -> tuple[int, str, bool]:
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return 0, "none", False
    last = messages[-1] if isinstance(messages[-1], dict) else {}
    content = last.get("content")
    media_on_last = any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in (content if isinstance(content, list) else [])
    )
    return len(messages), str(last.get("role") or "unknown"), media_on_last


def _log_upstream_media_metrics(
    request_id: str,
    metrics: UpstreamMediaMetrics,
) -> None:
    if metrics.empty:
        return
    logger.info(
        "request_media id=%s probe=%d transfer=%d bytes=%d cdn_hit=%d cdn_miss=%d fallback=%d retry=%d file_reuse=%d file_upload=%d file_stale=%d wall_ms=%d cache_ms=%d probe_ms=%d create_ms=%d settle_ms=%d transfer_ms=%d confirm_ms=%d prepare_parallel=%d/%d transfer_parallel=%d/%d",
        request_id,
        metrics.probe_count,
        metrics.transfer_count,
        metrics.transfer_bytes,
        metrics.cdn_hit,
        metrics.cdn_miss,
        metrics.fallback_count,
        metrics.retry_count,
        metrics.file_cache_hit,
        metrics.file_cache_miss,
        metrics.file_cache_stale,
        metrics.upload_wall_ms,
        metrics.cache_validation_ms,
        metrics.probe_ms,
        metrics.create_ms,
        metrics.settle_ms,
        metrics.transfer_ms,
        metrics.confirm_ms,
        metrics.max_prepare_parallel,
        metrics.configured_prepare_parallel,
        metrics.max_parallel,
        metrics.configured_parallel,
    )


def _log_upstream_stage_metrics(
    request_id: str,
    metrics: UpstreamStageMetrics,
) -> None:
    if metrics.empty:
        return
    logger.info(
        "request_upstream_stage id=%s schema_version=%d home_ms=%d media_ms=%d prepare_ms=%d requirements_ms=%d submit_headers_ms=%d submit_namelookup_ms=%d submit_connect_ms=%d submit_appconnect_ms=%d submit_pretransfer_ms=%d submit_starttransfer_ms=%d submit_server_wait_ms=%d first_event_ms=%d pre_stream_ms=%d provider_first_parsed_string_ms=%d upstream_events_before_first_parsed_string=%d provider_first_emitted_string_ms=%d handoff_used=%d handoff_seen_ms=%d handoff_sse_tail_ms=%d handoff_start_ms=%d handoff_endpoint_ms=%d handoff_connect_ms=%d handoff_first_frame_ms=%d handoff_first_item_ms=%d handoff_first_item_topic_class=%d handoff_items_expected=%d handoff_items_conversations=%d handoff_items_unscoped=%d handoff_done_topic_class=%d handoff_total_ms=%d",
        request_id,
        metrics.schema_version,
        metrics.home_ms,
        metrics.media_ms,
        metrics.prepare_ms,
        metrics.requirements_ms,
        metrics.submit_headers_ms,
        metrics.submit_namelookup_ms,
        metrics.submit_connect_ms,
        metrics.submit_appconnect_ms,
        metrics.submit_pretransfer_ms,
        metrics.submit_starttransfer_ms,
        metrics.submit_server_wait_ms,
        metrics.first_event_ms,
        metrics.pre_stream_ms,
        metrics.provider_first_parsed_string_ms,
        metrics.upstream_events_before_first_parsed_string,
        metrics.provider_first_emitted_string_ms,
        metrics.handoff_used,
        metrics.handoff_seen_ms,
        metrics.handoff_sse_tail_ms,
        metrics.handoff_start_ms,
        metrics.handoff_endpoint_ms,
        metrics.handoff_connect_ms,
        metrics.handoff_first_frame_ms,
        metrics.handoff_first_item_ms,
        metrics.handoff_first_item_topic_class,
        metrics.handoff_items_expected,
        metrics.handoff_items_conversations,
        metrics.handoff_items_unscoped,
        metrics.handoff_done_topic_class,
        metrics.handoff_total_ms,
    )


def _bounded_millisecond_header(value: str | None) -> int:
    try:
        parsed = int(value or "")
    except (TypeError, ValueError):
        return 0
    return parsed if 0 <= parsed <= 3_600_000 else 0


def _log_gateway_stage_metrics(
    request_id: str,
    *,
    attempt_no: int,
    phase: str,
    outcome: str,
    pre_acquire_ms: int,
    account_acquire_ms: int = 0,
    worker_headers_ms: int = 0,
    worker_first_chunk_ms: int = 0,
    gateway_prefetch_ms: int = 0,
    response_ready_ms: int = 0,
    prefetched_events: int = 0,
    prefetched_bytes: int = 0,
    prefetched_effective: bool = False,
) -> None:
    worker_http_overhead_ms = (
        max(0, worker_headers_ms - worker_first_chunk_ms)
        if worker_first_chunk_ms
        else 0
    )
    logger.info(
        "request_gateway_stage id=%s attempt=%d phase=%s outcome=%s pre_acquire_ms=%d account_acquire_ms=%d worker_headers_ms=%d worker_first_chunk_ms=%d worker_http_overhead_ms=%d gateway_prefetch_ms=%d response_ready_ms=%d prefetched_events=%d prefetched_bytes=%d prefetched_effective=%s",
        request_id,
        attempt_no,
        phase,
        outcome,
        pre_acquire_ms,
        account_acquire_ms,
        worker_headers_ms,
        worker_first_chunk_ms,
        worker_http_overhead_ms,
        gateway_prefetch_ms,
        response_ready_ms,
        prefetched_events,
        prefetched_bytes,
        prefetched_effective,
    )


def _payload_has_explicit_web_search(payload: dict[str, Any]) -> bool:
    if "web_search" in payload:
        return payload.get("web_search") is True
    features = payload.get("features")
    return bool(
        isinstance(features, dict)
        and (
            features.get("web_search") is True
            or features.get("webSearch") is True
        )
    )


def _payload_has_search_intent(payload: dict[str, Any]) -> bool:
    if _payload_has_explicit_web_search(payload):
        return True
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}
            )
        else:
            text = ""
        return bool(_SEARCH_INTENT_RE.search(text))
    return False


def _error(status_code: int, message: str, error_type: str = "gateway_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "param": None, "code": None}},
    )


def _safe_failover_reason(status_code: int) -> str | None:
    """Classify failures that are safe to retry before any response is exposed.

    A 401/403 proves that the selected account cannot serve the request and a
    429 proves that its selected lane is unavailable. Ambiguous transport and
    5xx failures are intentionally excluded because the upstream may already
    have accepted the request.
    """

    if status_code in {401, 403}:
        return "failover_auth"
    if status_code == 429:
        return "failover_rate_limit"
    return None


def _attempt_request_id(request_id: str, attempt_no: int) -> str:
    if attempt_no <= 1:
        return request_id
    digest = hashlib.sha256(
        f"{request_id}\0{attempt_no}".encode("utf-8")
    ).hexdigest()[:24]
    return f"retry-{digest}"


def _request_payload(body: ChatCompletionRequest, settings: Settings) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    if settings.strict_external_media:
        _enforce_external_media(payload)
    if _payload_has_search_intent(payload):
        # Route explicit and plain-language search requests through ChatGPT's
        # native web search. OpenaiAccount then emits the official search
        # progress as reasoning deltas before the first answer token.
        payload["web_search"] = True
    version = payload.pop("turtle_model_version", None)
    thinking_level = payload.pop("turtle_thinking_level", None)
    for routing_field in (
        "turtle_account_pool_id",
        "turtle_user_id",
        "turtle_chat_id",
        "turtle_request_id",
        "turtle_claude_model",
        "turtle_claude_thinking",
        # Only the Gateway may assign a gpt4free conversation cache key. A
        # caller-supplied value could otherwise collide with another Turtle
        # chat inside the shared worker process.
        "conversation_id",
    ):
        payload.pop(routing_field, None)
    upstream_model, upstream_reasoning_effort = resolve_selection(
        public_model_name=settings.public_model_name,
        default_upstream_model=settings.upstream_model,
        version=version,
        thinking_level=thinking_level,
    )
    payload["model"] = upstream_model
    if version is not None or thinking_level is not None:
        payload.pop("reasoning_effort", None)
        if upstream_reasoning_effort is not None:
            # gpt4free accepts the OpenAI-compatible field name and the
            # reviewed overlay forwards the allowlisted 5.6 values as the
            # ChatGPT private `thinking_effort` field.
            payload["reasoning_effort"] = upstream_reasoning_effort
    return payload


def _claude_request_payload(
    body: ChatCompletionRequest,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    payload = body.model_dump(exclude_none=True)
    if settings.strict_external_media:
        _enforce_external_media(payload)
    if _payload_has_explicit_web_search(payload):
        # The composer toggle makes Claude's native search tool available for
        # the request. Claude then decides whether the prompt benefits from
        # using it; search words in the prompt are not required and cannot
        # silently override a disabled toggle.
        payload["web_search"] = True
    version = str(payload.get("turtle_claude_model") or "").strip()
    level = str(payload.get("turtle_claude_thinking") or "").strip()
    if not version and not level:
        version, level = "claude-sonnet-5", "standard"
        payload["turtle_claude_model"] = version
        payload["turtle_claude_thinking"] = level
    selection_key = f"{version}:{level}"
    if selection_key not in {route.key for route in CLAUDE_ROUTES}:
        raise SelectionError("unsupported Claude model or thinking selection")
    for routing_field in (
        "turtle_account_pool_id",
        "turtle_user_id",
        "turtle_chat_id",
        "turtle_request_id",
        "turtle_model_version",
        "turtle_thinking_level",
        "conversation_id",
    ):
        payload.pop(routing_field, None)
    payload["model"] = settings.claude_public_model_name
    return payload, selection_key


def _derive_upstream_conversation_key(
    gateway_api_key: str,
    pool_id: str,
    turtle_chat_id: str,
) -> str:
    """Return an opaque, stable key for one Turtle chat inside an account pool.

    The account ID is deliberately absent: a soft-affinity migration within
    the same pool keeps the key stable, while process isolation gives each
    account worker a separate key namespace. The raw Turtle chat ID never
    crosses the Gateway boundary.
    """

    material = (
        b"turtle:gpt4free-conversation:v1\0"
        + pool_id.encode("utf-8")
        + b"\0"
        + turtle_chat_id.encode("utf-8")
    )
    digest = hmac.new(
        gateway_api_key.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()
    return f"turtle-v1-{digest}"


def _rewrite_nonstream(payload: dict[str, Any], public_model: str) -> dict[str, Any]:
    normalized = deepcopy(payload)
    normalized["model"] = public_model
    normalized.pop("conversation", None)
    normalized.pop("conversation_id", None)
    normalized.pop("provider", None)
    choices = normalized.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                message["content"] = normalize_markdown_boundaries(content)
    return normalized


def _has_effective_stream_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_effective_stream_value(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key in (
        "content",
        "text",
        "output_text",
        "tool_calls",
        "function_call",
        "image_url",
    ):
        if key in value and _has_effective_stream_value(value[key]):
            return True
    for key in ("choices", "message", "delta", "output"):
        if key in value and _has_effective_stream_value(value[key]):
            return True
    return False


def _sse_data_has_effective_content(data: str) -> bool:
    if data == "[DONE]":
        return False
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return False
    return _has_effective_stream_value(payload)


def _sse_data_has_error(data: str) -> bool:
    if data == "[DONE]":
        return False
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and bool(payload.get("error"))


def _sse_data_has_answer_text(data: str) -> bool:
    if data == "[DONE]":
        return False
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content.strip():
            return True
    return False


def _mock_nonstream(body: ChatCompletionRequest, settings: Settings) -> dict[str, Any]:
    answer = mock_answer(body.messages)
    return {
        "id": f"chatcmpl-web-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _mock_stream(body: ChatCompletionRequest, settings: Settings) -> AsyncIterator[bytes]:
    completion_id = f"chatcmpl-web-{uuid.uuid4().hex}"
    created = int(time.time())

    def event(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        value = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": body.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()

    yield event({"role": "assistant", "content": ""})
    for part in chunks(mock_answer(body.messages)):
        yield event({"content": part})
    yield event({}, "stop")
    yield b"data: [DONE]\n\n"


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    login_control_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()
    install_redaction_filter()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        # Uvicorn applies its logging configuration after the module is imported.
        # Reinstall filters here so newly configured handlers redact secrets too.
        install_redaction_filter()
        logger.disabled = False
        logger.setLevel(logging.INFO)
        application.state.settings = resolved
        application.state.account_pool = build_account_pool(
            enabled=resolved.account_pool_enabled,
            claude_enabled=resolved.claude_pool_enabled,
            database_url=resolved.account_pool_database_url,
            default_pool_id=resolved.default_account_pool_id,
            default_claude_pool_id=resolved.default_claude_account_pool_id,
            upstream_base_url=resolved.upstream_base_url,
            upstream_health_path=resolved.upstream_health_path,
            claude_upstream_base_url=resolved.claude_upstream_base_url,
            claude_upstream_health_path=resolved.claude_upstream_health_path,
            upstream_api_key=resolved.upstream_api_key,
            upstream_timeout_seconds=resolved.upstream_timeout_seconds,
            lease_seconds=resolved.account_lease_seconds,
            cooldown_seconds=resolved.account_cooldown_seconds,
            recovery_poll_seconds=resolved.account_recovery_poll_seconds,
            allowed_hosts=resolved.account_allowed_hosts,
            transport=upstream_transport,
        )
        application.state.login_control = LoginControlClient(
            base_url=resolved.login_control_url,
            secret_path=resolved.login_control_secret_file,
            transport=login_control_transport,
        )
        shared_database_pool = getattr(
            application.state.account_pool.store,
            "connection_pool",
            None,
        )
        application.state.upstream_cleanup = UpstreamCleanupManager(
            database_url=resolved.account_pool_database_url,
            account_pool=application.state.account_pool,
            enabled=resolved.upstream_cleanup_enabled,
            execute=resolved.upstream_cleanup_execute,
            ttl_seconds=resolved.upstream_cleanup_ttl_seconds,
            conversation_action=(
                resolved.upstream_cleanup_conversation_action
            ),
            interval_seconds=resolved.upstream_cleanup_interval_seconds,
            batch_size=resolved.upstream_cleanup_batch_size,
            connection_pool=shared_database_pool,
        )
        application.state.project_usage = build_project_usage(
            database_url=resolved.account_pool_database_url,
            master_key=resolved.gateway_api_key,
            connection_pool=shared_database_pool,
        )
        await application.state.account_pool.start()
        await application.state.project_usage.start()
        await application.state.upstream_cleanup.start()
        try:
            yield
        finally:
            await application.state.upstream_cleanup.close()
            await application.state.login_control.close()
            await application.state.project_usage.close()
            await application.state.account_pool.close()

    application = FastAPI(title="ChatGPT Web Gateway PoC", version="0.1.0", lifespan=lifespan)

    async def require_key(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
            supplied, resolved.gateway_api_key
        ):
            raise HTTPException(
                status_code=401,
                detail={"message": "invalid API key", "type": "authentication_error"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def require_call_key(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> ProjectCaller:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and supplied:
            if hmac.compare_digest(supplied, resolved.gateway_api_key):
                return ProjectCaller(
                    key_id=None,
                    owner_user_id=None,
                    project_name="Gateway internal",
                    key_prefix="master",
                    is_master=True,
                )
            caller = await request.app.state.project_usage.authenticate(supplied)
            if caller is not None:
                return caller
        raise HTTPException(
            status_code=401,
            detail={"message": "invalid API key", "type": "authentication_error"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @application.get("/")
    async def root() -> dict[str, str]:
        return {"service": "chatgpt-web-gateway-poc", "status": "ok"}

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, Any]:
        upstream_ok = None
        claude_upstream_ok = None
        account_pool: dict[str, Any] | None = None
        if resolved.backend == "upstream":
            gpt_probe = await request.app.state.account_pool.probe_pool(
                resolved.default_account_pool_id
            )
            claude_probe = (
                await request.app.state.account_pool.probe_pool(
                    resolved.default_claude_account_pool_id
                )
                if resolved.claude_pool_enabled
                else {"ok": False, "items": []}
            )
            upstream_ok = bool(gpt_probe["ok"])
            claude_upstream_ok = bool(claude_probe["ok"])
            snapshot = await request.app.state.account_pool.snapshot()
            account_pool = {
                "enabled": bool(snapshot["enabled"]),
                "backend": snapshot["backend"],
                "pool_count": len(snapshot["pools"]),
                "account_count": len(snapshot["accounts"]),
                "ready_count": sum(1 for item in snapshot["accounts"] if item["available"]),
                "active": sum(int(item["active"]) for item in snapshot["accounts"]),
            }
        return {
            "ok": resolved.backend == "mock" or upstream_ok is True,
            "backend": resolved.backend,
            "upstream_reachable": upstream_ok,
            "public_model": resolved.public_model_name,
            "public_models": [resolved.public_model_name]
            + (
                [resolved.claude_public_model_name]
                if resolved.claude_pool_enabled
                else []
            ),
            "providers": {
                "gpt": {
                    "ok": resolved.backend == "mock" or upstream_ok is True,
                    "public_model": resolved.public_model_name,
                    "status": (
                        "ready"
                        if upstream_ok
                        else str(
                            (gpt_probe.get("items") or [{}])[0].get("state")
                            or "degraded"
                        )
                    )
                    if resolved.backend == "upstream"
                    else "ready",
                },
                "claude": {
                    "ok": resolved.claude_pool_enabled
                    and (resolved.backend == "mock" or claude_upstream_ok is True),
                    "public_model": resolved.claude_public_model_name,
                    "status": (
                        "ready"
                        if claude_upstream_ok
                        else str(
                            (claude_probe.get("items") or [{}])[0].get("state")
                            or "degraded"
                        )
                    )
                    if resolved.backend == "upstream"
                    and resolved.claude_pool_enabled
                    else "ready",
                    "verified_route_count": (
                        len(CLAUDE_ROUTES) if claude_upstream_ok else 0
                    ),
                },
            },
            "strict_external_media": resolved.strict_external_media,
            "account_pool": account_pool,
        }

    def account_error(exc: Exception, status_code: int = 400) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": str(exc),
                    "type": "account_pool_error",
                    "param": None,
                    "code": None,
                }
            },
        )

    def login_error(exc: Exception) -> JSONResponse:
        status_code = (
            503
            if isinstance(exc, LoginControlUnavailable)
            else 409
            if isinstance(exc, (LoginControlError, LoginRuntimeMissing))
            else 400
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": str(exc),
                    "type": "account_login_error",
                    "param": None,
                    "code": None,
                }
            },
        )

    async def account_pool_snapshot(request: Request) -> dict[str, Any]:
        snapshot = await request.app.state.account_pool.snapshot()
        accounts = snapshot.get("accounts", [])
        runtimes = await asyncio.gather(
            *(request.app.state.login_control.status(str(item["id"])) for item in accounts),
            return_exceptions=True,
        )
        for account, runtime in zip(accounts, runtimes, strict=True):
            account["login_runtime"] = (
                runtime
                if isinstance(runtime, dict)
                else {
                    "account_id": account["id"],
                    "configured": False,
                    "control_state": "unavailable",
                }
            )
        snapshot["login_control"] = {
            "configured": bool(request.app.state.login_control.enabled),
        }
        return snapshot

    @application.get(
        "/internal/upstream-cleanup/status",
        dependencies=[Depends(require_key)],
    )
    async def upstream_cleanup_status(request: Request) -> dict[str, Any]:
        return await request.app.state.upstream_cleanup.status()

    @application.put(
        "/internal/upstream-cleanup/config",
        dependencies=[Depends(require_key)],
    )
    async def update_upstream_cleanup_config(
        payload: dict[str, Any],
        request: Request,
    ):
        retention_seconds = payload.get("retention_seconds")
        conversation_action = payload.get("conversation_action")
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or not 300 <= retention_seconds <= 365 * 24 * 60 * 60
        ):
            return _error(
                400,
                "保留时长必须在 5 分钟至 365 天之间",
                "invalid_request_error",
            )
        if conversation_action not in {"archive", "delete"}:
            return _error(
                400,
                "上游对话处理方式只支持 archive 或 delete",
                "invalid_request_error",
            )
        try:
            return await request.app.state.upstream_cleanup.update_policy(
                retention_seconds=retention_seconds,
                conversation_action=conversation_action,
                updated_by="admin_console",
            )
        except RuntimeError as exc:
            return _error(503, str(exc), "service_unavailable")

    @application.post(
        "/internal/upstream-cleanup/schedule",
        dependencies=[Depends(require_key)],
    )
    async def schedule_upstream_cleanup(
        payload: dict[str, Any],
        request: Request,
    ):
        chat_id = payload.get("chat_id")
        user_id = payload.get("user_id")
        if chat_id is not None and (
            not isinstance(chat_id, str) or not 1 <= len(chat_id) <= 200
        ):
            return _error(400, "chat_id 无效", "invalid_request_error")
        if user_id is not None and (
            not isinstance(user_id, str) or not 1 <= len(user_id) <= 200
        ):
            return _error(400, "user_id 无效", "invalid_request_error")
        if not chat_id and not user_id:
            return _error(
                400,
                "必须提供 chat_id 或 user_id",
                "invalid_request_error",
            )
        count = await request.app.state.upstream_cleanup.schedule(
            chat_id=chat_id,
            user_id=user_id,
            reason=(
                "local_chat_deleted"
                if chat_id
                else "local_user_chats_deleted"
            ),
        )
        return {"ok": True, "scheduled": count}

    @application.post(
        "/internal/upstream-cleanup/run",
        dependencies=[Depends(require_key)],
    )
    async def run_upstream_cleanup(request: Request) -> dict[str, Any]:
        return await request.app.state.upstream_cleanup.run_once()

    @application.get("/internal/account-pools", dependencies=[Depends(require_key)])
    async def account_pools(request: Request) -> dict[str, Any]:
        return await account_pool_snapshot(request)

    @application.get(
        "/internal/account-pools/{pool_id}/capacity",
        dependencies=[Depends(require_key)],
    )
    async def account_pool_capacity(
        pool_id: str,
        request: Request,
        selection_key: str | None = None,
    ):
        try:
            return await request.app.state.account_pool.capacity(pool_id, selection_key)
        except AccountPoolConflict as exc:
            return account_error(exc, 404)

    @application.post("/internal/account-pools", dependencies=[Depends(require_key)])
    async def create_account_pool(form: AccountPoolForm, request: Request):
        try:
            return await request.app.state.account_pool.create_pool(
                provider=form.provider,
                name=form.name,
                description=form.description,
            )
        except AccountPoolConflict as exc:
            return account_error(exc)

    @application.put(
        "/internal/account-pools/{pool_id}", dependencies=[Depends(require_key)]
    )
    async def update_account_pool(pool_id: str, form: AccountPoolForm, request: Request):
        try:
            return await request.app.state.account_pool.update_pool(
                pool_id,
                name=form.name,
                description=form.description,
                enabled=form.enabled,
            )
        except AccountPoolConflict as exc:
            return account_error(exc)

    @application.delete(
        "/internal/account-pools/{pool_id}", dependencies=[Depends(require_key)]
    )
    async def delete_account_pool(pool_id: str, request: Request):
        try:
            return await request.app.state.account_pool.delete_pool(pool_id)
        except AccountPoolConflict as exc:
            return account_error(exc, 409)

    @application.post(
        "/internal/account-pools/{pool_id}/accounts", dependencies=[Depends(require_key)]
    )
    async def create_account(pool_id: str, form: AccountForm, request: Request):
        try:
            return await request.app.state.account_pool.create_account(
                pool_id=pool_id,
                name=form.name,
                worker_endpoint=form.worker_endpoint,
                health_path=form.health_path,
                max_concurrency=form.max_concurrency,
                priority=form.priority,
                quota_profile=form.quota_profile,
            )
        except AccountPoolConflict as exc:
            return account_error(exc)

    @application.post(
        "/internal/account-pools/{pool_id}/accounts/onboard",
        dependencies=[Depends(require_key)],
    )
    async def onboard_account(
        pool_id: str,
        form: AccountOnboardForm,
        request: Request,
    ):
        """Create a private runtime and immediately open its manual login window."""

        account_id = f"acct-{uuid.uuid4().hex[:12]}"
        runtime_created = False
        try:
            snapshot = await request.app.state.account_pool.snapshot()
            target_pool = next(
                (
                    item
                    for item in snapshot.get("pools", [])
                    if str(item.get("id")) == pool_id
                ),
                None,
            )
            if target_pool is None:
                raise AccountPoolConflict("账号池不存在")
            provider = str(target_pool.get("provider") or "")
            runtime = await request.app.state.login_control.provision(
                account_id,
                provider=provider,
            )
            runtime_created = True
            worker_port = int(runtime.get("worker_port") or 0)
            if not 1 <= worker_port <= 65535:
                raise LoginControlError("登录控制返回了无效的账号 worker")
            account = await request.app.state.account_pool.create_account(
                account_id=account_id,
                pool_id=pool_id,
                name=form.name,
                worker_endpoint=f"http://host.docker.internal:{worker_port}/v1",
                health_path=(
                    "/healthz"
                    if provider == "claude"
                    else "/api/OpenaiAccount/quota"
                ),
                max_concurrency=1,
                priority=0,
                quota_profile="untracked",
            )
        except AccountPoolConflict as exc:
            if runtime_created:
                with contextlib.suppress(LoginControlError):
                    await request.app.state.login_control.rollback_provision(account_id)
            return account_error(exc)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)

        try:
            account = await request.app.state.account_pool.begin_reauth(account_id)
            runtime = await request.app.state.login_control.open(account_id)
        except AccountPoolConflict as exc:
            return account_error(exc, 409)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)
        return {
            "account_id": account_id,
            "pool_id": pool_id,
            "name": account.get("name") or form.name,
            "state": "waiting_for_login",
            "account_status": account.get("status"),
            "runtime": runtime,
        }

    @application.put("/internal/accounts/{account_id}", dependencies=[Depends(require_key)])
    async def update_account(account_id: str, form: AccountForm, request: Request):
        try:
            return await request.app.state.account_pool.update_account(
                account_id,
                name=form.name,
                worker_endpoint=form.worker_endpoint,
                health_path=form.health_path,
                max_concurrency=form.max_concurrency,
                priority=form.priority,
                enabled=form.enabled,
                quota_profile=form.quota_profile,
            )
        except AccountPoolConflict as exc:
            return account_error(exc)

    @application.put(
        "/internal/accounts/{account_id}/settings",
        dependencies=[Depends(require_key)],
    )
    async def update_account_settings(
        account_id: str,
        form: AccountSettingsForm,
        request: Request,
    ):
        snapshot = await request.app.state.account_pool.snapshot()
        current = next(
            (
                item
                for item in snapshot.get("accounts", [])
                if str(item.get("id")) == account_id
            ),
            None,
        )
        if current is None:
            return account_error(AccountPoolConflict("账号不存在"), 404)
        try:
            return await request.app.state.account_pool.update_account(
                account_id,
                name=form.name,
                worker_endpoint=str(current.get("worker_endpoint") or ""),
                health_path=current.get("health_path"),
                max_concurrency=(
                    int(form.max_concurrency)
                    if form.max_concurrency is not None
                    else int(current.get("max_concurrency") or 1)
                ),
                priority=int(current.get("priority") or 0),
                enabled=form.enabled,
                quota_profile=form.quota_profile
                or str(current.get("quota_profile") or "untracked"),
            )
        except AccountPoolConflict as exc:
            return account_error(exc)

    @application.post(
        "/internal/accounts/{account_id}/runtime/prepare",
        dependencies=[Depends(require_key)],
    )
    async def prepare_account_runtime(account_id: str, request: Request):
        pool = request.app.state.account_pool
        snapshot = await pool.snapshot()
        current = next(
            (
                item
                for item in snapshot.get("accounts", [])
                if str(item.get("id")) == account_id
            ),
            None,
        )
        if current is None:
            return account_error(AccountPoolConflict("账号不存在"), 404)
        if current.get("deployment_managed"):
            return login_error(LoginControlError("主账号运行时由部署管理，不能自动替换"))
        try:
            provider = str(current.get("provider") or "gpt")
            runtime = await request.app.state.login_control.provision(
                account_id,
                provider=provider,
            )
            worker_port = int(runtime.get("worker_port") or 0)
            if not 1 <= worker_port <= 65535:
                raise LoginControlError("登录控制返回了无效的账号 worker")
            await pool.update_account(
                account_id,
                name=str(current.get("name") or account_id),
                worker_endpoint=f"http://host.docker.internal:{worker_port}/v1",
                health_path=(
                    "/healthz"
                    if provider == "claude"
                    else "/api/OpenaiAccount/quota"
                ),
                max_concurrency=int(current.get("max_concurrency") or 1),
                priority=int(current.get("priority") or 0),
                enabled=False,
            )
            account = await pool.begin_reauth(account_id)
            runtime = await request.app.state.login_control.open(account_id)
            return {
                "account_id": account_id,
                "state": "waiting_for_login",
                "account_status": account.get("status"),
                "runtime": runtime,
            }
        except AccountPoolConflict as exc:
            return account_error(exc, 409)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)

    @application.post(
        "/internal/accounts/{account_id}/probe", dependencies=[Depends(require_key)]
    )
    async def probe_account(account_id: str, request: Request):
        try:
            return await request.app.state.account_pool.probe_account(account_id)
        except AccountPoolConflict as exc:
            return account_error(exc, 404)

    @application.post(
        "/internal/accounts/{account_id}/reauth/start",
        dependencies=[Depends(require_key)],
    )
    async def start_account_reauth(account_id: str, request: Request):
        try:
            runtime = await request.app.state.login_control.status(account_id)
            if not runtime.get("configured"):
                if runtime.get("control_state") == "unavailable":
                    raise LoginControlUnavailable("登录控制服务暂时不可达")
                raise LoginRuntimeMissing("账号没有可用的独立登录运行时")
            account = await request.app.state.account_pool.begin_reauth(account_id)
            runtime = await request.app.state.login_control.open(account_id)
            return {
                "account_id": account_id,
                "state": "waiting_for_login",
                "account_status": account.get("status"),
                "runtime": runtime,
            }
        except AccountPoolConflict as exc:
            return account_error(exc, 409)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)

    @application.post(
        "/internal/accounts/{account_id}/reauth/cancel",
        dependencies=[Depends(require_key)],
    )
    async def cancel_account_reauth(account_id: str, request: Request):
        try:
            await request.app.state.account_pool.begin_reauth(account_id)
            runtime = await request.app.state.login_control.cancel(account_id)
            return {
                "account_id": account_id,
                "state": "reauth_required",
                "runtime": runtime,
            }
        except AccountPoolConflict as exc:
            return account_error(exc, 409)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)

    @application.post(
        "/internal/accounts/{account_id}/reauth/verify",
        dependencies=[Depends(require_key)],
    )
    async def verify_account_reauth(account_id: str, request: Request):
        pool = request.app.state.account_pool
        try:
            await pool.begin_reauth(account_id)
            account = await asyncio.to_thread(pool.store.account, account_id)
            provider_label = (
                "Claude"
                if account is not None and account.provider == "claude"
                else "ChatGPT"
            )
            runtime = await request.app.state.login_control.status(account_id)
            browser_state = runtime.get("browser_state")
            if account is not None and account.provider == "claude":
                if browser_state not in {"manual", "ready"}:
                    raise LoginControlError(
                        "Claude 手工登录窗口未打开，请先发起重新登录"
                    )
                runtime = await request.app.state.login_control.prepare_capture(
                    account_id
                )
                browser_state = runtime.get("browser_state")
            if browser_state != "ready":
                raise LoginControlError("专用登录窗口未打开，请先发起重新登录")
            captured = await pool.capture_account_auth(account_id)
            if not captured.get("ok"):
                raise LoginControlError(
                    f"未能从安全登录页捕获登录状态；请确认已进入 {provider_label} 首页后重试"
                )
            live = await pool.probe_account(account_id, persist=False)
            if not live.get("ok"):
                raise LoginControlError(
                    "登录状态已捕获，但真实账号检查未通过；请稍后重试"
                )
            await request.app.state.login_control.restart(account_id)
            final: dict[str, Any] | None = None
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(1)
                final = await pool.probe_account(account_id, persist=False)
                if final.get("ok"):
                    break
            assert final is not None
            await asyncio.to_thread(
                pool.store.mark_probe,
                account_id,
                state=final["state"],
                http_status=final.get("http_status"),
                latency_ms=int(final.get("latency_ms") or 0),
                upstream_display_name=final.get("upstream_display_name"),
                allow_reauth=True,
            )
            if not final.get("ok"):
                raise LoginControlError(
                    "登录已捕获，但账号 worker 重启后的真实检查未通过"
                )
            return {
                "account_id": account_id,
                "state": "ready",
                "ok": True,
                "latency_ms": int(final.get("latency_ms") or 0),
                "upstream_display_name": final.get("upstream_display_name"),
            }
        except AccountPoolConflict as exc:
            return account_error(exc, 409)
        except (LoginControlError, LoginControlUnavailable, LoginRuntimeMissing) as exc:
            return login_error(exc)

    @application.post(
        "/internal/account-pools/{pool_id}/probe", dependencies=[Depends(require_key)]
    )
    async def probe_account_pool(pool_id: str, request: Request):
        try:
            return await request.app.state.account_pool.probe_pool(pool_id)
        except AccountPoolConflict as exc:
            return account_error(exc, 404)

    @application.post(
        "/internal/project-api/keys",
        dependencies=[Depends(require_key)],
    )
    async def create_project_api_key(
        form: ProjectApiKeyForm,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.create_key(
                form.owner_user_id,
                form.name,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ProjectKeyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get(
        "/internal/project-api/config",
        dependencies=[Depends(require_key)],
    )
    async def project_api_pricing_config(request: Request) -> dict[str, Any]:
        return await request.app.state.project_usage.pricing_config()

    @application.put(
        "/internal/project-api/config",
        dependencies=[Depends(require_key)],
    )
    async def update_project_api_pricing_config(
        form: ProjectApiPricingConfigForm,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.set_pricing_config(
                form.cost_multiplier,
                updated_by=form.updated_by,
            )
        except ProjectKeyConflict as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get(
        "/internal/project-api/keys",
        dependencies=[Depends(require_key)],
    )
    async def list_project_api_keys(
        request: Request,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "items": await request.app.state.project_usage.list_keys(
                owner_user_id
            )
        }

    @application.delete(
        "/internal/project-api/keys/{key_id}",
        dependencies=[Depends(require_key)],
    )
    async def revoke_project_api_key(
        key_id: str,
        request: Request,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.revoke_key(
                key_id,
                owner_user_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目密钥不存在") from exc

    @application.get(
        "/internal/project-api/permissions",
        dependencies=[Depends(require_key)],
    )
    async def project_api_permissions(request: Request) -> dict[str, Any]:
        return {
            "items": await request.app.state.project_usage.permissions()
        }

    @application.get(
        "/internal/project-api/permissions/{user_id}",
        dependencies=[Depends(require_key)],
    )
    async def project_api_permission(
        user_id: str,
        request: Request,
    ) -> dict[str, Any]:
        return await request.app.state.project_usage.permission(user_id)

    @application.put(
        "/internal/project-api/permissions/{user_id}",
        dependencies=[Depends(require_key)],
    )
    async def update_project_api_permission(
        user_id: str,
        form: ProjectApiPermissionForm,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.set_permission(
                user_id,
                enabled=form.enabled,
                updated_by=form.updated_by,
                max_keys=form.max_keys,
            )
        except ProjectKeyConflict as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.delete(
        "/internal/project-api/permissions/{user_id}",
        dependencies=[Depends(require_key)],
    )
    async def delete_project_api_permission(
        user_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.delete_permission(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目 API 权限不存在") from exc
        except ProjectKeyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/internal/project-api/permissions/{user_id}/credits",
        dependencies=[Depends(require_key)],
    )
    async def grant_project_api_credit(
        user_id: str,
        form: ProjectApiCreditGrantForm,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await request.app.state.project_usage.grant_credit(
                user_id,
                form.amount_microusd,
                reason=form.reason,
                idempotency_key=form.idempotency_key,
                updated_by=form.updated_by,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目 API 权限不存在") from exc
        except ProjectKeyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get(
        "/internal/project-api/permissions/{user_id}/credits",
        dependencies=[Depends(require_key)],
    )
    async def project_api_credit_ledger(
        user_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="额度流水分页参数无效")
        permission = await request.app.state.project_usage.permission(user_id)
        if permission.get("created_at") is None:
            raise HTTPException(status_code=404, detail="项目 API 权限不存在")
        return {
            "balance_microusd": permission.get("balance_microusd"),
            "reserved_microusd": int(permission.get("reserved_microusd") or 0),
            "items": await request.app.state.project_usage.credit_ledger(
                user_id,
                limit=limit,
            ),
        }

    @application.get(
        "/internal/project-api/usage",
        dependencies=[Depends(require_key)],
    )
    async def project_api_usage(
        request: Request,
        hours: int = 24,
        owner_user_id: str | None = None,
        key_id: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if hours not in {1, 6, 24, 168, 720}:
            raise HTTPException(
                status_code=422,
                detail="用量范围只支持 1、6、24、168 或 720 小时",
            )
        if outcome not in {None, "success", "error", "cancelled"}:
            raise HTTPException(status_code=422, detail="调用状态筛选值无效")
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=422, detail="调用记录分页参数无效")
        return await request.app.state.project_usage.summary(
            hours,
            owner_user_id=owner_user_id,
            key_id=key_id,
            model=model,
            outcome=outcome,
            limit=limit,
            offset=offset,
        )

    @application.get("/v1/models")
    async def models(
        caller: ProjectCaller = Depends(require_call_key),
    ) -> dict[str, Any]:
        data = [model_metadata(resolved.public_model_name)]
        if caller.is_master and resolved.claude_pool_enabled:
            data.append(
                claude_model_metadata(
                    resolved.claude_public_model_name,
                    tuple(route.key for route in CLAUDE_ROUTES),
                )
            )
        return {
            "object": "list",
            "data": data,
        }

    @application.get("/v1/project/context")
    async def project_context(
        caller: ProjectCaller = Depends(require_call_key),
    ) -> dict[str, Any]:
        """Return the minimal caller scope needed by the loopback media proxy.

        This route is not published by Open WebUI. It deliberately excludes
        key material, balances, permissions, prompts, and Provider identity.
        Authentication is performed on every request so revocation remains
        immediate.
        """

        return {
            "object": "project.context",
            "project_key_id": caller.key_id,
            "owner_user_id": caller.owner_user_id,
            "is_master": caller.is_master,
        }

    @application.post("/v1/images/generations")
    async def image_generations(
        body: ImageGenerationRequest,
        request: Request,
        caller: ProjectCaller = Depends(require_call_key),
    ):
        started = time.monotonic()
        if not caller.is_master:
            return _error(
                403,
                "项目 API 暂未开放图片生成",
                "project_api_forbidden",
            )
        if not body.turtle_user_id:
            return _error(
                400,
                "图片生成缺少用户路由信息",
                "invalid_request_error",
            )
        required_profiles = frozenset(body.turtle_required_quota_profiles)
        if not required_profiles:
            return _error(
                400,
                "图片生成缺少独立套餐路由",
                "invalid_request_error",
            )
        request_id = body.turtle_request_id or f"img-{uuid.uuid4().hex[:20]}"
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,64}", request_id):
            return _error(
                400,
                "图片请求标识无效",
                "invalid_request_error",
            )

        pool_id = body.turtle_account_pool_id or resolved.default_account_pool_id
        upstream_payload = body.model_dump(
            mode="json",
            exclude_none=True,
        )
        for field in (
            "turtle_account_pool_id",
            "turtle_user_id",
            "turtle_chat_id",
            "turtle_request_id",
            "turtle_required_quota_profiles",
        ):
            upstream_payload.pop(field, None)
        upstream_payload["n"] = 1
        upstream_payload["response_format"] = "url"

        generated: list[dict[str, Any]] = []
        last_failure: tuple[int, str] | None = None
        total_attempts = 0
        logger.info(
            "image_request_route id=%s official_tasks=1 chat_bound=%s profiles=%s",
            request_id,
            int(bool(body.turtle_chat_id)),
            ",".join(sorted(required_profiles)),
        )
        attempted_account_ids: set[str] = set()
        for attempt_no in range(
            1,
            resolved.account_failover_max_attempts + 1,
        ):
            total_attempts += 1
            attempt_id = f"{request_id}-{attempt_no}"[:64]
            try:
                account_lease = await request.app.state.account_pool.acquire(
                    pool_id=pool_id,
                    request_id=attempt_id,
                    user_id=body.turtle_user_id,
                    chat_id=body.turtle_chat_id,
                    selection_key="image:create",
                    excluded_account_ids=frozenset(attempted_account_ids),
                    required_quota_profiles=required_profiles,
                    migration_reason_hint="image_profile_route",
                )
            except AccountUnavailable as exc:
                last_failure = (503, str(exc))
                break
            attempted_account_ids.add(account_lease.account.id)
            payload = dict(upstream_payload)
            if body.turtle_chat_id:
                payload["conversation_id"] = (
                    _derive_upstream_conversation_key(
                        resolved.gateway_api_key,
                        account_lease.account.pool_id,
                        body.turtle_chat_id,
                    )
                )
            try:
                upstream = await request.app.state.account_pool.client_for(
                    account_lease.account
                )
                result = await upstream.image_generation(payload)
                rows = result.get("data")
                if not isinstance(rows, list) or not rows:
                    raise UpstreamFailure(
                        502,
                        "上游返回空图片结果",
                    )
                generated = [
                    dict(image)
                    for image in rows
                    if isinstance(image, dict) and image.get("url")
                ]
                if not generated:
                    raise UpstreamFailure(
                        502,
                        "上游图片结果无效",
                    )
            except asyncio.CancelledError:
                await account_lease.release(
                    outcome="cancelled",
                    status_code=499,
                    error_class="client_cancelled",
                )
                raise
            except UpstreamFailure as exc:
                failover_reason = _safe_failover_reason(exc.status_code)
                await account_lease.release(
                    outcome="error",
                    status_code=exc.status_code,
                    error_class=failover_reason or "image_upstream",
                    retry_after_seconds=exc.retry_after_seconds,
                )
                last_failure = (exc.status_code, exc.message)
                if failover_reason is not None:
                    logger.warning(
                        "image_request_failover id=%s attempt=%d account=%s reason=%s status=%d",
                        request_id,
                        attempt_no,
                        account_lease.account.id,
                        failover_reason,
                        exc.status_code,
                    )
                    continue
                break
            except Exception:
                await account_lease.release(
                    outcome="error",
                    status_code=502,
                    error_class="image_upstream",
                )
                last_failure = (502, "上游图片生成失败")
                break

            await account_lease.release(
                outcome="success",
                status_code=200,
            )
            logger.info(
                "image_task_completed id=%s assets=%d account=%s",
                request_id,
                len(generated),
                account_lease.account.id,
            )
            break

        if not generated:
            status_code, message = last_failure or (502, "上游图片生成失败")
            logger.warning(
                "image_request_failed id=%s status=%d assets=0 attempts=%d",
                request_id,
                status_code,
                total_attempts,
            )
            return _error(status_code, message, "upstream_error")
        response: dict[str, Any] = {
            "created": int(time.time()),
            "data": generated,
        }
        logger.info(
            "image_request_completed id=%s official_tasks=1 assets=%d attempts=%d total_ms=%d",
            request_id,
            len(generated),
            total_attempts,
            int((time.monotonic() - started) * 1000),
        )
        return response

    @application.post("/v1/chat/completions")
    async def completions(
        body: ChatCompletionRequest,
        request: Request,
        caller: ProjectCaller = Depends(require_call_key),
    ):
        started = time.monotonic()
        header_request_id = str(request.headers.get("idempotency-key") or "").strip()
        if header_request_id and (
            not 8 <= len(header_request_id) <= 128
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                for character in header_request_id
            )
        ):
            return _error(
                400,
                "Idempotency-Key 必须是 8 到 128 位 URL 安全字符",
                "invalid_request_error",
            )
        supplied_request_id = body.turtle_request_id or header_request_id or None
        request_id = supplied_request_id or uuid.uuid4().hex[:12]
        estimated_prompt_tokens = estimate_chat_input_tokens(
            [message.model_dump(mode="json") for message in body.messages]
        )

        async def authorize_project_request(
            route: str | None,
        ) -> JSONResponse | None:
            if caller.is_master or caller.key_id is None:
                return None
            extra = body.model_extra or {}
            requested_output = extra.get(
                "max_completion_tokens",
                extra.get("max_tokens", 32768),
            )
            try:
                output_tokens = max(1, min(int(requested_output), 32768))
            except (TypeError, ValueError, OverflowError):
                output_tokens = 32768
            official_authorization = simulated_cost(
                price=price_for_route(route),
                input_tokens=max(1, estimated_prompt_tokens),
                output_tokens=output_tokens,
            )
            official_authorization = max(
                1,
                (official_authorization * 120 + 99) // 100,
            )
            try:
                await request.app.state.project_usage.begin_request(
                    caller.key_id,
                    request_id,
                    authorization_microusd=official_authorization,
                )
            except ProjectRequestConflict as exc:
                return _error(409, str(exc), "idempotency_conflict")
            except ProjectBalanceInsufficient as exc:
                return _error(402, str(exc), "insufficient_credit")
            except (KeyError, PermissionError) as exc:
                return _error(403, str(exc), "project_api_forbidden")
            return None

        async def record_project_usage(
            *,
            provider: str,
            route: str | None,
            outcome: str,
            status_code: int,
            payload: dict[str, Any] | None = None,
            fallback_points: int = 1,
            estimated_completion_token_count: int | None = None,
        ) -> None:
            if caller.is_master or caller.key_id is None:
                return
            amounts = extract_usage(
                payload,
                route=route,
                fallback_points=fallback_points,
                estimated_prompt_tokens=estimated_prompt_tokens,
                estimated_completion_token_count=estimated_completion_token_count,
            )
            try:
                await request.app.state.project_usage.record(
                    request_id=request_id,
                    key_id=caller.key_id,
                    provider=provider,
                    model=body.model,
                    route=route,
                    stream=body.stream,
                    outcome=outcome,
                    status_code=status_code,
                    prompt_tokens=amounts.prompt_tokens,
                    cached_tokens=amounts.cached_tokens,
                    cache_write_tokens=amounts.cache_write_tokens,
                    completion_tokens=amounts.completion_tokens,
                    total_tokens=amounts.total_tokens,
                    usage_source=amounts.source,
                    points=amounts.points,
                    pricing_profile=amounts.pricing_profile,
                    price_card_version=amounts.price_card_version,
                    input_rate_nano_usd=amounts.input_rate_nano_usd,
                    cached_input_rate_nano_usd=amounts.cached_input_rate_nano_usd,
                    cache_write_rate_nano_usd=amounts.cache_write_rate_nano_usd,
                    output_rate_nano_usd=amounts.output_rate_nano_usd,
                    official_cost_microusd=amounts.official_cost_microusd,
                    latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
            except Exception:
                logger.warning(
                    "project_usage_record_failed id=%s project_key=%s",
                    request_id,
                    caller.key_prefix,
                )

        if (
            not caller.is_master
            and caller.key_id is not None
            and supplied_request_id is None
        ):
            return _error(
                400,
                "项目 API 的每个调用都必须提供 Idempotency-Key",
                "idempotency_key_required",
            )

        allowed_models = {resolved.public_model_name}
        if caller.is_master and resolved.claude_pool_enabled:
            allowed_models.add(resolved.claude_public_model_name)
        if body.model not in allowed_models:
            await record_project_usage(
                provider="unknown",
                route=None,
                outcome="error",
                status_code=404,
                fallback_points=0,
            )
            return _error(404, f"unknown model: {body.model}", "invalid_request_error")
        provider = (
            "claude"
            if body.model == resolved.claude_public_model_name
            else "gpt"
        )
        public_model = (
            resolved.claude_public_model_name
            if provider == "claude"
            else resolved.public_model_name
        )
        default_pool_id = (
            resolved.default_claude_account_pool_id
            if provider == "claude"
            else resolved.default_account_pool_id
        )

        logger.info(
            "request_started id=%s stream=%s backend=%s tool_count=%d",
            request_id,
            body.stream,
            resolved.backend,
            len(getattr(body, "tools", None) or []),
        )

        if resolved.backend == "mock":
            authorization_error = await authorize_project_request("mock")
            if authorization_error is not None:
                return authorization_error
            if body.stream:
                async def metered_mock_stream() -> AsyncIterator[bytes]:
                    outcome = "success"
                    status_code = 200
                    try:
                        async for chunk in _mock_stream(body, resolved):
                            yield chunk
                    except (asyncio.CancelledError, GeneratorExit):
                        outcome = "cancelled"
                        status_code = 499
                        raise
                    finally:
                        await record_project_usage(
                            provider=provider,
                            route="mock",
                            outcome=outcome,
                            status_code=status_code,
                        )

                return StreamingResponse(
                    metered_mock_stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            result = _mock_nonstream(body, resolved)
            await record_project_usage(
                provider=provider,
                route="mock",
                outcome="success",
                status_code=200,
                payload=result,
            )
            logger.info(
                "request_completed id=%s total_ms=%d",
                request_id,
                int((time.monotonic() - started) * 1000),
            )
            return result

        try:
            if provider == "claude":
                payload, selection_key = _claude_request_payload(body, resolved)
            else:
                payload = _request_payload(body, resolved)
                selection_key = resolve_selection_key(
                    public_model_name=resolved.public_model_name,
                    version=body.turtle_model_version,
                    thinking_level=body.turtle_thinking_level,
                )
        except SelectionError as exc:
            logger.warning("request_failed id=%s status=400", request_id)
            await record_project_usage(
                provider=provider,
                route=None,
                outcome="error",
                status_code=400,
                fallback_points=0,
            )
            return _error(400, str(exc), "invalid_request_error")
        if (
            not caller.is_master
            and resolved.strict_external_media
            and _has_unsealed_image_url(payload)
        ):
            await record_project_usage(
                provider=provider,
                route=selection_key,
                outcome="error",
                status_code=400,
                fallback_points=0,
            )
            return _error(
                400,
                (
                    "Project API 图片必须使用 Turtle 托管媒体来源；"
                    "当前不支持任意公网 image_url 或内联文件"
                ),
                "invalid_request_error",
            )
        authorization_error = await authorize_project_request(selection_key)
        if authorization_error is not None:
            return authorization_error
        message_count, last_role, media_on_last = _message_shape(payload)
        logger.info(
            "request_route id=%s upstream_model=%s thinking_effort=%s media_count=%d message_count=%d last_role=%s media_on_last=%s",
            request_id,
            payload["model"],
            payload.get("reasoning_effort", "default"),
            _image_url_count(payload),
            message_count,
            last_role,
            media_on_last,
        )
        account_pool_id = body.turtle_account_pool_id or default_pool_id
        attempted_account_ids: set[str] = set()
        migration_reason_hint: str | None = None
        last_safe_failure: tuple[int, str] | None = None
        account_lease = None
        upstream = None
        upstream_response = None
        upstream_prefetched: list[str] = []
        upstream_iterator: AsyncIterator[str] | None = None

        for attempt_no in range(1, resolved.account_failover_max_attempts + 1):
            upstream_prefetched = []
            upstream_iterator = None
            pre_acquire_ms = int((time.monotonic() - started) * 1000)
            account_acquire_started = time.monotonic()
            try:
                account_lease = await request.app.state.account_pool.acquire(
                    pool_id=account_pool_id,
                    request_id=_attempt_request_id(request_id, attempt_no),
                    user_id=body.turtle_user_id,
                    chat_id=body.turtle_chat_id,
                    selection_key=selection_key,
                    excluded_account_ids=frozenset(attempted_account_ids),
                    migration_reason_hint=migration_reason_hint,
                )
                account_acquire_ms = int(
                    (time.monotonic() - account_acquire_started) * 1000
                )
            except AccountUnavailable as exc:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="account_acquire",
                    outcome="unavailable",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=int(
                        (time.monotonic() - account_acquire_started) * 1000
                    ),
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                if last_safe_failure is not None:
                    status_code, message = last_safe_failure
                    logger.warning(
                        "request_failed id=%s status=%s reason=failover_exhausted attempts=%d",
                        request_id,
                        status_code,
                        len(attempted_account_ids),
                    )
                    await record_project_usage(
                        provider=provider,
                        route=selection_key,
                        outcome="error",
                        status_code=status_code,
                    )
                    return _error(status_code, message, "upstream_error")
                logger.warning(
                    "request_failed id=%s status=503 reason=account_pool",
                    request_id,
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="error",
                    status_code=503,
                    fallback_points=0,
                )
                return _error(503, str(exc), "account_pool_unavailable")

            attempted_account_ids.add(account_lease.account.id)
            if provider == "gpt" and body.turtle_chat_id is not None:
                payload["conversation_id"] = _derive_upstream_conversation_key(
                    resolved.gateway_api_key,
                    account_lease.account.pool_id,
                    body.turtle_chat_id,
                )
                await request.app.state.upstream_cleanup.record(
                    account_id=account_lease.account.id,
                    pool_id=account_lease.account.pool_id,
                    user_id=body.turtle_user_id,
                    chat_id=body.turtle_chat_id,
                    metadata=UpstreamResourceMetadata(
                        conversation_cache_key=payload["conversation_id"],
                    ),
                )
            try:
                upstream = await request.app.state.account_pool.client_for(
                    account_lease.account
                )
            except asyncio.CancelledError:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="worker_client",
                    outcome="cancelled",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                await account_lease.release(
                    outcome="cancelled",
                    status_code=499,
                    error_class="client_cancelled",
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="cancelled",
                    status_code=499,
                    fallback_points=0,
                )
                raise
            except Exception:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="worker_client",
                    outcome="error",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                await account_lease.release(
                    outcome="error",
                    status_code=502,
                    error_class="upstream_client",
                )
                last_safe_failure = (502, "无法建立上游连接")
                migration_reason_hint = "failover_worker"
                logger.warning(
                    "request_failover id=%s attempt=%d account=%s reason=worker",
                    request_id,
                    attempt_no,
                    account_lease.account.id,
                )
                continue

            logger.info(
                "request_account id=%s attempt=%d pool=%s account=%s",
                request_id,
                attempt_no,
                account_lease.account.pool_id,
                account_lease.account.id,
            )

            if not body.stream:
                try:
                    result = await upstream.completion(payload)
                except asyncio.CancelledError:
                    await account_lease.release(
                        outcome="cancelled",
                        status_code=499,
                        error_class="client_cancelled",
                    )
                    await record_project_usage(
                        provider=provider,
                        route=selection_key,
                        outcome="cancelled",
                        status_code=499,
                    )
                    raise
                except UpstreamFailure as exc:
                    failover_reason = _safe_failover_reason(exc.status_code)
                    await account_lease.release(
                        outcome="error",
                        status_code=exc.status_code,
                        error_class=failover_reason or "upstream_request",
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                    if failover_reason is not None:
                        last_safe_failure = (exc.status_code, exc.message)
                        migration_reason_hint = failover_reason
                        logger.warning(
                            "request_failover id=%s attempt=%d account=%s reason=%s status=%s",
                            request_id,
                            attempt_no,
                            account_lease.account.id,
                            failover_reason,
                            exc.status_code,
                        )
                        continue
                    logger.warning(
                        "request_failed id=%s status=%s",
                        request_id,
                        exc.status_code,
                    )
                    await record_project_usage(
                        provider=provider,
                        route=selection_key,
                        outcome="error",
                        status_code=exc.status_code,
                    )
                    return _error(exc.status_code, exc.message, "upstream_error")
                except Exception:
                    await account_lease.release(
                        outcome="error",
                        status_code=502,
                        error_class="upstream_request",
                    )
                    logger.warning(
                        "request_failed id=%s status=502 reason=upstream_request",
                        request_id,
                    )
                    await record_project_usage(
                        provider=provider,
                        route=selection_key,
                        outcome="error",
                        status_code=502,
                    )
                    return _error(502, "上游请求失败", "upstream_error")
                if provider == "gpt":
                    _log_upstream_media_metrics(
                        request_id,
                        extract_upstream_media_metrics(result),
                    )
                    _log_upstream_stage_metrics(
                        request_id,
                        extract_upstream_stage_metrics(result),
                    )
                    await request.app.state.upstream_cleanup.record(
                        account_id=account_lease.account.id,
                        pool_id=account_lease.account.pool_id,
                        user_id=body.turtle_user_id,
                        chat_id=body.turtle_chat_id,
                        metadata=extract_upstream_resource_metadata(result),
                    )
                await account_lease.release(outcome="success", status_code=200)
                logger.info(
                    "request_completed id=%s attempts=%d total_ms=%d",
                    request_id,
                    attempt_no,
                    int((time.monotonic() - started) * 1000),
                )
                normalized_result = _rewrite_nonstream(result, public_model)
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="success",
                    status_code=200,
                    payload=normalized_result,
                )
                return normalized_result

            upstream_open_started = time.monotonic()
            try:
                upstream_response = await upstream.open_stream(payload)
                worker_headers_ms = int(
                    (time.monotonic() - upstream_open_started) * 1000
                )
                worker_first_chunk_ms = _bounded_millisecond_header(
                    upstream_response.headers.get(
                        "x-turtle-worker-first-chunk-ms"
                    )
                )
            except asyncio.CancelledError:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="worker_headers",
                    outcome="cancelled",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    worker_headers_ms=int(
                        (time.monotonic() - upstream_open_started) * 1000
                    ),
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                await account_lease.release(
                    outcome="cancelled",
                    status_code=499,
                    error_class="client_cancelled",
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="cancelled",
                    status_code=499,
                )
                raise
            except UpstreamFailure as exc:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="worker_headers",
                    outcome=f"http_{exc.status_code}",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    worker_headers_ms=int(
                        (time.monotonic() - upstream_open_started) * 1000
                    ),
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                failover_reason = _safe_failover_reason(exc.status_code)
                await account_lease.release(
                    outcome="error",
                    status_code=exc.status_code,
                    error_class=failover_reason or "upstream_connect",
                    retry_after_seconds=exc.retry_after_seconds,
                )
                if failover_reason is not None:
                    last_safe_failure = (exc.status_code, exc.message)
                    migration_reason_hint = failover_reason
                    logger.warning(
                        "request_failover id=%s attempt=%d account=%s reason=%s status=%s",
                        request_id,
                        attempt_no,
                        account_lease.account.id,
                        failover_reason,
                        exc.status_code,
                    )
                    continue
                logger.warning(
                    "request_failed id=%s status=%s",
                    request_id,
                    exc.status_code,
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="error",
                    status_code=exc.status_code,
                )
                return _error(exc.status_code, exc.message, "upstream_error")
            except Exception:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="worker_headers",
                    outcome="error",
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    worker_headers_ms=int(
                        (time.monotonic() - upstream_open_started) * 1000
                    ),
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                )
                await account_lease.release(
                    outcome="error",
                    status_code=502,
                    error_class="upstream_connect",
                )
                logger.warning(
                    "request_failed id=%s status=502 reason=upstream_connect",
                    request_id,
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="error",
                    status_code=502,
                )
                return _error(502, "上游连接失败", "upstream_error")

            candidate_iterator = upstream.stream_data(upstream_response)
            terminal_without_content = False
            prefetch_started = time.monotonic()
            prefetch_outcome = "exception"
            prefetched_bytes = 0
            prefetched_effective = False
            try:
                for _ in range(256):
                    try:
                        data = await anext(candidate_iterator)
                    except StopAsyncIteration:
                        terminal_without_content = True
                        break
                    upstream_prefetched.append(data)
                    prefetched_bytes += len(data.encode("utf-8", errors="ignore"))
                    if _sse_data_has_error(data):
                        terminal_without_content = True
                        break
                    if _sse_data_has_effective_content(data):
                        prefetched_effective = True
                        break
                    if data == "[DONE]":
                        terminal_without_content = True
                        break
                    if prefetched_bytes >= 1024 * 1024:
                        break
                prefetch_outcome = (
                    "empty"
                    if terminal_without_content
                    else "effective"
                    if prefetched_effective
                    else "limit"
                )
            except asyncio.CancelledError:
                prefetch_outcome = "cancelled"
                await account_lease.release(
                    outcome="cancelled",
                    status_code=499,
                    error_class="client_cancelled",
                )
                with contextlib.suppress(Exception):
                    await upstream_response.aclose()
                raise
            except UpstreamFailure as exc:
                prefetch_outcome = f"http_{exc.status_code}"
                await account_lease.release(
                    outcome="error",
                    status_code=exc.status_code,
                    error_class="upstream_prefetch",
                    retry_after_seconds=exc.retry_after_seconds,
                )
                with contextlib.suppress(Exception):
                    await upstream_response.aclose()
                logger.warning(
                    "request_failed id=%s status=%s reason=upstream_prefetch",
                    request_id,
                    exc.status_code,
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="error",
                    status_code=exc.status_code,
                )
                return _error(exc.status_code, exc.message, "upstream_error")
            finally:
                _log_gateway_stage_metrics(
                    request_id,
                    attempt_no=attempt_no,
                    phase="gateway_prefetch",
                    outcome=prefetch_outcome,
                    pre_acquire_ms=pre_acquire_ms,
                    account_acquire_ms=account_acquire_ms,
                    worker_headers_ms=worker_headers_ms,
                    worker_first_chunk_ms=worker_first_chunk_ms,
                    gateway_prefetch_ms=int(
                        (time.monotonic() - prefetch_started) * 1000
                    ),
                    response_ready_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                    prefetched_events=len(upstream_prefetched),
                    prefetched_bytes=prefetched_bytes,
                    prefetched_effective=prefetched_effective,
                )

            if terminal_without_content:
                await account_lease.release(
                    outcome="error",
                    status_code=502,
                    error_class="upstream_empty_stream",
                )
                with contextlib.suppress(Exception):
                    await upstream_response.aclose()
                last_safe_failure = (502, "上游返回空结果")
                migration_reason_hint = "failover_empty_stream"
                logger.warning(
                    "request_failover id=%s attempt=%d account=%s reason=empty_stream status=502",
                    request_id,
                    attempt_no,
                    account_lease.account.id,
                )
                upstream_response = None
                continue

            upstream_iterator = candidate_iterator
            break
        else:
            if last_safe_failure is not None:
                status_code, message = last_safe_failure
                logger.warning(
                    "request_failed id=%s status=%s reason=failover_limit attempts=%d",
                    request_id,
                    status_code,
                    len(attempted_account_ids),
                )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome="error",
                    status_code=status_code,
                )
                return _error(status_code, message, "upstream_error")
            await record_project_usage(
                provider=provider,
                route=selection_key,
                outcome="error",
                status_code=503,
                fallback_points=0,
            )
            return _error(503, "当前账号组没有可调度账号", "account_pool_unavailable")

        if (
            account_lease is None
            or upstream is None
            or upstream_response is None
            or upstream_iterator is None
        ):
            await record_project_usage(
                provider=provider,
                route=selection_key,
                outcome="error",
                status_code=502,
                fallback_points=0,
            )
            return _error(502, "上游连接失败", "upstream_error")

        async def relay() -> AsyncIterator[bytes]:
            first_effective_data = True
            first_answer_text = True
            saw_effective_content = False
            sent_done = False
            stream_error: UpstreamFailure | None = None
            stream_cancelled = False
            finalized = False
            tracked_conversation_id: str | None = None
            tracked_input_file_ids: set[str] = set()
            tracked_generated_asset_ids: set[str] = set()
            tracked_media_metrics = UpstreamMediaMetrics()
            tracked_stage_metrics = UpstreamStageMetrics()
            reported_usage_payload: dict[str, Any] | None = None
            estimated_stream_completion_tokens = 0

            async def source_data() -> AsyncIterator[str]:
                for item in upstream_prefetched:
                    yield item
                assert upstream_iterator is not None
                async for item in upstream_iterator:
                    yield item

            async def finalize(
                *,
                outcome: str,
                status_code: int,
                error_class: str | None,
            ) -> None:
                nonlocal finalized
                if finalized:
                    return
                finalized = True
                _log_upstream_media_metrics(request_id, tracked_media_metrics)
                _log_upstream_stage_metrics(request_id, tracked_stage_metrics)
                if provider == "gpt":
                    try:
                        await request.app.state.upstream_cleanup.record(
                            account_id=account_lease.account.id,
                            pool_id=account_lease.account.pool_id,
                            user_id=body.turtle_user_id,
                            chat_id=body.turtle_chat_id,
                            metadata=UpstreamResourceMetadata(
                                conversation_id=tracked_conversation_id,
                                input_file_ids=tuple(
                                    sorted(tracked_input_file_ids)
                                ),
                                generated_asset_ids=tuple(
                                    sorted(tracked_generated_asset_ids)
                                ),
                            ),
                        )
                    except Exception:
                        logger.warning(
                            "request_cleanup_tracking_failed id=%s",
                            request_id,
                        )
                try:
                    await account_lease.release(
                        outcome=outcome,
                        status_code=status_code,
                        error_class=error_class,
                    )
                except Exception:
                    logger.warning(
                        "request_lease_release_failed id=%s",
                        request_id,
                    )
                await record_project_usage(
                    provider=provider,
                    route=selection_key,
                    outcome=outcome,
                    status_code=status_code,
                    payload=reported_usage_payload,
                    estimated_completion_token_count=(
                        estimated_stream_completion_tokens or None
                    ),
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if outcome == "success":
                    logger.info(
                        "request_completed id=%s total_ms=%d",
                        request_id,
                        elapsed_ms,
                    )
                else:
                    logger.warning(
                        "request_failed id=%s status=%d reason=%s total_ms=%d",
                        request_id,
                        status_code,
                        error_class or "unknown",
                        elapsed_ms,
                    )

            try:
                stream_content_tail = ""
                presentation = SearchPresentationBuffer(
                    enabled=(
                        provider == "gpt"
                        and _payload_has_search_intent(payload)
                    )
                )
                async for data in source_data():
                    if provider == "gpt":
                        metadata = extract_upstream_resource_metadata(data)
                        current_media_metrics = extract_upstream_media_metrics(data)
                        if not current_media_metrics.empty:
                            tracked_media_metrics = current_media_metrics
                        current_stage_metrics = extract_upstream_stage_metrics(data)
                        if not current_stage_metrics.empty:
                            tracked_stage_metrics = current_stage_metrics
                        tracked_conversation_id = (
                            metadata.conversation_id
                            or tracked_conversation_id
                        )
                        tracked_input_file_ids.update(
                            metadata.input_file_ids
                        )
                        tracked_generated_asset_ids.update(
                            metadata.generated_asset_ids
                        )
                    upstream_events = normalize_sse_events(
                        data,
                        public_model,
                        resolved.stream_chunk_chars,
                        stream_content_tail,
                    )
                    normalized_events = [
                        presented
                        for upstream_event in upstream_events
                        for presented in presentation.feed(upstream_event)
                    ]
                    for event_index, normalized in enumerate(normalized_events):
                        if _sse_data_has_error(normalized):
                            stream_error = UpstreamFailure(
                                502,
                                "上游消息流异常，请重试",
                            )
                            await finalize(
                                outcome="error",
                                status_code=502,
                                error_class="upstream_stream",
                            )
                            error_payload = {
                                "error": {
                                    "message": stream_error.message,
                                    "type": "upstream_stream_error",
                                    "code": None,
                                }
                            }
                            yield (
                                "data: "
                                + json.dumps(
                                    error_payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + "\n\n"
                            ).encode()
                            yield b"data: [DONE]\n\n"
                            sent_done = True
                            with contextlib.suppress(Exception):
                                await upstream_response.aclose()
                            return
                        event_has_effective_content = (
                            _sse_data_has_effective_content(normalized)
                        )
                        event_has_answer_text = _sse_data_has_answer_text(
                            normalized
                        )
                        if event_has_effective_content:
                            saw_effective_content = True
                            if first_effective_data:
                                first_effective_data = False
                                logger.info(
                                    "request_first_chunk id=%s ttft_ms=%d",
                                    request_id,
                                    int((time.monotonic() - started) * 1000),
                                )
                        if normalized != "[DONE]":
                            try:
                                normalized_payload = json.loads(normalized)
                            except json.JSONDecodeError:
                                normalized_payload = None
                            if (
                                isinstance(normalized_payload, dict)
                                and isinstance(normalized_payload.get("usage"), dict)
                            ):
                                reported_usage_payload = normalized_payload
                            if isinstance(normalized_payload, dict):
                                choices = normalized_payload.get("choices")
                                delta = (
                                    choices[0].get("delta")
                                    if isinstance(choices, list)
                                    and len(choices) == 1
                                    and isinstance(choices[0], dict)
                                    else None
                                )
                                content = (
                                    delta.get("content")
                                    if isinstance(delta, dict)
                                    else None
                                )
                                if isinstance(content, str) and content:
                                    stream_content_tail = (
                                        stream_content_tail + content
                                    )[-32:]
                                estimated_stream_completion_tokens += (
                                    estimate_completion_tokens(normalized_payload)
                                )
                        if normalized == "[DONE]":
                            sent_done = True
                            if saw_effective_content:
                                await finalize(
                                    outcome="success",
                                    status_code=200,
                                    error_class=None,
                                )
                            else:
                                await finalize(
                                    outcome="error",
                                    status_code=502,
                                    error_class="upstream_empty_stream",
                                )
                                error_payload = {
                                    "error": {
                                        "message": "上游返回空结果",
                                        "type": "upstream_stream_error",
                                        "code": None,
                                    }
                                }
                                yield (
                                    "data: "
                                    + json.dumps(
                                        error_payload,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    + "\n\n"
                                ).encode()
                            with contextlib.suppress(Exception):
                                await upstream_response.aclose()
                        if event_has_answer_text and first_answer_text:
                            first_answer_text = False
                            logger.info(
                                "request_first_answer_text id=%s ttft_ms=%d",
                                request_id,
                                int(
                                    (time.monotonic() - started)
                                    * 1000
                                ),
                            )
                        yield f"data: {normalized}\n\n".encode()
                        if normalized == "[DONE]":
                            return
                        if (
                            event_index < len(normalized_events) - 1
                            and resolved.stream_chunk_delay_ms > 0
                        ):
                            await asyncio.sleep(resolved.stream_chunk_delay_ms / 1000)
                if not sent_done:
                    if saw_effective_content:
                        await finalize(
                            outcome="success",
                            status_code=200,
                            error_class=None,
                        )
                    else:
                        await finalize(
                            outcome="error",
                            status_code=502,
                            error_class="upstream_empty_stream",
                        )
                        error_payload = {
                            "error": {
                                "message": "上游返回空结果",
                                "type": "upstream_stream_error",
                                "code": None,
                            }
                        }
                        yield (
                            "data: "
                            + json.dumps(
                                error_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n\n"
                        ).encode()
                    yield b"data: [DONE]\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                stream_cancelled = True
                raise
            except UpstreamFailure as exc:
                stream_error = exc
                await finalize(
                    outcome="error",
                    status_code=exc.status_code,
                    error_class="upstream_stream",
                )
                error_payload = {
                    "error": {"message": exc.message, "type": "upstream_stream_error", "code": None}
                }
                yield f"data: {json.dumps(error_payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
                yield b"data: [DONE]\n\n"
            finally:
                if not finalized:
                    finalization = asyncio.create_task(
                        finalize(
                            outcome=(
                                "cancelled"
                                if stream_cancelled
                                else "error"
                                if stream_error or not saw_effective_content
                                else "success"
                            ),
                            status_code=(
                                499
                                if stream_cancelled
                                else stream_error.status_code
                                if stream_error
                                else 502
                                if not saw_effective_content
                                else 200
                            ),
                            error_class=(
                                "client_cancelled"
                                if stream_cancelled
                                else "upstream_stream"
                                if stream_error
                                else "upstream_empty_stream"
                                if not saw_effective_content
                                else None
                            ),
                        )
                    )
                    try:
                        await asyncio.shield(finalization)
                    except asyncio.CancelledError:
                        with contextlib.suppress(asyncio.CancelledError):
                            await finalization
                        raise

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return application
