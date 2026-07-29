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
except ModuleNotFoundError as exc:
    openai_chat_module = None
    OpenaiChat = None
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
        self.instances.append(self)

    async def close(self) -> None:
        self.close_count += 1


@requires_runtime
def test_openai_account_reuses_and_bounds_persistent_http_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_chat_module, "StreamSession", FakeStreamSession)
    OpenaiChat._turtle_http_session = None
    OpenaiChat._turtle_http_session_key = None
    OpenaiChat._turtle_http_session_lock = None
    FakeStreamSession.instances.clear()

    async def exercise() -> None:
        first = await OpenaiChat._get_persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        )
        reused = await OpenaiChat._get_persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        )
        replacement = await OpenaiChat._get_persistent_session(
            proxy="http://127.0.0.1:17897",
            timeout=360,
            curl_infos=[],
        )

        assert reused is first
        assert replacement is not first
        assert len(FakeStreamSession.instances) == 2
        assert first.close_count == 1
        assert replacement.kwargs["max_clients"] == 32

        await replacement.close()

    asyncio.run(exercise())
    OpenaiChat._turtle_http_session = None
    OpenaiChat._turtle_http_session_key = None
    OpenaiChat._turtle_http_session_lock = None
