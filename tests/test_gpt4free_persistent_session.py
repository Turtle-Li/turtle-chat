from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
G4F_ROOT = ROOT / ".runtime" / "gpt4free-src"
sys.path.insert(0, str(G4F_ROOT))
RUNTIME_IMPORT_ERROR = None
try:
    openai_chat_module = importlib.import_module(
        "g4f.Provider.needs_auth.OpenaiChat"
    )
    OpenaiChat = openai_chat_module.OpenaiChat
    payload_has_explicit_rate_limit = (
        openai_chat_module._payload_has_explicit_rate_limit
    )
except ModuleNotFoundError as exc:
    openai_chat_module = None
    OpenaiChat = None
    payload_has_explicit_rate_limit = None
    RUNTIME_IMPORT_ERROR = exc


requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)


class FakeStreamSession:
    instances: list["FakeStreamSession"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.close_count = 0
        self.cookies = FakeCookies()
        self.instances.append(self)

    async def close(self) -> None:
        self.close_count += 1


class FakeCookies:
    def __init__(self) -> None:
        self.clear_count = 0

    def clear(self) -> None:
        self.clear_count += 1


@requires_runtime
def test_openai_account_leases_reusable_sessions_without_concurrent_sharing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_chat_module, "StreamSession", FakeStreamSession)
    OpenaiChat._turtle_http_session_pools = {}
    OpenaiChat._turtle_http_session_counts = {}
    OpenaiChat._turtle_http_session_lock = None
    FakeStreamSession.instances.clear()

    async def exercise() -> None:
        async with OpenaiChat._persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        ) as first:
            assert first.cookies.clear_count == 0
            async with OpenaiChat._persistent_session(
                proxy=None,
                timeout=360,
                curl_infos=[],
            ) as parallel:
                assert parallel is not first

        async with OpenaiChat._persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        ) as reused:
            assert reused is first

        assert len(FakeStreamSession.instances) == 2
        assert all(
            session.kwargs["max_clients"] == 4
            for session in FakeStreamSession.instances
        )
        assert first.cookies.clear_count == 0
        assert first.close_count == 0

        with pytest.raises(RuntimeError, match="discard this lease"):
            async with OpenaiChat._persistent_session(
                proxy="http://127.0.0.1:17897",
                timeout=360,
                curl_infos=[],
            ) as failed:
                raise RuntimeError("discard this lease")

        assert failed.close_count == 1
        assert (
            OpenaiChat._turtle_http_session_counts[
                ("http://127.0.0.1:17897", 360)
            ]
            == 0
        )

    asyncio.run(exercise())
    OpenaiChat._turtle_http_session_pools = {}
    OpenaiChat._turtle_http_session_counts = {}
    OpenaiChat._turtle_http_session_lock = None


@requires_runtime
def test_openai_account_recognizes_wrapped_explicit_rate_limits() -> None:
    assert payload_has_explicit_rate_limit(
        {"error": {"message": "You've hit your limit. Try again later."}}
    )
    assert payload_has_explicit_rate_limit(
        {"detail": {"code": "rate_limit_exceeded"}}
    )
    assert payload_has_explicit_rate_limit(
        {"error": {"type": "quota_exhausted"}}
    )
    assert not payload_has_explicit_rate_limit(
        {"error": {"message": "Temporary upstream server error"}}
    )


@requires_runtime
def test_openai_account_normalizes_streamed_quota_errors() -> None:
    class DummyConversation:
        p = None

    async def exercise() -> None:
        with pytest.raises(
            openai_chat_module.RateLimitError,
            match="account rate limit reached",
        ):
            await anext(
                OpenaiChat.iter_messages_line(
                    None,
                    None,
                    b'data: {"error":"You have hit your limit. Try again later."}',
                    DummyConversation(),
                    None,
                    None,
                )
            )

    asyncio.run(exercise())


@requires_runtime
def test_openai_account_continues_handoff_after_generic_sse_error() -> None:
    class DummyConversation:
        p = None
        task = None
        handoff_topic = "conversation-turn-safe-topic"

    async def exercise() -> None:
        iterator = OpenaiChat.iter_messages_line(
            None,
            None,
            b'data: {"error":"Error in message stream"}',
            DummyConversation(),
            None,
            None,
        )
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    asyncio.run(exercise())
