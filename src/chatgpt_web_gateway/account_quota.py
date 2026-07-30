"""Sanitized ChatGPT account budget profiles used by the local scheduler.

These profiles are not a live OpenAI balance API.  They combine the narrow
quantities OpenAI publishes with explicitly labelled Turtle dispatch budgets
for lanes whose exact allowance is not public.  The account-pool stores only
the selected profile id and its own successful-request counters.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


OFFICIAL_GPT56_CHATGPT_URL = (
    "https://help.openai.com/en/articles/20001354-gpt-56-in-chatgpt"
)
OFFICIAL_FREE_TIER_URL = (
    "https://help.openai.com/en/articles/9275245-using-chatgpt-s-free-tier-faq"
)
OFFICIAL_PRO_TIERS_URL = (
    "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers"
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

SELECTION_LABELS: dict[str, str] = {
    "latest:medium": "GPT-5.6 Sol 中",
    "latest:high": "GPT-5.6 Sol 高",
    "latest:xhigh": "GPT-5.6 Sol 极高",
    "latest:pro": "GPT-5.6 Sol Pro",
    "gpt-5-5:instant": "GPT-5.5 极速",
    "gpt-5-3:standard": "GPT-5.3",
    "o3:standard": "o3",
    "image:create": "ChatGPT 图片生图",
}
SELECTION_KEYS = tuple(SELECTION_LABELS)

CLAUDE_SELECTION_LABELS: dict[str, str] = {
    "claude-sonnet-5:standard": "Claude Sonnet 5 标准",
    "claude-sonnet-5:extended": "Claude Sonnet 5 扩展思考",
    "claude-opus-4-8:standard": "Claude Opus 4.8 标准",
    "claude-opus-4-8:extended": "Claude Opus 4.8 扩展思考",
    "claude-haiku-4-5:fast": "Claude Haiku 4.5 快速",
}
CLAUDE_SELECTION_KEYS = tuple(CLAUDE_SELECTION_LABELS)
SELECTION_LABELS_BY_PROVIDER = {
    "gpt": SELECTION_LABELS,
    "claude": CLAUDE_SELECTION_LABELS,
}
SELECTION_KEYS_BY_PROVIDER = {
    provider: tuple(labels)
    for provider, labels in SELECTION_LABELS_BY_PROVIDER.items()
}


def _lane(
    enabled: bool,
    dispatch_budget_count: int | None = None,
    window_seconds: int = 0,
    *,
    source: str = "turtle_recommendation",
    published_min: int | None = None,
    published_max: int | None = None,
    published_window_seconds: int | None = None,
    source_note: str = "",
    reserve_ratio: float = 0.05,
) -> dict[str, Any]:
    reserve_count = (
        max(1, int(dispatch_budget_count * reserve_ratio + 0.999))
        if enabled and dispatch_budget_count
        else 0
    )
    return {
        "enabled": bool(enabled),
        "dispatch_budget_count": dispatch_budget_count,
        "window_seconds": int(window_seconds),
        "reserve_count": reserve_count,
        "source": source,
        "published_min": published_min,
        "published_max": published_max,
        "published_window_seconds": published_window_seconds,
        "source_note": source_note,
    }


ACCOUNT_QUOTA_PROFILES: dict[str, dict[str, Any]] = {
    "untracked": {
        "label": "未设置",
        "description": "只做健康、并发和近期请求均衡；不声称知道上游剩余额度。",
        "official_note": "当前 ChatGPT Web 没有供本项目读取各模型实时剩余次数的官方接口。",
        "recommendation_note": "建议在确认账号订阅类型后选择一个保守调度模板。",
        "sources": [],
        "lanes": {key: _lane(True, None, 0, source="untracked") for key in SELECTION_KEYS},
    },
    "free": {
        "label": "Free 免费",
        "description": "免费账号只调度 GPT-5.5 极速；额度按官方五小时动态窗口处理。",
        "official_note": (
            "OpenAI 公开 Free 可有限使用 GPT-5.5 Instant，窗口为 5 小时；"
            "固定次数未公开且可能动态变化，Free 不包含 GPT-5.6 推理档。"
        ),
        "recommendation_note": (
            "本站不再用自定次数提前拦截；真实 429 与页面恢复时间优先。"
        ),
        "sources": [
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
            {"label": "ChatGPT Free Tier FAQ", "url": OFFICIAL_FREE_TIER_URL},
        ],
        "lanes": {
            "latest:medium": _lane(False),
            "latest:high": _lane(False),
            "latest:xhigh": _lane(False),
            "latest:pro": _lane(False),
            "gpt-5-5:instant": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                published_window_seconds=5 * 60 * 60,
                source_note="官方按 5 小时窗口动态调整，未公布固定次数。",
            ),
            "gpt-5-3:standard": _lane(False),
            "o3:standard": _lane(False),
            "image:create": _lane(
                True,
                10,
                24 * 60 * 60,
                source="observed_upstream",
                source_note=(
                    "2026-07-30 实测 ChatGPT Web image_gen 余量为 10/日；"
                    "每个任务按官方余量实际减少值计数，读取失败才按返回图片数兜底。"
                ),
                reserve_ratio=0,
            ),
        },
    },
    "go": {
        "label": "Go",
        "description": "按 Go 的公开可用性与保守路由映射限制账号调度。",
        "official_note": "公开值：GPT-5.5 极速 160 次/3 小时；Go Thinking 10 次/5 小时。",
        "recommendation_note": "Go 的 + Thinking 不属于当前 GPT-5.6 模型菜单，本站不做伪模型映射。",
        "sources": [{"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL}],
        "lanes": {
            "latest:medium": _lane(False),
            "latest:high": _lane(False),
            "latest:xhigh": _lane(False),
            "latest:pro": _lane(False),
            "gpt-5-5:instant": _lane(
                True,
                160,
                3 * 60 * 60,
                source="official_published",
                published_min=160,
                published_max=160,
            ),
            "gpt-5-3:standard": _lane(False),
            "o3:standard": _lane(False),
            "image:create": _lane(
                True,
                20,
                24 * 60 * 60,
                source="turtle_recommendation",
                source_note=(
                    "官方 Go 固定图片次数未公开，本站暂按 20/日保守调度；"
                    "每个任务按官方余量实际减少值计数，读取失败才按返回图片数兜底。"
                ),
                reserve_ratio=0.1,
            ),
        },
    },
    "plus": {
        "label": "Plus",
        "description": "按 Plus 官方可用性调度；未公布固定次数的档位不加本地硬限制。",
        "official_note": (
            "公开值：GPT-5.5 极速 160 次/3 小时；GPT-5.6 中、高可用；"
            "o3 为 100 次/周。"
        ),
        "recommendation_note": "GPT-5.6 推理档与 GPT-5.3 的固定次数未公开，以上游恢复时间与 429 为准。",
        "sources": [
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
            {"label": "o3 usage limits", "url": OFFICIAL_O3_LIMITS_URL},
        ],
        "lanes": {
            "latest:medium": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                source_note="官方当前未公布手动推理固定次数。",
            ),
            "latest:high": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                source_note="官方当前未公布手动推理固定次数。",
            ),
            "latest:xhigh": _lane(False),
            "latest:pro": _lane(False),
            "gpt-5-5:instant": _lane(
                True,
                160,
                3 * 60 * 60,
                source="official_published",
                published_min=160,
                published_max=160,
            ),
            "gpt-5-3:standard": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                source_note="官方模型菜单已确认可选；当前未公布固定次数。",
            ),
            "o3:standard": _lane(
                True,
                100,
                7 * 24 * 60 * 60,
                source="official_published",
                published_min=100,
                published_max=100,
                source_note="Plus 官方为 100 次/周，从首次使用起七天重置。",
                reserve_ratio=0.05,
            ),
            "image:create": _lane(
                True,
                100,
                24 * 60 * 60,
                source="observed_upstream",
                source_note=(
                    "2026-07-30 实测 ChatGPT Web image_gen 余量为 100/日；"
                    "每个任务按官方余量实际减少值计数，读取失败才按返回图片数兜底。"
                ),
                reserve_ratio=0.1,
            ),
        },
    },
    "pro-5x": {
        "label": "5× Pro",
        "description": "按 5× Pro 官方可用性与套餐倍率调度。",
        "official_note": "官方公开 5× Pro 的计划用量是 Plus 的 5 倍，并包含全部 Pro 档位。",
        "recommendation_note": (
            "部分模型有独立 allowance；官方没有公布逐模型固定次数，"
            "因此只标注 5× 套餐倍率，不虚构 Instant 的 160×5 硬上限。"
        ),
        "sources": [
            {"label": "About ChatGPT Pro tiers", "url": OFFICIAL_PRO_TIERS_URL},
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ],
        "lanes": {
            "latest:medium": _lane(True, None, 0, source="official_dynamic"),
            "latest:high": _lane(True, None, 0, source="official_dynamic"),
            "latest:xhigh": _lane(True, None, 0, source="official_dynamic"),
            "latest:pro": _lane(True, None, 0, source="official_dynamic"),
            "gpt-5-5:instant": _lane(
                True,
                None,
                0,
                source="official_multiplier",
                source_note="套餐总用量为 Plus 的 5×；本模型固定次数未公开。",
            ),
            "gpt-5-3:standard": _lane(True, None, 0, source="official_dynamic"),
            "o3:standard": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                source_note="Pro 官方为不设固定次数，仍受防滥用护栏约束。",
            ),
            "image:create": _lane(
                True,
                500,
                24 * 60 * 60,
                source="turtle_recommendation",
                source_note=(
                    "5× Pro 图片固定次数未公开，暂按 500/日调度；"
                    "每个任务按官方余量实际减少值计数，读取失败才按返回图片数兜底。"
                ),
                reserve_ratio=0.1,
            ),
        },
    },
    "pro-20x": {
        "label": "20× Pro",
        "description": "按 20× Pro 官方可用性与套餐倍率调度。",
        "official_note": "官方公开 20× Pro 的计划用量是 Plus 的 20 倍，并包含全部 Pro 档位。",
        "recommendation_note": (
            "部分模型有独立 allowance；官方没有公布逐模型固定次数，"
            "因此只标注 20× 套餐倍率，不虚构 Instant 的 160×20 硬上限。"
        ),
        "sources": [
            {"label": "About ChatGPT Pro tiers", "url": OFFICIAL_PRO_TIERS_URL},
            {"label": "GPT-5.6 in ChatGPT", "url": OFFICIAL_GPT56_CHATGPT_URL},
        ],
        "lanes": {
            "latest:medium": _lane(True, None, 0, source="official_dynamic"),
            "latest:high": _lane(True, None, 0, source="official_dynamic"),
            "latest:xhigh": _lane(True, None, 0, source="official_dynamic"),
            "latest:pro": _lane(True, None, 0, source="official_dynamic"),
            "gpt-5-5:instant": _lane(
                True,
                None,
                0,
                source="official_multiplier",
                source_note="套餐总用量为 Plus 的 20×；本模型固定次数未公开。",
            ),
            "gpt-5-3:standard": _lane(True, None, 0, source="official_dynamic"),
            "o3:standard": _lane(
                True,
                None,
                0,
                source="official_dynamic",
                source_note="Pro 官方为不设固定次数，仍受防滥用护栏约束。",
            ),
            "image:create": _lane(
                True,
                1000,
                24 * 60 * 60,
                source="observed_upstream",
                source_note=(
                    "2026-07-30 实测 ChatGPT Web image_gen 余量约 1000/日；"
                    "每个任务按官方余量实际减少值计数，读取失败才按返回图片数兜底。"
                ),
                reserve_ratio=0.1,
            ),
        },
    },
}

CLAUDE_ACCOUNT_QUOTA_PROFILES: dict[str, dict[str, Any]] = {
    "untracked": {
        "label": "未设置",
        "description": "只做健康、并发和逐档成功请求统计；不声称知道上游剩余额度。",
        "official_note": "Claude Web 没有供本项目读取逐模型实时余量的稳定接口。",
        "recommendation_note": "建议确认账号订阅类型后选择保守调度模板；真实 429 与页面恢复时间优先。",
        "sources": [],
        "lanes": {
            key: _lane(True, None, 0, source="untracked")
            for key in CLAUDE_SELECTION_KEYS
        },
    },
    "free": {
        "label": "Free 免费",
        "description": "只开放 Sonnet 标准与 Haiku 快速，并使用保守的本地日预算。",
        "official_note": (
            "Claude Free 的使用量有限且会动态变化；官方没有公布逐模型固定消息数。"
        ),
        "recommendation_note": (
            "Turtle 建议 Sonnet 标准 8 次/24 小时、Haiku 快速 20 次/24 小时；"
            "这些是站内调度预算，不是官方余额。"
        ),
        "sources": [{"label": "Claude usage limits", "url": OFFICIAL_CLAUDE_LIMITS_URL}],
        "lanes": {
            "claude-sonnet-5:standard": _lane(True, 8, 24 * 60 * 60, reserve_ratio=0.125),
            "claude-sonnet-5:extended": _lane(False),
            "claude-opus-4-8:standard": _lane(False),
            "claude-opus-4-8:extended": _lane(False),
            "claude-haiku-4-5:fast": _lane(True, 20, 24 * 60 * 60, reserve_ratio=0.1),
        },
    },
    "pro": {
        "label": "Pro",
        "description": "按 Claude Pro 的五小时会话与周限制使用保守的逐档本地预算。",
        "official_note": (
            "Claude Pro 每次会话至少为 Free 的 5 倍，基础会话额度每 5 小时重置，"
            "另有跨模型周额度；实际消耗取决于上下文、模型和思考强度。"
        ),
        "recommendation_note": (
            "官方未公布逐模型固定消息数；本站按 Sonnet/Haiku 五小时预算与高成本档周预算调度。"
        ),
        "sources": [
            {"label": "What is Claude Pro?", "url": OFFICIAL_CLAUDE_PRO_URL},
            {"label": "Claude usage limits", "url": OFFICIAL_CLAUDE_LIMITS_URL},
        ],
        "lanes": {
            "claude-sonnet-5:standard": _lane(True, 30, 5 * 60 * 60),
            "claude-sonnet-5:extended": _lane(True, 10, 7 * 24 * 60 * 60, reserve_ratio=0.1),
            "claude-opus-4-8:standard": _lane(True, 10, 7 * 24 * 60 * 60, reserve_ratio=0.1),
            "claude-opus-4-8:extended": _lane(True, 5, 7 * 24 * 60 * 60, reserve_ratio=0.2),
            "claude-haiku-4-5:fast": _lane(True, 80, 5 * 60 * 60),
        },
    },
    "max-5x": {
        "label": "Max 5×",
        "description": "以 Pro 逐档建议的 5 倍为本地预算，并保留 429 安全余量。",
        "official_note": (
            "Claude Max 5× 官方提供 Pro 每次会话用量的 5 倍，并设全模型周额度与 Sonnet 周额度。"
        ),
        "recommendation_note": "逐档数字是 Turtle 建议；官方周额度是共享额度，不能解释成每档独立官方余额。",
        "sources": [{"label": "What is Claude Max?", "url": OFFICIAL_CLAUDE_MAX_URL}],
        "lanes": {
            "claude-sonnet-5:standard": _lane(True, 150, 5 * 60 * 60),
            "claude-sonnet-5:extended": _lane(True, 50, 7 * 24 * 60 * 60),
            "claude-opus-4-8:standard": _lane(True, 50, 7 * 24 * 60 * 60),
            "claude-opus-4-8:extended": _lane(True, 25, 7 * 24 * 60 * 60),
            "claude-haiku-4-5:fast": _lane(True, 400, 5 * 60 * 60),
        },
    },
    "max-20x": {
        "label": "Max 20×",
        "description": "以 Pro 逐档建议的 20 倍为本地预算，并保留 429 安全余量。",
        "official_note": (
            "Claude Max 20× 官方提供 Pro 每次会话用量的 20 倍，并设全模型周额度与 Sonnet 周额度。"
        ),
        "recommendation_note": "逐档数字是 Turtle 建议；官方周额度是共享额度，不能解释成每档独立官方余额。",
        "sources": [{"label": "What is Claude Max?", "url": OFFICIAL_CLAUDE_MAX_URL}],
        "lanes": {
            "claude-sonnet-5:standard": _lane(True, 600, 5 * 60 * 60),
            "claude-sonnet-5:extended": _lane(True, 200, 7 * 24 * 60 * 60),
            "claude-opus-4-8:standard": _lane(True, 200, 7 * 24 * 60 * 60),
            "claude-opus-4-8:extended": _lane(True, 100, 7 * 24 * 60 * 60),
            "claude-haiku-4-5:fast": _lane(True, 1_600, 5 * 60 * 60),
        },
    },
}


def normalize_quota_profile(value: str | None, provider: str = "gpt") -> str:
    if provider == "claude":
        profile_id = str(value or "untracked").strip().lower()
        if profile_id not in CLAUDE_ACCOUNT_QUOTA_PROFILES:
            raise ValueError("Claude 账号额度模板无效")
        return profile_id
    if provider != "gpt":
        raise ValueError("账号 Provider 无效")
    profile_id = str(value or "untracked").strip().lower()
    if profile_id not in ACCOUNT_QUOTA_PROFILES:
        raise ValueError("账号额度模板无效")
    return profile_id


def selection_keys(provider: str) -> tuple[str, ...]:
    try:
        return SELECTION_KEYS_BY_PROVIDER[provider]
    except KeyError as exc:
        raise ValueError("账号 Provider 无效") from exc


def selection_label(provider: str, selection_key: str) -> str:
    return SELECTION_LABELS_BY_PROVIDER.get(provider, {}).get(
        selection_key,
        selection_key,
    )


def quota_lane(
    profile_id: str | None,
    selection_key: str,
    provider: str = "gpt",
) -> dict[str, Any]:
    normalized = normalize_quota_profile(profile_id, provider)
    if provider == "claude":
        profile = CLAUDE_ACCOUNT_QUOTA_PROFILES[normalized]
    else:
        profile = ACCOUNT_QUOTA_PROFILES[normalized]
    lane = profile["lanes"].get(selection_key)
    if lane is None:
        return _lane(False)
    return dict(lane)


def quota_profiles_payload(provider: str = "gpt") -> list[dict[str, Any]]:
    if provider == "claude":
        profiles = CLAUDE_ACCOUNT_QUOTA_PROFILES
        labels = CLAUDE_SELECTION_LABELS
    if provider != "gpt":
        if provider != "claude":
            raise ValueError("账号 Provider 无效")
    else:
        profiles = ACCOUNT_QUOTA_PROFILES
        labels = SELECTION_LABELS
    result: list[dict[str, Any]] = []
    for profile_id, profile in profiles.items():
        item = {key: deepcopy(value) for key, value in profile.items() if key != "lanes"}
        item["id"] = profile_id
        item["lanes"] = [
            {
                "selection_key": selection_key,
                "label": labels[selection_key],
                **deepcopy(profile["lanes"][selection_key]),
            }
            for selection_key in labels
        ]
        result.append(item)
    return result
