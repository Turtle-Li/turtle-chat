from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"


def test_generated_image_thumbnail_work_is_bounded_and_has_a_terminal_state() -> None:
    script = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")

    assert "const MANAGED_THUMBNAIL_TIMEOUT_MS = 12_000;" in script
    assert 'controller.abort("managed-thumbnail-timeout")' in script
    assert "signal: controller.signal" in script
    assert ".catch(() => markManagedThumbnailFailure(fileId))" in script
    assert 'gallery.dataset.imageState = "error";' in script
    assert "缩略图暂不可用，点按可打开原图" in script


def test_generated_gallery_preview_uses_the_managed_original_not_native_thumbnail() -> None:
    script = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")
    preview_branch = script[
        script.index('if (event.target.closest("[data-gallery-preview]"))')
        : script.index('const editButton = event.target.closest("[data-gallery-edit]")')
    ]

    assert "openFilePreview(" in preview_branch
    assert "entry.fileId" in preview_branch
    assert "entry.trigger" not in preview_branch
    assert ".trigger.click()" not in preview_branch
    assert "const MANAGED_PREVIEW_TIMEOUT_MS = 15_000;" in script
    assert 'controller.abort("managed-preview-timeout")' in script
    assert 'data-preview-retry' in script


def test_generated_gallery_hides_broken_images_and_stop_button_has_immediate_css() -> None:
    stylesheet = (BRANDING / "custom.css").read_text(encoding="utf-8")

    assert '.turtle-generated-gallery[data-image-state="loading"]' in stylesheet
    assert '.turtle-generated-gallery[data-image-state="error"]' in stylesheet
    assert ".turtle-generated-gallery-status" in stylesheet
    assert 'button:has(> svg path[d^="M2.25 12c0-5.385"])' in stylesheet


def test_storage_asset_version_changes_with_the_loading_fix() -> None:
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")

    assert "turtle-storage-controls.js?v=20260730.4" in patcher
