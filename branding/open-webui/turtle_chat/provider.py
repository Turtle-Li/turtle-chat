"""Provider-family identity for isolated GPT and Claude conversations."""

from __future__ import annotations

from typing import Any


MODEL_PROVIDERS = {
    "gpt-5-web": "gpt",
    "claude-web": "claude",
}
KNOWN_PROVIDERS = frozenset(MODEL_PROVIDERS.values())
DEFAULT_PROVIDER = "gpt"


def provider_for_model(model_id: Any) -> str | None:
    """Return the Turtle provider family for a published model identifier."""

    return MODEL_PROVIDERS.get(str(model_id or "").strip())


def _models_from_chat(chat: Any) -> list[str]:
    if not isinstance(chat, dict):
        return []
    models = chat.get("models")
    if isinstance(models, str):
        return [models]
    if isinstance(models, list):
        return [str(value) for value in models if value]
    return []


def provider_for_chat(chat: Any, meta: Any = None) -> str:
    """Resolve one immutable provider family without reading message content."""

    if isinstance(meta, dict):
        stored = str(meta.get("turtle_provider") or "").strip().lower()
        if stored in KNOWN_PROVIDERS:
            return stored

    for model_id in _models_from_chat(chat):
        provider = provider_for_model(model_id)
        if provider:
            return provider

    # All conversations predating Claude were GPT conversations. Keeping the
    # fallback deterministic also avoids scanning or exposing message bodies.
    return DEFAULT_PROVIDER


def meta_with_provider(meta: Any, chat: Any) -> dict[str, Any]:
    """Copy chat metadata and set provider identity once when it is missing."""

    result = dict(meta) if isinstance(meta, dict) else {}
    stored = str(result.get("turtle_provider") or "").strip().lower()
    if stored not in KNOWN_PROVIDERS:
        result["turtle_provider"] = provider_for_chat(chat, result)
    return result
