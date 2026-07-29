from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol
from urllib.parse import urlparse

from .auth import AuthSession
from .config import Settings
from .models import ClaudeRoute


class ResponseLike(Protocol):
    status_code: int

    def json(self) -> Any: ...

    def aiter_lines(self, *args, **kwargs) -> AsyncIterator[str | bytes]: ...

    async def aclose(self) -> None: ...


class Transport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        referer_path: str = "/",
        stream: bool = False,
    ) -> ResponseLike: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class ClaudeWebError(Exception):
    status_code: int
    operation: str
    reauthentication_required: bool = False

    def __str__(self) -> str:
        if self.reauthentication_required:
            return "Claude login has expired; sign in again in the dedicated browser"
        return f"Claude Web {self.operation} failed with HTTP {self.status_code}"


@dataclass(slots=True)
class CompletionHandle:
    conversation_id: str
    response: ResponseLike


async def _close_response(response: ResponseLike | None) -> None:
    if response is None:
        return
    result = response.aclose()
    if inspect.isawaitable(result):
        await result


class CurlCffiTransport:
    """Browser-fingerprint transport that sends cookies only to claude.ai."""

    def __init__(self, settings: Settings, auth: AuthSession) -> None:
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as exc:  # pragma: no cover - depends on the optional runtime extra
            raise RuntimeError(
                "Claude Web support requires the optional 'claude' dependencies"
            ) from exc

        self._settings = settings
        self._auth = auth
        options: dict[str, Any] = {
            "impersonate": "chrome",
            "timeout": settings.timeout_seconds,
            "allow_redirects": False,
        }
        if settings.proxy_url:
            options["proxy"] = settings.proxy_url
        self._session = AsyncSession(**options)

    def _headers(self, referer_path: str) -> dict[str, str]:
        trace_id = secrets.randbits(63)
        parent_id = secrets.randbits(63)
        high = secrets.randbits(63)
        return {
            "Accept": "text/event-stream",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Content-Type": "application/json",
            "Origin": self._settings.base_url,
            "Referer": f"{self._settings.base_url}{referer_path}",
            "Priority": "u=1, i",
            "anthropic-client-platform": "web_claude_ai",
            "anthropic-device-id": self._auth.device_id,
            "Cookie": self._auth.cookie_header(),
            "traceparent": f"00-{high:016x}{trace_id:016x}-{parent_id:016x}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": str(parent_id),
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": str(trace_id),
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        referer_path: str = "/",
        stream: bool = False,
    ) -> ResponseLike:
        if not path.startswith("/") or "//" in path:
            raise ValueError("Claude Web request path must be relative to claude.ai")
        try:
            return await self._session.request(
                method,
                f"{self._settings.base_url}{path}",
                json=json_payload,
                headers=self._headers(referer_path),
                stream=stream,
            )
        except Exception as exc:
            # curl_cffi exception text can contain request headers.  Never copy
            # it into logs, API responses, or chained exception messages.
            raise ClaudeWebError(502, "transport") from exc

    async def close(self) -> None:
        await self._session.close()


class ClaudeClient:
    def __init__(
        self,
        settings: Settings,
        auth: AuthSession,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.transport = transport or CurlCffiTransport(settings, auth)
        self.organization_uuid = auth.organization_uuid

    async def close(self) -> None:
        await self.transport.close()

    @staticmethod
    async def _expect(response: ResponseLike, operation: str, statuses: set[int]) -> None:
        if response.status_code in statuses:
            return
        raise ClaudeWebError(
            response.status_code if 400 <= response.status_code < 500 else 502,
            operation,
            reauthentication_required=response.status_code in {401, 403},
        )

    async def validate(self) -> str:
        """Always make a real authenticated request; cached org IDs are not proof."""

        response: ResponseLike | None = None
        try:
            response = await self.transport.request("GET", "/api/organizations")
            await self._expect(response, "organizations", {200})
            try:
                organizations = response.json()
            except (TypeError, ValueError) as exc:
                raise ClaudeWebError(502, "organizations") from exc
            if not isinstance(organizations, list) or not organizations:
                raise ClaudeWebError(502, "organizations")

            preferred: list[dict[str, Any]] = []
            fallback: list[dict[str, Any]] = []
            for value in organizations:
                if not isinstance(value, dict) or not str(value.get("uuid") or "").strip():
                    continue
                raven_type = str(value.get("raven_type") or "").strip().lower()
                tier = str(value.get("rate_limit_tier") or "").strip()
                if raven_type == "team" or tier in {
                    "default_claude_ai",
                    "default_claude_max_20x",
                    "default_raven_enterprise",
                }:
                    preferred.append(value)
                else:
                    fallback.append(value)
            chosen = (preferred or fallback)
            if not chosen:
                raise ClaudeWebError(502, "organizations")
            self.organization_uuid = str(chosen[0]["uuid"]).strip()
            return self.organization_uuid
        finally:
            await _close_response(response)

    async def _organization(self) -> str:
        if self.organization_uuid:
            return self.organization_uuid
        return await self.validate()

    async def create_conversation(self) -> str:
        organization = await self._organization()
        response: ResponseLike | None = None
        try:
            response = await self.transport.request(
                "POST",
                f"/api/organizations/{organization}/chat_conversations",
                json_payload={"name": "chat", "organization_uuid": organization},
                referer_path="/new",
            )
            await self._expect(response, "create conversation", {200, 201})
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ClaudeWebError(502, "create conversation") from exc
            conversation_id = str(payload.get("uuid") if isinstance(payload, dict) else "").strip()
            if not conversation_id:
                raise ClaudeWebError(502, "create conversation")
            return conversation_id
        finally:
            await _close_response(response)

    async def delete_conversation(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        organization = await self._organization()
        response: ResponseLike | None = None
        try:
            response = await self.transport.request(
                "DELETE",
                f"/api/organizations/{organization}/chat_conversations/{conversation_id}",
                referer_path=f"/chat/{conversation_id}",
            )
            await self._expect(response, "delete conversation", {200, 204})
        finally:
            await _close_response(response)

    async def start_completion(
        self,
        prompt: str,
        route: ClaudeRoute,
        *,
        web_search: bool = False,
    ) -> CompletionHandle:
        if not prompt.strip():
            raise ValueError("Claude Web prompt must not be empty")
        organization = await self._organization()
        conversation_id = await self.create_conversation()
        human_uuid = str(uuid.uuid4())
        assistant_uuid = str(uuid.uuid4())
        payload = {
            "prompt": prompt,
            "parent_message_uuid": str(uuid.uuid4()),
            "timezone": self.settings.timezone,
            "locale": self.settings.locale,
            "model": route.upstream_model,
            "effort": route.effort,
            "thinking_mode": route.thinking_mode,
            "tools": (
                [{"type": "web_search_v0", "name": "web_search"}]
                if web_search
                else []
            ),
            "turn_message_uuids": {
                "human_message_uuid": human_uuid,
                "assistant_message_uuid": assistant_uuid,
            },
            "attachments": [],
            "files": [],
            "sync_sources": [],
            "rendering_mode": "messages",
            "create_conversation_params": {
                "name": "",
                "model": route.upstream_model,
                "include_conversation_preferences": True,
                "paprika_mode": None,
                "compass_mode": None,
                "tool_search_mode": "auto",
                "is_temporary": False,
                "enabled_imagine": False,
            },
        }
        response: ResponseLike | None = None
        try:
            response = await self.transport.request(
                "POST",
                (
                    f"/api/organizations/{organization}/chat_conversations/"
                    f"{conversation_id}/completion"
                ),
                json_payload=payload,
                referer_path=f"/chat/{conversation_id}",
                stream=True,
            )
            await self._expect(response, "completion", {200})
            return CompletionHandle(conversation_id=conversation_id, response=response)
        except Exception:
            await _close_response(response)
            try:
                await self.delete_conversation(conversation_id)
            except Exception:
                pass
            raise


def _url_citation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_url = value.get("url")
    if not isinstance(raw_url, str) or len(raw_url) > 4096:
        return None
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    raw_title = value.get("title")
    title = str(raw_title).strip()[:500] if raw_title is not None else raw_url
    return {
        "type": "url_citation",
        "url_citation": {
            "url": raw_url,
            "title": title or raw_url,
        },
    }


async def iter_stream_deltas(response: ResponseLike) -> AsyncIterator[dict[str, Any]]:
    event_type = ""
    data_lines: list[str] = []
    viewed_hosts: set[str] = set()
    cited_urls: set[str] = set()

    def decode_event() -> tuple[str, str] | None:
        nonlocal event_type, data_lines
        raw = "\n".join(data_lines).strip()
        current_type = event_type
        event_type = ""
        data_lines = []
        if not raw or raw == "[DONE]":
            return None
        return current_type, raw

    async def emit(decoded: tuple[str, str] | None) -> AsyncIterator[dict[str, Any]]:
        if decoded is None:
            return
        declared_type, raw = decoded
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or declared_type or "").strip()
        if kind == "error":
            raise ClaudeWebError(502, "stream")
        block = payload.get("content_block")
        if kind == "content_block_start" and isinstance(block, dict):
            block_type = str(block.get("type") or "").strip()
            block_name = str(block.get("name") or "").strip()
            if block_type == "tool_use" and block_name == "web_search":
                yield {"content": None, "reasoning": "正在搜索网页\n"}
            elif block_type == "tool_result" and block_name == "web_search":
                content = block.get("content")
                if isinstance(content, list):
                    for source in content:
                        citation = _url_citation(source)
                        if citation is None:
                            continue
                        url = citation["url_citation"]["url"]
                        hostname = urlparse(url).hostname
                        if not hostname:
                            continue
                        label = hostname.casefold().removeprefix("www.")
                        if label in viewed_hosts:
                            continue
                        viewed_hosts.add(label)
                        yield {"content": None, "reasoning": f"正在查看 {label}\n"}
                        if len(viewed_hosts) >= 8:
                            break
        delta = payload.get("delta")
        if isinstance(delta, dict):
            text = delta.get("text")
            if isinstance(text, str) and text:
                yield {"content": text}
            if delta.get("type") == "citation_start_delta":
                citation = _url_citation(delta.get("citation"))
                if citation is not None:
                    url = citation["url_citation"]["url"]
                    if url not in cited_urls:
                        cited_urls.add(url)
                        yield {"content": None, "annotations": [citation]}
        completion = payload.get("completion")
        if isinstance(completion, str) and completion:
            yield {"content": completion}
        if kind in {"text", "text_delta"}:
            text = payload.get("text")
            if isinstance(text, str) and text:
                yield {"content": text}

    try:
        # curl_cffi 0.15 raises NotImplementedError when its async iterator is
        # asked to decode Unicode. Decode each byte line below so the same
        # parser works with curl_cffi and test transports.
        async for raw_line in response.aiter_lines():
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            line = line.rstrip("\r")
            if not line:
                async for text in emit(decode_event()):
                    yield text
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        async for text in emit(decode_event()):
            yield text
    finally:
        await _close_response(response)


async def iter_stream_text(response: ResponseLike) -> AsyncIterator[str]:
    async for delta in iter_stream_deltas(response):
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield content


async def collect_stream_text(response: ResponseLike) -> str:
    chunks: list[str] = []
    async for value in iter_stream_text(response):
        chunks.append(value)
    return "".join(chunks)


def openai_chunk(
    *,
    completion_id: str,
    public_model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> bytes:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": public_model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def completion_identity() -> tuple[str, int]:
    return f"chatcmpl-claude-{uuid.uuid4().hex}", int(time.time())
