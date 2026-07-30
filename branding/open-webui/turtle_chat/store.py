"""Persistent chat groups, per-model windows, and request usage.

Production stores Turtle tables beside Open WebUI in the deployment's
dedicated PostgreSQL database. Explicit paths retain SQLite for deterministic
unit tests and one-time migration input. Retired point-ledger tables remain
readable for audit and rollback, but active requests neither debit nor expose
them. Usage records contain immutable user IDs and routing metadata only; they
never store prompts, responses, files, credentials, or upstream account data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..turtle_database import connect_postgres, is_postgres_url, runtime_database_url
from .announcement import normalize_announcement


MAX_MODEL_LIMIT = 1_000_000
MAX_CONCURRENCY = 100
MAX_STORAGE_QUOTA_BYTES = 20 * 1024**4
GIB = 1024**3
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 366 * 24 * 60 * 60
STALE_RESERVATION_SECONDS = 60 * 60
DEFAULT_SUBSCRIPTION_DAYS = 30
MAX_SUBSCRIPTION_DAYS = 10 * 366
DEFAULT_SUBSCRIPTION_TIMEZONE = "Asia/Shanghai"
MAX_ANNOUNCEMENTS = 200

SELECTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "latest:medium",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "latest",
        "version_label": "GPT-5.6 Sol",
        "level": "medium",
        "level_label": "中",
    },
    {
        "key": "latest:high",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "latest",
        "version_label": "GPT-5.6 Sol",
        "level": "high",
        "level_label": "高",
    },
    {
        "key": "latest:xhigh",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "latest",
        "version_label": "GPT-5.6 Sol",
        "level": "xhigh",
        "level_label": "极高",
    },
    {
        "key": "latest:pro",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "latest",
        "version_label": "GPT-5.6 Sol",
        "level": "pro",
        "level_label": "Pro",
    },
    {
        "key": "gpt-5-5:instant",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "gpt-5-5",
        "version_label": "GPT-5.5",
        "level": "instant",
        "level_label": "极速",
    },
    {
        "key": "gpt-5-3:standard",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "gpt-5-3",
        "version_label": "GPT-5.3",
        "level": "standard",
        "level_label": "标准",
    },
    {
        "key": "o3:standard",
        "model_id": "gpt-5-web",
        "family": "gpt",
        "verification_state": "verified",
        "version": "o3",
        "version_label": "o3",
        "level": "standard",
        "level_label": "推理",
    },
    {
        "key": "image:create",
        "model_id": "gpt-image",
        "family": "gpt",
        "verification_state": "verified",
        "version": "image",
        "version_label": "ChatGPT 图片",
        "level": "create",
        "level_label": "生图",
    },
    {
        "key": "claude-sonnet-5:standard",
        "model_id": "claude-web",
        "family": "claude",
        "verification_state": "verified",
        "version": "claude-sonnet-5",
        "version_label": "Claude Sonnet 5",
        "level": "standard",
        "level_label": "标准",
    },
    {
        "key": "claude-sonnet-5:extended",
        "model_id": "claude-web",
        "family": "claude",
        "verification_state": "verified",
        "version": "claude-sonnet-5",
        "version_label": "Claude Sonnet 5",
        "level": "extended",
        "level_label": "扩展思考",
    },
    {
        "key": "claude-opus-4-8:standard",
        "model_id": "claude-web",
        "family": "claude",
        "verification_state": "verified",
        "version": "claude-opus-4-8",
        "version_label": "Claude Opus 4.8",
        "level": "standard",
        "level_label": "标准",
    },
    {
        "key": "claude-opus-4-8:extended",
        "model_id": "claude-web",
        "family": "claude",
        "verification_state": "verified",
        "version": "claude-opus-4-8",
        "version_label": "Claude Opus 4.8",
        "level": "extended",
        "level_label": "扩展思考",
    },
    {
        "key": "claude-haiku-4-5:fast",
        "model_id": "claude-web",
        "family": "claude",
        "verification_state": "verified",
        "version": "claude-haiku-4-5",
        "version_label": "Claude Haiku 4.5",
        "level": "fast",
        "level_label": "快速",
    },
)

SELECTION_BY_KEY = {item["key"]: item for item in SELECTIONS}
SELECTION_KEYS = tuple(item["key"] for item in SELECTIONS)
PROVIDER_FAMILIES = ("gpt", "claude")
DEFAULT_PROVIDER_DISPLAY = {"gpt": "GPT", "claude": "Claude"}
SELECTION_KEYS_BY_FAMILY = {
    family: tuple(item["key"] for item in SELECTIONS if item["family"] == family)
    for family in PROVIDER_FAMILIES
}
DEFAULT_USER_SELECTIONS = (
    "latest:medium",
    "latest:high",
    "gpt-5-5:instant",
    "gpt-5-3:standard",
    "o3:standard",
    "image:create",
    "claude-sonnet-5:standard",
    "claude-haiku-4-5:fast",
)
DEFAULT_ADMIN_SELECTIONS = SELECTION_KEYS


def _rule(
    enabled: bool,
    limit_count: int | None,
    window_seconds: int,
    fallback_key: str | None = None,
    *,
    source: str = "site_rule",
    source_note: str = "",
    published_window_seconds: int | None = None,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "limit_count": limit_count,
        "window_seconds": window_seconds if limit_count is not None else 0,
        "fallback_key": fallback_key if enabled and limit_count is not None else None,
        "source": source,
        "source_note": source_note,
        "published_window_seconds": published_window_seconds,
    }


# These are conservative internal templates for the two isolated web-account
# providers. They are Turtle fairness windows, not either provider's official
# remaining allowance.
DEFAULT_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "basic",
        "name": "基础组",
        "description": "日常使用；GPT Pro/极高与 Claude Opus/扩展思考默认关闭。",
        "default_role": "user",
        "is_system": True,
        "storage_quota_bytes": 2 * GIB,
        "max_concurrency": 2,
        "default_user_concurrency": 1,
        "rules": {
            "latest:medium": _rule(True, 30, 3 * 60 * 60, "gpt-5-5:instant"),
            "latest:high": _rule(True, 20, 7 * 24 * 60 * 60, "latest:medium"),
            "latest:xhigh": _rule(False, 10, 7 * 24 * 60 * 60),
            "latest:pro": _rule(False, 5, 7 * 24 * 60 * 60),
            "gpt-5-5:instant": _rule(True, 80, 3 * 60 * 60),
            "gpt-5-3:standard": _rule(True, None, 0),
            "o3:standard": _rule(True, 100, 7 * 24 * 60 * 60, "latest:high"),
            "image:create": _rule(True, 20, 3 * 60 * 60),
            "claude-sonnet-5:standard": _rule(
                True, 30, 5 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(False, 10, 7 * 24 * 60 * 60),
            "claude-opus-4-8:standard": _rule(False, 10, 7 * 24 * 60 * 60),
            "claude-opus-4-8:extended": _rule(False, 5, 7 * 24 * 60 * 60),
            "claude-haiku-4-5:fast": _rule(True, 80, 5 * 60 * 60),
        },
    },
    {
        "id": "pro",
        "name": "Pro 组",
        "description": "开放全部已验证档位，并提供更高的独立时间窗额度。",
        "default_role": None,
        "is_system": True,
        "storage_quota_bytes": 50 * GIB,
        "max_concurrency": 4,
        "default_user_concurrency": 2,
        "rules": {
            "latest:medium": _rule(True, 150, 3 * 60 * 60, "gpt-5-5:instant"),
            "latest:high": _rule(True, 100, 7 * 24 * 60 * 60, "latest:medium"),
            "latest:xhigh": _rule(True, 50, 7 * 24 * 60 * 60, "latest:high"),
            "latest:pro": _rule(True, 25, 7 * 24 * 60 * 60, "latest:high"),
            "gpt-5-5:instant": _rule(True, 400, 3 * 60 * 60),
            "gpt-5-3:standard": _rule(True, None, 0),
            "o3:standard": _rule(True, None, 0),
            "image:create": _rule(True, 200, 3 * 60 * 60),
            "claude-sonnet-5:standard": _rule(
                True, 150, 5 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(
                True, 100, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:standard": _rule(
                True, 50, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:extended": _rule(
                True, 25, 7 * 24 * 60 * 60, "claude-opus-4-8:standard"
            ),
            "claude-haiku-4-5:fast": _rule(True, 400, 5 * 60 * 60),
        },
    },
    {
        "id": "admin",
        "name": "管理员组",
        "description": "全部已验证档位；默认不限制站内次数。",
        "default_role": "admin",
        "is_system": True,
        "storage_quota_bytes": 50 * GIB,
        "max_concurrency": 4,
        "default_user_concurrency": 2,
        "rules": {key: _rule(True, None, 0) for key in SELECTION_KEYS},
    },
)


OFFICIAL_GPT56_CHATGPT_URL = (
    "https://help.openai.com/en/articles/20001354-gpt-56-in-chatgpt"
)
OFFICIAL_PRO_TIERS_URL = (
    "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers"
)
OFFICIAL_CHATGPT_RELEASE_NOTES_URL = (
    "https://help.openai.com/en/articles/6825453-chatgpt-release-notes"
)
OFFICIAL_O3_LIMITS_URL = (
    "https://help.openai.com/en/articles/9824962-openai-o1-o1-mini-and-o3-mini-usage-limits-on-chatgpt-and-the-api"
)
OFFICIAL_CLAUDE_PRO_URL = (
    "https://support.claude.com/en/articles/8325606-what-is-the-pro-plan"
)
OFFICIAL_CLAUDE_MAX_URL = (
    "https://support.claude.com/en/articles/11049741-what-is-the-max-plan"
)
OFFICIAL_CLAUDE_LIMITS_URL = (
    "https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work"
)


# Plan availability and published notes follow the official product surfaces.
# Every selectable lane also has a concrete Turtle scheduling window so users
# can see an exact local remaining count and refresh time. A ``site_rule`` is
# intentionally labelled as a local allowance and must not be described as an
# official fixed limit.
CHAT_PLAN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "free",
        "sort_order": 10,
        "label": "Free 免费",
        "default_name": "Free 免费组",
        "default_description": "按 Free 官方可用性配置，并使用本站固定调度额度。",
        "official_note": (
            "官方公开：Free 可有限使用 GPT-5.5 极速，按 5 小时窗口动态调整；"
            "不包含 GPT-5.6 推理档。"
        ),
        "recommendation_note": "本站调度额度：GPT-5.5 极速 10 次/5 小时；不代表官方固定上限。",
        "sources": (
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ),
        "rules": {
            "latest:medium": _rule(False, None, 0, source="not_in_plan"),
            "latest:high": _rule(False, None, 0, source="not_in_plan"),
            "latest:xhigh": _rule(False, None, 0, source="not_in_plan"),
            "latest:pro": _rule(False, None, 0, source="not_in_plan"),
            "gpt-5-5:instant": _rule(
                True,
                10,
                5 * 60 * 60,
                source="site_rule",
                source_note="本站固定调度额度；官方只公布动态 5 小时窗口。",
                published_window_seconds=5 * 60 * 60,
            ),
            "gpt-5-3:standard": _rule(False, None, 0, source="not_in_plan"),
            "o3:standard": _rule(False, None, 0, source="not_in_plan"),
            "image:create": _rule(
                True,
                2,
                24 * 60 * 60,
                source="site_rule",
                source_note=(
                    "图片额度独立于文字消息；每次官方生图任务计 1 次，"
                    "任务返回多张仍计 1 次；官方 Free 上限动态变化。"
                ),
            ),
        },
    },
    {
        "id": "go",
        "sort_order": 20,
        "label": "Go",
        "default_name": "Go 组",
        "default_description": "按 Go 官方公开的可用性与次数配置。",
        "official_note": (
            "官方公开：GPT-5.5 极速 160 次/3 小时，Go Thinking 10 次/5 小时，"
            "且标准对话不包含 GPT-5.6。"
        ),
        "recommendation_note": (
            "Go 的 + Thinking 不是截图中的 GPT-5.6 模型菜单，本站不把它伪装成另一条模型路线。"
        ),
        "sources": (
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ),
        "rules": {
            "latest:medium": _rule(False, None, 0, source="not_in_plan"),
            "latest:high": _rule(False, None, 0, source="not_in_plan"),
            "latest:xhigh": _rule(False, None, 0, source="not_in_plan"),
            "latest:pro": _rule(False, None, 0, source="not_in_plan"),
            "gpt-5-5:instant": _rule(
                True,
                160,
                3 * 60 * 60,
                source="official_published",
            ),
            "gpt-5-3:standard": _rule(False, None, 0, source="not_in_plan"),
            "o3:standard": _rule(False, None, 0, source="not_in_plan"),
            "image:create": _rule(
                True,
                10,
                24 * 60 * 60,
                source="site_rule",
                source_note=(
                    "图片额度独立于文字消息；每次官方生图任务计 1 次，"
                    "任务返回多张仍计 1 次；官方 Go 固定次数未公开。"
                ),
            ),
        },
    },
    {
        "id": "plus",
        "sort_order": 30,
        "label": "Plus",
        "default_name": "Plus 组",
        "default_description": "按 Plus 官方模型可用性配置，并使用本站固定调度额度。",
        "official_note": (
            "官方公开：GPT-5.5 极速 160 次/3 小时；GPT-5.6 中、高可用；"
            "旧模型菜单包含 GPT-5.3 与 o3，极高与 Pro 不包含。"
        ),
        "recommendation_note": (
            "本站调度额度：中 150 次/3 小时，高 100 次/7 天，GPT-5.3 "
            "160 次/3 小时；这些是本站可计算额度，不代表官方固定上限。"
        ),
        "sources": (
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
            {
                "label": "ChatGPT Release Notes",
                "url": OFFICIAL_CHATGPT_RELEASE_NOTES_URL,
            },
            {"label": "o3 usage limits", "url": OFFICIAL_O3_LIMITS_URL},
        ),
        "rules": {
            "latest:medium": _rule(
                True,
                150,
                3 * 60 * 60,
                source="site_rule",
                source_note="本站固定调度额度；官方当前未公布固定次数。",
            ),
            "latest:high": _rule(
                True,
                100,
                7 * 24 * 60 * 60,
                source="site_rule",
                source_note="本站固定调度额度；官方当前未公布固定次数。",
            ),
            "latest:xhigh": _rule(False, None, 0, source="not_in_plan"),
            "latest:pro": _rule(False, None, 0, source="not_in_plan"),
            "gpt-5-5:instant": _rule(
                True,
                160,
                3 * 60 * 60,
                source="official_published",
            ),
            "gpt-5-3:standard": _rule(
                True,
                160,
                3 * 60 * 60,
                source="site_rule",
                source_note="本站固定调度额度；官方模型菜单未公布固定消息数。",
            ),
            "o3:standard": _rule(
                True,
                100,
                7 * 24 * 60 * 60,
                "latest:high",
                source="official_published",
                source_note="Plus 官方为 100 次/周，从首次使用起七天重置。",
            ),
            "image:create": _rule(
                True,
                40,
                3 * 60 * 60,
                source="site_rule",
                source_note=(
                    "图片额度独立于文字消息；每次官方生图任务计 1 次，"
                    "任务返回多张仍计 1 次；以历史常见 50/3h 留出 20% "
                    "安全余量，官方当前未公布固定值。"
                ),
            ),
        },
    },
    {
        "id": "pro-5x",
        "sort_order": 40,
        "label": "5× Pro",
        "default_name": "5× Pro 组",
        "default_description": "按 5× Pro 官方可用性配置，并使用本站固定调度额度。",
        "official_note": (
            "官方公开：5× Pro 与 20× Pro 能力相同；5× Pro 的计划用量为 Plus 的 5 倍。"
        ),
        "recommendation_note": (
            "本站按 Plus 调度基线的 5 倍提供可计算额度；这是本站调度规则，"
            "不代表官方逐模型固定上限。"
        ),
        "sources": (
            {"label": "About ChatGPT Pro tiers", "url": OFFICIAL_PRO_TIERS_URL},
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ),
        "rules": {
            "latest:medium": _rule(
                True, 750, 3 * 60 * 60, source="site_rule"
            ),
            "latest:high": _rule(
                True, 500, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "latest:xhigh": _rule(
                True, 250, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "latest:pro": _rule(
                True, 125, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "gpt-5-5:instant": _rule(
                True,
                800,
                3 * 60 * 60,
                source="site_rule",
                source_note="本站按 Plus 调度基线的 5 倍计算。",
            ),
            "image:create": _rule(
                True,
                200,
                3 * 60 * 60,
                source="site_rule",
                source_note=(
                    "每次官方生图任务计 1 次，任务返回多张仍计 1 次；"
                    "按本站 Plus 图片基线的 5 倍计算。"
                ),
            ),
            "gpt-5-3:standard": _rule(
                True,
                800,
                3 * 60 * 60,
                source="site_rule",
            ),
            "o3:standard": _rule(
                True,
                500,
                7 * 24 * 60 * 60,
                source="site_rule",
                source_note="本站按 Plus 调度基线的 5 倍计算。",
            ),
        },
    },
    {
        "id": "pro-20x",
        "sort_order": 50,
        "label": "20× Pro",
        "default_name": "20× Pro 组",
        "default_description": "按 20× Pro 官方可用性配置，并使用本站固定调度额度。",
        "official_note": (
            "官方公开：20× Pro 与 5× Pro 能力相同；20× Pro 的计划用量为 Plus 的 20 倍。"
        ),
        "recommendation_note": (
            "本站按 Plus 调度基线的 20 倍提供可计算额度；这是本站调度规则，"
            "不代表官方逐模型固定上限。"
        ),
        "sources": (
            {"label": "About ChatGPT Pro tiers", "url": OFFICIAL_PRO_TIERS_URL},
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ),
        "rules": {
            "latest:medium": _rule(
                True, 3_000, 3 * 60 * 60, source="site_rule"
            ),
            "latest:high": _rule(
                True, 2_000, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "latest:xhigh": _rule(
                True, 1_000, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "latest:pro": _rule(
                True, 500, 7 * 24 * 60 * 60, source="site_rule"
            ),
            "gpt-5-5:instant": _rule(
                True,
                3_200,
                3 * 60 * 60,
                source="site_rule",
                source_note="本站按 Plus 调度基线的 20 倍计算。",
            ),
            "image:create": _rule(
                True,
                800,
                3 * 60 * 60,
                source="site_rule",
                source_note=(
                    "每次官方生图任务计 1 次，任务返回多张仍计 1 次；"
                    "按本站 Plus 图片基线的 20 倍计算。"
                ),
            ),
            "gpt-5-3:standard": _rule(
                True,
                3_200,
                3 * 60 * 60,
                source="site_rule",
            ),
            "o3:standard": _rule(
                True,
                2_000,
                7 * 24 * 60 * 60,
                source="site_rule",
                source_note="本站按 Plus 调度基线的 20 倍计算。",
            ),
        },
    },
)

CLAUDE_PLAN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "free",
        "label": "Free 免费",
        "default_name": "Claude Free 组",
        "default_description": "仅开放 Sonnet 标准与 Haiku 快速；使用保守的站内日预算。",
        "official_note": "Claude Free 用量有限且动态变化，官方未公布逐模型固定消息数。",
        "recommendation_note": "本站建议 Sonnet 标准 8 次/24 小时、Haiku 快速 20 次/24 小时。",
        "sources": (
            {"label": "Claude usage limits", "url": OFFICIAL_CLAUDE_LIMITS_URL},
        ),
        "rules": {
            "claude-sonnet-5:standard": _rule(
                True, 8, 24 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(False, None, 0),
            "claude-opus-4-8:standard": _rule(False, None, 0),
            "claude-opus-4-8:extended": _rule(False, None, 0),
            "claude-haiku-4-5:fast": _rule(True, 20, 24 * 60 * 60),
        },
    },
    {
        "id": "pro",
        "label": "Pro",
        "default_name": "Claude Pro 组",
        "default_description": "按五小时会话和周额度使用保守的逐档站内预算。",
        "official_note": (
            "Claude Pro 每次会话至少为 Free 的 5 倍，基础会话额度每 5 小时重置，"
            "另有跨模型周额度。"
        ),
        "recommendation_note": (
            "实际消耗受上下文、模型和思考强度影响；逐档数字是站内建议，不是官方余额。"
        ),
        "sources": (
            {"label": "What is Claude Pro?", "url": OFFICIAL_CLAUDE_PRO_URL},
            {"label": "Claude usage limits", "url": OFFICIAL_CLAUDE_LIMITS_URL},
        ),
        "rules": {
            "claude-sonnet-5:standard": _rule(
                True, 30, 5 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(
                True, 10, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:standard": _rule(
                True, 10, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:extended": _rule(
                True, 5, 7 * 24 * 60 * 60, "claude-opus-4-8:standard"
            ),
            "claude-haiku-4-5:fast": _rule(True, 80, 5 * 60 * 60),
        },
    },
    {
        "id": "max-5x",
        "label": "Max 5×",
        "default_name": "Claude Max 5× 组",
        "default_description": "以 Pro 站内建议值的 5 倍为基准，开放全部已验证档位。",
        "official_note": (
            "Claude Max 5× 官方提供 Pro 每次会话用量的 5 倍，"
            "并设全模型周额度与 Sonnet 周额度。"
        ),
        "recommendation_note": "官方周额度是共享额度；本站逐档数字仅用于公平调度。",
        "sources": (
            {"label": "What is Claude Max?", "url": OFFICIAL_CLAUDE_MAX_URL},
        ),
        "rules": {
            "claude-sonnet-5:standard": _rule(
                True, 150, 5 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(
                True, 50, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:standard": _rule(
                True, 50, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:extended": _rule(
                True, 25, 7 * 24 * 60 * 60, "claude-opus-4-8:standard"
            ),
            "claude-haiku-4-5:fast": _rule(True, 400, 5 * 60 * 60),
        },
    },
    {
        "id": "max-20x",
        "label": "Max 20×",
        "default_name": "Claude Max 20× 组",
        "default_description": "以 Pro 站内建议值的 20 倍为基准，开放全部已验证档位。",
        "official_note": (
            "Claude Max 20× 官方提供 Pro 每次会话用量的 20 倍，"
            "并设全模型周额度与 Sonnet 周额度。"
        ),
        "recommendation_note": "官方周额度是共享额度；本站逐档数字仅用于公平调度。",
        "sources": (
            {"label": "What is Claude Max?", "url": OFFICIAL_CLAUDE_MAX_URL},
        ),
        "rules": {
            "claude-sonnet-5:standard": _rule(
                True, 600, 5 * 60 * 60, "claude-haiku-4-5:fast"
            ),
            "claude-sonnet-5:extended": _rule(
                True, 200, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:standard": _rule(
                True, 200, 7 * 24 * 60 * 60, "claude-sonnet-5:standard"
            ),
            "claude-opus-4-8:extended": _rule(
                True, 100, 7 * 24 * 60 * 60, "claude-opus-4-8:standard"
            ),
            "claude-haiku-4-5:fast": _rule(True, 1_600, 5 * 60 * 60),
        },
    },
)

CLAUDE_PLAN_GROUP_IDS = {
    "free": "claude-free-plan",
    "pro": "claude-pro-plan",
    "max-5x": "claude-max-5x",
    "max-20x": "claude-max-20x",
}
GPT_PLAN_GROUP_IDS = {
    "free": "gpt-free-plan",
    "go": "gpt-go-plan",
    "plus": "gpt-plus-plan",
    "pro-5x": "gpt-pro-5x",
    "pro-20x": "gpt-pro-20x",
}
PLAN_GROUP_IDS_BY_PROVIDER = {
    "gpt": GPT_PLAN_GROUP_IDS,
    "claude": CLAUDE_PLAN_GROUP_IDS,
}
PLAN_GROUP_PRESET_BY_ID = {
    group_id: (provider, preset_id)
    for provider, groups in PLAN_GROUP_IDS_BY_PROVIDER.items()
    for preset_id, group_id in groups.items()
}
PLAN_PRESET_SORT_ORDER = {
    provider: {
        str(preset["id"]): int(preset.get("sort_order") or (index + 1) * 10)
        for index, preset in enumerate(presets)
    }
    for provider, presets in {
        "gpt": CHAT_PLAN_PRESETS,
        "claude": CLAUDE_PLAN_PRESETS,
    }.items()
}

RETIRED_LEGACY_GROUP_IDS = frozenset({"pro"})


def _model_group_sort_order(group: dict[str, Any]) -> int:
    group_id = str(group.get("id") or "")
    provider = str(group.get("provider_family") or "")
    if group_id == f"{provider}-disabled":
        return 0
    template = PLAN_GROUP_PRESET_BY_ID.get(group_id)
    if template is not None:
        return 100 + PLAN_PRESET_SORT_ORDER[template[0]][template[1]]
    if group.get("default_role") == "admin":
        return 900
    if group.get("default_role") == "user":
        return 600
    if str(group.get("legacy_group_id") or "") == "pro":
        return 650
    if bool(group.get("is_system")):
        return 700
    return 800


def chat_plan_presets(provider_family: str = "gpt") -> list[dict[str, Any]]:
    """Return safe JSON-ready copies of the editable subscription presets."""

    provider = str(provider_family or "gpt").strip().lower()
    if provider == "gpt":
        presets = CHAT_PLAN_PRESETS
        selection_keys = SELECTION_KEYS
    elif provider == "claude":
        presets = CLAUDE_PLAN_PRESETS
        selection_keys = SELECTION_KEYS_BY_FAMILY["claude"]
    else:
        raise ChatPolicyError("账号 Provider 无效")
    result: list[dict[str, Any]] = []
    for preset in presets:
        item = {key: value for key, value in preset.items() if key != "rules"}
        item["provider_family"] = provider
        item["sources"] = [dict(source) for source in preset["sources"]]
        item["rules"] = [
            {
                "selection_key": key,
                **dict(preset["rules"].get(key) or _rule(False, None, 0)),
            }
            for key in selection_keys
        ]
        result.append(item)
    return result

DEFAULT_GROUP_BY_ROLE = {"user": "basic", "admin": "admin"}
DEFAULT_MODEL_GROUP_BY_ROLE = {
    family: {"user": f"{family}-basic", "admin": f"{family}-admin"}
    for family in PROVIDER_FAMILIES
}


class ChatPolicyError(ValueError):
    """Raised when a user asks for a selection outside their policy."""


class ChatSubscriptionError(ChatPolicyError):
    """Raised when an ordinary user's subscription cannot authorize a chat."""

    def __init__(self, subscription: dict[str, Any]):
        status = str(subscription.get("status") or "inactive")
        messages = {
            "pending": "账户等待管理员激活，当前不能发送消息",
            "scheduled": "订阅尚未开始，当前不能发送消息",
            "expired": "订阅已到期，请联系管理员续订",
            "cancelled": "订阅已停止，请联系管理员重新开通",
            "inactive": "账户尚未开通订阅，请联系管理员",
        }
        super().__init__(messages.get(status, messages["inactive"]))
        self.subscription = subscription


class ChatAnnouncementConflict(ChatPolicyError):
    """Raised when a dismissal targets an announcement that was replaced."""


class ChatAnnouncementNotFound(ChatPolicyError):
    """Raised when an administrator targets a missing or deleted announcement."""


class ChatModelQuotaError(ChatPolicyError):
    """Raised when a model window is exhausted and no fallback is available."""

    def __init__(self, selection_key: str, reset_at: int | None):
        selection = SELECTION_BY_KEY.get(selection_key, {})
        label = " · ".join(
            value
            for value in (selection.get("version_label"), selection.get("level_label"))
            if value
        ) or selection_key
        super().__init__(f"{label} 的当前额度已用完")
        self.selection_key = selection_key
        self.reset_at = reset_at


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    request_id: str
    user_id: str
    requested_selection_key: str
    selection_key: str
    group_id: str | None
    model_group_id: str | None
    provider_family: str
    fallback_from: str | None = None


def _now() -> int:
    return int(time.time())


def _subscription_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_SUBSCRIPTION_TIMEZONE)
    except ZoneInfoNotFoundError:
        raise ChatPolicyError("服务器缺少 Asia/Shanghai 时区数据")


def _subscription_end_of_day(
    starts_at: int,
    days: int = DEFAULT_SUBSCRIPTION_DAYS,
) -> int:
    timezone = _subscription_timezone()
    local_start = datetime.fromtimestamp(int(starts_at), timezone)
    end_date = local_start.date() + timedelta(days=int(days))
    local_end = datetime.combine(
        end_date,
        datetime_time(23, 59, 59),
        tzinfo=timezone,
    )
    return int(local_end.timestamp())


def _normalize_allowed(values: Iterable[str]) -> list[str]:
    requested = {str(value).strip() for value in values}
    unknown = requested.difference(SELECTION_BY_KEY)
    if unknown:
        raise ChatPolicyError(f"包含未知模型档位：{', '.join(sorted(unknown))}")
    normalized = [key for key in SELECTION_KEYS if key in requested]
    if not normalized:
        raise ChatPolicyError("至少保留一个可用模型档位")
    return normalized


def _selection_label(selection_key: str | None) -> str | None:
    if not selection_key:
        return None
    selection = SELECTION_BY_KEY.get(selection_key)
    if not selection:
        return selection_key
    return f"{selection['version_label']} · {selection['level_label']}"


class ChatStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        database_url: str | None = None,
    ):
        resolved_url = str(database_url or runtime_database_url()).strip()
        self.database_url = resolved_url if path is None and is_postgres_url(resolved_url) else ""
        self.backend = "postgresql" if self.database_url else "sqlite"
        self.path = (
            None
            if self.database_url
            else Path(
                path
                or os.getenv("TURTLE_CHAT_DB_PATH", "/app/backend/data/turtle-chat.db")
            )
        )
        self._lock = threading.RLock()
        self._initialize()

    def _guard(self):
        return self._lock if self.backend == "sqlite" else nullcontext()

    def _connect(self):
        if self.database_url:
            return connect_postgres(self.database_url)
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _begin(self, connection, *, lock_key: str | None = None) -> None:
        if self.backend == "postgresql":
            connection.begin(lock_key=lock_key)
        else:
            connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _is_integrity_error(error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError) or getattr(error, "sqlstate", None) in {
            "23503",
            "23505",
        }

    @staticmethod
    def _ensure_column(
        connection,
        table: str,
        column: str,
        declaration: str,
    ) -> bool:
        if getattr(connection, "backend", "sqlite") == "postgresql":
            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = current_schema() AND table_name = ?
                    """,
                    (table,),
                )
            }
        else:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            return True
        return False

    def _initialize(self) -> None:
        with self._guard(), self._connect() as connection:
            if self.backend == "sqlite":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_policy (
                    user_id       TEXT PRIMARY KEY,
                    allowed_json  TEXT NOT NULL,
                    metered       INTEGER NOT NULL CHECK (metered IN (0, 1)),
                    updated_by    TEXT NOT NULL,
                    updated_at    INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_ledger (
                    id            TEXT PRIMARY KEY,
                    user_id       TEXT NOT NULL,
                    delta         INTEGER NOT NULL CHECK (delta != 0),
                    reason        TEXT NOT NULL,
                    created_by    TEXT NOT NULL,
                    created_at    INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_ledger_user_created_idx
                    ON chat_ledger (user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS chat_usage (
                    id            TEXT PRIMARY KEY,
                    request_id    TEXT NOT NULL UNIQUE,
                    user_id       TEXT NOT NULL,
                    selection_key TEXT NOT NULL,
                    quota_window_id TEXT,
                    cost          INTEGER NOT NULL CHECK (cost >= 0),
                    status        TEXT NOT NULL CHECK (status IN ('reserved', 'committed', 'released')),
                    created_at    INTEGER NOT NULL,
                    finalized_at  INTEGER
                );
                CREATE INDEX IF NOT EXISTS chat_usage_user_created_idx
                    ON chat_usage (user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS chat_usage_user_selection_created_idx
                    ON chat_usage (user_id, selection_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS chat_usage_user_status_idx
                    ON chat_usage (user_id, status);
                CREATE INDEX IF NOT EXISTS chat_usage_stale_reservation_idx
                    ON chat_usage (created_at) WHERE status = 'reserved';

                CREATE TABLE IF NOT EXISTS chat_group (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL UNIQUE,
                    description   TEXT NOT NULL,
                    default_role  TEXT CHECK (default_role IS NULL OR default_role IN ('user', 'admin')),
                    is_system     INTEGER NOT NULL CHECK (is_system IN (0, 1)),
                    storage_quota_bytes BIGINT NOT NULL DEFAULT 2147483648,
                    max_concurrency INTEGER NOT NULL DEFAULT 2,
                    default_user_concurrency INTEGER NOT NULL DEFAULT 1,
                    gpt_account_pool_id TEXT NOT NULL DEFAULT 'gpt-default',
                    updated_by    TEXT NOT NULL,
                    created_at    INTEGER NOT NULL,
                    updated_at    INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS chat_group_default_role_idx
                    ON chat_group (default_role) WHERE default_role IS NOT NULL;

                CREATE TABLE IF NOT EXISTS chat_group_rule (
                    group_id       TEXT NOT NULL REFERENCES chat_group(id) ON DELETE CASCADE,
                    selection_key  TEXT NOT NULL,
                    enabled        INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    limit_count    INTEGER CHECK (limit_count IS NULL OR limit_count > 0),
                    window_seconds INTEGER NOT NULL CHECK (window_seconds >= 0),
                    fallback_key   TEXT,
                    PRIMARY KEY (group_id, selection_key)
                );

                CREATE TABLE IF NOT EXISTS chat_user_group (
                    user_id      TEXT PRIMARY KEY,
                    group_id     TEXT NOT NULL REFERENCES chat_group(id) ON DELETE RESTRICT,
                    assigned_by  TEXT NOT NULL,
                    assigned_at  INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_user_group_group_idx
                    ON chat_user_group (group_id);

                CREATE TABLE IF NOT EXISTS chat_model_group (
                    id              TEXT PRIMARY KEY,
                    provider_family TEXT NOT NULL CHECK (provider_family IN ('gpt', 'claude')),
                    name            TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    default_role    TEXT CHECK (default_role IS NULL OR default_role IN ('user', 'admin')),
                    is_system       INTEGER NOT NULL CHECK (is_system IN (0, 1)),
                    account_pool_id TEXT,
                    legacy_group_id TEXT,
                    updated_by      TEXT NOT NULL,
                    created_at      INTEGER NOT NULL,
                    updated_at      INTEGER NOT NULL,
                    UNIQUE (provider_family, name),
                    UNIQUE (provider_family, default_role),
                    UNIQUE (provider_family, legacy_group_id)
                );

                CREATE TABLE IF NOT EXISTS chat_model_group_rule (
                    group_id       TEXT NOT NULL REFERENCES chat_model_group(id) ON DELETE CASCADE,
                    selection_key  TEXT NOT NULL,
                    enabled        INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    limit_count    INTEGER CHECK (limit_count IS NULL OR limit_count > 0),
                    window_seconds INTEGER NOT NULL CHECK (window_seconds >= 0),
                    fallback_key   TEXT,
                    PRIMARY KEY (group_id, selection_key)
                );

                CREATE TABLE IF NOT EXISTS chat_user_model_group (
                    user_id         TEXT NOT NULL,
                    provider_family TEXT NOT NULL CHECK (provider_family IN ('gpt', 'claude')),
                    group_id        TEXT NOT NULL REFERENCES chat_model_group(id) ON DELETE RESTRICT,
                    assigned_by     TEXT NOT NULL,
                    assigned_at     INTEGER NOT NULL,
                    PRIMARY KEY (user_id, provider_family)
                );
                CREATE INDEX IF NOT EXISTS chat_user_model_group_group_idx
                    ON chat_user_model_group (group_id);

                CREATE TABLE IF NOT EXISTS chat_user_concurrency (
                    user_id         TEXT PRIMARY KEY,
                    max_concurrency INTEGER NOT NULL CHECK (max_concurrency > 0),
                    updated_by      TEXT NOT NULL,
                    updated_at      INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_provider_display (
                    provider_family TEXT PRIMARY KEY CHECK (provider_family IN ('gpt', 'claude')),
                    display_name     TEXT NOT NULL,
                    updated_by       TEXT NOT NULL,
                    created_at       INTEGER NOT NULL,
                    updated_at       INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_subscription (
                    user_id       TEXT PRIMARY KEY,
                    starts_at     BIGINT NOT NULL,
                    expires_at    BIGINT NOT NULL,
                    state         TEXT NOT NULL CHECK (state IN ('active', 'cancelled')),
                    created_by    TEXT NOT NULL,
                    updated_by    TEXT NOT NULL,
                    created_at    BIGINT NOT NULL,
                    updated_at    BIGINT NOT NULL,
                    CHECK (expires_at > starts_at)
                );
                CREATE INDEX IF NOT EXISTS chat_subscription_state_expiry_idx
                    ON chat_subscription (state, expires_at);

                CREATE TABLE IF NOT EXISTS chat_subscription_event (
                    id             TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    action         TEXT NOT NULL,
                    old_starts_at  BIGINT,
                    old_expires_at BIGINT,
                    old_state      TEXT,
                    new_starts_at  BIGINT,
                    new_expires_at BIGINT,
                    new_state      TEXT,
                    actor_id       TEXT NOT NULL,
                    created_at     BIGINT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_subscription_event_user_created_idx
                    ON chat_subscription_event (user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS chat_announcement (
                    id            INTEGER PRIMARY KEY CHECK (id = 1),
                    revision      BIGINT NOT NULL CHECK (revision > 0),
                    title         TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    enabled       INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_by    TEXT NOT NULL,
                    created_at    BIGINT NOT NULL,
                    updated_at    BIGINT NOT NULL,
                    CHECK (length(title) <= 120),
                    CHECK (length(body_markdown) <= 20000)
                );

                CREATE TABLE IF NOT EXISTS chat_announcement_receipt (
                    user_id      TEXT PRIMARY KEY,
                    revision     BIGINT NOT NULL CHECK (revision > 0),
                    dismissed_at BIGINT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_announcement_receipt_revision_idx
                    ON chat_announcement_receipt (revision);

                CREATE TABLE IF NOT EXISTS chat_announcement_item (
                    id            TEXT PRIMARY KEY,
                    revision      BIGINT NOT NULL CHECK (revision > 0),
                    title         TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    enabled       INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_by    TEXT NOT NULL,
                    created_at    BIGINT NOT NULL,
                    updated_at    BIGINT NOT NULL,
                    sort_order    BIGINT NOT NULL DEFAULT 0,
                    deleted_at    BIGINT,
                    deleted_by    TEXT,
                    CHECK (length(id) > 0 AND length(id) <= 80),
                    CHECK (length(title) <= 120),
                    CHECK (length(body_markdown) <= 20000)
                );
                CREATE TABLE IF NOT EXISTS chat_announcement_item_receipt (
                    announcement_id TEXT NOT NULL,
                    user_id          TEXT NOT NULL,
                    revision         BIGINT NOT NULL CHECK (revision > 0),
                    dismissed_at     BIGINT NOT NULL,
                    PRIMARY KEY (announcement_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS chat_announcement_item_receipt_user_idx
                    ON chat_announcement_item_receipt (user_id, dismissed_at DESC);

                CREATE TABLE IF NOT EXISTS chat_quota_window (
                    user_id       TEXT NOT NULL,
                    selection_key TEXT NOT NULL,
                    started_at    INTEGER NOT NULL,
                    window_id     TEXT,
                    PRIMARY KEY (user_id, selection_key)
                );
                """
            )
            self._ensure_column(
                connection,
                "chat_announcement_item",
                "sort_order",
                "BIGINT NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                UPDATE chat_announcement_item
                   SET sort_order = updated_at * 1000000000
                 WHERE sort_order = 0
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS chat_announcement_item_active_updated_idx
                    ON chat_announcement_item (enabled, sort_order DESC)
                    WHERE deleted_at IS NULL
                """
            )
            self._migrate_announcements_v2(connection)
            self._ensure_column(
                connection, "chat_usage", "requested_selection_key", "TEXT"
            )
            self._ensure_column(connection, "chat_usage", "fallback_from", "TEXT")
            self._ensure_column(connection, "chat_usage", "quota_window_id", "TEXT")
            self._ensure_column(connection, "chat_usage", "group_id", "TEXT")
            self._ensure_column(connection, "chat_usage", "model_group_id", "TEXT")
            self._ensure_column(connection, "chat_usage", "provider_family", "TEXT")
            self._ensure_column(connection, "chat_usage", "queued_at_ms", "BIGINT")
            self._ensure_column(connection, "chat_usage", "admitted_at_ms", "BIGINT")
            self._ensure_column(connection, "chat_usage", "connected_at_ms", "BIGINT")
            self._ensure_column(connection, "chat_usage", "first_content_at_ms", "BIGINT")
            self._ensure_column(connection, "chat_usage", "completed_at_ms", "BIGINT")
            self._ensure_column(connection, "chat_usage", "queue_ms", "INTEGER")
            self._ensure_column(connection, "chat_usage", "connect_ms", "INTEGER")
            self._ensure_column(connection, "chat_usage", "ttft_ms", "INTEGER")
            self._ensure_column(connection, "chat_usage", "total_ms", "INTEGER")
            self._ensure_column(connection, "chat_usage", "http_status", "INTEGER")
            self._ensure_column(connection, "chat_usage", "outcome", "TEXT")
            self._ensure_column(connection, "chat_usage", "error_type", "TEXT")
            self._ensure_column(connection, "chat_usage", "error_phase", "TEXT")
            self._ensure_column(connection, "chat_quota_window", "window_id", "TEXT")
            storage_added = self._ensure_column(
                connection,
                "chat_group",
                "storage_quota_bytes",
                "BIGINT NOT NULL DEFAULT 2147483648",
            )
            group_concurrency_added = self._ensure_column(
                connection,
                "chat_group",
                "max_concurrency",
                "INTEGER NOT NULL DEFAULT 2",
            )
            user_concurrency_added = self._ensure_column(
                connection,
                "chat_group",
                "default_user_concurrency",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                "chat_group",
                "gpt_account_pool_id",
                "TEXT NOT NULL DEFAULT 'gpt-default'",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS chat_usage_window_status_idx
                    ON chat_usage (user_id, selection_key, quota_window_id, status)
                """
            )
            self._seed_groups(connection)
            self._seed_provider_display(connection)
            self._migrate_model_groups(connection)
            self._seed_plan_groups(connection)
            if storage_added:
                connection.execute(
                    "UPDATE chat_group SET storage_quota_bytes = ? WHERE id IN ('pro', 'admin')",
                    (50 * GIB,),
                )
            if group_concurrency_added:
                connection.execute(
                    "UPDATE chat_group SET max_concurrency = 4 WHERE id IN ('pro', 'admin')"
                )
            if user_concurrency_added:
                connection.execute(
                    "UPDATE chat_group SET default_user_concurrency = 2 WHERE id IN ('pro', 'admin')"
                )
            if self.path is not None:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass

    @staticmethod
    def _subscription_row(connection, user_id: str):
        return connection.execute(
            """
            SELECT user_id, starts_at, expires_at, state, created_by, updated_by,
                   created_at, updated_at
              FROM chat_subscription
             WHERE user_id = ?
            """,
            (str(user_id),),
        ).fetchone()

    @staticmethod
    def _write_subscription_event(
        connection,
        *,
        user_id: str,
        action: str,
        old_row,
        new_row,
        actor_id: str,
        created_at: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO chat_subscription_event
                (id, user_id, action, old_starts_at, old_expires_at, old_state,
                 new_starts_at, new_expires_at, new_state, actor_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{time.time_ns():020d}-{uuid.uuid4().hex}",
                str(user_id),
                str(action),
                int(old_row["starts_at"]) if old_row is not None else None,
                int(old_row["expires_at"]) if old_row is not None else None,
                str(old_row["state"]) if old_row is not None else None,
                int(new_row["starts_at"]) if new_row is not None else None,
                int(new_row["expires_at"]) if new_row is not None else None,
                str(new_row["state"]) if new_row is not None else None,
                str(actor_id),
                int(created_at),
            ),
        )

    @staticmethod
    def _subscription_payload(
        row,
        role: str,
        *,
        now: int,
    ) -> dict[str, Any]:
        normalized_role = str(role or "pending")
        configured = row is not None
        starts_at = int(row["starts_at"]) if row is not None else None
        expires_at = int(row["expires_at"]) if row is not None else None
        stored_state = str(row["state"]) if row is not None else None
        if normalized_role == "admin":
            status = "unlimited"
            active = True
        elif normalized_role == "pending":
            status = "pending"
            active = False
        elif row is None:
            status = "inactive"
            active = False
        elif stored_state == "cancelled":
            status = "cancelled"
            active = False
        elif starts_at is not None and now < starts_at:
            status = "scheduled"
            active = False
        elif expires_at is not None and now > expires_at:
            status = "expired"
            active = False
        else:
            status = "active"
            active = True
        return {
            "status": status,
            "active": active,
            "configured": configured,
            "starts_at": starts_at,
            "expires_at": expires_at,
            "remaining_seconds": (
                max(0, int(expires_at) - int(now))
                if expires_at is not None and status in {"active", "scheduled"}
                else None
            ),
            "state": stored_state,
            "timezone": DEFAULT_SUBSCRIPTION_TIMEZONE,
            "default_days": DEFAULT_SUBSCRIPTION_DAYS,
            "updated_at": int(row["updated_at"]) if row is not None else None,
            "server_time": int(now),
        }

    @staticmethod
    def _normalize_subscription_window(
        *,
        starts_at: int | None,
        expires_at: int | None,
        duration_days: int,
        now: int,
    ) -> tuple[int, int]:
        try:
            normalized_days = int(duration_days)
        except (TypeError, ValueError) as exc:
            raise ChatPolicyError("订阅天数必须是整数") from exc
        if not 1 <= normalized_days <= MAX_SUBSCRIPTION_DAYS:
            raise ChatPolicyError(
                f"订阅天数必须在 1–{MAX_SUBSCRIPTION_DAYS} 天之间"
            )
        normalized_start = int(starts_at) if starts_at is not None else int(now)
        normalized_expiry = (
            int(expires_at)
            if expires_at is not None
            else _subscription_end_of_day(normalized_start, normalized_days)
        )
        if normalized_start < 0 or normalized_expiry <= normalized_start:
            raise ChatPolicyError("订阅到期时间必须晚于开始时间")
        if (
            normalized_expiry - normalized_start
            > (MAX_SUBSCRIPTION_DAYS + 1) * 24 * 60 * 60
        ):
            raise ChatPolicyError("单次订阅跨度不能超过 10 年")
        return normalized_start, normalized_expiry

    def subscription_for_user(
        self,
        user_id: str,
        role: str,
        *,
        create_default: bool = True,
    ) -> dict[str, Any]:
        """Resolve a user's subscription and lazily migrate existing members.

        Existing ordinary users receive one default 30-day window on first
        read after this feature is deployed. Pending users never auto-activate,
        and administrators are always exempt.
        """

        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-subscription:{user_id}")
            try:
                row = self._subscription_row(connection, user_id)
                if row is None and str(role) == "user" and create_default:
                    starts_at, expires_at = self._normalize_subscription_window(
                        starts_at=now,
                        expires_at=None,
                        duration_days=DEFAULT_SUBSCRIPTION_DAYS,
                        now=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO chat_subscription
                            (user_id, starts_at, expires_at, state, created_by,
                             updated_by, created_at, updated_at)
                        VALUES (?, ?, ?, 'active', 'system:migration',
                                'system:migration', ?, ?)
                        """,
                        (str(user_id), starts_at, expires_at, now, now),
                    )
                    row = self._subscription_row(connection, user_id)
                    self._write_subscription_event(
                        connection,
                        user_id=user_id,
                        action="default_provision",
                        old_row=None,
                        new_row=row,
                        actor_id="system:migration",
                        created_at=now,
                    )
                payload = self._subscription_payload(row, role, now=now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def require_active_subscription(
        self,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        subscription = self.subscription_for_user(user_id, role)
        if not subscription["active"]:
            raise ChatSubscriptionError(subscription)
        return subscription

    def set_subscription(
        self,
        user_id: str,
        role: str,
        *,
        starts_at: int | None = None,
        expires_at: int | None = None,
        duration_days: int = DEFAULT_SUBSCRIPTION_DAYS,
        updated_by: str,
    ) -> dict[str, Any]:
        if str(role) == "admin":
            raise ChatPolicyError("管理员账号无需设置订阅有效期")
        now = _now()
        normalized_start, normalized_expiry = self._normalize_subscription_window(
            starts_at=starts_at,
            expires_at=expires_at,
            duration_days=duration_days,
            now=now,
        )
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-subscription:{user_id}")
            try:
                old_row = self._subscription_row(connection, user_id)
                action = "activate" if old_row is None else "update"
                if old_row is not None and (
                    str(old_row["state"]) == "cancelled"
                    or int(old_row["expires_at"]) < now
                ):
                    action = "reactivate"
                created_by = (
                    str(old_row["created_by"]) if old_row is not None else str(updated_by)
                )
                created_at = (
                    int(old_row["created_at"]) if old_row is not None else now
                )
                connection.execute(
                    """
                    INSERT INTO chat_subscription
                        (user_id, starts_at, expires_at, state, created_by,
                         updated_by, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        starts_at = excluded.starts_at,
                        expires_at = excluded.expires_at,
                        state = 'active',
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(user_id),
                        normalized_start,
                        normalized_expiry,
                        created_by,
                        str(updated_by),
                        created_at,
                        now,
                    ),
                )
                new_row = self._subscription_row(connection, user_id)
                self._write_subscription_event(
                    connection,
                    user_id=user_id,
                    action=action,
                    old_row=old_row,
                    new_row=new_row,
                    actor_id=updated_by,
                    created_at=now,
                )
                payload = self._subscription_payload(new_row, role, now=now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def extend_subscription(
        self,
        user_id: str,
        role: str,
        *,
        days: int = DEFAULT_SUBSCRIPTION_DAYS,
        updated_by: str,
    ) -> dict[str, Any]:
        if str(role) == "admin":
            raise ChatPolicyError("管理员账号无需设置订阅有效期")
        try:
            normalized_days = int(days)
        except (TypeError, ValueError) as exc:
            raise ChatPolicyError("续订天数必须是整数") from exc
        if not 1 <= normalized_days <= MAX_SUBSCRIPTION_DAYS:
            raise ChatPolicyError(
                f"续订天数必须在 1–{MAX_SUBSCRIPTION_DAYS} 天之间"
            )
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-subscription:{user_id}")
            try:
                old_row = self._subscription_row(connection, user_id)
                current = (
                    old_row is not None
                    and str(old_row["state"]) == "active"
                    and int(old_row["expires_at"]) >= now
                )
                starts_at = int(old_row["starts_at"]) if current else now
                base_at = int(old_row["expires_at"]) if current else now
                expires_at = _subscription_end_of_day(base_at, normalized_days)
                if (
                    expires_at - starts_at
                    > (MAX_SUBSCRIPTION_DAYS + 1) * 24 * 60 * 60
                ):
                    raise ChatPolicyError("续订后的总跨度不能超过 10 年")
                created_by = (
                    str(old_row["created_by"]) if old_row is not None else str(updated_by)
                )
                created_at = (
                    int(old_row["created_at"]) if old_row is not None else now
                )
                connection.execute(
                    """
                    INSERT INTO chat_subscription
                        (user_id, starts_at, expires_at, state, created_by,
                         updated_by, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        starts_at = excluded.starts_at,
                        expires_at = excluded.expires_at,
                        state = 'active',
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(user_id),
                        starts_at,
                        expires_at,
                        created_by,
                        str(updated_by),
                        created_at,
                        now,
                    ),
                )
                new_row = self._subscription_row(connection, user_id)
                self._write_subscription_event(
                    connection,
                    user_id=user_id,
                    action="extend" if current else "renew",
                    old_row=old_row,
                    new_row=new_row,
                    actor_id=updated_by,
                    created_at=now,
                )
                payload = self._subscription_payload(new_row, role, now=now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def cancel_subscription(
        self,
        user_id: str,
        role: str,
        *,
        updated_by: str,
    ) -> dict[str, Any]:
        if str(role) == "admin":
            raise ChatPolicyError("管理员账号无需设置订阅有效期")
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-subscription:{user_id}")
            try:
                old_row = self._subscription_row(connection, user_id)
                if old_row is None:
                    raise ChatPolicyError("该用户尚未配置订阅")
                connection.execute(
                    """
                    UPDATE chat_subscription
                       SET state = 'cancelled', updated_by = ?, updated_at = ?
                     WHERE user_id = ?
                    """,
                    (str(updated_by), now, str(user_id)),
                )
                new_row = self._subscription_row(connection, user_id)
                self._write_subscription_event(
                    connection,
                    user_id=user_id,
                    action="cancel",
                    old_row=old_row,
                    new_row=new_row,
                    actor_id=updated_by,
                    created_at=now,
                )
                payload = self._subscription_payload(new_row, role, now=now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def subscription_events(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(100, int(limit)))
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, action, old_starts_at, old_expires_at,
                       old_state, new_starts_at, new_expires_at, new_state,
                       actor_id, created_at
                  FROM chat_subscription_event
                 WHERE user_id = ?
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (str(user_id), normalized_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _migrate_announcements_v2(self, connection) -> None:
        """Copy the former singleton and its receipts once into the list schema."""

        self._begin(connection, lock_key="chat-announcement:v2-migration")
        try:
            legacy = connection.execute(
                """
                SELECT revision, title, body_markdown, enabled, updated_by,
                       created_at, updated_at
                  FROM chat_announcement
                 WHERE id = 1
                """
            ).fetchone()
            if legacy is not None:
                connection.execute(
                    """
                    INSERT INTO chat_announcement_item
                        (id, revision, title, body_markdown, enabled, updated_by,
                         created_at, updated_at, sort_order, deleted_at, deleted_by)
                    VALUES ('legacy-singleton', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        int(legacy["revision"]),
                        str(legacy["title"]),
                        str(legacy["body_markdown"]),
                        int(bool(legacy["enabled"])),
                        str(legacy["updated_by"]),
                        int(legacy["created_at"]),
                        int(legacy["updated_at"]),
                        int(legacy["updated_at"]) * 1_000_000_000,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chat_announcement_item_receipt
                        (announcement_id, user_id, revision, dismissed_at)
                    SELECT 'legacy-singleton', user_id, revision, dismissed_at
                      FROM chat_announcement_receipt
                     WHERE 1 = 1
                    ON CONFLICT(announcement_id, user_id) DO NOTHING
                    """
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _announcement_payload(
        row,
        *,
        dismissed: bool = False,
        role: str | None = None,
    ) -> dict[str, Any]:
        if row is None:
            return {
                "id": None,
                "revision": 0,
                "title": "",
                "body_markdown": "",
                "enabled": False,
                "updated_by": None,
                "created_at": None,
                "updated_at": None,
                "dismissed": False,
                "should_show": False,
            }
        enabled = bool(row["enabled"])
        return {
            "id": str(row["id"]),
            "revision": int(row["revision"]),
            "title": str(row["title"]),
            "body_markdown": str(row["body_markdown"]),
            "enabled": enabled,
            "updated_by": str(row["updated_by"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "dismissed": bool(dismissed),
            "should_show": bool(
                enabled
                and not dismissed
                and str(role or "") in {"user", "pending"}
            ),
        }

    @staticmethod
    def _normalize_announcement_id(announcement_id: str) -> str:
        normalized = str(announcement_id or "").strip()
        if not normalized or len(normalized) > 80:
            raise ChatAnnouncementNotFound("公告不存在或已删除")
        return normalized

    def announcements_admin(self) -> list[dict[str, Any]]:
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, revision, title, body_markdown, enabled, updated_by,
                       created_at, updated_at
                 FROM chat_announcement_item
                 WHERE deleted_at IS NULL
                 ORDER BY sort_order DESC, id DESC
                 LIMIT 200
                """
            ).fetchall()
        return [self._announcement_payload(row) for row in rows]

    def announcements_for_user(
        self,
        user_id: str,
        role: str,
    ) -> list[dict[str, Any]]:
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT item.id, item.revision, item.title, item.body_markdown,
                       item.enabled, item.updated_by, item.created_at,
                       item.updated_at, receipt.revision AS dismissed_revision
                  FROM chat_announcement_item AS item
                  LEFT JOIN chat_announcement_item_receipt AS receipt
                    ON receipt.announcement_id = item.id
                   AND receipt.user_id = ?
                 WHERE item.deleted_at IS NULL
                   AND item.enabled = 1
                 ORDER BY item.sort_order DESC, item.id DESC
                 LIMIT 200
                """,
                (str(user_id),),
            ).fetchall()
        return [
            self._announcement_payload(
                row,
                dismissed=bool(
                    row["dismissed_revision"] is not None
                    and int(row["dismissed_revision"]) == int(row["revision"])
                ),
                role=role,
            )
            for row in rows
        ]

    def announcement_admin(self) -> dict[str, Any]:
        """Compatibility view for the previously cached singleton admin UI."""

        items = self.announcements_admin()
        return items[0] if items else self._announcement_payload(None)

    def announcement_for_user(
        self,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        """Compatibility view returning the first unread, then first active item."""

        items = self.announcements_for_user(user_id, role)
        return (
            next((item for item in items if item["should_show"]), None)
            or (items[0] if items else self._announcement_payload(None))
        )

    def create_announcement(
        self,
        *,
        title: str,
        body_markdown: str,
        enabled: bool,
        updated_by: str,
    ) -> dict[str, Any]:
        try:
            normalized_title, normalized_body, normalized_enabled = (
                normalize_announcement(
                    title,
                    body_markdown,
                    enabled=enabled,
                )
            )
        except ValueError as exc:
            raise ChatPolicyError(str(exc)) from exc
        announcement_id = str(uuid.uuid4())
        now = _now()
        sort_order = time.time_ns()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key="chat-announcement:create")
            try:
                current_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                          FROM chat_announcement_item
                         WHERE deleted_at IS NULL
                        """
                    ).fetchone()[0]
                )
                if current_count >= MAX_ANNOUNCEMENTS:
                    raise ChatPolicyError(
                        f"公告最多保留 {MAX_ANNOUNCEMENTS} 条，请先删除旧公告"
                    )
                connection.execute(
                    """
                    INSERT INTO chat_announcement_item
                        (id, revision, title, body_markdown, enabled, updated_by,
                         created_at, updated_at, sort_order, deleted_at, deleted_by)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        announcement_id,
                        normalized_title,
                        normalized_body,
                        int(normalized_enabled),
                        str(updated_by),
                        now,
                        now,
                        sort_order,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, revision, title, body_markdown, enabled, updated_by,
                           created_at, updated_at
                      FROM chat_announcement_item
                     WHERE id = ?
                    """,
                    (announcement_id,),
                ).fetchone()
                payload = self._announcement_payload(row)
                payload["changed"] = True
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def update_announcement(
        self,
        announcement_id: str,
        *,
        title: str,
        body_markdown: str,
        enabled: bool,
        updated_by: str,
    ) -> dict[str, Any]:
        normalized_id = self._normalize_announcement_id(announcement_id)
        try:
            normalized_title, normalized_body, normalized_enabled = (
                normalize_announcement(
                    title,
                    body_markdown,
                    enabled=enabled,
                )
            )
        except ValueError as exc:
            raise ChatPolicyError(str(exc)) from exc
        now = _now()
        sort_order = time.time_ns()
        with self._guard(), self._connect() as connection:
            self._begin(
                connection,
                lock_key=f"chat-announcement:update:{normalized_id}",
            )
            try:
                old_row = connection.execute(
                    """
                    SELECT id, revision, title, body_markdown, enabled, updated_by,
                           created_at, updated_at
                      FROM chat_announcement_item
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (normalized_id,),
                ).fetchone()
                if old_row is None:
                    raise ChatAnnouncementNotFound("公告不存在或已删除")
                unchanged = bool(
                    str(old_row["title"]) == normalized_title
                    and str(old_row["body_markdown"]) == normalized_body
                    and bool(old_row["enabled"]) == normalized_enabled
                )
                if unchanged:
                    payload = self._announcement_payload(old_row)
                    payload["changed"] = False
                    connection.execute("COMMIT")
                    return payload
                connection.execute(
                    """
                    UPDATE chat_announcement_item
                       SET revision = revision + 1,
                           title = ?,
                           body_markdown = ?,
                           enabled = ?,
                           updated_by = ?,
                           updated_at = ?,
                           sort_order = ?
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        normalized_title,
                        normalized_body,
                        int(normalized_enabled),
                        str(updated_by),
                        now,
                        sort_order,
                        normalized_id,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, revision, title, body_markdown, enabled, updated_by,
                           created_at, updated_at
                      FROM chat_announcement_item
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (normalized_id,),
                ).fetchone()
                payload = self._announcement_payload(row)
                payload["changed"] = True
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def delete_announcement(
        self,
        announcement_id: str,
        *,
        deleted_by: str,
    ) -> dict[str, Any]:
        normalized_id = self._normalize_announcement_id(announcement_id)
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(
                connection,
                lock_key=f"chat-announcement:delete:{normalized_id}",
            )
            try:
                row = connection.execute(
                    """
                    SELECT id
                      FROM chat_announcement_item
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (normalized_id,),
                ).fetchone()
                if row is None:
                    raise ChatAnnouncementNotFound("公告不存在或已删除")
                connection.execute(
                    """
                    UPDATE chat_announcement_item
                       SET enabled = 0,
                           deleted_at = ?,
                           deleted_by = ?,
                           updated_at = ?
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (now, str(deleted_by), now, normalized_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"id": normalized_id, "deleted": True}

    def set_announcement(
        self,
        *,
        title: str,
        body_markdown: str,
        enabled: bool,
        updated_by: str,
    ) -> dict[str, Any]:
        """Compatibility mutation for the previously cached singleton admin UI."""

        current = self.announcement_admin()
        if current.get("id"):
            return self.update_announcement(
                str(current["id"]),
                title=title,
                body_markdown=body_markdown,
                enabled=enabled,
                updated_by=updated_by,
            )
        return self.create_announcement(
            title=title,
            body_markdown=body_markdown,
            enabled=enabled,
            updated_by=updated_by,
        )

    def dismiss_announcement(
        self,
        user_id: str,
        announcement_id: str,
        revision: int,
    ) -> dict[str, Any]:
        normalized_id = self._normalize_announcement_id(announcement_id)
        try:
            normalized_revision = int(revision)
        except (TypeError, ValueError) as exc:
            raise ChatAnnouncementConflict("公告版本无效，请刷新页面") from exc
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(
                connection,
                lock_key=f"chat-announcement-receipt:{normalized_id}:{user_id}",
            )
            try:
                row = connection.execute(
                    """
                    SELECT id, revision, title, body_markdown, enabled, updated_by,
                           created_at, updated_at
                      FROM chat_announcement_item
                     WHERE id = ? AND deleted_at IS NULL
                    """,
                    (normalized_id,),
                ).fetchone()
                if row is None or not bool(row["enabled"]):
                    raise ChatAnnouncementConflict("当前公告已停用或删除")
                if int(row["revision"]) != normalized_revision:
                    raise ChatAnnouncementConflict("公告已经更新，请刷新后重新查看")
                connection.execute(
                    """
                    INSERT INTO chat_announcement_item_receipt
                        (announcement_id, user_id, revision, dismissed_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(announcement_id, user_id) DO UPDATE SET
                        revision = excluded.revision,
                        dismissed_at = excluded.dismissed_at
                    """,
                    (normalized_id, str(user_id), normalized_revision, now),
                )
                payload = self._announcement_payload(
                    row,
                    dismissed=True,
                    role="user",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return payload

    def dismiss_current_announcement(
        self,
        user_id: str,
        role: str,
        revision: int,
    ) -> dict[str, Any]:
        """Compatibility mutation for old clients that did not send an item ID."""

        items = self.announcements_for_user(user_id, role)
        current = next(
            (
                item
                for item in items
                if item["should_show"] and int(item["revision"]) == int(revision)
            ),
            None,
        )
        if current is None:
            current = next(
                (item for item in items if int(item["revision"]) == int(revision)),
                None,
            )
        if current is None:
            raise ChatAnnouncementConflict("公告已经更新，请刷新后重新查看")
        return self.dismiss_announcement(
            user_id,
            str(current["id"]),
            revision,
        )

    def _seed_groups(self, connection) -> None:
        created_at = _now()
        for group in DEFAULT_GROUPS:
            connection.execute(
                """
                INSERT INTO chat_group
                    (id, name, description, default_role, is_system,
                     storage_quota_bytes, max_concurrency, default_user_concurrency,
                     updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'system', ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    group["id"],
                    group["name"],
                    group["description"],
                    group["default_role"],
                    int(group["is_system"]),
                    int(group["storage_quota_bytes"]),
                    int(group["max_concurrency"]),
                    int(group["default_user_concurrency"]),
                    created_at,
                    created_at,
                ),
            )
            for selection_key in SELECTION_KEYS:
                rule = group["rules"][selection_key]
                connection.execute(
                    """
                    INSERT INTO chat_group_rule
                        (group_id, selection_key, enabled, limit_count, window_seconds, fallback_key)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        group["id"],
                        selection_key,
                        int(rule["enabled"]),
                        rule["limit_count"],
                        rule["window_seconds"],
                        rule["fallback_key"],
                    ),
                )

    def _seed_provider_display(self, connection) -> None:
        created_at = _now()
        for provider_family, display_name in DEFAULT_PROVIDER_DISPLAY.items():
            connection.execute(
                """
                INSERT INTO chat_provider_display
                    (provider_family, display_name, updated_by, created_at, updated_at)
                VALUES (?, ?, 'system', ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (provider_family, display_name, created_at, created_at),
            )

    @staticmethod
    def _legacy_model_group_id(provider_family: str, legacy_group_id: str) -> str:
        candidate = f"{provider_family}-{legacy_group_id}"
        if len(candidate) <= 80:
            return candidate
        stable = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"turtle:model-group:{provider_family}:{legacy_group_id}",
        )
        return f"{provider_family}-{stable}"

    def _migrate_model_groups(self, connection) -> None:
        """Project every legacy combined group into one group per Provider.

        The projection is additive and idempotent. Existing Provider groups and
        assignments always win, so restarting after an administrator edits the
        new model groups never overwrites those edits.
        """

        created_at = _now()
        groups = connection.execute(
            """
            SELECT id, name, description, default_role, is_system,
                   gpt_account_pool_id, updated_by, created_at, updated_at
              FROM chat_group
             ORDER BY created_at, id
            """
        ).fetchall()
        for row in groups:
            legacy_group_id = str(row["id"])
            legacy_rules = self._group_rules(connection, legacy_group_id)
            if not legacy_rules:
                continue
            for provider_family in PROVIDER_FAMILIES:
                model_group_id = self._legacy_model_group_id(
                    provider_family, legacy_group_id
                )
                connection.execute(
                    """
                    INSERT INTO chat_model_group
                        (id, provider_family, name, description, default_role,
                         is_system, account_pool_id, legacy_group_id, updated_by,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        model_group_id,
                        provider_family,
                        str(row["name"]),
                        str(row["description"]),
                        row["default_role"],
                        int(row["is_system"]),
                        (
                            str(row["gpt_account_pool_id"] or "gpt-default")
                            if provider_family == "gpt"
                            else "claude-default"
                        ),
                        legacy_group_id,
                        str(row["updated_by"]),
                        int(row["created_at"]),
                        int(row["updated_at"]),
                    ),
                )
                for selection_key in SELECTION_KEYS_BY_FAMILY[provider_family]:
                    rule = legacy_rules.get(selection_key) or _rule(False, None, 0)
                    connection.execute(
                        """
                        INSERT INTO chat_model_group_rule
                            (group_id, selection_key, enabled, limit_count,
                             window_seconds, fallback_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            model_group_id,
                            selection_key,
                            int(rule["enabled"]),
                            rule["limit_count"],
                            rule["window_seconds"],
                            rule["fallback_key"],
                        ),
                    )

        for provider_family in PROVIDER_FAMILIES:
            disabled_id = f"{provider_family}-disabled"
            connection.execute(
                """
                INSERT INTO chat_model_group
                    (id, provider_family, name, description, default_role,
                     is_system, account_pool_id, legacy_group_id, updated_by,
                     created_at, updated_at)
                VALUES (?, ?, '未开通组', '该 Provider 的全部模型档位均关闭。',
                        NULL, 1, ?, NULL, 'system', ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    disabled_id,
                    provider_family,
                    f"{provider_family}-default",
                    created_at,
                    created_at,
                ),
            )
            for selection_key in SELECTION_KEYS_BY_FAMILY[provider_family]:
                connection.execute(
                    """
                    INSERT INTO chat_model_group_rule
                        (group_id, selection_key, enabled, limit_count,
                         window_seconds, fallback_key)
                    VALUES (?, ?, 0, NULL, 0, NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    (disabled_id, selection_key),
                )

        connection.execute(
            """
            UPDATE chat_model_group
               SET account_pool_id = provider_family || '-default'
             WHERE account_pool_id IS NULL OR account_pool_id = ''
            """
        )

        assignments = connection.execute(
            """
            SELECT user_id, group_id, assigned_by, assigned_at
              FROM chat_user_group
            """
        ).fetchall()
        for assignment in assignments:
            for provider_family in PROVIDER_FAMILIES:
                model_group = connection.execute(
                    """
                    SELECT id FROM chat_model_group
                     WHERE provider_family = ? AND legacy_group_id = ?
                    """,
                    (provider_family, str(assignment["group_id"])),
                ).fetchone()
                if model_group is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO chat_user_model_group
                        (user_id, provider_family, group_id, assigned_by, assigned_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(assignment["user_id"]),
                        provider_family,
                        str(model_group["id"]),
                        str(assignment["assigned_by"]),
                        int(assignment["assigned_at"]),
                    ),
                )

    def _seed_plan_groups(self, connection) -> None:
        """Create immutable plan baselines with explicit Turtle scheduling limits."""

        created_at = _now()
        for provider in PROVIDER_FAMILIES:
            for preset in chat_plan_presets(provider):
                group_id = PLAN_GROUP_IDS_BY_PROVIDER[provider][preset["id"]]
                connection.execute(
                    """
                    INSERT INTO chat_model_group
                        (id, provider_family, name, description, default_role,
                         is_system, account_pool_id, legacy_group_id, updated_by,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 1, ?,
                            NULL, 'system', ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        provider_family = excluded.provider_family,
                        name = excluded.name,
                        description = excluded.description,
                        default_role = NULL,
                        is_system = 1,
                        account_pool_id = excluded.account_pool_id,
                        legacy_group_id = NULL,
                        updated_by = 'system',
                        updated_at = excluded.updated_at
                    """,
                    (
                        group_id,
                        provider,
                        preset["default_name"],
                        preset["default_description"],
                        f"{provider}-default",
                        created_at,
                        created_at,
                    ),
                )
                for rule in preset["rules"]:
                    connection.execute(
                        """
                        INSERT INTO chat_model_group_rule
                            (group_id, selection_key, enabled, limit_count,
                             window_seconds, fallback_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (group_id, selection_key) DO UPDATE SET
                            enabled = excluded.enabled,
                            limit_count = excluded.limit_count,
                            window_seconds = excluded.window_seconds,
                            fallback_key = excluded.fallback_key
                        """,
                        (
                            group_id,
                            rule["selection_key"],
                            int(rule["enabled"]),
                            rule["limit_count"],
                            rule["window_seconds"],
                            rule["fallback_key"],
                        ),
                    )

    @staticmethod
    def default_allowed(role: str) -> list[str]:
        values = DEFAULT_ADMIN_SELECTIONS if role == "admin" else DEFAULT_USER_SELECTIONS
        return list(values)

    @staticmethod
    def _read_legacy_policy(
        connection, user_id: str
    ):
        return connection.execute(
            "SELECT allowed_json, updated_at FROM chat_policy WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()

    @staticmethod
    def _group_row(connection, group_id: str):
        return connection.execute(
            """
            SELECT id, name, description, default_role, is_system,
                   storage_quota_bytes, max_concurrency, default_user_concurrency,
                   gpt_account_pool_id,
                   created_at, updated_at
              FROM chat_group WHERE id = ?
            """,
            (str(group_id),),
        ).fetchone()

    @staticmethod
    def _group_rules(
        connection, group_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT selection_key, enabled, limit_count, window_seconds, fallback_key
              FROM chat_group_rule WHERE group_id = ?
            """,
            (str(group_id),),
        ).fetchall()
        return {
            row["selection_key"]: {
                "selection_key": row["selection_key"],
                "enabled": bool(row["enabled"]),
                "limit_count": row["limit_count"],
                "window_seconds": int(row["window_seconds"]),
                "fallback_key": row["fallback_key"],
            }
            for row in rows
            if row["selection_key"] in SELECTION_BY_KEY
        }

    @staticmethod
    def _normalize_provider_family(provider_family: str) -> str:
        normalized = str(provider_family or "").strip().lower()
        if normalized not in PROVIDER_FAMILIES:
            raise ChatPolicyError("未知的模型 Provider")
        return normalized

    @staticmethod
    def _normalize_provider_display_name(display_name: str) -> str:
        normalized = str(display_name or "").strip()
        if not 1 <= len(normalized) <= 40:
            raise ChatPolicyError("模型展示名称必须为 1–40 字")
        if any(ord(character) < 32 for character in normalized):
            raise ChatPolicyError("模型展示名称不能包含控制字符")
        return normalized

    def provider_display_settings(self) -> list[dict[str, Any]]:
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider_family, display_name, updated_by, created_at, updated_at
                  FROM chat_provider_display
                """
            ).fetchall()
        by_provider = {str(row["provider_family"]): dict(row) for row in rows}
        return [
            by_provider.get(
                provider,
                {
                    "provider_family": provider,
                    "display_name": DEFAULT_PROVIDER_DISPLAY[provider],
                    "updated_by": "system",
                    "created_at": 0,
                    "updated_at": 0,
                },
            )
            for provider in PROVIDER_FAMILIES
        ]

    def provider_display_names(self) -> dict[str, str]:
        return {
            str(item["provider_family"]): str(item["display_name"])
            for item in self.provider_display_settings()
        }

    def set_provider_display_name(
        self,
        provider_family: str,
        display_name: str,
        *,
        updated_by: str,
    ) -> dict[str, Any]:
        provider = self._normalize_provider_family(provider_family)
        normalized_name = self._normalize_provider_display_name(display_name)
        timestamp = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-provider-display:{provider}")
            try:
                connection.execute(
                    """
                    INSERT INTO chat_provider_display
                        (provider_family, display_name, updated_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider_family) DO UPDATE SET
                        display_name = excluded.display_name,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        provider,
                        normalized_name,
                        str(updated_by),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return next(
            item
            for item in self.provider_display_settings()
            if item["provider_family"] == provider
        )

    @staticmethod
    def _model_group_row(connection, group_id: str):
        return connection.execute(
            """
            SELECT id, provider_family, name, description, default_role,
                   is_system, account_pool_id, legacy_group_id,
                   created_at, updated_at
              FROM chat_model_group WHERE id = ?
            """,
            (str(group_id),),
        ).fetchone()

    @staticmethod
    def _model_group_rules(
        connection,
        group_id: str,
        provider_family: str,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT selection_key, enabled, limit_count, window_seconds, fallback_key
              FROM chat_model_group_rule WHERE group_id = ?
            """,
            (str(group_id),),
        ).fetchall()
        allowed_keys = set(SELECTION_KEYS_BY_FAMILY[provider_family])
        return {
            row["selection_key"]: {
                "selection_key": row["selection_key"],
                "enabled": bool(row["enabled"]),
                "limit_count": row["limit_count"],
                "window_seconds": int(row["window_seconds"]),
                "fallback_key": row["fallback_key"],
            }
            for row in rows
            if row["selection_key"] in allowed_keys
        }

    def _default_group_id(self, connection, role: str) -> str:
        normalized_role = "admin" if role == "admin" else "user"
        row = connection.execute(
            "SELECT id FROM chat_group WHERE default_role = ?", (normalized_role,)
        ).fetchone()
        return str(row["id"] if row else DEFAULT_GROUP_BY_ROLE[normalized_role])

    def _default_model_group_id(
        self,
        connection,
        provider_family: str,
        role: str,
    ) -> str:
        normalized_provider = self._normalize_provider_family(provider_family)
        normalized_role = "admin" if role == "admin" else "user"
        row = connection.execute(
            """
            SELECT id FROM chat_model_group
             WHERE provider_family = ? AND default_role = ?
            """,
            (normalized_provider, normalized_role),
        ).fetchone()
        fallback = DEFAULT_MODEL_GROUP_BY_ROLE[normalized_provider][normalized_role]
        return str(row["id"] if row else fallback)

    def _resolved_resource_group(
        self,
        connection,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        assignment = connection.execute(
            """
            SELECT g.id, g.name, g.description, g.default_role, g.is_system,
                   g.storage_quota_bytes, g.max_concurrency,
                   g.default_user_concurrency, g.gpt_account_pool_id,
                   g.created_at, g.updated_at
              FROM chat_user_group ug
              JOIN chat_group g ON g.id = ug.group_id
             WHERE ug.user_id = ?
            """,
            (str(user_id),),
        ).fetchone()
        if assignment is not None:
            group = dict(assignment)
            group["source"] = "assigned"
            return group
        group_id = self._default_group_id(connection, role)
        row = self._group_row(connection, group_id)
        if row is None:
            raise ChatPolicyError("默认资源分组不存在")
        group = dict(row)
        group["source"] = "default"
        return group

    def _resolved_model_policy(
        self,
        connection,
        user_id: str,
        role: str,
        provider_family: str,
        legacy,
    ) -> dict[str, Any]:
        provider = self._normalize_provider_family(provider_family)
        provider_keys = SELECTION_KEYS_BY_FAMILY[provider]
        assignment = connection.execute(
            """
            SELECT g.id, g.provider_family, g.name, g.description,
                   g.default_role, g.is_system, g.account_pool_id,
                   g.legacy_group_id, g.created_at, g.updated_at
              FROM chat_user_model_group ug
              JOIN chat_model_group g ON g.id = ug.group_id
             WHERE ug.user_id = ? AND ug.provider_family = ?
               AND g.provider_family = ug.provider_family
            """,
            (str(user_id), provider),
        ).fetchone()

        if assignment is not None:
            group = dict(assignment)
            group["source"] = "assigned"
            rules = self._model_group_rules(connection, group["id"], provider)
        elif legacy is not None:
            try:
                legacy_allowed = _normalize_allowed(json.loads(legacy["allowed_json"]))
            except (json.JSONDecodeError, TypeError, ChatPolicyError):
                legacy_allowed = self.default_allowed(role)
            allowed_set = {
                key
                for key in legacy_allowed
                if SELECTION_BY_KEY[key]["family"] == provider
            }
            template_id = self._default_model_group_id(connection, provider, role)
            template_rules = self._model_group_rules(connection, template_id, provider)
            pro_row = connection.execute(
                """
                SELECT id FROM chat_model_group
                 WHERE provider_family = ? AND legacy_group_id = 'pro'
                """,
                (provider,),
            ).fetchone()
            pro_rules = (
                self._model_group_rules(connection, str(pro_row["id"]), provider)
                if pro_row is not None
                else {}
            )
            rules = {}
            for key in provider_keys:
                base = dict(
                    template_rules.get(key)
                    or pro_rules.get(key)
                    or _rule(False, None, 0)
                )
                base["selection_key"] = key
                base["enabled"] = key in allowed_set
                if base["enabled"] and base.get("limit_count") is None and role != "admin":
                    fallback = pro_rules.get(key)
                    if fallback:
                        base.update(fallback)
                        base["enabled"] = True
                rules[key] = base
            group = {
                "id": None,
                "provider_family": provider,
                "name": "单用户自定义（旧配置）",
                "description": "保留升级前的逐用户权限；分配 Provider 分组后由分组统一管理。",
                "default_role": None,
                "is_system": False,
                "account_pool_id": f"{provider}-default",
                "legacy_group_id": None,
                "source": "legacy",
                "created_at": None,
                "updated_at": legacy["updated_at"],
            }
        else:
            group_id = self._default_model_group_id(connection, provider, role)
            row = self._model_group_row(connection, group_id)
            if row is None:
                raise ChatPolicyError(f"{provider} 默认模型分组不存在")
            group = dict(row)
            group["source"] = "default"
            rules = self._model_group_rules(connection, group_id, provider)

        for key in provider_keys:
            rules.setdefault(
                key,
                {
                    "selection_key": key,
                    "enabled": False,
                    "limit_count": None,
                    "window_seconds": 0,
                    "fallback_key": None,
                },
            )
        allowed = [key for key in provider_keys if rules[key]["enabled"]]
        return {"group": group, "rules": rules, "allowed": allowed}

    def _resolved_policy(
        self,
        connection,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        user_id = str(user_id)
        legacy = self._read_legacy_policy(connection, user_id)
        resource_group = self._resolved_resource_group(connection, user_id, role)
        provider_policies = {
            provider: self._resolved_model_policy(
                connection,
                user_id,
                role,
                provider,
                legacy,
            )
            for provider in PROVIDER_FAMILIES
        }
        rules = {
            key: provider_policies[SELECTION_BY_KEY[key]["family"]]["rules"][key]
            for key in SELECTION_KEYS
        }
        allowed = [key for key in SELECTION_KEYS if rules[key]["enabled"]]
        provider_groups = {
            provider: provider_policies[provider]["group"]
            for provider in PROVIDER_FAMILIES
        }
        updated_values = [int(resource_group.get("updated_at") or 0)]
        updated_values.extend(
            int(group.get("updated_at") or 0) for group in provider_groups.values()
        )
        if legacy is not None:
            updated_values.append(int(legacy["updated_at"] or 0))
        return {
            "allowed": allowed,
            "customized": any(
                group.get("source") in {"assigned", "legacy"}
                for group in (resource_group, *provider_groups.values())
            ),
            "updated_at": max(updated_values),
            "group": resource_group,
            "resource_group": resource_group,
            "provider_groups": provider_groups,
            "rules": rules,
        }

    def policy_for_user(self, user_id: str, role: str) -> dict[str, Any]:
        with self._guard(), self._connect() as connection:
            policy = self._resolved_policy(connection, user_id, role)
        return {
            key: value
            for key, value in policy.items()
            if key != "rules"
        }

    def image_routing_for_user(self, user_id: str, role: str) -> dict[str, Any]:
        """Resolve the independent image lane and its allowed account tiers.

        Custom and legacy GPT groups default to Plus-class image workers. That
        conservative default prevents an ordinary group from silently spilling
        into a 20× Pro account. The immutable plan groups retain their explicit
        tier; the currently deployed 5× product may use either a future 5×
        worker or a 20× worker while still receiving only its 5× user budget.
        """

        with self._guard(), self._connect() as connection:
            policy = self._resolved_policy(connection, user_id, role)
        group = dict(policy["provider_groups"]["gpt"])
        group_id = str(group.get("id") or "")
        preset = PLAN_GROUP_PRESET_BY_ID.get(group_id)
        plan = (
            str(preset[1])
            if preset is not None and preset[0] == "gpt"
            else ("pro-20x" if role == "admin" else "plus")
        )
        required_profiles = {
            "free": ("free",),
            "go": ("go", "plus"),
            "plus": ("plus",),
            "pro-5x": ("pro-5x", "pro-20x"),
            "pro-20x": ("pro-20x",),
        }[plan]
        return {
            "selection_key": "image:create",
            "plan": plan,
            "account_pool_id": str(
                group.get("account_pool_id") or "gpt-default"
            ),
            "required_quota_profiles": list(required_profiles),
            "model_group_id": group_id or None,
        }

    def concurrency_for_user(self, user_id: str, role: str) -> dict[str, Any]:
        with self._guard(), self._connect() as connection:
            policy = self._resolved_policy(connection, user_id, role)
            group = policy["group"]
            row = connection.execute(
                "SELECT max_concurrency FROM chat_user_concurrency WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        group_limit = max(1, int(group.get("max_concurrency") or 1))
        default_user_limit = max(
            1, min(group_limit, int(group.get("default_user_concurrency") or 1))
        )
        override = int(row["max_concurrency"]) if row is not None else None
        effective_user_limit = min(group_limit, override or default_user_limit)
        return {
            "group_id": group.get("id") or "legacy",
            "group_name": group.get("name") or "旧版自定义",
            "gpt_account_pool_id": (
                policy.get("provider_groups", {}).get("gpt", {}).get("account_pool_id")
                or group.get("gpt_account_pool_id")
                or "gpt-default"
            ),
            "claude_account_pool_id": (
                policy.get("provider_groups", {})
                .get("claude", {})
                .get("account_pool_id")
                or "claude-default"
            ),
            "account_pool_ids": {
                provider: (
                    policy.get("provider_groups", {})
                    .get(provider, {})
                    .get("account_pool_id")
                    or f"{provider}-default"
                )
                for provider in PROVIDER_FAMILIES
            },
            "group_max_concurrency": group_limit,
            "default_user_concurrency": default_user_limit,
            "user_override": override,
            "user_max_concurrency": effective_user_limit,
        }

    def storage_quota_for_user(self, user_id: str, role: str = "user") -> dict[str, Any]:
        with self._guard(), self._connect() as connection:
            policy = self._resolved_policy(connection, user_id, role)
        group = policy["group"]
        return {
            "tier": "group",
            "source": "group",
            "group_id": group.get("id") or "legacy",
            "group_name": group.get("name") or "旧版自定义",
            "quota_bytes": max(0, int(group.get("storage_quota_bytes") or 0)),
        }

    def set_user_concurrency(
        self,
        user_id: str,
        role: str,
        *,
        max_concurrency: int | None,
        updated_by: str,
    ) -> dict[str, Any]:
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-concurrency:{user_id}")
            try:
                policy = self._resolved_policy(connection, user_id, role)
                group_limit = max(1, int(policy["group"].get("max_concurrency") or 1))
                if max_concurrency is None:
                    connection.execute(
                        "DELETE FROM chat_user_concurrency WHERE user_id = ?",
                        (str(user_id),),
                    )
                else:
                    try:
                        normalized = int(max_concurrency)
                    except (TypeError, ValueError) as exc:
                        raise ChatPolicyError("用户并发上限必须是整数") from exc
                    if not 1 <= normalized <= group_limit:
                        raise ChatPolicyError(
                            f"用户并发上限必须在 1–{group_limit} 之间，且不能超过所属分组"
                        )
                    connection.execute(
                        """
                        INSERT INTO chat_user_concurrency
                            (user_id, max_concurrency, updated_by, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            max_concurrency = excluded.max_concurrency,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                        """,
                        (str(user_id), normalized, str(updated_by), _now()),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.concurrency_for_user(user_id, role)

    def set_policy(
        self,
        user_id: str,
        *,
        allowed: Iterable[str],
        updated_by: str,
    ) -> dict[str, Any]:
        """Keep the legacy per-user model override API for a safe migration path."""
        normalized = _normalize_allowed(allowed)
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-policy:{user_id}")
            try:
                connection.execute("DELETE FROM chat_user_group WHERE user_id = ?", (str(user_id),))
                connection.execute(
                    "DELETE FROM chat_user_model_group WHERE user_id = ?",
                    (str(user_id),),
                )
                connection.execute(
                    """
                    INSERT INTO chat_policy (user_id, allowed_json, metered, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        allowed_json = excluded.allowed_json,
                        metered = 0,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(user_id),
                        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                        0,
                        str(updated_by),
                        _now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.policy_for_user(user_id, "user")

    def _normalize_group_rules(
        self,
        values: Iterable[dict[str, Any]],
        *,
        provider_family: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, dict[str, Any]]:
        provider = (
            self._normalize_provider_family(provider_family)
            if provider_family is not None
            else None
        )
        valid_keys = (
            SELECTION_KEYS_BY_FAMILY[provider] if provider is not None else SELECTION_KEYS
        )
        valid_key_set = set(valid_keys)
        supplied: dict[str, dict[str, Any]] = {}
        for value in values:
            selection_key = str(value.get("selection_key") or "").strip()
            if selection_key not in SELECTION_BY_KEY:
                raise ChatPolicyError("分组规则包含未知模型档位")
            if selection_key not in valid_key_set:
                raise ChatPolicyError("模型分组只能配置所属 Provider 的档位")
            if selection_key in supplied:
                raise ChatPolicyError("同一模型档位不能重复配置")
            enabled = bool(value.get("enabled"))
            raw_limit = value.get("limit_count")
            limit_count = None if raw_limit in (None, "") else int(raw_limit)
            if limit_count is not None and not 1 <= limit_count <= MAX_MODEL_LIMIT:
                raise ChatPolicyError(f"单模型次数必须在 1–{MAX_MODEL_LIMIT} 之间")
            window_seconds = int(value.get("window_seconds") or 0)
            if limit_count is not None and not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS:
                raise ChatPolicyError("限额时间窗必须在 1 分钟到 366 天之间")
            fallback_key = str(value.get("fallback_key") or "").strip() or None
            if fallback_key is not None and fallback_key not in SELECTION_BY_KEY:
                raise ChatPolicyError("自动降级档位不存在")
            if fallback_key == selection_key:
                raise ChatPolicyError("自动降级不能指向自身")
            if (
                fallback_key is not None
                and SELECTION_BY_KEY[fallback_key]["family"]
                != SELECTION_BY_KEY[selection_key]["family"]
            ):
                raise ChatPolicyError("自动降级不能跨 GPT 与 Claude Provider")
            supplied[selection_key] = {
                "selection_key": selection_key,
                "enabled": enabled,
                "limit_count": limit_count if enabled else None,
                "window_seconds": window_seconds if enabled and limit_count is not None else 0,
                "fallback_key": fallback_key if enabled and limit_count is not None else None,
            }

        rules = {
            key: supplied.get(
                key,
                {
                    "selection_key": key,
                    "enabled": False,
                    "limit_count": None,
                    "window_seconds": 0,
                    "fallback_key": None,
                },
            )
            for key in valid_keys
        }
        enabled = {key for key, rule in rules.items() if rule["enabled"]}
        if not enabled and not allow_empty:
            raise ChatPolicyError("分组至少开放一个模型档位")
        for key, rule in rules.items():
            fallback_key = rule["fallback_key"]
            if fallback_key and fallback_key not in enabled:
                raise ChatPolicyError(f"{_selection_label(key)} 的降级目标未在本分组开放")

        def visit(key: str, path: set[str]) -> None:
            fallback_key = rules[key]["fallback_key"]
            if not fallback_key:
                return
            if fallback_key in path:
                raise ChatPolicyError("自动降级规则不能形成循环")
            visit(fallback_key, path | {fallback_key})

        for key in enabled:
            visit(key, {key})
        return rules

    @staticmethod
    def _normalize_group_text(name: str, description: str) -> tuple[str, str]:
        normalized_name = str(name or "").strip()
        normalized_description = str(description or "").strip()
        if not 1 <= len(normalized_name) <= 40:
            raise ChatPolicyError("分组名称必须为 1–40 字")
        if len(normalized_description) > 200:
            raise ChatPolicyError("分组说明不能超过 200 字")
        return normalized_name, normalized_description

    @staticmethod
    def _normalize_group_capacity(
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
    ) -> tuple[int, int, int]:
        try:
            storage = int(storage_quota_bytes)
            group_limit = int(max_concurrency)
            user_limit = int(default_user_concurrency)
        except (TypeError, ValueError) as exc:
            raise ChatPolicyError("空间额度与并发上限必须是整数") from exc
        if not 0 <= storage <= MAX_STORAGE_QUOTA_BYTES:
            raise ChatPolicyError("分组空间额度超出允许范围")
        if not 1 <= group_limit <= MAX_CONCURRENCY:
            raise ChatPolicyError(f"分组最大并发必须在 1–{MAX_CONCURRENCY} 之间")
        if not 1 <= user_limit <= group_limit:
            raise ChatPolicyError("单用户默认并发必须大于 0 且不能超过分组最大并发")
        return storage, group_limit, user_limit

    def _write_resource_group(
        self,
        group_id: str,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int,
        max_concurrency: int,
        default_user_concurrency: int,
        updated_by: str,
        create: bool,
    ) -> dict[str, Any]:
        normalized_name, normalized_description = self._normalize_group_text(
            name, description
        )
        storage, group_limit, user_limit = self._normalize_group_capacity(
            storage_quota_bytes,
            max_concurrency,
            default_user_concurrency,
        )
        timestamp = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-resource-group:{group_id}")
            try:
                if create:
                    connection.execute(
                        """
                        INSERT INTO chat_group
                            (id, name, description, default_role, is_system,
                             storage_quota_bytes, max_concurrency,
                             default_user_concurrency, gpt_account_pool_id,
                             updated_by, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 0, ?, ?, ?, 'gpt-default', ?, ?, ?)
                        """,
                        (
                            str(group_id),
                            normalized_name,
                            normalized_description,
                            storage,
                            group_limit,
                            user_limit,
                            str(updated_by),
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE chat_group
                           SET name = ?, description = ?, storage_quota_bytes = ?,
                               max_concurrency = ?, default_user_concurrency = ?,
                               updated_by = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            normalized_name,
                            normalized_description,
                            storage,
                            group_limit,
                            user_limit,
                            str(updated_by),
                            timestamp,
                            str(group_id),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ChatPolicyError("资源分组不存在")
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                if self._is_integrity_error(exc):
                    raise ChatPolicyError("资源分组名称已存在") from exc
                raise
        result = self.group_by_id(group_id)
        if result is None:
            raise ChatPolicyError("资源分组保存失败")
        return result

    def create_resource_group(
        self,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_resource_group(
            str(uuid.uuid4()),
            name=name,
            description=description,
            storage_quota_bytes=storage_quota_bytes,
            max_concurrency=max_concurrency,
            default_user_concurrency=default_user_concurrency,
            updated_by=updated_by,
            create=True,
        )

    def update_resource_group(
        self,
        group_id: str,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_resource_group(
            group_id,
            name=name,
            description=description,
            storage_quota_bytes=storage_quota_bytes,
            max_concurrency=max_concurrency,
            default_user_concurrency=default_user_concurrency,
            updated_by=updated_by,
            create=False,
        )

    def _write_model_group(
        self,
        group_id: str,
        *,
        provider_family: str,
        name: str,
        description: str,
        account_pool_id: str | None,
        rules: Iterable[dict[str, Any]],
        updated_by: str,
        create: bool,
    ) -> dict[str, Any]:
        provider = self._normalize_provider_family(provider_family)
        normalized_name, normalized_description = self._normalize_group_text(
            name, description
        )
        normalized_rules = self._normalize_group_rules(
            rules,
            provider_family=provider,
            allow_empty=True,
        )
        normalized_pool_id = str(
            account_pool_id or f"{provider}-default"
        ).strip()
        if not normalized_pool_id or len(normalized_pool_id) > 80:
            raise ChatPolicyError(f"{provider} 账号池无效")
        timestamp = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-model-group:{group_id}")
            try:
                if create:
                    connection.execute(
                        """
                        INSERT INTO chat_model_group
                            (id, provider_family, name, description, default_role,
                             is_system, account_pool_id, legacy_group_id,
                             updated_by, created_at, updated_at)
                        VALUES (?, ?, ?, ?, NULL, 0, ?, NULL, ?, ?, ?)
                        """,
                        (
                            str(group_id),
                            provider,
                            normalized_name,
                            normalized_description,
                            normalized_pool_id,
                            str(updated_by),
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    existing = self._model_group_row(connection, group_id)
                    if existing is None:
                        raise ChatPolicyError("模型分组不存在")
                    if str(existing["provider_family"]) != provider:
                        raise ChatPolicyError("模型分组不能更换 Provider")
                    if str(group_id) in PLAN_GROUP_PRESET_BY_ID:
                        raise ChatPolicyError(
                            "官方套餐模板不能直接修改，请先复制为自定义分组"
                        )
                    connection.execute(
                        """
                        UPDATE chat_model_group
                           SET name = ?, description = ?, account_pool_id = ?,
                               updated_by = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            normalized_name,
                            normalized_description,
                            normalized_pool_id,
                            str(updated_by),
                            timestamp,
                            str(group_id),
                        ),
                    )
                connection.execute(
                    "DELETE FROM chat_model_group_rule WHERE group_id = ?",
                    (str(group_id),),
                )
                for selection_key in SELECTION_KEYS_BY_FAMILY[provider]:
                    rule = normalized_rules[selection_key]
                    connection.execute(
                        """
                        INSERT INTO chat_model_group_rule
                            (group_id, selection_key, enabled, limit_count,
                             window_seconds, fallback_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(group_id),
                            selection_key,
                            int(rule["enabled"]),
                            rule["limit_count"],
                            rule["window_seconds"],
                            rule["fallback_key"],
                        ),
                    )
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                if self._is_integrity_error(exc):
                    raise ChatPolicyError("该 Provider 下的分组名称已存在") from exc
                raise
        result = self.model_group_by_id(group_id)
        if result is None:
            raise ChatPolicyError("模型分组保存失败")
        return result

    def create_model_group(
        self,
        *,
        provider_family: str,
        name: str,
        description: str,
        account_pool_id: str | None = None,
        rules: Iterable[dict[str, Any]],
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_model_group(
            str(uuid.uuid4()),
            provider_family=provider_family,
            name=name,
            description=description,
            account_pool_id=account_pool_id,
            rules=rules,
            updated_by=updated_by,
            create=True,
        )

    def update_model_group(
        self,
        group_id: str,
        *,
        provider_family: str,
        name: str,
        description: str,
        account_pool_id: str | None = None,
        rules: Iterable[dict[str, Any]],
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_model_group(
            group_id,
            provider_family=provider_family,
            name=name,
            description=description,
            account_pool_id=account_pool_id,
            rules=rules,
            updated_by=updated_by,
            create=False,
        )

    def model_group_by_id(self, group_id: str) -> dict[str, Any] | None:
        with self._guard(), self._connect() as connection:
            row = self._model_group_row(connection, group_id)
            if row is None:
                return None
            group = dict(row)
            group["is_system"] = bool(group["is_system"])
            group["is_retired"] = (
                str(group.get("legacy_group_id") or "")
                in RETIRED_LEGACY_GROUP_IDS
            )
            template = PLAN_GROUP_PRESET_BY_ID.get(str(group_id))
            group["is_plan_template"] = template is not None
            group["template_preset_id"] = template[1] if template else None
            group["sort_order"] = _model_group_sort_order(group)
            group["member_count"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chat_user_model_group WHERE group_id = ?",
                    (str(group_id),),
                ).fetchone()[0]
                or 0
            )
            provider = str(group["provider_family"])
            rules = self._model_group_rules(connection, group_id, provider)
            group["rules"] = [
                rules.get(key)
                or {
                    "selection_key": key,
                    "enabled": False,
                    "limit_count": None,
                    "window_seconds": 0,
                    "fallback_key": None,
                }
                for key in SELECTION_KEYS_BY_FAMILY[provider]
            ]
        return group

    def list_model_groups(
        self,
        provider_family: str | None = None,
    ) -> list[dict[str, Any]]:
        provider = (
            self._normalize_provider_family(provider_family)
            if provider_family is not None
            else None
        )
        with self._guard(), self._connect() as connection:
            if provider is None:
                rows = connection.execute(
                    "SELECT id FROM chat_model_group"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM chat_model_group WHERE provider_family = ?",
                    (provider,),
                ).fetchall()
        groups = [
            group
            for row in rows
            if (group := self.model_group_by_id(row["id"])) is not None
            and (
                not bool(group.get("is_retired"))
                or int(group.get("member_count") or 0) > 0
            )
        ]
        return sorted(
            groups,
            key=lambda group: (
                str(group.get("provider_family") or ""),
                int(group.get("sort_order") or 800),
                str(group.get("name") or "").casefold(),
                str(group.get("id") or ""),
            ),
        )

    def delete_model_group(self, group_id: str) -> None:
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-model-group:{group_id}")
            try:
                row = self._model_group_row(connection, group_id)
                if row is None:
                    raise ChatPolicyError("模型分组不存在")
                if bool(row["is_system"]) or row["default_role"] is not None:
                    raise ChatPolicyError("系统模型分组不能删除")
                members = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_user_model_group WHERE group_id = ?",
                        (str(group_id),),
                    ).fetchone()[0]
                    or 0
                )
                if members:
                    raise ChatPolicyError(
                        f"仍有 {members} 位用户属于该模型分组，不能删除"
                    )
                connection.execute(
                    "DELETE FROM chat_model_group WHERE id = ?",
                    (str(group_id),),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _sync_legacy_model_groups(
        self,
        connection,
        group_id: str,
        normalized_rules: dict[str, dict[str, Any]],
        *,
        updated_by: str,
        timestamp: int,
    ) -> None:
        resource = self._group_row(connection, group_id)
        if resource is None:
            raise ChatPolicyError("资源分组不存在")
        for provider in PROVIDER_FAMILIES:
            model_group_id = self._legacy_model_group_id(provider, group_id)
            connection.execute(
                """
                INSERT INTO chat_model_group
                    (id, provider_family, name, description, default_role,
                     is_system, account_pool_id, legacy_group_id, updated_by,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    model_group_id,
                    provider,
                    str(resource["name"]),
                    str(resource["description"]),
                    resource["default_role"],
                    int(resource["is_system"]),
                    (
                        str(resource["gpt_account_pool_id"] or "gpt-default")
                        if provider == "gpt"
                        else "claude-default"
                    ),
                    str(group_id),
                    str(updated_by),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM chat_model_group
                 WHERE provider_family = ? AND legacy_group_id = ?
                """,
                (provider, str(group_id)),
            ).fetchone()
            if row is None:
                raise ChatPolicyError("兼容模型分组创建失败")
            model_group_id = str(row["id"])
            connection.execute(
                """
                UPDATE chat_model_group
                   SET name = ?, description = ?, account_pool_id = ?,
                       updated_by = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    str(resource["name"]),
                    str(resource["description"]),
                    (
                        str(resource["gpt_account_pool_id"] or "gpt-default")
                        if provider == "gpt"
                        else "claude-default"
                    ),
                    str(updated_by),
                    timestamp,
                    model_group_id,
                ),
            )
            connection.execute(
                "DELETE FROM chat_model_group_rule WHERE group_id = ?",
                (model_group_id,),
            )
            for key in SELECTION_KEYS_BY_FAMILY[provider]:
                rule = normalized_rules[key]
                connection.execute(
                    """
                    INSERT INTO chat_model_group_rule
                        (group_id, selection_key, enabled, limit_count,
                         window_seconds, fallback_key)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        model_group_id,
                        key,
                        int(rule["enabled"]),
                        rule["limit_count"],
                        rule["window_seconds"],
                        rule["fallback_key"],
                    ),
                )

    def _write_group(
        self,
        group_id: str,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
        gpt_account_pool_id: str = "gpt-default",
        rules: Iterable[dict[str, Any]],
        updated_by: str,
        create: bool,
    ) -> dict[str, Any]:
        normalized_name, normalized_description = self._normalize_group_text(name, description)
        storage, group_limit, user_limit = self._normalize_group_capacity(
            storage_quota_bytes,
            max_concurrency,
            default_user_concurrency,
        )
        normalized_rules = self._normalize_group_rules(rules)
        normalized_pool_id = str(gpt_account_pool_id or "").strip()
        if not normalized_pool_id or len(normalized_pool_id) > 80:
            raise ChatPolicyError("ChatGPT 账号组无效")
        timestamp = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-group:{group_id}")
            try:
                if create:
                    connection.execute(
                        """
                        INSERT INTO chat_group
                            (id, name, description, default_role, is_system,
                             storage_quota_bytes, max_concurrency, default_user_concurrency,
                             gpt_account_pool_id,
                             updated_by, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(group_id),
                            normalized_name,
                            normalized_description,
                            storage,
                            group_limit,
                            user_limit,
                            normalized_pool_id,
                            str(updated_by),
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE chat_group
                           SET name = ?, description = ?, storage_quota_bytes = ?,
                               max_concurrency = ?, default_user_concurrency = ?,
                               gpt_account_pool_id = ?, updated_by = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            normalized_name,
                            normalized_description,
                            storage,
                            group_limit,
                            user_limit,
                            normalized_pool_id,
                            str(updated_by),
                            timestamp,
                            str(group_id),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ChatPolicyError("聊天分组不存在")
                connection.execute("DELETE FROM chat_group_rule WHERE group_id = ?", (str(group_id),))
                for key in SELECTION_KEYS:
                    rule = normalized_rules[key]
                    connection.execute(
                        """
                        INSERT INTO chat_group_rule
                            (group_id, selection_key, enabled, limit_count, window_seconds, fallback_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(group_id),
                            key,
                            int(rule["enabled"]),
                            rule["limit_count"],
                            rule["window_seconds"],
                            rule["fallback_key"],
                        ),
                    )
                self._sync_legacy_model_groups(
                    connection,
                    str(group_id),
                    normalized_rules,
                    updated_by=str(updated_by),
                    timestamp=timestamp,
                )
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                if self._is_integrity_error(exc):
                    raise ChatPolicyError("分组名称已存在") from exc
                raise
        result = self.group_by_id(group_id)
        if result is None:
            raise ChatPolicyError("聊天分组保存失败")
        return result

    def create_group(
        self,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
        gpt_account_pool_id: str = "gpt-default",
        rules: Iterable[dict[str, Any]],
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_group(
            str(uuid.uuid4()),
            name=name,
            description=description,
            storage_quota_bytes=storage_quota_bytes,
            max_concurrency=max_concurrency,
            default_user_concurrency=default_user_concurrency,
            gpt_account_pool_id=gpt_account_pool_id,
            rules=rules,
            updated_by=updated_by,
            create=True,
        )

    def update_group(
        self,
        group_id: str,
        *,
        name: str,
        description: str,
        storage_quota_bytes: int = 2 * GIB,
        max_concurrency: int = 2,
        default_user_concurrency: int = 1,
        gpt_account_pool_id: str = "gpt-default",
        rules: Iterable[dict[str, Any]],
        updated_by: str,
    ) -> dict[str, Any]:
        return self._write_group(
            group_id,
            name=name,
            description=description,
            storage_quota_bytes=storage_quota_bytes,
            max_concurrency=max_concurrency,
            default_user_concurrency=default_user_concurrency,
            gpt_account_pool_id=gpt_account_pool_id,
            rules=rules,
            updated_by=updated_by,
            create=False,
        )

    def group_by_id(self, group_id: str) -> dict[str, Any] | None:
        with self._guard(), self._connect() as connection:
            row = self._group_row(connection, group_id)
            if row is None:
                return None
            group = dict(row)
            group["is_system"] = bool(group["is_system"])
            group["is_retired"] = str(group_id) in RETIRED_LEGACY_GROUP_IDS
            for field in (
                "storage_quota_bytes",
                "max_concurrency",
                "default_user_concurrency",
            ):
                group[field] = int(group[field])
            group["member_count"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chat_user_group WHERE group_id = ?", (str(group_id),)
                ).fetchone()[0]
                or 0
            )
            legacy_rules = self._group_rules(connection, group_id)
            group["rules"] = [
                legacy_rules.get(key)
                or {
                    "selection_key": key,
                    "enabled": False,
                    "limit_count": None,
                    "window_seconds": 0,
                    "fallback_key": None,
                }
                for key in SELECTION_KEYS
            ]
        return group

    def list_groups(self) -> list[dict[str, Any]]:
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM chat_group
                 ORDER BY CASE default_role WHEN 'user' THEN 0 WHEN 'admin' THEN 2 ELSE 1 END,
                          created_at, name
                """
            ).fetchall()
        return [
            group
            for row in rows
            if (group := self.group_by_id(row["id"])) is not None
            and (
                not bool(group.get("is_retired"))
                or int(group.get("member_count") or 0) > 0
            )
        ]

    def delete_group(self, group_id: str) -> None:
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-group:{group_id}")
            try:
                row = self._group_row(connection, group_id)
                if row is None:
                    raise ChatPolicyError("聊天分组不存在")
                if bool(row["is_system"]) or row["default_role"] is not None:
                    raise ChatPolicyError("系统默认分组不能删除")
                members = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_user_group WHERE group_id = ?", (str(group_id),)
                    ).fetchone()[0]
                    or 0
                )
                if members:
                    raise ChatPolicyError(
                        f"仍有 {members} 位用户属于该资源分组，不能删除"
                    )
                connection.execute("DELETE FROM chat_group WHERE id = ?", (str(group_id),))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def assign_resource_group(
        self,
        user_id: str,
        group_id: str,
        *,
        assigned_by: str,
        role: str = "user",
    ) -> dict[str, Any]:
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-policy:{user_id}")
            try:
                if self._group_row(connection, group_id) is None:
                    raise ChatPolicyError("资源分组不存在")
                connection.execute(
                    """
                    INSERT INTO chat_user_group (user_id, group_id, assigned_by, assigned_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        group_id = excluded.group_id,
                        assigned_by = excluded.assigned_by,
                        assigned_at = excluded.assigned_at
                    """,
                    (str(user_id), str(group_id), str(assigned_by), _now()),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.policy_for_user(user_id, role)

    def assign_model_group(
        self,
        user_id: str,
        provider_family: str,
        group_id: str,
        *,
        assigned_by: str,
        role: str = "user",
    ) -> dict[str, Any]:
        provider = self._normalize_provider_family(provider_family)
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-policy:{user_id}")
            try:
                group = self._model_group_row(connection, group_id)
                if group is None:
                    raise ChatPolicyError("模型分组不存在")
                if str(group["provider_family"]) != provider:
                    raise ChatPolicyError("模型分组不属于所选 Provider")
                connection.execute(
                    """
                    INSERT INTO chat_user_model_group
                        (user_id, provider_family, group_id, assigned_by, assigned_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, provider_family) DO UPDATE SET
                        group_id = excluded.group_id,
                        assigned_by = excluded.assigned_by,
                        assigned_at = excluded.assigned_at
                    """,
                    (
                        str(user_id),
                        provider,
                        str(group_id),
                        str(assigned_by),
                        _now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.policy_for_user(user_id, role)

    def bulk_assign_groups(
        self,
        user_ids: Iterable[str],
        *,
        resource_group_id: str | None = None,
        model_group_ids: dict[str, str] | None = None,
        assigned_by: str,
    ) -> int:
        """Atomically move a bounded user set between resource/model groups."""

        normalized_user_ids = sorted(
            {
                str(user_id).strip()
                for user_id in user_ids
                if str(user_id).strip()
            }
        )
        if not normalized_user_ids:
            raise ChatPolicyError("请至少选择一位用户")
        if len(normalized_user_ids) > 200:
            raise ChatPolicyError("单次最多批量调整 200 位用户")

        normalized_resource_group_id = (
            str(resource_group_id).strip() if resource_group_id else None
        )
        normalized_model_group_ids = {
            self._normalize_provider_family(provider): str(group_id).strip()
            for provider, group_id in (model_group_ids or {}).items()
            if str(group_id).strip()
        }
        if (
            normalized_resource_group_id is None
            and not normalized_model_group_ids
        ):
            raise ChatPolicyError("请至少选择一个需要调整的分组")

        with self._guard(), self._connect() as connection:
            self._begin(connection)
            try:
                if normalized_resource_group_id is not None:
                    if (
                        self._group_row(connection, normalized_resource_group_id)
                        is None
                    ):
                        raise ChatPolicyError("资源分组不存在")

                for provider, group_id in normalized_model_group_ids.items():
                    group = self._model_group_row(connection, group_id)
                    if group is None:
                        raise ChatPolicyError("模型分组不存在")
                    if str(group["provider_family"]) != provider:
                        raise ChatPolicyError("模型分组不属于所选 Provider")

                if self.backend == "postgresql":
                    for user_id in normalized_user_ids:
                        connection.execute(
                            """
                            SELECT pg_advisory_xact_lock(
                                hashtextextended(?, 0)
                            )
                            """,
                            (f"chat-policy:{user_id}",),
                        )

                assigned_at = _now()
                for user_id in normalized_user_ids:
                    if normalized_resource_group_id is not None:
                        connection.execute(
                            """
                            INSERT INTO chat_user_group
                                (user_id, group_id, assigned_by, assigned_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(user_id) DO UPDATE SET
                                group_id = excluded.group_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at
                            """,
                            (
                                user_id,
                                normalized_resource_group_id,
                                str(assigned_by),
                                assigned_at,
                            ),
                        )
                    for provider, group_id in normalized_model_group_ids.items():
                        connection.execute(
                            """
                            INSERT INTO chat_user_model_group
                                (user_id, provider_family, group_id,
                                 assigned_by, assigned_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(user_id, provider_family) DO UPDATE SET
                                group_id = excluded.group_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at
                            """,
                            (
                                user_id,
                                provider,
                                group_id,
                                str(assigned_by),
                                assigned_at,
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(normalized_user_ids)

    def assign_group(
        self, user_id: str, group_id: str, *, assigned_by: str, role: str = "user"
    ) -> dict[str, Any]:
        """Compatibility assignment for the former all-in-one group API."""

        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-policy:{user_id}")
            try:
                if self._group_row(connection, group_id) is None:
                    raise ChatPolicyError("聊天分组不存在")
                assigned_at = _now()
                connection.execute(
                    """
                    INSERT INTO chat_user_group (user_id, group_id, assigned_by, assigned_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        group_id = excluded.group_id,
                        assigned_by = excluded.assigned_by,
                        assigned_at = excluded.assigned_at
                    """,
                    (str(user_id), str(group_id), str(assigned_by), assigned_at),
                )
                for provider in PROVIDER_FAMILIES:
                    model_group = connection.execute(
                        """
                        SELECT id FROM chat_model_group
                         WHERE provider_family = ? AND legacy_group_id = ?
                        """,
                        (provider, str(group_id)),
                    ).fetchone()
                    if model_group is None:
                        raise ChatPolicyError("聊天分组缺少对应的 Provider 配置")
                    connection.execute(
                        """
                        INSERT INTO chat_user_model_group
                            (user_id, provider_family, group_id, assigned_by, assigned_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, provider_family) DO UPDATE SET
                            group_id = excluded.group_id,
                            assigned_by = excluded.assigned_by,
                            assigned_at = excluded.assigned_at
                        """,
                        (
                            str(user_id),
                            provider,
                            str(model_group["id"]),
                            str(assigned_by),
                            assigned_at,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.policy_for_user(user_id, role)

    def _cleanup_stale(
        self,
        connection,
        user_id: str,
        now: int | None = None,
    ) -> None:
        timestamp = int(now or _now())
        cutoff = timestamp - STALE_RESERVATION_SECONDS
        connection.execute(
            """
            UPDATE chat_usage
               SET status = 'released', finalized_at = ?
             WHERE user_id = ? AND status = 'reserved' AND created_at < ?
            """,
            (timestamp, str(user_id), cutoff),
        )

    @staticmethod
    def _window_counts(
        connection,
        user_id: str,
        selection_key: str,
        window_id: str | None,
        started_at: int,
        reset_at: int,
    ) -> tuple[int, int]:
        if window_id:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'committed' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0)
                  FROM chat_usage
                 WHERE user_id = ? AND selection_key = ? AND quota_window_id = ?
                """,
                (str(user_id), selection_key, str(window_id)),
            ).fetchone()
        else:
            # Compatibility path for an active window created before window IDs
            # were introduced. The next reservation upgrades it transactionally.
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'committed' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0)
                  FROM chat_usage
                 WHERE user_id = ? AND selection_key = ?
                   AND created_at >= ? AND created_at < ?
                """,
                (str(user_id), selection_key, int(started_at), int(reset_at)),
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def _model_statuses(
        self,
        connection,
        user_id: str,
        role: str,
        *,
        now: int,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        policy = self._resolved_policy(connection, user_id, role)
        statuses: dict[str, dict[str, Any]] = {}
        for key in SELECTION_KEYS:
            rule = policy["rules"][key]
            enabled = bool(rule["enabled"])
            limit_count = rule["limit_count"]
            window_seconds = int(rule["window_seconds"] or 0)
            started_at: int | None = None
            reset_at: int | None = None
            window_id: str | None = None
            used_count = 0
            reserved_count = 0
            if enabled and limit_count is not None:
                window = connection.execute(
                    """
                    SELECT started_at, window_id FROM chat_quota_window
                     WHERE user_id = ? AND selection_key = ?
                    """,
                    (str(user_id), key),
                ).fetchone()
                if window is not None:
                    started_at = int(window["started_at"])
                    window_id = window["window_id"]
                    reset_at = started_at + window_seconds
                    if now >= reset_at:
                        connection.execute(
                            "DELETE FROM chat_quota_window WHERE user_id = ? AND selection_key = ?",
                            (str(user_id), key),
                        )
                        started_at = None
                        reset_at = None
                    else:
                        used_count, reserved_count = self._window_counts(
                            connection,
                            user_id,
                            key,
                            window_id,
                            started_at,
                            reset_at,
                        )
            remaining_count = (
                max(0, int(limit_count) - used_count - reserved_count)
                if enabled and limit_count is not None
                else None
            )
            exhausted = enabled and limit_count is not None and remaining_count == 0
            status = "forbidden" if not enabled else "exhausted" if exhausted else "available"
            statuses[key] = {
                "selection_key": key,
                "allowed": enabled,
                "available": enabled and not exhausted,
                "status": status,
                "limit_count": limit_count,
                "used_count": used_count,
                "reserved_count": reserved_count,
                "remaining_count": remaining_count,
                "window_seconds": window_seconds,
                "window_started_at": started_at,
                "reset_at": reset_at,
                "fallback_key": rule["fallback_key"],
                "fallback_label": _selection_label(rule["fallback_key"]),
            }
        return policy, statuses

    def quota_summary(self, user_id: str, role: str) -> dict[str, Any]:
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-quota:{user_id}")
            try:
                self._cleanup_stale(connection, user_id, now)
                policy, models = self._model_statuses(
                    connection, user_id, role, now=now
                )
                request_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM chat_usage
                         WHERE user_id = ? AND status = 'committed'
                        """,
                        (str(user_id),),
                    ).fetchone()[0]
                    or 0
                )
                fallback_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM chat_usage
                         WHERE user_id = ? AND status = 'committed' AND fallback_from IS NOT NULL
                        """,
                        (str(user_id),),
                    ).fetchone()[0]
                    or 0
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "group": policy["group"],
            "resource_group": policy["resource_group"],
            "provider_groups": policy["provider_groups"],
            "models": models,
            "request_count": request_count,
            "fallback_count": fallback_count,
            "server_time": now,
        }

    def default_selection(
        self,
        user_id: str,
        role: str,
        model_id: str = "gpt-5-web",
    ) -> dict[str, Any]:
        summary = self.quota_summary(user_id, role)
        preferred_by_model = {
            "gpt-5-web": (
                "gpt-5-5:instant",
                "latest:medium",
                "latest:high",
                "gpt-5-3:standard",
                "o3:standard",
            ),
            "claude-web": (
                "claude-sonnet-5:standard",
                "claude-haiku-4-5:fast",
                "claude-sonnet-5:extended",
                "claude-opus-4-8:standard",
                "claude-opus-4-8:extended",
            ),
        }
        if model_id not in preferred_by_model:
            raise ChatPolicyError("当前模型不受 Turtle 分组策略管理")
        for preferred in preferred_by_model[model_id]:
            if summary["models"][preferred]["available"]:
                return SELECTION_BY_KEY[preferred]
        for key in SELECTION_KEYS:
            if (
                SELECTION_BY_KEY[key]["model_id"] == model_id
                and summary["models"][key]["allowed"]
            ):
                return SELECTION_BY_KEY[key]
        raise ChatPolicyError("当前账号没有可用的该 Provider 模型档位")

    def task_selection(
        self,
        user_id: str,
        role: str,
        model_id: str = "gpt-5-web",
    ) -> dict[str, Any]:
        """Use the lightest allowed lane for Open WebUI's background helpers."""
        policy = self.policy_for_user(user_id, role)
        preferred_by_model = {
            "gpt-5-web": ("gpt-5-5:instant", "gpt-5-3:standard", "latest:medium"),
            "claude-web": ("claude-haiku-4-5:fast", "claude-sonnet-5:standard"),
        }
        if model_id not in preferred_by_model:
            raise ChatPolicyError("当前模型不受 Turtle 分组策略管理")
        for preferred in preferred_by_model[model_id]:
            if preferred in policy["allowed"]:
                return SELECTION_BY_KEY[preferred]
        for key in policy["allowed"]:
            if SELECTION_BY_KEY[key]["model_id"] == model_id:
                return SELECTION_BY_KEY[key]
        raise ChatPolicyError("当前账号没有可用于后台任务的该 Provider 档位")

    @staticmethod
    def _ensure_window(
        connection,
        user_id: str,
        selection_key: str,
        now: int,
        window_seconds: int,
    ) -> tuple[int, str]:
        proposed_window_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO chat_quota_window
                (user_id, selection_key, started_at, window_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (str(user_id), selection_key, int(now), proposed_window_id),
        )
        row = connection.execute(
            """
            SELECT started_at, window_id FROM chat_quota_window
             WHERE user_id = ? AND selection_key = ?
            """,
            (str(user_id), selection_key),
        ).fetchone()
        started_at = int(row["started_at"])
        window_id = row["window_id"]
        if not window_id:
            # Upgrade a pre-migration active window without losing its current
            # usage. Future resets create a fresh ID, so same-second history can
            # never leak into the new allowance window.
            window_id = proposed_window_id
            connection.execute(
                """
                UPDATE chat_quota_window SET window_id = ?
                 WHERE user_id = ? AND selection_key = ? AND window_id IS NULL
                """,
                (window_id, str(user_id), selection_key),
            )
            connection.execute(
                """
                UPDATE chat_usage SET quota_window_id = ?
                 WHERE user_id = ? AND selection_key = ?
                   AND quota_window_id IS NULL
                   AND created_at >= ? AND created_at < ?
                """,
                (
                    window_id,
                    str(user_id),
                    selection_key,
                    started_at,
                    started_at + int(window_seconds),
                ),
            )
        return started_at, str(window_id)

    def reserve(
        self,
        user_id: str,
        role: str,
        *,
        version: str,
        level: str,
        model_id: str | None = None,
        request_id: str | None = None,
        queued_at_ms: int | None = None,
        admitted_at_ms: int | None = None,
    ) -> Reservation:
        requested_key = f"{version}:{level}"
        if requested_key not in SELECTION_BY_KEY:
            raise ChatPolicyError("未知或不支持的模型思考档位")
        if model_id is not None and SELECTION_BY_KEY[requested_key]["model_id"] != model_id:
            raise ChatPolicyError("模型档位不属于当前 Provider")
        reservation_id = str(uuid.uuid4())
        effective_request_id = str(request_id or uuid.uuid4())
        now = _now()
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-quota:{user_id}")
            try:
                self._cleanup_stale(connection, user_id, now)
                policy, statuses = self._model_statuses(
                    connection, user_id, role, now=now
                )
                if not statuses[requested_key]["allowed"]:
                    raise ChatPolicyError("你的分组未开通该模型或思考档位")

                effective_key = requested_key
                visited: set[str] = set()
                exhausted_resets: list[int] = []
                quota_window_id: str | None = None
                while True:
                    if effective_key in visited:
                        raise ChatPolicyError("自动降级规则形成循环")
                    visited.add(effective_key)
                    effective_status = statuses[effective_key]
                    available = bool(effective_status["available"])
                    current_reset = effective_status["reset_at"]
                    if available and effective_status["limit_count"] is not None:
                        started_at, candidate_window_id = self._ensure_window(
                            connection,
                            user_id,
                            effective_key,
                            now,
                            int(effective_status["window_seconds"]),
                        )
                        current_reset = started_at + int(
                            effective_status["window_seconds"]
                        )
                        used, reserved_count = self._window_counts(
                            connection,
                            user_id,
                            effective_key,
                            candidate_window_id,
                            started_at,
                            current_reset,
                        )
                        available = used + reserved_count < int(
                            effective_status["limit_count"]
                        )
                        if available:
                            quota_window_id = candidate_window_id
                    if available:
                        break
                    if current_reset is not None:
                        exhausted_resets.append(int(current_reset))
                    fallback_key = effective_status["fallback_key"]
                    if not fallback_key or not statuses.get(fallback_key, {}).get("allowed"):
                        reset_at = min(exhausted_resets) if exhausted_resets else None
                        raise ChatModelQuotaError(requested_key, reset_at)
                    effective_key = fallback_key

                selection = SELECTION_BY_KEY[effective_key]
                fallback_from = requested_key if effective_key != requested_key else None
                provider_family = str(selection.get("family") or "other")
                group_id = policy["resource_group"].get("id")
                model_group_id = policy["provider_groups"].get(
                    provider_family, {}
                ).get("id")
                queued_ms = int(queued_at_ms or int(time.time() * 1000))
                admitted_ms = int(admitted_at_ms or queued_ms)
                connection.execute(
                    """
                    INSERT INTO chat_usage
                        (id, request_id, user_id, selection_key, quota_window_id,
                         requested_selection_key,
                         fallback_from, group_id, model_group_id, provider_family,
                         queued_at_ms, admitted_at_ms, queue_ms,
                         cost, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                    """,
                    (
                        reservation_id,
                        effective_request_id,
                        str(user_id),
                        effective_key,
                        quota_window_id,
                        requested_key,
                        fallback_from,
                        group_id,
                        model_group_id,
                        provider_family,
                        queued_ms,
                        admitted_ms,
                        max(0, admitted_ms - queued_ms),
                        0,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return Reservation(
            id=reservation_id,
            request_id=effective_request_id,
            user_id=str(user_id),
            requested_selection_key=requested_key,
            selection_key=effective_key,
            group_id=group_id,
            model_group_id=model_group_id,
            provider_family=provider_family,
            fallback_from=fallback_from,
        )

    def finalize(self, reservation_id: str, status: str) -> None:
        if status not in {"committed", "released"}:
            raise ValueError("invalid final reservation status")
        with self._guard(), self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_usage
                   SET status = ?, finalized_at = ?
                 WHERE id = ? AND status = 'reserved'
                """,
                (status, _now(), str(reservation_id)),
            )

    def record_transport(
        self,
        reservation_id: str,
        *,
        connected_at_ms: int,
        connect_ms: int,
        http_status: int | None,
    ) -> None:
        with self._guard(), self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_usage
                   SET connected_at_ms = ?, connect_ms = ?, http_status = ?
                 WHERE id = ?
                """,
                (
                    int(connected_at_ms),
                    max(0, int(connect_ms)),
                    int(http_status) if http_status is not None else None,
                    str(reservation_id),
                ),
            )

    def record_first_content(
        self,
        reservation_id: str,
        *,
        first_content_at_ms: int,
        ttft_ms: int,
    ) -> None:
        with self._guard(), self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_usage
                   SET first_content_at_ms = COALESCE(first_content_at_ms, ?),
                       ttft_ms = COALESCE(ttft_ms, ?)
                 WHERE id = ?
                """,
                (int(first_content_at_ms), max(0, int(ttft_ms)), str(reservation_id)),
            )

    def record_completion(
        self,
        reservation_id: str,
        *,
        completed_at_ms: int,
        total_ms: int,
        outcome: str,
        http_status: int | None = None,
        error_type: str | None = None,
        error_phase: str | None = None,
    ) -> None:
        normalized_outcome = str(outcome or "error")[:32]
        normalized_error = str(error_type or "")[:64] or None
        normalized_phase = str(error_phase or "")[:48] or None
        with self._guard(), self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_usage
                   SET completed_at_ms = ?, total_ms = ?, outcome = ?,
                       http_status = COALESCE(?, http_status),
                       error_type = ?, error_phase = ?
                 WHERE id = ?
                """,
                (
                    int(completed_at_ms),
                    max(0, int(total_ms)),
                    normalized_outcome,
                    int(http_status) if http_status is not None else None,
                    normalized_error,
                    normalized_phase,
                    str(reservation_id),
                ),
            )

    def reset_quota_windows(self, user_id: str, selection_key: str | None = None) -> None:
        if selection_key is not None and selection_key not in SELECTION_BY_KEY:
            raise ChatPolicyError("未知模型档位")
        with self._guard(), self._connect() as connection:
            self._begin(connection, lock_key=f"chat-quota:{user_id}")
            try:
                if selection_key is None:
                    connection.execute(
                        "DELETE FROM chat_quota_window WHERE user_id = ?", (str(user_id),)
                    )
                else:
                    connection.execute(
                        "DELETE FROM chat_quota_window WHERE user_id = ? AND selection_key = ?",
                        (str(user_id), selection_key),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def admin_summary(self, days: int = 14, recent_limit: int = 12) -> dict[str, Any]:
        """Return prompt-free aggregate data for the administrator dashboard."""

        safe_days = min(31, max(7, int(days)))
        safe_recent_limit = min(50, max(1, int(recent_limit)))
        now = _now()
        cutoff = now - safe_days * 24 * 60 * 60
        day_cutoff = now - 24 * 60 * 60
        week_cutoff = now - 7 * 24 * 60 * 60
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, user_id, requested_selection_key, selection_key,
                       fallback_from, status, created_at, finalized_at
                  FROM chat_usage
                 WHERE created_at >= ?
                 ORDER BY created_at DESC, id DESC
                """,
                (cutoff,),
            ).fetchall()
            all_time_requests = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chat_usage WHERE status = 'committed'"
                ).fetchone()[0]
                or 0
            )

        daily: dict[str, dict[str, int]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        request_24h = 0
        request_7d = 0
        released_24h = 0
        fallback_7d = 0
        active_reservations = 0
        recent: list[dict[str, Any]] = []

        for raw in rows:
            row = dict(raw)
            created_at = int(row.get("created_at") or 0)
            status_value = str(row.get("status") or "")
            selection_key = str(row.get("selection_key") or "")
            selection = SELECTION_BY_KEY.get(selection_key, {})
            family = str(selection.get("family") or "other")
            date_key = time.strftime("%Y-%m-%d", time.localtime(created_at))
            date_bucket = daily.setdefault(
                date_key,
                {"requests": 0, "released": 0, "fallbacks": 0},
            )
            provider_bucket = by_provider.setdefault(
                family,
                {"family": family, "requests": 0, "fallbacks": 0},
            )

            if status_value == "committed":
                date_bucket["requests"] += 1
                provider_bucket["requests"] += 1
                if created_at >= day_cutoff:
                    request_24h += 1
                if created_at >= week_cutoff:
                    request_7d += 1
                if row.get("fallback_from"):
                    date_bucket["fallbacks"] += 1
                    provider_bucket["fallbacks"] += 1
                    if created_at >= week_cutoff:
                        fallback_7d += 1
            elif status_value == "released":
                date_bucket["released"] += 1
                if created_at >= day_cutoff:
                    released_24h += 1
            elif status_value == "reserved":
                active_reservations += 1

            if len(recent) < safe_recent_limit:
                recent.append(
                    {
                        "request_id": row.get("request_id"),
                        "user_id": row.get("user_id"),
                        "selection_key": selection_key,
                        "version_label": selection.get("version_label") or selection_key,
                        "level_label": selection.get("level_label") or "",
                        "family": family,
                        "status": status_value,
                        "fallback": bool(row.get("fallback_from")),
                        "created_at": created_at,
                    }
                )

        return {
            "all_time_requests": all_time_requests,
            "requests_24h": request_24h,
            "requests_7d": request_7d,
            "released_24h": released_24h,
            "fallbacks_7d": fallback_7d,
            "active_reservations": active_reservations,
            "daily": [
                {
                    "date": date_key,
                    **daily.get(
                        date_key,
                        {"requests": 0, "released": 0, "fallbacks": 0},
                    ),
                }
                for date_key in [
                    time.strftime(
                        "%Y-%m-%d",
                        time.localtime(now - offset * 24 * 60 * 60),
                    )
                    for offset in range(safe_days - 1, -1, -1)
                ]
            ],
            "providers": [by_provider[key] for key in sorted(by_provider)],
            "recent": recent,
        }

    def operations_summary(self, hours: int = 1) -> dict[str, Any]:
        """Return sanitized request timing and failure aggregates."""

        safe_hours = int(hours) if int(hours) in {1, 6, 24} else 1
        bucket_seconds = {1: 5 * 60, 6: 15 * 60, 24: 60 * 60}[safe_hours]
        now = _now()
        cutoff = now - safe_hours * 60 * 60
        bucket_start = cutoff - (cutoff % bucket_seconds)
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, user_id, group_id, provider_family, selection_key,
                       status, created_at, finalized_at, queued_at_ms, admitted_at_ms,
                       connected_at_ms, first_content_at_ms, completed_at_ms,
                       queue_ms, connect_ms, ttft_ms, total_ms, http_status,
                       outcome, error_type, error_phase
                  FROM chat_usage
                 WHERE created_at >= ?
                 ORDER BY created_at DESC, id DESC
                """,
                (cutoff,),
            ).fetchall()

        def percentile(values: list[int], percent: float) -> int | None:
            if not values:
                return None
            ordered = sorted(values)
            index = (len(ordered) - 1) * percent
            lower = int(index)
            upper = min(len(ordered) - 1, lower + 1)
            if lower == upper:
                return int(ordered[lower])
            weight = index - lower
            return int(round(ordered[lower] * (1 - weight) + ordered[upper] * weight))

        bucket_map: dict[int, dict[str, Any]] = {}
        cursor = bucket_start
        while cursor <= now:
            bucket_map[cursor] = {
                "at": cursor,
                "requests": 0,
                "errors": 0,
                "queue_values": [],
                "connect_values": [],
                "ttft_values": [],
                "total_values": [],
            }
            cursor += bucket_seconds

        by_provider: dict[str, dict[str, Any]] = {}
        by_group: dict[str, dict[str, Any]] = {}
        queue_values: list[int] = []
        connect_values: list[int] = []
        ttft_values: list[int] = []
        total_values: list[int] = []
        successes = 0
        errors = 0
        active = 0
        recent_errors: list[dict[str, Any]] = []

        for raw in rows:
            row = dict(raw)
            created_at = int(row.get("created_at") or 0)
            bucket_at = created_at - (created_at % bucket_seconds)
            bucket = bucket_map.get(bucket_at)
            family = str(row.get("provider_family") or "")
            if not family:
                family = str(SELECTION_BY_KEY.get(str(row.get("selection_key") or ""), {}).get("family") or "other")
            group_id = str(row.get("group_id") or "legacy")
            provider = by_provider.setdefault(
                family,
                {"family": family, "requests": 0, "errors": 0, "latencies": []},
            )
            group = by_group.setdefault(
                group_id,
                {"group_id": group_id, "requests": 0, "errors": 0},
            )
            provider["requests"] += 1
            group["requests"] += 1
            if bucket is not None:
                bucket["requests"] += 1

            status_value = str(row.get("status") or "")
            outcome = str(row.get("outcome") or "")
            is_active = status_value == "reserved" and not row.get("completed_at_ms")
            is_success = outcome == "success" or (not outcome and status_value == "committed")
            is_error = bool(outcome and outcome != "success") or (not outcome and status_value == "released")
            if is_active:
                active += 1
            elif is_success:
                successes += 1
            elif is_error:
                errors += 1
                provider["errors"] += 1
                group["errors"] += 1
                if bucket is not None:
                    bucket["errors"] += 1
                if len(recent_errors) < 30:
                    recent_errors.append(
                        {
                            "request_id": row.get("request_id"),
                            "user_id": row.get("user_id"),
                            "group_id": group_id,
                            "family": family,
                            "selection_key": row.get("selection_key"),
                            "http_status": row.get("http_status"),
                            "error_type": row.get("error_type") or "legacy_released",
                            "error_phase": row.get("error_phase") or "unknown",
                            "created_at": created_at,
                            "total_ms": row.get("total_ms"),
                        }
                    )

            for field, destination, bucket_field in (
                ("queue_ms", queue_values, "queue_values"),
                ("connect_ms", connect_values, "connect_values"),
                ("ttft_ms", ttft_values, "ttft_values"),
                ("total_ms", total_values, "total_values"),
            ):
                value = row.get(field)
                if value is None:
                    continue
                normalized = max(0, int(value))
                destination.append(normalized)
                if bucket is not None:
                    bucket[bucket_field].append(normalized)
                if field == "total_ms":
                    provider["latencies"].append(normalized)

        request_count = len(rows)
        completed_count = successes + errors
        provider_items = []
        for family in sorted(by_provider):
            item = by_provider[family]
            latencies = item.pop("latencies")
            provider_items.append(
                {
                    **item,
                    "error_rate": (item["errors"] / item["requests"]) if item["requests"] else 0,
                    "total_p50_ms": percentile(latencies, 0.50),
                    "total_p95_ms": percentile(latencies, 0.95),
                }
            )

        buckets = []
        for item in bucket_map.values():
            buckets.append(
                {
                    "at": item["at"],
                    "requests": item["requests"],
                    "errors": item["errors"],
                    "queue_avg_ms": (
                        int(sum(item["queue_values"]) / len(item["queue_values"]))
                        if item["queue_values"] else None
                    ),
                    "connect_avg_ms": (
                        int(sum(item["connect_values"]) / len(item["connect_values"]))
                        if item["connect_values"] else None
                    ),
                    "ttft_avg_ms": (
                        int(sum(item["ttft_values"]) / len(item["ttft_values"]))
                        if item["ttft_values"] else None
                    ),
                    "total_avg_ms": (
                        int(sum(item["total_values"]) / len(item["total_values"]))
                        if item["total_values"] else None
                    ),
                }
            )

        return {
            "hours": safe_hours,
            "bucket_seconds": bucket_seconds,
            "requests": request_count,
            "completed": completed_count,
            "successes": successes,
            "errors": errors,
            "active_reservations": active,
            "error_rate": (errors / completed_count) if completed_count else 0,
            "latency": {
                "queue_p50_ms": percentile(queue_values, 0.50),
                "queue_p95_ms": percentile(queue_values, 0.95),
                "connect_p50_ms": percentile(connect_values, 0.50),
                "connect_p95_ms": percentile(connect_values, 0.95),
                "ttft_p50_ms": percentile(ttft_values, 0.50),
                "ttft_p95_ms": percentile(ttft_values, 0.95),
                "total_p50_ms": percentile(total_values, 0.50),
                "total_p95_ms": percentile(total_values, 0.95),
            },
            "buckets": buckets,
            "providers": provider_items,
            "groups": [by_group[key] for key in sorted(by_group)],
            "recent_errors": recent_errors,
        }

    def recent_usage(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self._guard(), self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, requested_selection_key, selection_key, fallback_from,
                       status, created_at, finalized_at
                  FROM chat_usage
                 WHERE user_id = ?
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (str(user_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]


CHAT_STORE = ChatStore()
