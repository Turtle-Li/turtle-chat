from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from claude_web_worker.app import create_app
from claude_web_worker.auth import (
    AuthError,
    load_auth_session,
    save_auth_session,
    secure_auth_directory,
    session_from_cdp_cookies,
)
from claude_web_worker.client import (
    ClaudeClient,
    ClaudeWebError,
    CompletionHandle,
    collect_stream_text,
    iter_stream_deltas,
)
from claude_web_worker.config import Settings
from claude_web_worker.models import (
    ROUTE_BY_KEY,
    ChatMessage,
    save_verified_routes,
    serialize_history,
)


class FakeResponse:
    def __init__(self, lines: list[str] | None = None, *, payload=None, status_code: int = 200):
        self.lines = lines or []
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self.payload

    async def aiter_lines(self, *args, **kwargs):
        assert kwargs.get("decode_unicode") is not True
        for line in self.lines:
            yield line

    async def aclose(self):
        self.closed = True


def test_settings_use_gateway_upstream_key_for_host_worker(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_WORKER_API_KEY", raising=False)
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("UPSTREAM_API_KEY", "worker-key")

    assert Settings.from_env().worker_api_key == "worker-key"


def claude_sse(text: str) -> list[str]:
    return [
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}",
        "",
        "event: content_block_stop",
        'data: {"type":"content_block_stop","index":0}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]


class FakeClaudeClient:
    def __init__(self, response_text: str = "海龟回答"):
        self.response_text = response_text
        self.deleted: list[str] = []
        self.closed = False
        self.routes: list[str] = []
        self.web_search_values: list[bool] = []

    async def validate(self):
        return "org-test"

    async def close(self):
        self.closed = True

    async def start_completion(self, prompt, route, *, web_search=False):
        assert "conversation_json" in prompt
        self.routes.append(route.key)
        self.web_search_values.append(web_search)
        return CompletionHandle(
            conversation_id=f"conversation-{len(self.routes)}",
            response=FakeResponse(claude_sse(self.response_text)),
        )

    async def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)


class FakeProtocolTransport:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    async def request(
        self,
        method,
        path,
        *,
        json_payload=None,
        referer_path="/",
        stream=False,
    ):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "json": json_payload,
                "referer": referer_path,
                "stream": stream,
            }
        )
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


def make_settings(tmp_path: Path) -> Settings:
    auth_path = tmp_path / "claude-auth" / "session.json"
    return Settings(
        worker_api_key="worker-test-key",
        auth_path=auth_path,
        verified_models_path=auth_path.parent / "verified-models.json",
        health_cache_seconds=3600,
    )


def write_auth(settings: Settings) -> None:
    session = session_from_cdp_cookies(
        [
            {
                "name": "sessionKey",
                "value": "unit-test-only",
                "domain": ".claude.ai",
                "path": "/",
                "secure": True,
            },
            {
                "name": "should-not-be-captured",
                "value": "unrelated",
                "domain": ".example.test",
            },
        ]
    )
    save_auth_session(settings.auth_path, session.with_organization("org-test"))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-test-key"}


def test_auth_capture_keeps_only_claude_domain_and_private_permissions(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_auth(settings)
    loaded = load_auth_session(settings.auth_path)

    assert loaded.session_key == "unit-test-only"
    assert {cookie.domain for cookie in loaded.cookies} == {".claude.ai"}
    assert settings.auth_path.parent.stat().st_mode & 0o777 == 0o700
    assert settings.auth_path.stat().st_mode & 0o777 == 0o600


def test_auth_directory_rejects_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    os.symlink(real, linked)

    with pytest.raises(AuthError, match="symbolic links"):
        secure_auth_directory(linked)


def test_controlled_auth_capture_verifies_every_published_route(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    verified = tuple(ROUTE_BY_KEY)
    with (
        patch(
            "claude_web_worker.app._capture_and_validate",
            new=AsyncMock(),
        ) as capture,
        patch(
            "claude_web_worker.app._verify",
            new=AsyncMock(return_value=verified),
        ) as verify,
        patch(
            "claude_web_worker.app.ClaudeRuntime.ensure_ready",
            new=AsyncMock(return_value="ready"),
        ),
        TestClient(create_app(settings)) as client,
    ):
        response = client.post(
            "/api/ClaudeWeb/auth/capture",
            headers=headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "ready",
        "verified_route_count": len(verified),
    }
    capture.assert_awaited_once_with(settings.browser_port, settings.auth_path)
    verify.assert_awaited_once()


def test_models_stay_hidden_before_login_and_real_route_verification(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        missing_login = client.get("/v1/models", headers=headers())
    assert missing_login.status_code == 200
    assert missing_login.json()["data"] == []

    write_auth(settings)
    fake = FakeClaudeClient()
    with TestClient(create_app(settings, client_factory=lambda _settings, _auth: fake)) as client:
        unverified = client.get("/v1/models", headers=headers())
        health = client.get("/healthz")
    assert unverified.json()["data"] == []
    assert health.json()["status"] == "model_verification_required"


def test_verified_model_metadata_nonstream_and_cleanup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_auth(settings)
    save_verified_routes(settings.verified_models_path, ["claude-sonnet-5:standard"])
    fake = FakeClaudeClient()

    with TestClient(create_app(settings, client_factory=lambda _settings, _auth: fake)) as client:
        models = client.get("/v1/models", headers=headers())
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "claude-web",
                "messages": [
                    {"role": "user", "content": "记住我的名字是小龟"},
                    {"role": "assistant", "content": "好的"},
                    {"role": "user", "content": "我的名字是什么？"},
                ],
                "turtle_claude_model": "claude-sonnet-5",
                "turtle_claude_thinking": "standard",
            },
        )

    assert models.status_code == 200
    model = models.json()["data"][0]
    assert model["id"] == "claude-web"
    assert model["turtle"]["family"] == "claude"
    assert model["turtle"]["versions"][0]["id"] == "claude-sonnet-5"
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "海龟回答"
    assert fake.routes == ["claude-sonnet-5:standard"]
    assert fake.web_search_values == [False]
    assert fake.deleted == ["conversation-1"]


def test_verified_stream_uses_openai_chunks_done_and_cleanup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_auth(settings)
    save_verified_routes(settings.verified_models_path, ["claude-sonnet-5:extended"])
    fake = FakeClaudeClient("逐步输出")

    with TestClient(create_app(settings, client_factory=lambda _settings, _auth: fake)) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "claude-web",
                "messages": [{"role": "user", "content": "测试流式"}],
                "stream": True,
                "web_search": True,
                "turtle_claude_model": "claude-sonnet-5",
                "turtle_claude_thinking": "extended",
            },
        ) as response:
            lines = [line for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[-1] == "data: [DONE]"
    events = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(
        event["choices"][0]["delta"].get("content", "") for event in events
    ) == "逐步输出"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert fake.deleted == ["conversation-1"]
    assert fake.web_search_values == [True]


def test_claude_search_stream_exposes_real_progress_and_citation() -> None:
    lines = [
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","name":"web_search"}}',
        "",
        "event: content_block_start",
        (
            'data: {"type":"content_block_start","index":1,"content_block":'
            '{"type":"tool_result","name":"web_search","content":['
            '{"type":"knowledge","title":"Claude Search","url":"https://support.claude.com/a"},'
            '{"type":"knowledge","title":"Second result","url":"https://support.claude.com/b"},'
            '{"type":"knowledge","title":"Other result","url":"https://example.com/c"}]}}'
        ),
        "",
        "event: content_block_delta",
        (
            'data: {"type":"content_block_delta","index":2,"delta":'
            '{"type":"citation_start_delta","citation":{"title":"Claude Search",'
            '"url":"https://support.claude.com/a"}}}'
        ),
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":2,"delta":{"type":"text_delta","text":"已确认"}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]

    async def collect():
        return [delta async for delta in iter_stream_deltas(FakeResponse(lines))]

    deltas = asyncio.run(collect())

    assert [delta.get("reasoning") for delta in deltas if delta.get("reasoning")] == [
        "正在搜索网页\n",
        "正在查看 support.claude.com\n",
        "正在查看 example.com\n",
    ]
    assert [delta.get("content") for delta in deltas if delta.get("content")] == ["已确认"]
    assert [
        annotation
        for delta in deltas
        for annotation in delta.get("annotations", [])
    ] == [
        {
            "type": "url_citation",
            "url_citation": {
                "url": "https://support.claude.com/a",
                "title": "Claude Search",
            },
        }
    ]


def test_unverified_route_and_media_fail_closed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_auth(settings)
    save_verified_routes(settings.verified_models_path, ["claude-sonnet-5:standard"])
    fake = FakeClaudeClient()

    with TestClient(create_app(settings, client_factory=lambda _settings, _auth: fake)) as client:
        unverified = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "claude-web",
                "messages": [{"role": "user", "content": "test"}],
                "turtle_claude_model": "claude-opus-4-8",
                "turtle_claude_thinking": "standard",
            },
        )
        media = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "claude-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "inspect"},
                            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                        ],
                    }
                ],
                "turtle_claude_model": "claude-sonnet-5",
                "turtle_claude_thinking": "standard",
            },
        )

    assert unverified.status_code == 400
    assert media.status_code == 400
    assert fake.routes == []


def test_history_serialization_keeps_roles_and_text() -> None:
    prompt = serialize_history(
        [
            ChatMessage(role="system", content="系统规则"),
            ChatMessage(role="user", content="第一问"),
            ChatMessage(role="assistant", content="第一答"),
            ChatMessage(role="user", content="第二问"),
        ]
    )
    assert '"role":"system","content":"系统规则"' in prompt
    assert '"role":"assistant","content":"第一答"' in prompt


def test_worker_requires_bearer_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/models")
    assert response.status_code == 401


def test_direct_client_uses_claude_web_conversation_protocol_and_cleans_up(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    session = session_from_cdp_cookies(
        [{"name": "sessionKey", "value": "unit-test-only", "domain": ".claude.ai"}]
    )
    transport = FakeProtocolTransport(
        [
            FakeResponse(payload=[{"uuid": "org-test", "rate_limit_tier": "default_claude_ai"}]),
            FakeResponse(payload={"uuid": "conversation-test"}, status_code=201),
            FakeResponse(claude_sse("OK")),
            FakeResponse(status_code=204),
        ]
    )
    client = ClaudeClient(settings, session, transport=transport)

    async def exercise():
        assert await client.validate() == "org-test"
        route = ROUTE_BY_KEY["claude-sonnet-5:extended"]
        handle = await client.start_completion("test prompt", route, web_search=True)
        assert await collect_stream_text(handle.response) == "OK"
        await client.delete_conversation(handle.conversation_id)
        await client.close()

    asyncio.run(exercise())

    assert [request["method"] for request in transport.requests] == [
        "GET",
        "POST",
        "POST",
        "DELETE",
    ]
    completion = transport.requests[2]
    assert completion["path"] == (
        "/api/organizations/org-test/chat_conversations/conversation-test/completion"
    )
    assert completion["json"]["model"] == "claude-sonnet-5"
    assert completion["json"]["effort"] == "high"
    assert completion["json"]["thinking_mode"] == "extended"
    assert completion["json"]["locale"] == "en-US"
    assert completion["json"]["tools"] == [
        {"type": "web_search_v0", "name": "web_search"}
    ]
    assert completion["stream"] is True
    assert transport.closed is True


def test_direct_client_marks_unauthorized_session_for_reauthentication(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    session = session_from_cdp_cookies(
        [{"name": "sessionKey", "value": "unit-test-only", "domain": ".claude.ai"}]
    )
    client = ClaudeClient(
        settings,
        session,
        transport=FakeProtocolTransport([FakeResponse(status_code=403)]),
    )

    with pytest.raises(ClaudeWebError) as denied:
        asyncio.run(client.validate())
    assert denied.value.reauthentication_required is True
