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
        "? runImagePreparation(() =>\n"
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


def test_composer_media_is_prepared_before_send_and_uploaded_only_after_release() -> None:
    source = CONTROLS.read_text(encoding="utf-8")
    staged = source[
        source.index("const stageDeferredComposerUpload =")
        : source.index("window.fetch = async")
    ]

    assert "task.prepared = await preparation;" in staged
    assert 'task.state = "prepared";' in staged
    assert "await gate;" in staged
    assert 'task.state = "uploading";' in staged
    assert staged.index("task.prepared = await preparation;") < staged.index("await gate;")
    assert staged.index("await gate;") < staged.index("await directUpload(")
    assert "return await stageDeferredComposerUpload({" in source


def test_deferred_media_flush_covers_button_enter_cancel_and_duplicate_send() -> None:
    source = CONTROLS.read_text(encoding="utf-8")
    styles = (CONTROLS.parent / "custom.css").read_text(encoding="utf-8")

    assert "const flushDeferredComposerUploads = async () => {" in source
    assert "if (deferredUploadFlush) {" in source
    assert "return false;" in source
    assert 'document.addEventListener("submit", handleDeferredUploadSubmit, true);' in source
    assert 'document.addEventListener("keydown", handleDeferredUploadKeydown, true);' in source
    assert 'document.addEventListener("click", handleDeferredUploadRemoval, true);' in source
    assert "task.abortController.abort();" in source
    assert "cancelledUploadResponse()" in source
    assert "待发送" not in source
    assert "压缩中" not in source
    assert "正在上传 ${imageCount} 张图片…" in source
    assert "正在上传 ${active.length || 1} 个附件…" in source
    assert "turtle-deferred-upload-status" in source
    assert "setDeferredUploadBusy(true, tasks);" in source
    assert 'status.setAttribute("role", "status");' in source
    assert "#send-message-button[data-turtle-deferred-upload-busy=\"true\"]::after" in styles
    assert "top: 50%;" in styles
    assert "left: 50%;" in styles
    assert "translate(-50%, -50%) rotate(360deg)" in styles


def test_direct_cos_upload_retries_only_transient_failures_with_a_small_bound() -> None:
    source = CONTROLS.read_text(encoding="utf-8")

    assert "const DIRECT_UPLOAD_RETRY_DELAYS_MS = [0, 350, 1_000];" in source
    assert "const retryableUploadStatus = (status) =>" in source
    assert "[408, 425, 429]" in source
    assert "Number(status) >= 500" in source
    assert source.count("uploadBodyWithRetry(") == 2
