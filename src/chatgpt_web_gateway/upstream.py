from __future__ import annotations

import json
import re
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

import httpx

from .security import redact


@dataclass(slots=True)
class UpstreamFailure(Exception):
    status_code: int
    message: str
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class UpstreamResourceMetadata:
    conversation_id: str | None = None
    conversation_cache_key: str | None = None
    input_file_ids: tuple[str, ...] = ()
    generated_asset_ids: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.conversation_id
            or self.conversation_cache_key
            or self.input_file_ids
            or self.generated_asset_ids
        )


@dataclass(frozen=True, slots=True)
class UpstreamMediaMetrics:
    probe_count: int = 0
    transfer_count: int = 0
    transfer_bytes: int = 0
    cdn_hit: int = 0
    cdn_miss: int = 0
    fallback_count: int = 0
    retry_count: int = 0
    file_cache_hit: int = 0
    file_cache_miss: int = 0
    file_cache_stale: int = 0
    upload_wall_ms: int = 0
    cache_validation_ms: int = 0
    probe_ms: int = 0
    create_ms: int = 0
    settle_ms: int = 0
    transfer_ms: int = 0
    confirm_ms: int = 0
    configured_parallel: int = 0
    max_parallel: int = 0

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.probe_count,
                self.transfer_count,
                self.transfer_bytes,
                self.cdn_hit,
                self.cdn_miss,
                self.fallback_count,
                self.retry_count,
                self.file_cache_hit,
                self.file_cache_miss,
                self.file_cache_stale,
                self.upload_wall_ms,
                self.cache_validation_ms,
                self.probe_ms,
                self.create_ms,
                self.settle_ms,
                self.transfer_ms,
                self.confirm_ms,
                self.configured_parallel,
                self.max_parallel,
            )
        )


@dataclass(frozen=True, slots=True)
class UpstreamStageMetrics:
    home_ms: int = 0
    media_ms: int = 0
    prepare_ms: int = 0
    requirements_ms: int = 0
    submit_headers_ms: int = 0
    first_event_ms: int = 0
    pre_stream_ms: int = 0

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.home_ms,
                self.media_ms,
                self.prepare_ms,
                self.requirements_ms,
                self.submit_headers_ms,
                self.first_event_ms,
                self.pre_stream_ms,
            )
        )


_UPSTREAM_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")
_MARKDOWN_HEADING_AFTER_SENTENCE_RE = re.compile(
    r"(?<=[。！？.!?])[ \t]*(?=#{1,6}[ \t]+\S)"
)
_MARKDOWN_HEADING_AT_CHUNK_START_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S")
_MARKDOWN_HEADING_LINE_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+\S")
_SENTENCE_END_RE = re.compile(r"[。！？.!?][ \t]*$")
_ORPHANED_SEARCH_PREFIX_RE = re.compile(r"^[ \t]*[.．]\d+(?:[ \t]|[A-Za-z])")
_PLAIN_PARAGRAPH_CONTINUATION_RE = re.compile(
    r"\n[ \t]*\n(?![ \t]*#{1,6}[ \t]+\S)[ \t]*\S"
)
_EXPLICIT_UPSTREAM_LIMIT_RE = re.compile(
    r"\byou(?:['’]ve| have) hit your limit\b",
    re.IGNORECASE,
)
_TURTLE_RETRY_AFTER_RE = re.compile(
    r"\bturtle_retry_after_s=(\d{1,8})\b",
    re.IGNORECASE,
)
_VISIBLE_PROGRESS_PREFIX_RE = re.compile(
    r"(?:正在|先|将|会|准备|继续|接下来).{0,24}"
    r"(?:核对|查看|搜索|检索|查找|打开|确认|整理|验证|检查|提炼)"
    r"|(?:核对|查看|搜索|检索|查找|打开|确认|整理|验证|检查|提炼).{0,24}"
    r"(?:页面|官网|来源|结果|变化|信息)",
    re.IGNORECASE,
)


def normalize_markdown_boundaries(content: str, preceding_content: str = "") -> str:
    """Keep a streamed Markdown heading from joining the prior sentence."""
    normalized = _MARKDOWN_HEADING_AFTER_SENTENCE_RE.sub("\n\n", content)
    if (
        preceding_content
        and _SENTENCE_END_RE.search(preceding_content)
        and _MARKDOWN_HEADING_AT_CHUNK_START_RE.match(normalized)
    ):
        normalized = "\n\n" + normalized.lstrip(" \t")
    return normalized


class SearchPresentationBuffer:
    """Keep short search-work preambles out of the final Markdown answer."""

    def __init__(
        self,
        *,
        enabled: bool,
        detection_chars: int = 48,
        max_prefix_chars: int = 220,
    ) -> None:
        self.enabled = enabled
        self.detection_chars = detection_chars
        self.max_prefix_chars = max_prefix_chars
        self._resolved = not enabled
        self._pending: list[str] = []
        self._pending_content = ""

    @staticmethod
    def _event_content(payload: Any) -> str | None:
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or len(choices) != 1:
            return None
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        return content if isinstance(content, str) and content else None

    @staticmethod
    def _finish_reason(payload: Any) -> Any:
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or len(choices) != 1:
            return None
        choice = choices[0]
        return choice.get("finish_reason") if isinstance(choice, dict) else None

    @staticmethod
    def _rewrite(
        source: str,
        *,
        content: str | None,
        reasoning: str | None = None,
        unfinished: bool = False,
    ) -> str:
        payload = json.loads(source)
        choice = payload["choices"][0]
        delta = choice["delta"]
        delta["content"] = content
        if reasoning is None:
            delta.pop("reasoning", None)
            delta.pop("reasoning_content", None)
        else:
            delta["reasoning"] = reasoning
        if unfinished:
            choice["finish_reason"] = None
            payload.pop("usage", None)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _flush(self) -> list[str]:
        events = self._pending
        self._pending = []
        self._pending_content = ""
        self._resolved = True
        return events

    def feed(self, event: str) -> list[str]:
        if self._resolved:
            return [event]
        if event == "[DONE]":
            return [*self._flush(), event]
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            return [event]

        content = self._event_content(payload)
        if content is None:
            if self._finish_reason(payload) is not None:
                return [*self._flush(), event]
            return [event]

        self._pending.append(event)
        self._pending_content += content
        normalized = normalize_markdown_boundaries(self._pending_content)
        heading = _MARKDOWN_HEADING_LINE_RE.search(normalized)
        if heading is not None:
            prefix = normalized[: heading.start()].strip()
            answer = normalized[heading.start() :].lstrip()
            if not prefix:
                return self._flush()

            first_event = self._pending[0]
            last_event = self._pending[-1]
            self._pending = []
            self._pending_content = ""
            self._resolved = True
            if _ORPHANED_SEARCH_PREFIX_RE.match(prefix):
                return [self._rewrite(last_event, content=answer)]
            return [
                self._rewrite(
                    first_event,
                    content=None,
                    reasoning=f"{prefix}\n\n",
                    unfinished=True,
                ),
                self._rewrite(last_event, content=answer),
            ]

        has_progress_prefix = bool(
            _ORPHANED_SEARCH_PREFIX_RE.match(normalized)
            or _VISIBLE_PROGRESS_PREFIX_RE.search(normalized)
        )
        if (
            len(normalized) >= self.max_prefix_chars
            or (
                len(normalized) >= self.detection_chars
                and not has_progress_prefix
                and _PLAIN_PARAGRAPH_CONTINUATION_RE.search(normalized)
            )
        ):
            return self._flush()
        return []


class UpstreamClient:
    def __init__(
        self,
        *,
        base_url: str,
        health_path: str | None,
        auth_capture_path: str = "/api/OpenaiAccount/auth/capture",
        auth_capture_timeout_seconds: float = 45.0,
        api_key: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"}
        upstream_url = httpx.URL(base_url)
        if upstream_url.host == "host.docker.internal":
            # The Docker bridge proxy is a byte-for-byte relay to a worker
            # bound on the host loopback interface.  gpt4free derives request
            # state from the inbound Host header; leaving the Docker alias in
            # place can incorrectly trigger its browser-auth fallback even
            # though the cached ChatGPT session is valid.
            loopback_host = "127.0.0.1"
            if upstream_url.port is not None:
                loopback_host = f"{loopback_host}:{upstream_url.port}"
            headers["Host"] = loopback_host
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            transport=transport,
        )
        self._health_url = (
            self._client.base_url.copy_with(path=health_path) if health_path is not None else None
        )
        self._auth_capture_url = self._client.base_url.copy_with(
            path=auth_capture_path
        )
        self._cleanup_url = self._client.base_url.copy_with(
            path="/api/OpenaiAccount/turtle/cleanup"
        )
        self._auth_capture_timeout_seconds = max(
            1.0,
            float(auth_capture_timeout_seconds),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamFailure(502, f"upstream transport error: {redact(exc)}") from exc
        if response.is_error:
            await self._raise_for_status(response)
        try:
            value = response.json()
        except ValueError as exc:
            raise UpstreamFailure(502, "upstream returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise UpstreamFailure(502, "upstream returned an invalid response object")
        return value

    async def open_stream(self, payload: dict[str, Any]) -> httpx.Response:
        request = self._client.build_request("POST", "/chat/completions", json=payload)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise UpstreamFailure(502, f"upstream transport error: {redact(exc)}") from exc
        if response.is_error:
            try:
                await self._raise_for_status(response)
            finally:
                await response.aclose()
        return response

    async def cleanup_resource(
        self,
        *,
        resource_type: str,
        resource_id: str,
        dry_run: bool,
        conversation_action: str = "delete",
    ) -> dict[str, Any]:
        if resource_type not in {
            "conversation",
            "conversation_cache",
            "input_file",
            "generated_asset",
        } or not _UPSTREAM_RESOURCE_ID_RE.fullmatch(resource_id):
            raise ValueError("invalid upstream cleanup resource")
        if conversation_action not in {"archive", "delete"}:
            raise ValueError("invalid upstream conversation cleanup action")
        try:
            response = await self._client.post(
                self._cleanup_url,
                json={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "dry_run": bool(dry_run),
                    "conversation_action": conversation_action,
                },
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise UpstreamFailure(
                502,
                f"upstream cleanup transport error: {redact(exc)}",
            ) from exc
        if response.is_error:
            await self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamFailure(
                502,
                "upstream cleanup returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise UpstreamFailure(502, "upstream cleanup was not confirmed")
        return {
            "ok": True,
            "dry_run": payload.get("dry_run") is True,
            "http_status": (
                int(payload["http_status"])
                if isinstance(payload.get("http_status"), int)
                else None
            ),
        }

    async def stream_data(self, response: httpx.Response) -> AsyncIterator[str]:
        try:
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                yield line[5:].strip()
        except httpx.HTTPError as exc:
            raise UpstreamFailure(502, f"upstream stream error: {redact(exc)}") from exc
        finally:
            await response.aclose()

    async def models_available(self) -> bool:
        return bool((await self.probe()).get("ok"))

    async def capture_auth(self) -> dict[str, Any]:
        """Capture a completed OpenaiAccount browser login without returning auth data."""

        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._auth_capture_url,
                timeout=self._auth_capture_timeout_seconds,
            )
        except httpx.TimeoutException:
            return {
                "ok": False,
                "state": "degraded",
                "http_status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        except httpx.HTTPError:
            return {
                "ok": False,
                "state": "offline",
                "http_status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        if not response.is_success:
            return {
                "ok": False,
                "state": (
                    "auth_required"
                    if response.status_code in {401, 403}
                    else "degraded"
                ),
                "http_status": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        ok = isinstance(payload, dict) and payload.get("ok") is True
        return {
            "ok": ok,
            "state": "ready" if ok else "degraded",
            "http_status": response.status_code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    async def probe(self) -> dict[str, Any]:
        """Return a sanitized health result without exposing upstream bodies."""

        started = time.perf_counter()
        if self._health_url is not None:
            try:
                response = await self._client.get(self._health_url, timeout=10.0)
            except httpx.TimeoutException:
                return {
                    "ok": False,
                    "state": "degraded",
                    "http_status": None,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            except httpx.HTTPError:
                return {
                    "ok": False,
                    "state": "offline",
                    "http_status": None,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            if not response.is_success:
                return {
                    "ok": False,
                    "state": (
                        "auth_required" if response.status_code in {401, 403} else "degraded"
                    ),
                    "http_status": response.status_code,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
                ok = payload["ok"]
            else:
                ok = True
            reported = str(payload.get("status") or "").strip().lower() if isinstance(payload, dict) else ""
            state = (
                "auth_required"
                if reported in {"login_required", "reauthentication_required"}
                else "ready"
                if ok
                else "degraded"
            )
            result = {
                "ok": ok,
                "state": state,
                "http_status": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
            display_name = _safe_account_display_name(
                payload.get("name") if isinstance(payload, dict) else None
            )
            if ok and display_name:
                result["upstream_display_name"] = display_name
            return result

        try:
            health = await self._client.get("/health", timeout=10.0)
            if health.is_success:
                payload = health.json()
                if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
                    return {
                        "ok": payload["ok"],
                        "state": "ready" if payload["ok"] else "degraded",
                        "http_status": health.status_code,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    }
        except (httpx.HTTPError, ValueError):
            pass
        try:
            response = await self._client.get("/models", timeout=10.0)
        except httpx.HTTPError:
            return {
                "ok": False,
                "state": "offline",
                "http_status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        return {
            "ok": response.is_success,
            "state": (
                "ready"
                if response.is_success
                else "auth_required"
                if response.status_code in {401, 403}
                else "degraded"
            ),
            "http_status": response.status_code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        body = (await response.aread())[:2048].decode("utf-8", errors="replace")
        retry_after_seconds: int | None = None
        raw_retry_after = response.headers.get("retry-after")
        if raw_retry_after:
            value = raw_retry_after.strip()
            if value.isdigit():
                retry_after_seconds = int(value)
            else:
                try:
                    retry_after_seconds = max(
                        0,
                        int(parsedate_to_datetime(value).timestamp() - time.time()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        marker = _TURTLE_RETRY_AFTER_RE.search(body)
        if marker is not None:
            retry_after_seconds = int(marker.group(1))
        if retry_after_seconds is not None:
            retry_after_seconds = min(
                24 * 60 * 60,
                max(1, retry_after_seconds),
            )
        safe_body = _TURTLE_RETRY_AFTER_RE.sub("", body)
        message = redact(safe_body)[:500] or f"upstream HTTP {response.status_code}"
        if response.status_code >= 500 and _EXPLICIT_UPSTREAM_LIMIT_RE.search(body):
            # OpenaiAccount currently wraps ChatGPT's explicit pre-output
            # subscription-limit rejection in HTTP 500. Treat only this
            # unambiguous response as a lane-level 429 so the existing safe
            # pre-output failover and cooldown path can select another account.
            status = 429
        else:
            status = response.status_code if 400 <= response.status_code < 500 else 502
        raise UpstreamFailure(status, message, retry_after_seconds)


def _safe_account_display_name(value: Any) -> str | None:
    """Keep only the short human label returned by the upstream identity probe.

    Account IDs and the rest of the upstream response are deliberately ignored.
    The admin UI escapes this value again before rendering it.
    """

    if not isinstance(value, str):
        return None
    printable = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(printable.split())
    return normalized[:120] or None


def extract_upstream_resource_metadata(
    value: dict[str, Any] | str,
) -> UpstreamResourceMetadata:
    """Extract only opaque Turtle-owned resource IDs from a raw response."""

    if isinstance(value, str):
        if value == "[DONE]":
            return UpstreamResourceMetadata()
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return UpstreamResourceMetadata()
    else:
        payload = value
    if not isinstance(payload, dict):
        return UpstreamResourceMetadata()
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return UpstreamResourceMetadata()

    def resource_id(candidate: Any) -> str | None:
        normalized = str(candidate or "")
        return (
            normalized
            if _UPSTREAM_RESOURCE_ID_RE.fullmatch(normalized)
            else None
        )

    def resource_ids(candidate: Any) -> tuple[str, ...]:
        if not isinstance(candidate, list):
            return ()
        return tuple(
            sorted(
                {
                    normalized
                    for item in candidate
                    if (normalized := resource_id(item)) is not None
                }
            )
        )

    return UpstreamResourceMetadata(
        conversation_id=resource_id(conversation.get("conversation_id")),
        input_file_ids=resource_ids(
            conversation.get("turtle_input_file_ids")
        ),
        generated_asset_ids=resource_ids(
            conversation.get("turtle_generated_asset_ids")
        ),
    )


def extract_upstream_media_metrics(
    value: dict[str, Any] | str,
) -> UpstreamMediaMetrics:
    """Extract only bounded counters; URLs and cache records are ignored."""
    if isinstance(value, str):
        if value == "[DONE]":
            return UpstreamMediaMetrics()
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return UpstreamMediaMetrics()
    else:
        payload = value
    if not isinstance(payload, dict):
        return UpstreamMediaMetrics()
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return UpstreamMediaMetrics()
    metrics = conversation.get("turtle_media_metrics")
    if not isinstance(metrics, dict) or metrics.get("v") != 1:
        return UpstreamMediaMetrics()

    def counter(name: str, maximum: int) -> int:
        value = metrics.get(name)
        return value if isinstance(value, int) and 0 <= value <= maximum else 0

    return UpstreamMediaMetrics(
        probe_count=counter("probe_count", 10_000),
        transfer_count=counter("transfer_count", 10_000),
        transfer_bytes=counter("transfer_bytes", 10**15),
        cdn_hit=counter("cdn_hit", 20_000),
        cdn_miss=counter("cdn_miss", 20_000),
        fallback_count=counter("fallback_count", 20_000),
        retry_count=counter("retry_count", 20_000),
        file_cache_hit=counter("file_cache_hit", 10_000),
        file_cache_miss=counter("file_cache_miss", 10_000),
        file_cache_stale=counter("file_cache_stale", 10_000),
        upload_wall_ms=counter("upload_wall_ms", 3_600_000),
        cache_validation_ms=counter("cache_validation_ms", 3_600_000),
        probe_ms=counter("probe_ms", 3_600_000),
        create_ms=counter("create_ms", 3_600_000),
        settle_ms=counter("settle_ms", 3_600_000),
        transfer_ms=counter("transfer_ms", 3_600_000),
        confirm_ms=counter("confirm_ms", 3_600_000),
        configured_parallel=counter("configured_parallel", 4),
        max_parallel=counter("max_parallel", 4),
    )


def extract_upstream_stage_metrics(
    value: dict[str, Any] | str,
) -> UpstreamStageMetrics:
    """Extract bounded stage timings without retaining upstream payload data."""
    if isinstance(value, str):
        if value == "[DONE]":
            return UpstreamStageMetrics()
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return UpstreamStageMetrics()
    else:
        payload = value
    if not isinstance(payload, dict):
        return UpstreamStageMetrics()
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return UpstreamStageMetrics()
    metrics = conversation.get("turtle_upstream_stage_metrics")
    if not isinstance(metrics, dict) or metrics.get("v") != 1:
        return UpstreamStageMetrics()

    def timing(name: str) -> int:
        candidate = metrics.get(name)
        return (
            candidate
            if isinstance(candidate, int) and 0 <= candidate <= 3_600_000
            else 0
        )

    return UpstreamStageMetrics(
        home_ms=timing("home_ms"),
        media_ms=timing("media_ms"),
        prepare_ms=timing("prepare_ms"),
        requirements_ms=timing("requirements_ms"),
        submit_headers_ms=timing("submit_headers_ms"),
        first_event_ms=timing("first_event_ms"),
        pre_stream_ms=timing("pre_stream_ms"),
    )


def normalize_sse_data(
    data: str,
    public_model: str,
    preceding_content: str = "",
) -> str:
    if data == "[DONE]":
        return data
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data
    if isinstance(payload, dict):
        if "model" in payload:
            payload["model"] = public_model
        choices = payload.get("choices")
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
            delta = choices[0].get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                delta["content"] = normalize_markdown_boundaries(
                    content,
                    preceding_content,
                )
        # gpt4free may attach its process-local Conversation object to the
        # final chunk. It can contain upstream conversation/device fields and
        # is an implementation detail, so it must not cross the Gateway.
        payload.pop("conversation", None)
        payload.pop("conversation_id", None)
        payload.pop("provider", None)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def normalize_sse_events(
    data: str,
    public_model: str,
    chunk_chars: int,
    preceding_content: str = "",
) -> list[str]:
    """Normalize one upstream SSE event and gently split oversized text deltas."""
    normalized = normalize_sse_data(data, public_model, preceding_content)
    if normalized == "[DONE]" or chunk_chars <= 0:
        return [normalized]

    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return [normalized]

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return [normalized]
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    if not isinstance(content, str) or len(content) <= chunk_chars:
        return [normalized]

    pieces = [content[index : index + chunk_chars] for index in range(0, len(content), chunk_chars)]
    events: list[str] = []
    for index, piece in enumerate(pieces):
        event = deepcopy(payload)
        event_choice = event["choices"][0]
        event_choice["delta"]["content"] = piece
        if index < len(pieces) - 1:
            if "finish_reason" in event_choice:
                event_choice["finish_reason"] = None
            event.pop("usage", None)
        events.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    return events
