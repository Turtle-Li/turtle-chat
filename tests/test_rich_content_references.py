from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
G4F_ROOT = ROOT / ".runtime" / "gpt4free-src"
sys.path.insert(0, str(G4F_ROOT))
RUNTIME_IMPORT_ERROR = None
try:
    from g4f.Provider.needs_auth.OpenaiChat import (  # noqa: E402
        ContentReferences,
        _render_turtle_content_reference,
    )
except ModuleNotFoundError as exc:
    ContentReferences = None
    _render_turtle_content_reference = None
    RUNTIME_IMPORT_ERROR = exc

requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)


def decode_reference(markdown: str) -> tuple[str, dict]:
    match = re.search(
        r"\]\(/turtle/ref/v1/([a-z0-9-]+)#([A-Za-z0-9_-]+)\)",
        markdown,
    )
    assert match
    encoded = match.group(2)
    encoded += "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    return match.group(1), payload


@requires_runtime
def test_entity_reference_becomes_versioned_component_marker() -> None:
    references = ContentReferences()

    markdown = _render_turtle_content_reference(
        'entity\n["point_of_interest","Baan Kang Wat","Chiang Mai, Thailand"]',
        references,
    )

    assert markdown is not None
    kind, payload = decode_reference(markdown)
    assert kind == "entity"
    assert payload == {
        "v": 1,
        "entity_type": "point_of_interest",
        "name": "Baan Kang Wat",
        "description": "Chiang Mai, Thailand",
        "url": "",
    }
    assert '["point_of_interest"' not in markdown


@requires_runtime
def test_image_group_uses_matching_metadata_and_filters_unsafe_urls() -> None:
    references = ContentReferences()
    references.ingest_references(
        [
            {
                "type": "image_group",
                "images": [
                    {
                        "image_search_query": "wrong query",
                        "image_result": {
                            "title": "Wrong",
                            "content_url": "https://images.example.test/wrong.jpg",
                        },
                    }
                ],
            },
            {
                "type": "image_group",
                "layout": "bento",
                "images": [
                    {
                        "image_search_query": "Baan Kang Wat Chiang Mai",
                        "image_result": {
                            "title": "Baan Kang Wat",
                            "content_url": "https://images.example.test/baan.jpg",
                            "source_url": "https://example.test/baan",
                            "source_name": "Example",
                        },
                    },
                    {
                        "image_search_query": "Baan Kang Wat Chiang Mai",
                        "image_result": {
                            "title": "Unsafe image",
                            "content_url": "http://insecure.example.test/image.jpg",
                        },
                    },
                ],
            },
        ]
    )

    markdown = _render_turtle_content_reference(
        (
            'image_group\n{"aspect_ratio":"16:9","query":'
            '["Baan Kang Wat Chiang Mai"],"num_per_query":2}'
        ),
        references,
    )

    assert markdown is not None
    kind, payload = decode_reference(markdown)
    assert kind == "image-group"
    assert payload["queries"] == ["Baan Kang Wat Chiang Mai"]
    assert payload["layout"] == "bento"
    assert payload["aspect_ratio"] == "16:9"
    assert payload["images"] == [
        {
            "image_url": "https://images.example.test/baan.jpg",
            "title": "Baan Kang Wat",
            "source_url": "https://example.test/baan",
            "source_name": "Example",
            "query": "Baan Kang Wat Chiang Mai",
        }
    ]
    assert '"num_per_query"' not in markdown


@requires_runtime
def test_url_and_product_references_have_useful_safe_fallbacks() -> None:
    references = ContentReferences()

    url_markdown = _render_turtle_content_reference(
        'url\n{"url":"https://example.test/place","title":"Place"}',
        references,
    )
    products_markdown = _render_turtle_content_reference(
        (
            'products\n{"selections":[["sku-1","Local tea"]],'
            '"tags":[["gift","local"]]}'
        ),
        references,
    )
    product_markdown = _render_turtle_content_reference(
        'product_entity\n["sku-2","Tea set"]',
        references,
    )

    assert url_markdown == "[Place](https://example.test/place)"
    assert products_markdown is not None
    assert decode_reference(products_markdown) == (
        "products",
        {
            "v": 1,
            "items": [
                {
                    "name": "Local tea",
                    "tag": "gift · local",
                    "url": "",
                }
            ],
        },
    )
    assert product_markdown is not None
    kind, payload = decode_reference(product_markdown)
    assert kind == "product"
    assert payload["name"] == "Tea set"


@requires_runtime
def test_unknown_reference_never_leaks_internal_payload() -> None:
    references = ContentReferences()
    references.ingest_references(
        [{"type": "future_widget", "alt": "可读的上游替代文本"}]
    )

    rendered = _render_turtle_content_reference(
        'future_widget\n{"private_internal_state":"must-not-leak"}',
        references,
    )

    assert rendered == "可读的上游替代文本"
    assert "must-not-leak" not in rendered
    assert (
        _render_turtle_content_reference("cite\nturn0search0", references)
        is None
    )


@requires_runtime
def test_current_reference_families_have_generic_safe_adapters() -> None:
    references = ContentReferences()
    references.ingest_references(
        [
            {
                "type": "grouped_webpages",
                "items": [
                    {
                        "title": "Visitor guide",
                        "url": "https://example.test/guide",
                        "snippet": "Opening hours",
                    }
                ],
            },
            {
                "type": "map",
                "places": [
                    {
                        "name": "Baan Kang Wat",
                        "address": "Chiang Mai",
                        "url": "https://example.test/place",
                    }
                ],
            },
            {
                "type": "image_v2",
                "image": {
                    "content_url": "https://images.example.test/place.jpg",
                    "title": "Baan Kang Wat",
                },
            },
        ]
    )

    webpage_kind, webpage_payload = decode_reference(
        _render_turtle_content_reference("grouped_webpages\n0", references)
    )
    map_kind, map_payload = decode_reference(
        _render_turtle_content_reference("map\n1", references)
    )
    image_kind, image_payload = decode_reference(
        _render_turtle_content_reference("image_v2\n2", references)
    )

    assert webpage_kind == "reference-list"
    assert webpage_payload["items"][0]["name"] == "Visitor guide"
    assert map_kind == "reference-list"
    assert map_payload["items"][0]["subtitle"] == "Chiang Mai"
    assert image_kind == "image-group"
    assert image_payload["images"][0]["image_url"].endswith("/place.jpg")


@requires_runtime
def test_content_reference_delta_operations_keep_complete_metadata() -> None:
    references = ContentReferences()
    references.ingest_references([{"type": "image_group"}], "replace")
    references.update_reference(0, "replace", "alt", "Nearby art village")
    references.update_reference(
        0,
        "replace",
        "images",
        [{"image_search_query": "art village"}],
    )
    references.update_reference(
        0,
        "replace",
        "refs",
        {"ref_index": 1, "ref_type": "image"},
        1,
    )

    assert references.list[0]["alt"] == "Nearby art village"
    assert references.list[0]["images"][0]["image_search_query"] == "art village"
    assert references.list[0]["refs"] == [
        {},
        {"ref_index": 1, "ref_type": "image"},
    ]

    references.update_reference(0, "remove", "alt", None)
    assert "alt" not in references.list[0]


def test_open_webui_assets_include_all_rich_reference_renderers() -> None:
    script = (ROOT / "branding" / "open-webui" / "storage-controls.js").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "branding" / "open-webui" / "custom.css").read_text(
        encoding="utf-8"
    )

    assert 'const RICH_REFERENCE_PREFIX = "/turtle/ref/v1/";' in script
    assert "createImageGroupReference" in script
    assert "createEntityReference" in script
    assert "createProductsReference" in script
    assert "createReferenceList" in script
    assert "decorateRichContentReferences" in script
    assert "safeRichReferenceUrl" in script
    assert ".turtle-image-reference-group" in styles
    assert ".turtle-entity-reference" in styles
    assert ".turtle-products-reference" in styles
    assert ".turtle-reference-list" in styles


def test_pinned_gpt4free_overlay_contains_rich_reference_protocol() -> None:
    overlay = (
        ROOT / "patches" / "gpt4free-openaiaccount-gpt56.patch"
    ).read_text(encoding="utf-8")

    assert "+def _render_turtle_content_reference(" in overlay
    assert "+def _render_image_group_reference(" in overlay
    assert "+                                            turtle_reference = _render_turtle_content_reference(" in overlay
    assert "+                        references.ingest_references(" in overlay
    assert "+                references.ingest_references(" in overlay
    assert "Unsupported rich content reference" in overlay
