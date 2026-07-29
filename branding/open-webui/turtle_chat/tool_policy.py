"""Low-latency policy for Open WebUI's default builtin tool bundle."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_WORKSPACE_TOOL_INTENT = re.compile(
    r"""
    (?:
        (?:现在|当前).{0,6}(?:几点|时间|日期|几号|星期)
        |(?:今天|明天|后天|昨天)(?:是)?(?:几号|星期几|什么日期)
        |(?:搜索|搜一下|查找|找出|打开|查看|回顾).{0,10}
            (?:聊天|对话|历史记录|笔记|便签|知识库|频道|日历|日程|待办|任务)
        |(?:创建|新建|修改|更新|写入|删除).{0,10}
            (?:笔记|便签|自动化|日历|日程|待办|任务)
        |(?:提醒我|设置提醒|定时|自动化|委派|子代理)
        |(?:之前|以前|过去).{0,10}(?:聊过|说过|对话|聊天)
        |\bwhat\s+time\s+is\s+it\b
        |\b(?:current|local)\s+(?:time|date)\b
        |\b(?:search|find|open|view|review)\s+(?:my\s+)?(?:previous\s+)?
            (?:chats?|conversations?|history|notes?|knowledge\s+base|calendar|tasks?)\b
        |\b(?:create|add|update|edit|delete)\s+(?:a\s+|my\s+)?
            (?:note|automation|calendar\s+event|task|to-?do)\b
        |\b(?:remind\s+me|set\s+(?:a\s+)?reminder|delegate|subagent)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _has_items(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return bool(value)
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence):
        return bool(value)
    return bool(value)


def should_enable_builtin_tools(
    *,
    prompt: str | None,
    features: Mapping[str, Any] | None = None,
    files: Sequence[Any] | None = None,
    skill_ids: Sequence[str] | None = None,
    tool_ids: Sequence[str] | None = None,
    terminal_id: str | None = None,
    tool_servers: Sequence[Any] | None = None,
    model_knowledge: Sequence[Any] | None = None,
    note_chat: bool = False,
) -> bool:
    """Return whether this UI turn needs Open WebUI's large builtin tool set.

    Open WebUI 0.11 enables dozens of workspace tools for every browser turn.
    Resolving their permissions/specifications and sending all schemas upstream
    delays ordinary conversation. Explicit feature/context selections always
    retain the original tool behavior. A narrow intent guard also preserves the
    automatic workspace actions that have no composer toggle.
    """

    if note_chat:
        return True

    if features and any(bool(value) for value in features.values()):
        return True

    if any(
        _has_items(value)
        for value in (
            files,
            skill_ids,
            tool_ids,
            terminal_id,
            tool_servers,
            model_knowledge,
        )
    ):
        return True

    return bool(prompt and _WORKSPACE_TOOL_INTENT.search(prompt))
