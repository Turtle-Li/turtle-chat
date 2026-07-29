from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from g4f.Provider.needs_auth.OpenaiAccount import OpenaiAccount
from g4f.cookies import set_cookies_dir
from g4f.requests import StreamSession


MODELS_URL = "https://chatgpt.com/backend-api/models"


def _matches_56(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower()
    return any(marker in text for marker in ("gpt-5-6", "gpt-5.6", "5.6 sol"))


def _project_model(model: dict[str, Any]) -> dict[str, Any]:
    return {
        key: model.get(key)
        for key in (
            "slug",
            "title",
            "display_name",
            "reasoning_type",
            "default_thinking_effort",
            "configurable_thinking_effort",
            "thinking_efforts",
        )
        if key in model
    }


def _project_version(version: dict[str, Any]) -> dict[str, Any]:
    return {
        key: version.get(key)
        for key in (
            "id",
            "display_text",
            "display_text_full",
            "slugs",
            "intelligence_presets",
        )
        if key in version
    }


def _project_category(category: dict[str, Any]) -> dict[str, Any]:
    return {
        key: category.get(key)
        for key in (
            "category",
            "default_model",
            "human_category_name",
            "human_category_short_name",
            "model_lane",
            "model_version",
            "supported_models",
        )
        if key in category
    }


async def inspect(auth_dir: Path) -> dict[str, Any]:
    set_cookies_dir(str(auth_dir))
    auth = OpenaiAccount.get_auth_result()
    async with StreamSession(
        cookies=auth.cookies,
        headers=auth.headers,
        impersonate="chrome",
    ) as session:
        async with session.get(
            MODELS_URL,
            params={
                "iim": "false",
                "is_gizmo": "false",
                "supports_model_picker_upgrade_presets": "true",
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"ChatGPT models endpoint returned HTTP {response.status}")
            payload = await response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("ChatGPT models endpoint returned an invalid payload")

    return {
        "default_model_slug": payload.get("default_model_slug"),
        "models": [
            _project_model(model)
            for model in payload.get("models", [])
            if isinstance(model, dict) and _matches_56(model)
        ],
        "versions": [
            _project_version(version)
            for version in payload.get("versions", [])
            if isinstance(version, dict) and _matches_56(version)
        ],
        "categories": [
            _project_category(category)
            for category in payload.get("categories", [])
            if isinstance(category, dict) and _matches_56(category)
        ],
        "slider_settings": [
            setting
            for setting in payload.get("slider_settings", [])
            if isinstance(setting, dict) and _matches_56(setting)
        ],
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Print sanitized GPT-5.6 capability metadata from the saved ChatGPT session."
    )
    parser.add_argument(
        "--auth-dir",
        type=Path,
        default=root / ".runtime" / "gpt4free-auth",
    )
    args = parser.parse_args()
    result = asyncio.run(inspect(args.auth_dir.expanduser().resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
