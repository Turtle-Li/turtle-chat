from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "branding"
    / "open-webui"
    / "turtle_chat"
    / "upstream_cleanup.py"
)
SPEC = importlib.util.spec_from_file_location(
    "turtle_openwebui_upstream_cleanup",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OpenWebUICleanupHookTests(unittest.IsolatedAsyncioTestCase):
    def test_target_uses_first_gateway_without_exposing_other_keys(self) -> None:
        with patch.dict(
            MODULE.os.environ,
            {
                "OPENAI_API_BASE_URLS": (
                    "http://gateway:8000/v1;http://claude:8330/v1"
                ),
                "OPENAI_API_KEYS": "gateway-key;claude-key",
            },
            clear=True,
        ):
            target = MODULE._target()

        self.assertEqual(
            target,
            (
                "http://gateway:8000/internal/upstream-cleanup/schedule",
                "gateway-key",
            ),
        )

    async def test_schedule_is_background_and_contains_only_ids(self) -> None:
        captured: list[dict[str, str]] = []
        finished = asyncio.Event()

        async def fake_notify(payload):
            captured.append(payload)
            finished.set()

        with patch.object(MODULE, "_notify", fake_notify):
            MODULE.schedule_upstream_cleanup(
                chat_id="chat-a",
                user_id="user-a",
            )
            await asyncio.wait_for(finished.wait(), timeout=1)

        self.assertEqual(
            captured,
            [{"chat_id": "chat-a", "user_id": "user-a"}],
        )
