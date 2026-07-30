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

    @model_validator(mode="after")
    def validate_latest_user_image_count(self):
        latest_user = next(
            (message for message in reversed(self.messages) if message.role == "user"),
            None,
        )
        if latest_user is None or not isinstance(latest_user.content, list):
            return self
        image_count = sum(
            1
            for part in latest_user.content
            if isinstance(part, dict) and part.get("type") == "image_url"
        )
        if image_count > 20:
            raise ValueError("单条消息最多可附加 20 张图片")
        return self


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gpt-image", min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=32_000)
    n: int = Field(default=1, ge=1, le=4)
    response_format: Literal["url"] = "url"
    turtle_account_pool_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
    )
    turtle_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    turtle_chat_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    turtle_request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
    )
    turtle_required_quota_profiles: list[str] = Field(
        default_factory=list,
        max_length=4,
    )

    @field_validator("turtle_required_quota_profiles")
    @classmethod
    def validate_quota_profiles(cls, value: list[str]) -> list[str]:
        allowed = {"free", "go", "plus", "pro-5x", "pro-20x"}
        normalized: list[str] = []
        for item in value:
            profile = str(item or "").strip().lower()
            if profile not in allowed:
                raise ValueError("图片账号套餐无效")
            if profile not in normalized:
                normalized.append(profile)
        return normalized


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
