from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
G4F_ROOT = ROOT / ".runtime" / "gpt4free-src"
sys.path.insert(0, str(G4F_ROOT))
RUNTIME_IMPORT_ERROR = None
try:
    from g4f.providers.response import FinishReason, ToolCalls  # noqa: E402
    from g4f.providers.tool_support import ToolSupportProvider  # noqa: E402
except ModuleNotFoundError as exc:
    FinishReason = None
    ToolCalls = None
    ToolSupportProvider = None
    RUNTIME_IMPORT_ERROR = exc

requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "clock",
            "description": "Read the current time.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def test_fixed_overlay_contains_incremental_tool_passthrough() -> None:
    overlay = (ROOT / "patches" / "gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert "diff --git a/g4f/providers/tool_support.py" in overlay
    assert "+def _may_be_tool_call_prefix(text: str) -> bool:" in overlay
    assert '+    return not stripped or stripped.startswith(("{", "[", "`"))' in overlay
    assert "+                        for buffered_chunk in content_chunks:" in overlay
    assert "+        if passthrough:" in overlay


async def _collect(iterator):
    return [item async for item in iterator]


@requires_runtime
def test_plain_tool_emulation_response_streams_before_provider_finishes() -> None:
    asyncio.run(_plain_response_streams_before_provider_finishes())


async def _plain_response_streams_before_provider_finishes() -> None:
    release = asyncio.Event()

    async def provider_method(**_kwargs):
        yield "你"
        await release.wait()
        yield "好"
        yield FinishReason("stop")

    with (
        patch(
            "g4f.providers.tool_support.get_model_and_provider",
            return_value=("gpt-test", object()),
        ),
        patch(
            "g4f.providers.tool_support.get_async_provider_method",
            return_value=provider_method,
        ),
    ):
        response = ToolSupportProvider.create_async_generator(
            model="gpt-test",
            messages=[{"role": "user", "content": "你好"}],
            stream=True,
            tools=TOOLS,
        )
        first = await asyncio.wait_for(anext(response), timeout=0.2)
        assert first == "你"
        release.set()
        remaining = await _collect(response)

    assert "好" in remaining
    assert any(isinstance(item, FinishReason) for item in remaining)


@requires_runtime
def test_json_tool_call_stays_buffered_until_it_can_be_parsed() -> None:
    asyncio.run(_json_tool_call_stays_buffered_until_it_can_be_parsed())


async def _json_tool_call_stays_buffered_until_it_can_be_parsed() -> None:
    release = asyncio.Event()
    provider_emitted_prefix = asyncio.Event()

    async def provider_method(**_kwargs):
        yield '{"tool_calls": ['
        provider_emitted_prefix.set()
        await release.wait()
        yield '{"name": "clock", "arguments": {}}]}'
        yield FinishReason("stop")

    with (
        patch(
            "g4f.providers.tool_support.get_model_and_provider",
            return_value=("gpt-test", object()),
        ),
        patch(
            "g4f.providers.tool_support.get_async_provider_method",
            return_value=provider_method,
        ),
    ):
        response = ToolSupportProvider.create_async_generator(
            model="gpt-test",
            messages=[{"role": "user", "content": "现在几点？"}],
            stream=True,
            tools=TOOLS,
        )
        first_item = asyncio.create_task(anext(response))
        await asyncio.wait_for(provider_emitted_prefix.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not first_item.done()
        release.set()
        items = [await first_item, *await _collect(response)]

    assert any(isinstance(item, ToolCalls) for item in items)
    assert not any(isinstance(item, str) for item in items)
