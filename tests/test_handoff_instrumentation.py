from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
G4F_ROOT = ROOT / ".runtime" / "gpt4free-src"
sys.path.insert(0, str(G4F_ROOT))
RUNTIME_IMPORT_ERROR = None
try:
    from g4f.Provider.needs_auth.OpenaiChat import (  # noqa: E402
        HANDOFF_TOPIC_CONVERSATIONS,
        HANDOFF_TOPIC_EXPECTED,
        HANDOFF_TOPIC_UNSCOPED,
        OpenaiChat,
    )
except ModuleNotFoundError as exc:
    OpenaiChat = None
    HANDOFF_TOPIC_EXPECTED = None
    HANDOFF_TOPIC_CONVERSATIONS = None
    HANDOFF_TOPIC_UNSCOPED = None
    RUNTIME_IMPORT_ERROR = exc


requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)


@requires_runtime
def test_handoff_items_report_only_bounded_topic_classes() -> None:
    expected = "conversation-turn-expected"
    frame = [
        {
            "topic_id": expected,
            "encoded_item": 'data: {"expected":true}',
        },
        {
            "topic_id": "conversations",
            "encoded_item": 'data: {"compatibility":true}',
        },
        {
            "encoded_item": 'data: {"unscoped":true}',
        },
        {
            "topic_id": "conversation-turn-other",
            "encoded_item": 'data: {"other":true}',
        },
    ]

    items = list(OpenaiChat.iter_handoff_items(frame, expected))

    assert items == [
        ('data: {"expected":true}', HANDOFF_TOPIC_EXPECTED),
        ('data: {"compatibility":true}', HANDOFF_TOPIC_CONVERSATIONS),
        ('data: {"unscoped":true}', HANDOFF_TOPIC_UNSCOPED),
    ]


@requires_runtime
@pytest.mark.parametrize(
    ("frame", "expected_class"),
    [
        (
            {
                "topic_id": "conversation-turn-expected",
                "type": "message_stream_complete",
            },
            HANDOFF_TOPIC_EXPECTED,
        ),
        (
            {
                "topic_id": "conversations",
                "type": "done",
            },
            HANDOFF_TOPIC_CONVERSATIONS,
        ),
        (
            {"type": "done"},
            HANDOFF_TOPIC_UNSCOPED,
        ),
        (
            {
                "topic_id": "conversation-turn-other",
                "type": "done",
            },
            None,
        ),
    ],
)
def test_handoff_done_reports_topic_class(
    frame: dict,
    expected_class: int | None,
) -> None:
    expected = "conversation-turn-expected"

    assert (
        OpenaiChat.handoff_frame_done_topic_class(frame, expected)
        == expected_class
    )
    assert OpenaiChat.handoff_frame_done(frame, expected) is (
        expected_class is not None
    )
