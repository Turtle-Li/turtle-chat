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

_MEMORY_TOOL_INTENT = re.compile(
    r"""
    (?:
        (?:请|帮我)?(?:记住|记一下|保存到记忆|写入记忆|更新记忆|删除记忆|忘记)
        |\b(?:remember|memorize|save\s+(?:this\s+)?to\s+memory|update\s+(?:my\s+)?memory|
            forget|delete\s+from\s+memory)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_EXPLICIT_TOOL_FEATURES = (
    "web_search",
    "image_generation",
    "code_interpreter",
)

_IMAGE_GENERATION_INTENT = re.compile(
    r"""
    (?:
        (?:帮我|请|给我|为我)?(?:生成|画|绘制|制作|做|设计|创作|出)
        .{0,16}?
        (?:\d+\s*)?(?:张|幅|组|套)?
        (?:图片|图像|配图|插画|海报|封面(?:方图)?|效果图|商品图|主图)
        |\b(?:generate|create|make|draw|design|render)\b
        .{0,32}?
        \b(?:images?|pictures?|illustrations?|posters?|covers?|artwork)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_IMAGE_GENERATION_NEGATION = re.compile(
    r"""
    (?:
        (?:不要|别|无需|不需要|禁止).{0,8}(?:生成|画|绘制|制作|设计|创作|出图)
        |\b(?:do\s+not|don't|dont|no\s+need\s+to|without)\b
        .{0,20}?\b(?:generate|create|make|draw|design|render)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_IMAGE_GENERATION_EXPLANATION = re.compile(
    r"""
    (?:
        (?:如何|怎么|怎样|为什么|教程|解释|介绍).{0,18}(?:生成|画|绘制|制作|设计|创作)
        |\b(?:how|why|tutorial|explain)\b.{0,24}?
        \b(?:generate|create|make|draw|design|render)\b
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


def explicit_image_generation_intent(prompt: str | None) -> bool:
    """Recognize direct creation requests without treating questions as jobs."""

    if not prompt or not _IMAGE_GENERATION_INTENT.search(prompt):
        return False
    if _IMAGE_GENERATION_NEGATION.search(prompt):
        return False
    return not bool(_IMAGE_GENERATION_EXPLANATION.search(prompt))


def builtin_tool_reasons(
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
) -> tuple[str, ...]:
    """Return sanitized reasons for enabling Open WebUI's builtin tools."""

    reasons: list[str] = []

    if note_chat:
        reasons.append("note_chat")

    features = features or {}
    for feature in _EXPLICIT_TOOL_FEATURES:
        if bool(features.get(feature)):
            reasons.append(f"feature:{feature}")

    # Open WebUI enables memory context by default for ordinary UI requests.
    # Reading that context happens earlier in the middleware and does not need
    # the full CRUD tool bundle. Load those tools only for an explicit memory
    # action, keeping the default greeting path tool-free.
    if bool(features.get("memory")) and prompt and _MEMORY_TOOL_INTENT.search(prompt):
        reasons.append("feature:memory")

    for reason, value in (
        ("files", files),
        ("skills", skill_ids),
        ("selected_tools", tool_ids),
        ("terminal", terminal_id),
        ("tool_servers", tool_servers),
        ("model_knowledge", model_knowledge),
    ):
        if _has_items(value):
            reasons.append(reason)

    if prompt and _WORKSPACE_TOOL_INTENT.search(prompt):
        reasons.append("workspace_intent")

    return tuple(reasons)


def should_enable_builtin_tools(**kwargs: Any) -> bool:
    """Return whether this UI turn needs Open WebUI's large builtin tool set.

    Open WebUI 0.11 enables dozens of workspace tools for every browser turn.
    Resolving their permissions/specifications and sending all schemas upstream
    delays ordinary conversation. Explicit feature/context selections always
    retain the original tool behavior. A narrow intent guard also preserves the
    automatic workspace actions that have no composer toggle.
    """

    return bool(builtin_tool_reasons(**kwargs))
