from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_MEDIA = (
    ROOT / "branding" / "open-webui" / "turtle_project_api" / "media.py"
)
PROJECT_ROUTER = (
    ROOT / "branding" / "open-webui" / "turtle_project_api" / "router.py"
)


def _load_media_module():
    spec = importlib.util.spec_from_file_location("turtle_project_media_test", PROJECT_MEDIA)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEDIA = _load_media_module()
FILE_UUID = "11111111-2222-4333-8444-555555555555"
PUBLIC_FILE_ID = f"file-{FILE_UUID}"


def test_project_file_ids_and_openai_shaped_objects() -> None:
    assert MEDIA.public_file_id(FILE_UUID) == PUBLIC_FILE_ID
    assert MEDIA.public_file_id(PUBLIC_FILE_ID) == PUBLIC_FILE_ID
    assert MEDIA.internal_file_id(PUBLIC_FILE_ID) == FILE_UUID
    assert MEDIA.internal_file_id(FILE_UUID) is None
    assert MEDIA.internal_file_id("file-not-a-uuid") is None

    file = SimpleNamespace(
        id=FILE_UUID,
        filename="fallback.webp",
        created_at=1234,
        data={"status": "completed"},
        meta={
            "name": "photo.webp",
            "size": 2048,
            "data": {
                "project_api_key_id": "key-a",
                "project_api_purpose": "vision",
            },
        },
    )
    assert MEDIA.project_file_object(file) == {
        "id": PUBLIC_FILE_ID,
        "object": "file",
        "bytes": 2048,
        "created_at": 1234,
        "filename": "photo.webp",
        "purpose": "vision",
        "status": "completed",
    }
    assert MEDIA.project_file_scope(file.meta) == ("key-a", "vision")


def test_chat_accepts_official_input_image_and_file_id_image_url() -> None:
    calls: list[str] = []

    async def resolve(file_id: str) -> dict[str, str]:
        calls.append(file_id)
        return {
            "url": "https://files.chat.totools.cn/managed.webp?sign=test",
            "turtle_source": "sealed-source-token",
        }

    payload = {
        "model": "gpt-5-web",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "input_image",
                        "file_id": PUBLIC_FILE_ID,
                        "detail": "high",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": PUBLIC_FILE_ID, "detail": "low"},
                    },
                ],
            }
        ],
    }
    rewritten, count = asyncio.run(MEDIA.rewrite_image_inputs(payload, resolve))
    assert count == 2
    assert calls == [FILE_UUID]
    assert payload["messages"][0]["content"][1]["type"] == "input_image"
    images = rewritten["messages"][0]["content"][1:]
    assert [item["type"] for item in images] == ["image_url", "image_url"]
    assert images[0]["image_url"]["detail"] == "high"
    assert images[1]["image_url"]["detail"] == "low"
    assert all(
        item["image_url"]["url"].startswith("https://files.chat.totools.cn/")
        and item["image_url"]["turtle_source"] == "sealed-source-token"
        for item in images
    )


@pytest.mark.parametrize(
    "part",
    [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/unmanaged.png"},
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
        },
        {
            "type": "image_url",
            "image_url": {
                "url": PUBLIC_FILE_ID,
                "turtle_source": "client-supplied",
            },
        },
        {"type": "input_image", "file_id": "not-a-file"},
    ],
)
def test_chat_rejects_unmanaged_or_client_sealed_images(part: dict) -> None:
    payload = {
        "messages": [{"role": "user", "content": [part]}],
    }

    async def resolve(_file_id: str) -> dict[str, str]:
        raise AssertionError("unsafe input must fail before resolution")

    with pytest.raises(MEDIA.ProjectMediaReferenceError):
        asyncio.run(MEDIA.rewrite_image_inputs(payload, resolve))


def test_project_media_transport_never_accepts_or_returns_media_bodies() -> None:
    source = PROJECT_ROUTER.read_text(encoding="utf-8")
    create = source[
        source.index('@proxy_router.post("/files")')
        : source.index('@proxy_router.post("/files/{file_id}/complete")')
    ]
    content = source[
        source.index('@proxy_router.get("/files/{file_id}/content")')
        : source.index('@proxy_router.delete("/files/{file_id}")')
    ]

    assert 'content_type.startswith("application/json")' in create
    assert '"direct_upload_required"' in create
    assert "storage_presign_upload(" in create
    assert '"upload": {' in create
    assert "UploadFile" not in source
    assert "await request.form()" not in source
    assert "RedirectResponse(" in content
    assert "Storage.presign_download(" in content
    assert "use_cdn=True" in content
    assert "StreamingResponse" not in content


def test_project_media_routes_precede_the_openai_style_catch_all() -> None:
    source = PROJECT_ROUTER.read_text(encoding="utf-8")
    catch_all = source.index('"/{path:path}"')

    for route in (
        '@proxy_router.post("/files")',
        '@proxy_router.post("/files/{file_id}/complete")',
        '@proxy_router.get("/files")',
        '@proxy_router.get("/files/{file_id}")',
        '@proxy_router.get("/files/{file_id}/content")',
        '@proxy_router.delete("/files/{file_id}")',
    ):
        assert source.index(route) < catch_all


def test_project_files_are_key_scoped_and_chat_sources_are_server_sealed() -> None:
    source = PROJECT_ROUTER.read_text(encoding="utf-8")

    assert 'File.meta["data"]["project_api_key_id"].as_string() == actor.key_id' in source
    assert '"project_api_key_id": actor.key_id' in source
    assert "get_presigned_model_image_source_for_file(" in source
    assert "rewrite_image_inputs(payload, resolve)" in source
    assert "客户端不能提交 turtle_source" in PROJECT_MEDIA.read_text(encoding="utf-8")
