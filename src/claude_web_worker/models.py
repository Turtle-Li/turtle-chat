from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import AuthError, atomic_json_write, secure_auth_directory


CLAUDE_PUBLIC_MODEL = "claude-web"


@dataclass(frozen=True, slots=True)
class ClaudeRoute:
    version: str
    version_label: str
    level: str
    level_label: str
    upstream_model: str
    effort: str
    thinking_mode: str
    cost: int

    @property
    def key(self) -> str:
        return f"{self.version}:{self.level}"


# Candidate routes are deliberately small.  They become visible only after
# each exact route returns effective content through a real authenticated
# claude.ai conversation and is written to verified-models.json.
CLAUDE_ROUTES: tuple[ClaudeRoute, ...] = (
    ClaudeRoute(
        "claude-sonnet-5",
        "Claude Sonnet 5",
        "standard",
        "标准",
        "claude-sonnet-5",
        "medium",
        "standard",
        2,
    ),
    ClaudeRoute(
        "claude-sonnet-5",
        "Claude Sonnet 5",
        "extended",
        "扩展思考",
        "claude-sonnet-5",
        "high",
        "extended",
        3,
    ),
    ClaudeRoute(
        "claude-opus-4-8",
        "Claude Opus 4.8",
        "standard",
        "标准",
        "claude-opus-4-8",
        "medium",
        "standard",
        5,
    ),
    ClaudeRoute(
        "claude-opus-4-8",
        "Claude Opus 4.8",
        "extended",
        "扩展思考",
        "claude-opus-4-8",
        "high",
        "extended",
        10,
    ),
    ClaudeRoute(
        "claude-haiku-4-5",
        "Claude Haiku 4.5",
        "fast",
        "快速",
        "claude-haiku-4-5",
        "low",
        "off",
        1,
    ),
)

ROUTE_BY_KEY = {route.key: route for route in CLAUDE_ROUTES}


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            if not value:
                raise ValueError("message content must not be empty")
            return value
        if not value:
            raise ValueError("message content parts must not be empty")
        return value


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    web_search: bool = False
    turtle_claude_model: str | None = None
    turtle_claude_thinking: str | None = None


class UnsupportedContent(ValueError):
    pass


def _message_text(message: ChatMessage) -> str:
    if message.role == "tool":
        raise UnsupportedContent("Claude Web adapter does not support tool messages yet")
    if message.content is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    values: list[str] = []
    for part in message.content:
        if not isinstance(part, dict) or part.get("type") != "text":
            raise UnsupportedContent(
                "Claude Web adapter currently accepts text only; images and files stay disabled"
            )
        text = part.get("text")
        if not isinstance(text, str):
            raise UnsupportedContent("Claude Web text content is invalid")
        values.append(text)
    return "\n".join(values)


def serialize_history(messages: list[ChatMessage]) -> str:
    transcript = [
        {"role": message.role, "content": _message_text(message)}
        for message in messages
    ]
    if not any(item["role"] == "user" and item["content"].strip() for item in transcript):
        raise UnsupportedContent("Claude Web request requires a user message")
    encoded = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    return (
        "Continue the conversation represented by the JSON message array below. "
        "Respect system and developer messages as instructions, preserve the prior turns, "
        "and answer only as the assistant to the final user message.\n\n"
        f"<conversation_json>{encoded}</conversation_json>"
    )


def normalize_verified_routes(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return ()
    raw = payload.get("routes")
    if not isinstance(raw, list):
        return ()
    requested = {str(value).strip() for value in raw}
    return tuple(route.key for route in CLAUDE_ROUTES if route.key in requested)


def load_verified_routes(path: Path) -> tuple[str, ...]:
    path = Path(os.path.abspath(Path(path).expanduser()))
    secure_auth_directory(path.parent)
    if not path.is_file() or path.is_symlink():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return normalize_verified_routes(payload)


def save_verified_routes(path: Path, routes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(route.key for route in CLAUDE_ROUTES if route.key in set(routes))
    if not normalized:
        raise AuthError("No Claude route produced effective content; nothing was published")
    atomic_json_write(
        Path(path),
        {"version": 1, "verified_at": int(time.time()), "routes": list(normalized)},
    )
    return normalized


def resolve_route(
    version: str | None,
    level: str | None,
    verified_routes: tuple[str, ...],
) -> ClaudeRoute:
    allowed = set(verified_routes)
    if not allowed:
        raise ValueError("Claude models have not completed real-route verification")
    if version is None and level is None:
        for route in CLAUDE_ROUTES:
            if route.key in allowed:
                return route
    selected_version = str(version or "").strip()
    selected_level = str(level or "").strip()
    key = f"{selected_version}:{selected_level}"
    route = ROUTE_BY_KEY.get(key)
    if route is None or key not in allowed:
        raise ValueError("Unknown or unverified Claude model/thinking selection")
    return route


def model_metadata(public_model_name: str, verified_routes: tuple[str, ...]) -> dict[str, Any]:
    allowed = set(verified_routes)
    versions: list[dict[str, Any]] = []
    for version in dict.fromkeys(route.version for route in CLAUDE_ROUTES):
        routes = [route for route in CLAUDE_ROUTES if route.version == version and route.key in allowed]
        if not routes:
            continue
        versions.append(
            {
                "id": version,
                "label": routes[0].version_label,
                "default_thinking_level": routes[0].level,
                "thinking_levels": [
                    {"id": route.level, "label": route.level_label, "key": route.key}
                    for route in routes
                ],
            }
        )
    return {
        "id": public_model_name,
        "object": "model",
        "created": 0,
        "owned_by": "turtle-claude-web",
        "name": "Claude",
        "turtle": {
            "family": "claude",
            "family_label": "Claude",
            "default_version": versions[0]["id"] if versions else "",
            "version_field": "turtle_claude_model",
            "thinking_field": "turtle_claude_thinking",
            "versions": versions,
        },
    }
