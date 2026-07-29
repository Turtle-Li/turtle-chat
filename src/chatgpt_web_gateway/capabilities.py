from __future__ import annotations

from typing import Any


class SelectionError(ValueError):
    pass


GPT_PUBLIC_MODEL = "gpt-5-web"
GPT_DEFAULT_VERSION = "gpt-5-5"


_GPT_ROUTES: dict[str, dict[str, tuple[str, str | None]]] = {
    # Match the current consumer ChatGPT picker: GPT-5.6 Sol reasoning,
    # GPT-5.5 Instant, GPT-5.3 and o3. The private model slugs are explicitly
    # allowlisted and must pass real route acceptance before this list changes.
    # The private model slugs were verified through the authenticated web
    # provider and are deliberately allowlisted here.
    "latest": {
        # gpt4free's OpenAI-compatible schema accepts these transport values;
        # the reviewed overlay converts them to ChatGPT's private
        # standard/extended/max values immediately before the web request.
        "medium": ("gpt-5-6-thinking", "medium"),
        "high": ("gpt-5-6-thinking", "high"),
        "xhigh": ("gpt-5-6-thinking", "x-high"),
        "pro": ("gpt-5-6-pro", None),
    },
    "gpt-5-5": {
        "instant": ("gpt-5-5-instant", None),
    },
    "gpt-5-3": {
        "standard": ("gpt-5-3", None),
    },
    "o3": {
        "standard": ("o3", None),
    },
}


_VERSION_LABELS = {
    "latest": "GPT-5.6 Sol",
    "gpt-5-5": "GPT-5.5",
    "gpt-5-3": "GPT-5.3",
    "o3": "o3",
}


_THINKING_LABELS = {
    "instant": "极速",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "pro": "Pro",
    "standard": "标准",
}


_VERSION_DEFAULT_LEVELS = {
    "latest": "medium",
    "gpt-5-5": "instant",
    "gpt-5-3": "standard",
    "o3": "standard",
}


def model_metadata(public_model_name: str) -> dict[str, Any]:
    model: dict[str, Any] = {
        "id": public_model_name,
        "object": "model",
        "created": 0,
        "owned_by": "turtle-gpt",
    }
    if public_model_name != GPT_PUBLIC_MODEL:
        model["name"] = public_model_name
        return model

    versions = []
    for version_id, levels in _GPT_ROUTES.items():
        versions.append(
            {
                "id": version_id,
                "label": _VERSION_LABELS[version_id],
                "default_thinking_level": _VERSION_DEFAULT_LEVELS.get(version_id, "auto"),
                "thinking_levels": [
                    {
                        "id": level_id,
                        "label": _THINKING_LABELS[level_id],
                    }
                    for level_id in levels
                ],
            }
        )

    model.update(
        {
            "name": "GPT",
            "turtle": {
                "family": "gpt",
                "family_label": "GPT",
                "default_version": GPT_DEFAULT_VERSION,
                "version_field": "turtle_model_version",
                "thinking_field": "turtle_thinking_level",
                "picker": {
                    "style": "chatgpt",
                    "section_label": "智能",
                    "mode_order": [
                        {
                            "selection_key": "gpt-5-5:instant",
                            "label": "极速",
                            "badge": "5.5",
                        },
                        {"selection_key": "latest:medium", "label": "中"},
                        {"selection_key": "latest:high", "label": "高"},
                        {"selection_key": "latest:xhigh", "label": "极高"},
                        {"selection_key": "latest:pro", "label": "Pro"},
                    ],
                    "model_order": ["latest", "gpt-5-5", "gpt-5-3", "o3"],
                },
                "versions": versions,
            },
        }
    )
    return model


def resolve_selection(
    *,
    public_model_name: str,
    default_upstream_model: str,
    version: str | None,
    thinking_level: str | None,
) -> tuple[str, str | None]:
    if version is None and thinking_level is None:
        return default_upstream_model, None
    if public_model_name != GPT_PUBLIC_MODEL:
        raise SelectionError("runtime model controls are not available for this model family")

    selected_version = version or GPT_DEFAULT_VERSION
    levels = _GPT_ROUTES.get(selected_version)
    if levels is None:
        raise SelectionError(f"unsupported GPT version: {selected_version}")

    selected_level = thinking_level or _VERSION_DEFAULT_LEVELS.get(selected_version, "auto")
    route = levels.get(selected_level)
    if route is None:
        raise SelectionError(
            f"thinking level {selected_level!r} is not supported by {_VERSION_LABELS[selected_version]}"
        )
    return route


def resolve_selection_key(
    *,
    public_model_name: str,
    version: str | None,
    thinking_level: str | None,
) -> str:
    """Return the stable local quota lane for an allowlisted GPT route."""

    if public_model_name != GPT_PUBLIC_MODEL:
        raise SelectionError("runtime model controls are not available for this model family")
    selected_version = version or GPT_DEFAULT_VERSION
    levels = _GPT_ROUTES.get(selected_version)
    if levels is None:
        raise SelectionError(f"unsupported GPT version: {selected_version}")
    selected_level = thinking_level or _VERSION_DEFAULT_LEVELS.get(selected_version, "auto")
    if selected_level not in levels:
        raise SelectionError(
            f"thinking level {selected_level!r} is not supported by {_VERSION_LABELS[selected_version]}"
        )
    return f"{selected_version}:{selected_level}"
