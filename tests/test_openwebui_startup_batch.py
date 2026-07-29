from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STARTUP_BATCH_PATH = (
    PROJECT_ROOT
    / "branding"
    / "open-webui"
    / "turtle_chat"
    / "startup_batch.py"
)
spec = importlib.util.spec_from_file_location("turtle_startup_batch", STARTUP_BATCH_PATH)
assert spec and spec.loader
startup_batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup_batch)
ExistingChatWriteBatch = startup_batch.ExistingChatWriteBatch


def _upsert(history: dict, message_id: str, message: dict) -> dict:
    messages = history.setdefault("messages", {})
    existing = messages.get(message_id, {})
    saved = {
        **existing,
        **message,
        "id": message.get("id") or existing.get("id") or message_id,
        "childrenIds": message.get(
            "childrenIds",
            existing.get("childrenIds", []),
        ),
    }
    messages[message_id] = saved
    history["currentId"] = message_id
    return saved


def test_existing_chat_batch_stages_one_complete_turn_without_mutating_source() -> None:
    source_chat = {
        "title": "Existing",
        "models": ["gpt-5-web"],
        "history": {
            "currentId": "assistant-old",
            "messages": {
                "assistant-old": {
                    "id": "assistant-old",
                    "role": "assistant",
                    "content": "old",
                    "childrenIds": [],
                }
            },
        },
    }
    source = SimpleNamespace(
        id="chat-1",
        user_id="user-1",
        chat=deepcopy(source_chat),
        variables={"old": True},
    )

    batch = ExistingChatWriteBatch(source, _upsert)
    batch.stage_message(
        "user-new",
        {
            "id": "user-new",
            "role": "user",
            "parentId": "assistant-old",
            "childrenIds": [],
            "content": "hello",
        },
    )
    batch.stage_message(
        "assistant-old",
        {"childrenIds": ["user-new"]},
    )
    batch.stage_message(
        "user-new",
        {"childrenIds": ["assistant-new"]},
    )
    batch.stage_message(
        "assistant-new",
        {
            "id": "assistant-new",
            "role": "assistant",
            "parentId": "user-new",
            "childrenIds": [],
            "content": "",
            "done": False,
        },
    )
    batch.update_chat_fields(files=[], models=["gpt-5-web"])
    batch.update_variables({"new": True})

    assert source.chat == source_chat
    assert batch.chat["history"]["currentId"] == "assistant-new"
    assert batch.messages["assistant-old"]["childrenIds"] == ["user-new"]
    assert batch.messages["user-new"]["childrenIds"] == ["assistant-new"]
    assert batch.messages["assistant-new"]["parentId"] == "user-new"
    assert batch.chat["files"] == []
    assert batch.variables == {"new": True}
