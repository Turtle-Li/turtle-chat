from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} was not found in {path}")


def test_history_page_uses_indexed_depth_range_without_offset_or_row_limit():
    history_path = BRANDING / "turtle_chat" / "history.py"
    range_source = _function_source(history_path, "_range_messages")
    assert "TurtleChatHistoryMessage.depth >= range_start" in range_source
    assert "TurtleChatHistoryMessage.depth < range_end" in range_source
    assert ".offset(" not in range_source
    assert ".limit(" not in range_source

    source = history_path.read_text(encoding="utf-8")
    assert '"turtle_chat_history_range_idx"' in source
    assert '"chat_id",\n            "depth",\n            "message_id"' in source


def test_exact_chat_reads_are_rerouted_to_the_paged_history_api():
    cache = (BRANDING / "client-read-cache.js").read_text(encoding="utf-8")
    assert "/api/v1/turtle/chat/history/${encodeURIComponent(resource.chatId)}/initial" in cache
    assert "/range?before_depth=${encodeURIComponent(beforeDepth)}" in cache
    assert "window.__turtleHistoryPager" in cache
    assert 'headers.set("X-Turtle-History-Response", "paged")' in cache

    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "const loadMoreMessages = async () =>" in patcher
    assert "window.__turtleHistoryPager.loadOlder(v(),c())" in patcher
    assert "c().turtlePage?.hasMore&&" not in patcher
    assert "while(K&&L<=b())" in patcher
    assert "return L<=b()" in patcher
    assert "TURTLE_ASSET_VERSION" in patcher
    assert "version_patched_module_chain" in patcher
    assert "initial_targets={messages_module.resolve(), image_module.resolve()}" in patcher
    assert "sync_chat_history_index" in patcher

    dockerfile = (BRANDING / "Dockerfile").read_text(encoding="utf-8")
    assert "python -m compileall -q /app/backend/open_webui" in dockerfile


def test_asset_versioning_changes_only_patched_module_importer_chain(tmp_path):
    module_path = BRANDING / "turtle_asset_versioning.py"
    spec = importlib.util.spec_from_file_location("turtle_asset_versioning", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    immutable = tmp_path / "_app" / "immutable"
    chunks = immutable / "chunks"
    entry = immutable / "entry"
    chunks.mkdir(parents=True)
    entry.mkdir()
    leaf = chunks / "leaf.js"
    parent = chunks / "parent.js"
    shared = chunks / "shared.js"
    start = entry / "start.js"
    index = tmp_path / "index.html"

    leaf.write_text("export const leaf = true;\n", encoding="utf-8")
    parent.write_text(
        'import { leaf } from "./leaf.js"; export { leaf };\n',
        encoding="utf-8",
    )
    shared.write_text("export const shared = true;\n", encoding="utf-8")
    start.write_text(
        'import "../chunks/parent.js"; import "../chunks/shared.js";\n',
        encoding="utf-8",
    )
    index.write_text(
        '<script type="module" src="/_app/immutable/entry/start.js"></script>'
        '<link rel="modulepreload" href="/_app/immutable/chunks/shared.js">',
        encoding="utf-8",
    )

    versioned = module.version_patched_module_chain(
        immutable_root=immutable,
        index_path=index,
        initial_targets={leaf},
        version="0123456789abcdef",
    )

    assert versioned == {leaf.resolve(), parent.resolve(), start.resolve()}
    assert '"./leaf.js?v=0123456789abcdef"' in parent.read_text(encoding="utf-8")
    assert '"../chunks/parent.js?v=0123456789abcdef"' in start.read_text(
        encoding="utf-8"
    )
    assert '"../chunks/shared.js"' in start.read_text(encoding="utf-8")
    index_source = index.read_text(encoding="utf-8")
    assert "/entry/start.js?v=0123456789abcdef" in index_source
    assert "/chunks/shared.js?v=" not in index_source


def test_main_files_and_static_thumbnails_have_distinct_cdn_prefixes():
    core = (BRANDING / "turtle_storage" / "core.py").read_text(encoding="utf-8")
    provider = (BRANDING / "turtle_storage" / "provider.py").read_text(encoding="utf-8")
    assert 'MAIN_OBJECT_NAMESPACE = "files"' in core
    assert 'THUMBNAIL_OBJECT_NAMESPACE = "thumbnails"' in core
    assert "parts[-4] = THUMBNAIL_OBJECT_NAMESPACE" in provider
    assert "return f\"s3://{bucket}/{key}{cls.THUMBNAIL_SUFFIX}\"" in provider
    assert '"files_base_url": "https://files.chat.totools.cn"' in core
    assert '"images_base_url": "https://img.chat.totools.cn"' in core
    assert "files_auth_key_ciphertext" in core
    assert "images_auth_key_ciphertext" in core
    assert "def _presign_cdn_download" in provider
    assert "hashlib.md5(" in provider
    assert 'f"{uri}-{timestamp}-{random_value}-{uid}-{auth_key}"' in provider


def test_model_input_is_cdn_first_with_a_signed_cos_fallback():
    media = (BRANDING / "turtle_storage" / "media.py").read_text(encoding="utf-8")
    pump = (BRANDING / "turtle_storage" / "pump.py").read_text(encoding="utf-8")
    assert "use_cdn=True" in media
    assert "use_cdn=False" in media
    assert "seal_model_source(" in media
    assert 'MODEL_SOURCE_CONTEXT = b"turtle-model-source-v1\\0"' in pump


def test_chat_inline_images_use_static_thumbnail_and_original_preview_is_cached():
    router = (BRANDING / "turtle_storage" / "router.py").read_text(encoding="utf-8")
    controls = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")

    assert '@router.get("/files/{file_id}/thumbnail")' in router
    assert "variant=\"thumbnail\"" in router
    assert '"Cache-Control": f"private, max-age={ttl}"' in router
    assert "MANAGED_IMAGE_SELECTOR" in controls
    assert "/api/v1/turtle/storage/files/" in controls
    assert '"/api/v1/turtle/storage/files/$1/thumbnail"' in patcher
    assert "'Cache-Control': 'private, max-age=300'" in patcher
