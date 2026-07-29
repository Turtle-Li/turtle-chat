from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ROOT / "branding" / "open-webui" / "storage-controls.js"


def test_multi_image_client_pipeline_bounds_cpu_and_body_transfers() -> None:
    source = CONTROLS.read_text(encoding="utf-8")

    assert "const IMAGE_PREPARE_CONCURRENCY = 2;" in source
    assert "const DIRECT_UPLOAD_TRANSFER_CONCURRENCY = 3;" in source
    assert "const createTaskLimiter = (concurrency) => {" in source
    assert (
        "const runImagePreparation = createTaskLimiter("
        "IMAGE_PREPARE_CONCURRENCY);"
    ) in source
    assert (
        "const runDirectUploadTransfer = createTaskLimiter("
        "DIRECT_UPLOAD_TRANSFER_CONCURRENCY);"
    ) in source
    assert (
        "await runImagePreparation(() =>\n"
        "            prepareImageAssets(originalFile, storageCapability.media),"
    ) in source
    assert source.count("runDirectUploadTransfer(() =>") == 2


def test_multi_image_client_pipeline_keeps_control_plane_outside_body_limiter() -> None:
    source = CONTROLS.read_text(encoding="utf-8")
    direct_upload = source[
        source.index("const directUpload = async")
        : source.index("window.fetch = async")
    ]

    assert 'apiFetch("/uploads/presign"' in direct_upload
    assert 'apiFetch("/uploads/complete"' in direct_upload
    assert "Promise.allSettled(uploads)" in direct_upload
    assert direct_upload.index('apiFetch("/uploads/presign"') < direct_upload.index(
        "runDirectUploadTransfer(() =>"
    )
