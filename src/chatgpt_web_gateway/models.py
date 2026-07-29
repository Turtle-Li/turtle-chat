from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | list[dict[str, Any]] | None):
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
    turtle_model_version: str | None = None
    turtle_thinking_level: str | None = None
    turtle_claude_model: str | None = None
    turtle_claude_thinking: str | None = None
    # These routing hints are added by the authenticated Turtle backend. They
    # are consumed by the Gateway and must never be forwarded to ChatGPT.
    turtle_account_pool_id: str | None = Field(default=None, min_length=1, max_length=80)
    turtle_user_id: str | None = Field(default=None, min_length=1, max_length=128)
    turtle_chat_id: str | None = Field(default=None, min_length=1, max_length=128)
    turtle_request_id: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def discard_empty_historical_assistant_messages(cls, value: Any):
        if not isinstance(value, dict):
            return value
        messages = value.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            return value

        filtered = []
        last_index = len(messages) - 1
        for index, message in enumerate(messages):
            if (
                index < last_index
                and isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("content") in ("", [])
                and not message.get("tool_calls")
                and not message.get("function_call")
            ):
                continue
            filtered.append(message)
        if len(filtered) == len(messages):
            return value
        return {**value, "messages": filtered}


class AccountPoolForm(BaseModel):
    provider: Literal["gpt", "claude"] = "gpt"
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    enabled: bool = True


class AccountForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    worker_endpoint: str = Field(min_length=8, max_length=240)
    health_path: str | None = Field(
        default="/api/OpenaiAccount/quota",
        max_length=160,
    )
    max_concurrency: int = Field(default=1, ge=1, le=20)
    priority: int = Field(default=0, ge=-100, le=100)
    quota_profile: str = Field(default="untracked", min_length=1, max_length=24)
    enabled: bool = False


class AccountOnboardForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class AccountSettingsForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = False
    quota_profile: str | None = Field(default=None, min_length=1, max_length=24)
    max_concurrency: int | None = Field(default=None, ge=1, le=20)


class ProjectApiKeyForm(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner_user_id: str = Field(min_length=1, max_length=128)


class ProjectApiPermissionForm(BaseModel):
    enabled: bool
    updated_by: str = Field(min_length=1, max_length=128)
    max_keys: int | None = Field(default=None, ge=1, le=100)


class ProjectApiPricingConfigForm(BaseModel):
    cost_multiplier: float = Field(ge=0, le=100)
    updated_by: str = Field(min_length=1, max_length=128)


class ProjectApiCreditGrantForm(BaseModel):
    amount_microusd: int = Field(ge=1, le=1_000_000_000_000)
    reason: str = Field(min_length=2, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)
    updated_by: str = Field(min_length=1, max_length=128)
