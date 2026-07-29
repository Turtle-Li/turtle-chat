"""Versioned OpenAI API price simulation for project usage.

The active upstream is ChatGPT Web, so these values never represent an actual
OpenAI Platform invoice. They are an immutable reference snapshot used to show
what the same token mix would approximately cost at standard API rates.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


PRICE_CARD_VERSION = "openai-standard-2026-07-25"


@dataclass(frozen=True, slots=True)
class ApiPrice:
    model: str
    input_rate_nano_usd: int
    cached_input_rate_nano_usd: int | None
    cache_write_rate_nano_usd: int | None
    output_rate_nano_usd: int


# Rate unit: nano-USD per token. Dividing by 1,000 gives USD per 1M tokens.
OPENAI_STANDARD_PRICES: dict[str, ApiPrice] = {
    "gpt-5.6-sol": ApiPrice("gpt-5.6-sol", 5_000, 500, 6_250, 30_000),
    "gpt-5.5": ApiPrice("gpt-5.5", 5_000, 500, None, 30_000),
    "gpt-5.3": ApiPrice("gpt-5.3", 1_750, 175, None, 14_000),
    "o3": ApiPrice("o3", 2_000, 500, None, 8_000),
    "gpt-5.4": ApiPrice("gpt-5.4", 2_500, 250, None, 15_000),
    "gpt-5.4-pro": ApiPrice("gpt-5.4-pro", 30_000, None, None, 180_000),
}


def price_for_route(route: str | None) -> ApiPrice:
    normalized = str(route or "").strip().lower()
    if normalized.startswith("o3"):
        return OPENAI_STANDARD_PRICES["o3"]
    if normalized.startswith("gpt-5-3") or normalized == "gpt-5.3":
        return OPENAI_STANDARD_PRICES["gpt-5.3"]
    if normalized.startswith("gpt-5-4:pro") or normalized == "gpt-5-4-pro":
        return OPENAI_STANDARD_PRICES["gpt-5.4-pro"]
    if normalized.startswith("gpt-5-4") or normalized == "gpt-5-4-thinking":
        return OPENAI_STANDARD_PRICES["gpt-5.4"]
    if normalized.startswith("gpt-5-5") or normalized == "gpt-5-5-instant":
        return OPENAI_STANDARD_PRICES["gpt-5.5"]
    return OPENAI_STANDARD_PRICES["gpt-5.6-sol"]


def simulated_cost(
    *,
    price: ApiPrice,
    input_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int,
) -> int:
    """Return estimated micro-USD using the immutable rate snapshot."""

    prompt = max(0, int(input_tokens))
    cached = max(0, min(prompt, int(cached_input_tokens)))
    written = max(0, min(prompt - cached, int(cache_write_tokens)))

    # If a profile has no separate cached/write rate, those tokens remain at
    # the ordinary input rate rather than silently becoming free.
    billed_cached = cached if price.cached_input_rate_nano_usd is not None else 0
    billed_written = written if price.cache_write_rate_nano_usd is not None else 0
    ordinary = max(0, prompt - billed_cached - billed_written)
    nano_usd = (
        ordinary * price.input_rate_nano_usd
        + billed_cached * (price.cached_input_rate_nano_usd or 0)
        + billed_written * (price.cache_write_rate_nano_usd or 0)
        + max(0, int(output_tokens)) * price.output_rate_nano_usd
    )
    return math.ceil(nano_usd / 1_000) if nano_usd else 0


_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+|[^\w\s]", re.UNICODE)


def estimate_text_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for transient fallback metering."""

    value = str(text or "")
    if not value:
        return 0
    ascii_text = "".join(character if ord(character) < 128 else " " for character in value)
    ascii_tokens = sum(
        max(1, math.ceil(len(piece) / 4))
        for piece in _ASCII_WORD.findall(ascii_text)
    )
    non_ascii_tokens = sum(ord(character) >= 128 and not character.isspace() for character in value)
    return int(ascii_tokens + non_ascii_tokens)


def estimate_value_tokens(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return estimate_text_tokens(str(value))
    if isinstance(value, str):
        return estimate_text_tokens(value)
    if isinstance(value, list):
        return sum(estimate_value_tokens(item) for item in value)
    if isinstance(value, dict):
        return sum(
            estimate_text_tokens(str(key)) + estimate_value_tokens(item)
            for key, item in value.items()
            if key not in {"image_url", "url"}
        )
    return 0


def estimate_chat_input_tokens(messages: list[dict[str, Any]]) -> int:
    # The fixed per-message allowance approximates chat envelope tokens. Image
    # token billing depends on dimensions/detail and is intentionally omitted.
    return max(
        1,
        3
        + sum(
            4
            + estimate_text_tokens(str(message.get("role") or ""))
            + estimate_value_tokens(message.get("content"))
            + estimate_value_tokens(message.get("tool_calls"))
            + estimate_value_tokens(message.get("function_call"))
            for message in messages
            if isinstance(message, dict)
        ),
    )


def estimate_completion_tokens(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return 0
    total = 0
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        delta = choice.get("delta")
        if isinstance(message, dict):
            total += estimate_value_tokens(message.get("content"))
            total += estimate_value_tokens(message.get("tool_calls"))
            total += estimate_value_tokens(message.get("function_call"))
        if isinstance(delta, dict):
            total += estimate_value_tokens(delta.get("content"))
            total += estimate_value_tokens(delta.get("tool_calls"))
            total += estimate_value_tokens(delta.get("function_call"))
    return total
