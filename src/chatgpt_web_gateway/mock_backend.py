from __future__ import annotations

import re
from typing import Iterable

from .models import ChatMessage


def _text(message: ChatMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    if not isinstance(message.content, list):
        return ""
    return "\n".join(
        str(item.get("text", ""))
        for item in message.content
        if item.get("type") in {"text", "input_text"}
    )


def mock_answer(messages: Iterable[ChatMessage]) -> str:
    materialized = list(messages)
    last_user = next((_text(item) for item in reversed(materialized) if item.role == "user"), "")
    if "叫什么" in last_user or "名字是什么" in last_user:
        transcript = "\n".join(_text(item) for item in materialized if item.role == "user")
        match = re.search(r"名字(?:是|叫)\s*([\w\u4e00-\u9fff-]{1,30})", transcript)
        if match:
            return match.group(1)
    if last_user.strip().lower() == "hello":
        return "hello"
    return f"mock: {last_user}" if last_user else "mock: empty input"


def chunks(text: str, width: int = 8) -> Iterable[str]:
    for start in range(0, len(text), width):
        yield text[start : start + width]

