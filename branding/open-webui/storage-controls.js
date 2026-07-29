(() => {
  "use strict";

  const API_ROOT = "/api/v1/turtle/storage";
  const CHAT_API_ROOT = "/api/v1/turtle/chat";
  const PROJECT_API_ROOT = "/api/v1/turtle/project-api";
  const RICH_REFERENCE_PREFIX = "/turtle/ref/v1/";
  const UPLOAD_PATHS = new Set(["/api/v1/files", "/api/v1/files/"]);
  const MANAGED_IMAGE_SELECTOR =
    'img[src*="/api/v1/files/"], img[src*="/api/v1/turtle/storage/files/"][src*="/thumbnail"]';
  const DEFAULT_MEDIA = {
    max_image_dimension: 2048,
    image_quality: 0.82,
    image_format: "image/webp",
  };
  const STATIC_THUMBNAIL = {
    max_dimension: 480,
    quality: 0.72,
    content_type: "image/webp",
    max_bytes: 2 * 1024 * 1024,
  };
  const previewObjectUrls = new Set();
  const mediaUrlCache = new Map();
  let capturedAuthorization = "";
  let capabilityCache = null;
  let capabilityCachedAt = 0;
  let capabilityRequest = null;
  let lastFocusedElement = null;
  let launcherResizeObserver = null;
  let launcherObservedSidebar = null;
  let launcherRefreshTimer = null;
  let launcherRetryCount = 0;
  let projectAccess = null;
  let projectAccessRequest = null;
  let projectPanelState = {
    tab: "overview",
    bundle: null,
    usage: null,
    hours: 24,
    keyId: "",
    outcome: "",
    offset: 0,
    limit: 50,
    newSecret: "",
  };
  let mountQueued = false;
  const SPACE_BATCH_SIZE = 24;
  const SPACE_CACHE_REVALIDATE_MS = 5_000;
  const spacePageCache = new Map();
  const spacePageRequests = new Map();
  let spaceCacheToken = null;
  let spaceCacheGeneration = 0;
  let spacePrefetchTimer = null;
  let spaceSession = null;
  let spaceSessionGeneration = 0;
  let thumbnailObserver = null;
  let sentinelObserver = null;
  let thumbnailQueue = [];
  let thumbnailActive = 0;
  const spaceAbortControllers = new Set();
  const THUMBNAIL_CONCURRENCY = 4;
  const MEDIA_URL_EXPIRY_SKEW_MS = 30_000;
  const managedThumbnailAttempts = new Map();
  const managedThumbnailQueue = [];
  let managedThumbnailActive = false;
  let managedThumbnailScanQueued = false;

  const originalFetch = window.fetch.bind(window);

  const pathOf = (input) => {
    try {
      return new URL(typeof input === "string" ? input : input?.url, window.location.origin).pathname;
    } catch (_error) {
      return "";
    }
  };

  const headersFrom = (input, init) => {
    const headers = new Headers(
      init?.headers || (typeof Request !== "undefined" && input instanceof Request ? input.headers : undefined),
    );
    const auth = headers.get("Authorization");
    if (auth && auth !== capturedAuthorization) {
      capturedAuthorization = auth;
      if (projectAccess === false) projectAccess = null;
    }
    return headers;
  };

  const storedToken = () => {
    let value = localStorage.getItem("token") || "";
    if (!value) return "";
    try {
      const parsed = JSON.parse(value);
      if (typeof parsed === "string") value = parsed;
    } catch (_error) {
      // Open WebUI normally stores the raw token, not JSON.
    }
    return value;
  };

  const authHeaders = (extra = {}) => {
    const headers = new Headers(extra);
    const token = storedToken();
    const storedAuthorization = token ? `Bearer ${token}` : "";
    if (storedAuthorization && storedAuthorization !== capturedAuthorization) {
      capturedAuthorization = storedAuthorization;
      if (projectAccess === false) projectAccess = null;
    }
    if (capturedAuthorization) headers.set("Authorization", capturedAuthorization);
    return headers;
  };

  const errorMessage = async (response, fallback = "操作失败") => {
    try {
      const payload = await response.clone().json();
      const detail = payload?.detail;
      if (typeof detail === "string") return detail;
      if (detail?.message) return detail.message;
      if (payload?.message) return payload.message;
    } catch (_error) {
      // Fall through to a concise status message.
    }
    return `${fallback}（HTTP ${response.status}）`;
  };

  const apiFetch = async (path, init = {}) => {
    const headers = authHeaders(init.headers);
    const response = await originalFetch(`${API_ROOT}${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });
    if (response.status === 401 || response.status === 403) throw new Error("请先登录或确认管理员权限");
    return response;
  };

  const chatApiFetch = async (path, init = {}) => {
    const headers = authHeaders(init.headers);
    const response = await originalFetch(`${CHAT_API_ROOT}${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });
    if (response.status === 401 || response.status === 403) throw new Error("请先登录或确认管理员权限");
    return response;
  };

  const projectApiAccess = async () => {
    if (projectAccess != null) return projectAccess;
    if (projectAccessRequest) return projectAccessRequest;
    if (!storedToken() && !capturedAuthorization) {
      projectAccess = false;
      return false;
    }
    projectAccessRequest = (async () => {
      const response = await originalFetch(`${PROJECT_API_ROOT}/me?hours=1`, {
        headers: authHeaders(),
        credentials: "same-origin",
      });
      if (!response.ok) {
        projectAccess = false;
        return false;
      }
      const payload = await response.json();
      projectAccess = Boolean(payload?.enabled);
      return projectAccess;
    })().finally(() => {
      projectAccessRequest = null;
    });
    return projectAccessRequest;
  };

  const projectApiFetch = async (path, init = {}) => {
    const response = await originalFetch(`${PROJECT_API_ROOT}${path}`, {
      ...init,
      headers: authHeaders(init.headers),
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(await errorMessage(response, "API 密钥操作失败"));
    return response.json();
  };

  const capabilities = async (force = false) => {
    if (!force && capabilityCache && Date.now() - capabilityCachedAt < 60_000) return capabilityCache;
    if (capabilityRequest) return capabilityRequest;
    capabilityRequest = (async () => {
      const response = await apiFetch("/capabilities");
      if (!response.ok) throw new Error(await errorMessage(response, "无法读取存储能力"));
      capabilityCache = await response.json();
      capabilityCachedAt = Date.now();
      return capabilityCache;
    })();
    try {
      return await capabilityRequest;
    } finally {
      capabilityRequest = null;
    }
  };

  const resetSpacePageCache = () => {
    spaceCacheGeneration += 1;
    spacePageCache.clear();
    spacePageRequests.clear();
    if (spacePrefetchTimer != null) window.clearTimeout(spacePrefetchTimer);
    spacePrefetchTimer = null;
  };

  const syncSpaceCacheIdentity = () => {
    const token = storedToken();
    if (spaceCacheToken === null) {
      spaceCacheToken = token;
      return;
    }
    if (spaceCacheToken !== token) {
      spaceCacheToken = token;
      resetSpacePageCache();
      mediaUrlCache.clear();
      revokePreviews();
      abortSpaceRequests();
      capabilityCache = null;
      capabilityCachedAt = 0;
      projectAccess = null;
      projectAccessRequest = null;
    }
  };

  const spacePageKey = (kind, cursor = null) => `${kind}:${cursor || "root"}`;

  const cachedSpacePage = (kind = "all") => {
    syncSpaceCacheIdentity();
    return spacePageCache.get(spacePageKey(kind)) || null;
  };

  const fetchSpaceBatch = async (kind = "all", cursor = null, force = false, options = {}) => {
    syncSpaceCacheIdentity();
    const key = spacePageKey(kind, cursor);
    const cached = spacePageCache.get(key);
    if (!cursor && !force && cached) return cached.data;
    if (spacePageRequests.has(key)) return spacePageRequests.get(key);

    const generation = spaceCacheGeneration;
    const request = (async () => {
      const query = new URLSearchParams({
        kind,
        limit: String(SPACE_BATCH_SIZE),
        include_summary: String(!cursor),
      });
      if (cursor) query.set("cursor", cursor);
      const response = await apiFetch(`/me?${query.toString()}`, { signal: options.signal });
      if (!response.ok) throw new Error(await errorMessage(response, "空间读取失败"));
      const data = await response.json();
      if (generation !== spaceCacheGeneration) throw new Error("登录状态已变化，请重新打开我的空间");
      if (!cursor) spacePageCache.set(key, { data, cachedAt: Date.now() });
      if (kind === "all" && !cursor && data.quota) {
        if (capabilityCache) capabilityCache = { ...capabilityCache, quota: data.quota };
        const launcher = document.querySelector("#turtle-space-launcher");
        if (launcher) updateLauncherUsage(launcher, { quota: data.quota });
      }
      return data;
    })();
    spacePageRequests.set(key, request);
    try {
      return await request;
    } finally {
      if (spacePageRequests.get(key) === request) spacePageRequests.delete(key);
    }
  };

  const prefetchSpacePage = () => {
    void fetchSpaceBatch("all").catch(() => {});
  };

  const scheduleSpacePrefetch = (delay = 180) => {
    if (spacePrefetchTimer != null || cachedSpacePage("all")) return;
    spacePrefetchTimer = window.setTimeout(() => {
      spacePrefetchTimer = null;
      prefetchSpacePage();
    }, delay);
  };

  const invalidateSpaceData = () => {
    resetSpacePageCache();
    capabilityCache = null;
    capabilityCachedAt = 0;
    scheduleLauncherRefresh();
  };

  const imageCompressible = (file) =>
    file instanceof Blob && ["image/jpeg", "image/png", "image/webp"].includes(file.type.toLowerCase());

  const canvasBlob = async (bitmap, width, height, type, quality) => {
    if (typeof OffscreenCanvas !== "undefined") {
      const canvas = new OffscreenCanvas(width, height);
      canvas.getContext("2d", { alpha: true }).drawImage(bitmap, 0, 0, width, height);
      return canvas.convertToBlob({ type, quality });
    }
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d", { alpha: true }).drawImage(bitmap, 0, 0, width, height);
    return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
  };

  const prepareImageAssets = async (file, media = DEFAULT_MEDIA) => {
    if (!file?.type?.startsWith("image/") || typeof createImageBitmap !== "function") {
      return { file, thumbnail: null };
    }
    let bitmap;
    try {
      try {
        bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (_error) {
        bitmap = await createImageBitmap(file);
      }
      const sourceMax = Math.max(bitmap.width, bitmap.height);
      let preparedFile = file;
      if (file instanceof File && imageCompressible(file)) {
        const maxDimension = Number(media.max_image_dimension) || DEFAULT_MEDIA.max_image_dimension;
        const scale = Math.min(1, maxDimension / sourceMax);
        const width = Math.max(1, Math.round(bitmap.width * scale));
        const height = Math.max(1, Math.round(bitmap.height * scale));
        const type = media.image_format || "image/webp";
        const quality = Number(media.image_quality) || DEFAULT_MEDIA.image_quality;
        const blob = await canvasBlob(bitmap, width, height, type, quality);
        if (blob && blob.size < file.size) {
          const stem = file.name.replace(/\.[^.]+$/, "") || "image";
          const extension = type === "image/webp" ? ".webp" : file.name.match(/\.[^.]+$/)?.[0] || "";
          preparedFile = new File([blob], `${stem}${extension}`, {
            type: blob.type || type,
            lastModified: file.lastModified || Date.now(),
          });
        }
      }

      const thumbnailScale = Math.min(1, STATIC_THUMBNAIL.max_dimension / sourceMax);
      const thumbnailWidth = Math.max(1, Math.round(bitmap.width * thumbnailScale));
      const thumbnailHeight = Math.max(1, Math.round(bitmap.height * thumbnailScale));
      const thumbnailBlob = await canvasBlob(
        bitmap,
        thumbnailWidth,
        thumbnailHeight,
        STATIC_THUMBNAIL.content_type,
        STATIC_THUMBNAIL.quality,
      );
      const thumbnail = thumbnailBlob
        && thumbnailBlob.type === STATIC_THUMBNAIL.content_type
        && thumbnailBlob.size > 0
        && thumbnailBlob.size <= STATIC_THUMBNAIL.max_bytes
        ? { blob: thumbnailBlob, width: thumbnailWidth, height: thumbnailHeight }
        : null;
      return { file: preparedFile, thumbnail };
    } catch (_error) {
      return { file, thumbnail: null };
    } finally {
      bitmap?.close?.();
    }
  };

  const replaceFormFile = (form, nextFile, bytesChanged = false) => {
    const next = new FormData();
    for (const [key, value] of form.entries()) {
      if (key === "file") next.append(key, nextFile, nextFile.name);
      else if (key === "metadata" && bytesChanged && typeof value === "string") {
        const metadata = parseMetadata(value);
        next.append(key, JSON.stringify(metadata));
      }
      else next.append(key, value);
    }
    return next;
  };

  const parseMetadata = (value) => {
    if (!value || typeof value !== "string") return {};
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        // Compression changes the bytes, so an upstream hash of the original
        // browser file must not be reused for the compressed object.
        delete parsed.file_hash;
        return parsed;
      }
      return {};
    } catch (_error) {
      return {};
    }
  };

  const jsonResponseError = (message, status = 400) =>
    new Response(JSON.stringify({ detail: message }), {
      status,
      headers: { "Content-Type": "application/json" },
    });

  const sendOriginalUpload = (input, init, body) => {
    const headers = headersFrom(input, init);
    headers.delete("Content-Type");
    if (typeof Request !== "undefined" && input instanceof Request) {
      return originalFetch(new Request(input, { ...init, headers, body }));
    }
    return originalFetch(input, { ...init, headers, body });
  };

  const cancelReservation = async (fileId) => {
    try {
      await apiFetch(`/uploads/${encodeURIComponent(fileId)}`, { method: "DELETE" });
    } catch (_error) {
      // The server also expires abandoned reservations after one hour.
    }
  };

  const directUpload = async (file, form, thumbnail = null) => {
    const presign = await apiFetch("/uploads/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || "application/octet-stream",
        size: file.size,
        metadata: parseMetadata(form.get("metadata")),
        // Media bytes must never fall back to Open WebUI processing on the
        // bandwidth-constrained application host.
        process: false,
        thumbnail: thumbnail
          ? {
              content_type: STATIC_THUMBNAIL.content_type,
              size: thumbnail.blob.size,
              width: thumbnail.width,
              height: thumbnail.height,
            }
          : null,
      }),
    });
    if (!presign.ok) throw new Error(await errorMessage(presign, "无法准备对象存储上传"));
    const ticket = await presign.json();
    try {
      if (thumbnail && !ticket.thumbnail_upload?.upload_url) {
        throw new Error("静态缩略图上传地址缺失");
      }
      const uploads = [
        originalFetch(ticket.upload_url, {
          method: "PUT",
          headers: ticket.headers || { "Content-Type": file.type },
          body: file,
          credentials: "omit",
          mode: "cors",
        }),
      ];
      if (thumbnail && ticket.thumbnail_upload) {
        uploads.push(originalFetch(ticket.thumbnail_upload.upload_url, {
          method: "PUT",
          headers: ticket.thumbnail_upload.headers || { "Content-Type": STATIC_THUMBNAIL.content_type },
          body: thumbnail.blob,
          credentials: "omit",
          mode: "cors",
        }));
      }
      const settled = await Promise.allSettled(uploads);
      const rejected = settled.find((result) => result.status === "rejected");
      if (rejected) throw rejected.reason;
      const results = settled.map((result) => result.value);
      const failed = results.find((response) => !response.ok);
      if (failed) throw new Error(`COS 上传失败（HTTP ${failed.status}）`);
      const complete = await apiFetch("/uploads/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: ticket.file_id }),
      });
      if (!complete.ok) throw new Error(await errorMessage(complete, "COS 文件校验失败"));
      invalidateSpaceData();
      return complete;
    } catch (error) {
      await cancelReservation(ticket.file_id);
      throw error;
    }
  };

  window.fetch = async (input, init) => {
    headersFrom(input, init);
    const method = String(init?.method || input?.method || "GET").toUpperCase();
    const path = pathOf(input);
    const form = init?.body instanceof FormData ? init.body : null;
    if (method !== "POST" || !UPLOAD_PATHS.has(path) || !form) return originalFetch(input, init);

    const originalFile = form.get("file");
    if (!(originalFile instanceof File)) return originalFetch(input, init);

    try {
      let storageCapability;
      try {
        storageCapability = await capabilities();
      } catch (_error) {
        storageCapability = { direct_upload: false, strict_external_media: true, media: DEFAULT_MEDIA };
      }
      const isMedia = ["image/", "video/"].some((prefix) => originalFile.type.startsWith(prefix));
      if (!isMedia) {
        if (storageCapability.strict_external_media) {
          throw new Error("当前仅允许图片和视频直传对象存储，其他文件上传已为保护服务器带宽而关闭");
        }
        return originalFetch(input, init);
      }
      const prepared = originalFile.type.startsWith("image/")
        ? await prepareImageAssets(originalFile, storageCapability.media)
        : { file: originalFile, thumbnail: null };
      const file = prepared.file;
      const compressedForm = replaceFormFile(form, file, file !== originalFile);
      const requestUrl = typeof input === "string" ? input : input.url;
      const needsServerProcessing = new URL(requestUrl, window.location.origin).searchParams.get("process") === "true";
      if (!storageCapability.direct_upload) {
        if (storageCapability.strict_external_media) {
          throw new Error("对象存储直传不可用，已阻止素材经过主服务器");
        }
        return sendOriginalUpload(input, init, compressedForm);
      }
      if (needsServerProcessing && !storageCapability.strict_external_media) {
        return sendOriginalUpload(input, init, compressedForm);
      }
      return await directUpload(file, compressedForm, prepared.thumbnail);
    } catch (error) {
      return jsonResponseError(error?.message || "媒体上传失败，请稍后重试");
    }
  };

  const managedFileId = (value) => {
    try {
      const path = new URL(String(value || ""), window.location.origin).pathname;
      return (
        path.match(/\/api\/v1\/files\/([0-9a-f-]{32,40})\/content(?:\/|$)/i)?.[1]
        || path.match(
          /\/api\/v1\/turtle\/storage\/files\/([0-9a-f-]{32,40})\/thumbnail(?:\/|$)/i,
        )?.[1]
        || ""
      );
    } catch (_error) {
      return "";
    }
  };

  const refreshManagedThumbnailImages = (fileId) => {
    document.querySelectorAll(MANAGED_IMAGE_SELECTOR).forEach((image) => {
      if (managedFileId(image.getAttribute("src") || image.src) !== fileId) return;
      if (image.complete && image.naturalWidth > 0) return;
      try {
        const source = new URL(image.getAttribute("src") || image.src, window.location.origin);
        source.searchParams.set("turtle_thumbnail_ready", String(Date.now()));
        image.src =
          source.origin === window.location.origin
            ? `${source.pathname}${source.search}`
            : source.toString();
      } catch (_error) {
        // A later DOM pass can retry the stable thumbnail endpoint.
      }
    });
  };

  const cancelStaticThumbnail = async (fileId) => {
    try {
      await apiFetch(`/thumbnails/${encodeURIComponent(fileId)}`, { method: "DELETE" });
    } catch (_error) {
      // Stale thumbnail reservations are also released server-side after one hour.
    }
  };

  const ensureStaticThumbnail = async (fileId) => {
    const statusResponse = await apiFetch(`/thumbnails/${encodeURIComponent(fileId)}`);
    if (!statusResponse.ok) return false;
    const statusPayload = await statusResponse.json();
    if (statusPayload.ready || !statusPayload.eligible) return true;

    const sourceResponse = await apiFetch(
      `/files/${encodeURIComponent(fileId)}/url?variant=original`,
    );
    if (!sourceResponse.ok) throw new Error(await errorMessage(sourceResponse, "无法读取缩略图来源"));
    const source = await sourceResponse.json();
    const imageResponse = await originalFetch(source.url, source.direct
      ? { credentials: "omit", mode: "cors", cache: "no-store" }
      : { headers: authHeaders(), credentials: "same-origin", cache: "no-store" });
    if (!imageResponse.ok) throw new Error("原图读取失败，稍后会重试缩略图生成");
    const imageBlob = await imageResponse.blob();
    const prepared = await prepareImageAssets(imageBlob, DEFAULT_MEDIA);
    if (!prepared.thumbnail) throw new Error("当前浏览器无法生成静态缩略图");

    const thumbnail = prepared.thumbnail;
    const presignResponse = await apiFetch("/thumbnails/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: fileId,
        content_type: STATIC_THUMBNAIL.content_type,
        size: thumbnail.blob.size,
        width: thumbnail.width,
        height: thumbnail.height,
      }),
    });
    if (!presignResponse.ok) throw new Error(await errorMessage(presignResponse, "无法准备静态缩略图"));
    const ticket = await presignResponse.json();
    if (ticket.ready) return true;
    if (!ticket.thumbnail_upload?.upload_url) throw new Error("静态缩略图上传地址缺失");

    try {
      const upload = await originalFetch(ticket.thumbnail_upload.upload_url, {
        method: "PUT",
        headers: ticket.thumbnail_upload.headers || { "Content-Type": STATIC_THUMBNAIL.content_type },
        body: thumbnail.blob,
        credentials: "omit",
        mode: "cors",
      });
      if (!upload.ok) throw new Error(`静态缩略图上传失败（HTTP ${upload.status}）`);
      const complete = await apiFetch("/thumbnails/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: fileId }),
      });
      if (!complete.ok) throw new Error(await errorMessage(complete, "静态缩略图校验失败"));
      mediaUrlCache.delete(`${fileId}:thumbnail`);
      invalidateSpaceData();
      return true;
    } catch (error) {
      await cancelStaticThumbnail(fileId);
      throw error;
    }
  };

  const drainManagedThumbnailQueue = () => {
    if (managedThumbnailActive || !managedThumbnailQueue.length) return;
    managedThumbnailActive = true;
    const fileId = managedThumbnailQueue.shift();
    void ensureStaticThumbnail(fileId)
      .then((finished) => {
        if (finished) {
          managedThumbnailAttempts.set(fileId, Number.POSITIVE_INFINITY);
          refreshManagedThumbnailImages(fileId);
        }
      })
      .catch(() => {})
      .finally(() => {
        managedThumbnailActive = false;
        drainManagedThumbnailQueue();
      });
  };

  const enqueueManagedThumbnail = (fileId) => {
    if (!fileId) return;
    const lastAttempt = managedThumbnailAttempts.get(fileId);
    if (lastAttempt === Number.POSITIVE_INFINITY || (lastAttempt && Date.now() - lastAttempt < 5 * 60_000)) return;
    managedThumbnailAttempts.set(fileId, Date.now());
    managedThumbnailQueue.push(fileId);
    drainManagedThumbnailQueue();
  };

  const scheduleManagedThumbnailScan = () => {
    if (managedThumbnailScanQueued) return;
    managedThumbnailScanQueued = true;
    requestAnimationFrame(() => {
      managedThumbnailScanQueued = false;
      document.querySelectorAll(MANAGED_IMAGE_SELECTOR).forEach((image) => {
        enqueueManagedThumbnail(managedFileId(image.getAttribute("src") || image.src));
      });
    });
  };

  const generatedAttachmentName = (link) => {
    const label = String(link?.textContent || "").replace(/\s+/g, " ").trim();
    const normalized = label.replace(/^(?:下载|Download)\s*/i, "").trim();
    return (
      normalized.length <= 220
      && !/[\\/]/.test(normalized)
      && /\.[a-z0-9]{1,10}$/i.test(normalized)
    ) ? normalized : "";
  };

  const generatedAttachmentType = (name) => {
    const extension = String(name || "").match(/\.([a-z0-9]{1,10})$/i)?.[1]?.toUpperCase();
    return extension ? `${extension} 文件` : "生成附件";
  };

  const generatedGalleryIcon = (name) => {
    const paths = {
      edit: '<path d="m4 20 4.2-1 10.9-10.9a2.1 2.1 0 0 0-3-3L5.2 16 4 20Z"></path><path d="m14.7 6.5 3 3"></path>',
      download: '<path d="M12 3v12"></path><path d="m7.5 10.5 4.5 4.5 4.5-4.5"></path><path d="M5 20h14"></path>',
      stack: '<rect x="5" y="3" width="14" height="14" rx="3"></rect><path d="M3 8v9a4 4 0 0 0 4 4h9"></path>',
    };
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.download}</svg>`;
  };

  const generatedImageEntry = (element) => {
    const image = element.matches?.(MANAGED_IMAGE_SELECTOR)
      ? element
      : element.querySelector?.(MANAGED_IMAGE_SELECTOR);
    if (!image) return null;
    const source = image.getAttribute("src") || image.currentSrc || image.src || "";
    const fileId = managedFileId(source);
    if (!fileId) return null;
    const trigger =
      image.closest('button[aria-label="显示图像预览"], button[aria-label="Show image preview"]')
      || element.querySelector?.('button[aria-label="显示图像预览"], button[aria-label="Show image preview"]')
      || image.closest("button")
      || element.querySelector?.("button");
    return {
      fileId,
      source,
      alt: image.getAttribute("alt") || "生成图片",
      trigger,
    };
  };

  const generatedImageEntries = (container) => {
    const entries = [];
    const seen = new Set();
    Array.from(container.children).forEach((child) => {
      const entry = generatedImageEntry(child);
      if (!entry || seen.has(entry.fileId)) return;
      seen.add(entry.fileId);
      entries.push(entry);
    });
    return entries;
  };

  const fetchManagedOriginal = async (fileId) => {
    const sourceResponse = await apiFetch(
      `/files/${encodeURIComponent(fileId)}/url?variant=original`,
    );
    if (!sourceResponse.ok) {
      throw new Error(await errorMessage(sourceResponse, "原图读取失败"));
    }
    const source = await sourceResponse.json();
    const fileResponse = await originalFetch(
      source.url,
      source.direct
        ? { credentials: "omit", mode: "cors", cache: "no-store" }
        : { headers: authHeaders(), credentials: "same-origin", cache: "no-store" },
    );
    if (!fileResponse.ok) throw new Error("原图读取失败");
    return fileResponse.blob();
  };

  const imageExtension = (contentType) => ({
    "image/avif": "avif",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
  }[String(contentType || "").split(";", 1)[0].toLowerCase()] || "png");

  const prepareGeneratedImageEdit = async (entry, button) => {
    const chatInput = document.querySelector("#chat-input");
    const chatForm = chatInput?.closest("form");
    const fileInput =
      chatForm?.querySelector('input[type="file"][multiple]')
      || chatForm?.parentElement?.querySelector('input[type="file"][multiple]');
    if (!chatInput || !fileInput || typeof DataTransfer !== "function") {
      return toast("当前输入框暂时无法接收这张图片，请刷新后重试", "error");
    }

    const originalCopy = button.innerHTML;
    button.disabled = true;
    button.textContent = "准备中…";
    try {
      const blob = await fetchManagedOriginal(entry.fileId);
      const extension = imageExtension(blob.type);
      const transfer = new DataTransfer();
      transfer.items.add(
        new File(
          [blob],
          `turtle-edit-source-${Date.now()}.${extension}`,
          { type: blob.type || `image/${extension}` },
        ),
      );
      fileInput.files = transfer.files;
      fileInput.dispatchEvent(new Event("change", { bubbles: true }));
      chatInput.scrollIntoView({ behavior: "smooth", block: "center" });
      window.setTimeout(() => chatInput.focus(), 180);
      toast("图片已放入输入框，请描述你想修改的内容", "success");
    } catch (error) {
      toast(error?.message || "图片准备失败，请稍后重试", "error");
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.innerHTML = originalCopy;
      }
    }
  };

  const crc32Table = (() => {
    const table = new Uint32Array(256);
    for (let index = 0; index < 256; index += 1) {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) {
        value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
      }
      table[index] = value >>> 0;
    }
    return table;
  })();

  const crc32 = (bytesValue) => {
    let value = 0xffffffff;
    for (const byte of bytesValue) {
      value = crc32Table[(value ^ byte) & 0xff] ^ (value >>> 8);
    }
    return (value ^ 0xffffffff) >>> 0;
  };

  const zipTimestamp = (date = new Date()) => {
    const year = Math.max(1980, date.getFullYear());
    return {
      date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
      time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
    };
  };

  const storedZip = (files) => {
    const encoder = new TextEncoder();
    const localParts = [];
    const centralParts = [];
    const stamp = zipTimestamp();
    let offset = 0;

    files.forEach(({ name, bytes: fileBytes }) => {
      const nameBytes = encoder.encode(name);
      const checksum = crc32(fileBytes);
      const local = new Uint8Array(30 + nameBytes.length);
      const localView = new DataView(local.buffer);
      localView.setUint32(0, 0x04034b50, true);
      localView.setUint16(4, 20, true);
      localView.setUint16(6, 0x0800, true);
      localView.setUint16(8, 0, true);
      localView.setUint16(10, stamp.time, true);
      localView.setUint16(12, stamp.date, true);
      localView.setUint32(14, checksum, true);
      localView.setUint32(18, fileBytes.length, true);
      localView.setUint32(22, fileBytes.length, true);
      localView.setUint16(26, nameBytes.length, true);
      local.set(nameBytes, 30);
      localParts.push(local, fileBytes);

      const central = new Uint8Array(46 + nameBytes.length);
      const centralView = new DataView(central.buffer);
      centralView.setUint32(0, 0x02014b50, true);
      centralView.setUint16(4, 20, true);
      centralView.setUint16(6, 20, true);
      centralView.setUint16(8, 0x0800, true);
      centralView.setUint16(10, 0, true);
      centralView.setUint16(12, stamp.time, true);
      centralView.setUint16(14, stamp.date, true);
      centralView.setUint32(16, checksum, true);
      centralView.setUint32(20, fileBytes.length, true);
      centralView.setUint32(24, fileBytes.length, true);
      centralView.setUint16(28, nameBytes.length, true);
      centralView.setUint32(42, offset, true);
      central.set(nameBytes, 46);
      centralParts.push(central);
      offset += local.length + fileBytes.length;
    });

    const centralSize = centralParts.reduce((total, part) => total + part.length, 0);
    if (offset > 0xffffffff || centralSize > 0xffffffff) {
      throw new Error("图片总量过大，无法在浏览器中打包");
    }
    const end = new Uint8Array(22);
    const endView = new DataView(end.buffer);
    endView.setUint32(0, 0x06054b50, true);
    endView.setUint16(8, files.length, true);
    endView.setUint16(10, files.length, true);
    endView.setUint32(12, centralSize, true);
    endView.setUint32(16, offset, true);
    return new Blob([...localParts, ...centralParts, end], { type: "application/zip" });
  };

  const triggerBlobDownload = (blob, name) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2_000);
  };

  const downloadGeneratedGallery = async (entries, button) => {
    if (entries.length === 1) return downloadFile(entries[0].fileId);
    const originalCopy = button.innerHTML;
    button.disabled = true;
    try {
      const files = [];
      for (let index = 0; index < entries.length; index += 1) {
        button.textContent = `正在打包 ${index + 1}/${entries.length}`;
        const blob = await fetchManagedOriginal(entries[index].fileId);
        files.push({
          name: `turtle-image-${String(index + 1).padStart(2, "0")}.${imageExtension(blob.type)}`,
          bytes: new Uint8Array(await blob.arrayBuffer()),
        });
      }
      const archive = storedZip(files);
      const timestamp = new Date()
        .toISOString()
        .replace(/[-:]/g, "")
        .replace(/\..+$/, "")
        .replace("T", "-");
      triggerBlobDownload(archive, `turtle-images-${timestamp}.zip`);
      toast(`已将 ${entries.length} 张图片打包下载`, "success");
    } catch (error) {
      toast(error?.message || "图片打包失败，请稍后重试", "error");
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.innerHTML = originalCopy;
      }
    }
  };

  const setGeneratedGalleryDownloadMenu = (gallery, open) => {
    if (!gallery) return;
    const menu = gallery.querySelector("[data-gallery-download-menu]");
    const trigger = gallery.querySelector("[data-gallery-download]");
    if (!menu || !trigger) return;
    const expanded = Boolean(open && (gallery._turtleEntries?.length || 0) > 1);
    menu.hidden = !expanded;
    trigger.setAttribute("aria-expanded", String(expanded));
  };

  const dismissGeneratedGalleryDownloadMenus = (event) => {
    if (!(event.target instanceof Element)) return;
    document.querySelectorAll("[data-gallery-download-menu]:not([hidden])").forEach((menu) => {
      if (!menu.parentElement?.contains(event.target)) {
        setGeneratedGalleryDownloadMenu(menu.closest(".turtle-generated-gallery"), false);
      }
    });
  };

  const setGeneratedGalleryActive = (gallery, index) => {
    const entries = gallery._turtleEntries || [];
    const resolvedIndex = Math.max(0, Math.min(Number(index) || 0, entries.length - 1));
    const entry = entries[resolvedIndex];
    if (!entry) return;
    gallery.dataset.activeFileId = entry.fileId;
    gallery.querySelector("[data-gallery-main-image]").src = entry.source;
    gallery.querySelector("[data-gallery-main-image]").alt = entry.alt;
    gallery.querySelector("[data-gallery-preview]").setAttribute(
      "aria-label",
      `预览第 ${resolvedIndex + 1} 张图片`,
    );
    const position = gallery.querySelector("[data-gallery-position]");
    if (position) position.textContent = `${resolvedIndex + 1} / ${entries.length}`;
    const currentLabel = gallery.querySelector("[data-gallery-download-current-label]");
    if (currentLabel) currentLabel.textContent = `第 ${resolvedIndex + 1} 张图片`;
    gallery.querySelectorAll("[data-gallery-index]").forEach((thumbnail) => {
      const active = Number(thumbnail.dataset.galleryIndex) === resolvedIndex;
      thumbnail.dataset.active = String(active);
      thumbnail.setAttribute("aria-selected", String(active));
      thumbnail.tabIndex = active ? 0 : -1;
    });
  };

  const populateGeneratedGallery = (gallery, entries) => {
    gallery._turtleEntries = entries;
    gallery.dataset.count = String(entries.length);
    const rail = gallery.querySelector("[data-gallery-rail]");
    rail.replaceChildren();
    entries.forEach((entry, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.galleryIndex = String(index);
      button.setAttribute("role", "tab");
      button.setAttribute("aria-label", `查看第 ${index + 1} 张图片`);
      const image = document.createElement("img");
      image.src = entry.source;
      image.alt = "";
      image.loading = "lazy";
      button.append(image);
      rail.append(button);
    });
    const batchButton = gallery.querySelector("[data-gallery-download-all]");
    if (batchButton) batchButton.hidden = entries.length < 2;
    const batchLabel = gallery.querySelector("[data-gallery-download-all-label]");
    if (batchLabel) batchLabel.textContent = `${entries.length} 张图片 · ZIP`;
    const downloadButton = gallery.querySelector("[data-gallery-download]");
    if (downloadButton) {
      const label = entries.length > 1 ? "选择下载方式" : "下载当前图片";
      downloadButton.setAttribute("aria-label", label);
      downloadButton.title = label;
      if (entries.length > 1) downloadButton.setAttribute("aria-haspopup", "menu");
      else downloadButton.removeAttribute("aria-haspopup");
    }
    if (entries.length < 2) setGeneratedGalleryDownloadMenu(gallery, false);
    const preferred = entries.findIndex((entry) => entry.fileId === gallery.dataset.activeFileId);
    setGeneratedGalleryActive(gallery, preferred >= 0 ? preferred : 0);
  };

  const createGeneratedGallery = (source, entries) => {
    const gallery = document.createElement("section");
    gallery.className = "turtle-generated-gallery";
    gallery.setAttribute("aria-label", `生成图片，共 ${entries.length} 张`);
    gallery.innerHTML = `
      <div class="turtle-generated-gallery-stage">
        <button type="button" class="turtle-generated-gallery-main" data-gallery-preview>
          <img data-gallery-main-image alt="" />
        </button>
        <div class="turtle-generated-gallery-overlay">
          <button type="button" data-gallery-edit>${generatedGalleryIcon("edit")}<span>编辑</span></button>
          <div class="turtle-generated-gallery-download">
            <button type="button" data-gallery-download aria-label="选择下载方式" title="选择下载方式" aria-haspopup="menu" aria-expanded="false">${generatedGalleryIcon("download")}</button>
            <div class="turtle-generated-gallery-download-menu" data-gallery-download-menu role="menu" aria-label="下载图片" hidden>
              <button type="button" data-gallery-download-current role="menuitem">
                ${generatedGalleryIcon("download")}
                <span><strong>下载当前图片</strong><small data-gallery-download-current-label></small></span>
              </button>
              <button type="button" data-gallery-download-all role="menuitem">
                ${generatedGalleryIcon("stack")}
                <span><strong>下载全部图片</strong><small data-gallery-download-all-label></small></span>
              </button>
            </div>
          </div>
        </div>
      </div>
      <div class="turtle-generated-gallery-rail" data-gallery-rail role="tablist" aria-label="图片缩略图"></div>
      <footer>
        <span><strong>生成图片</strong><small data-gallery-position></small></span>
      </footer>`;
    gallery._turtleSource = source;
    source._turtleGallery = gallery;
    source.before(gallery);
    source.classList.add("turtle-generated-gallery-source");
    source.setAttribute("aria-hidden", "true");

    gallery.addEventListener("click", (event) => {
      const thumbnail = event.target.closest("[data-gallery-index]");
      if (thumbnail) {
        setGeneratedGalleryActive(gallery, thumbnail.dataset.galleryIndex);
        return;
      }
      const entriesNow = gallery._turtleEntries || [];
      const activeIndex = Math.max(
        0,
        entriesNow.findIndex((entry) => entry.fileId === gallery.dataset.activeFileId),
      );
      const entry = entriesNow[activeIndex];
      if (!entry) return;
      if (event.target.closest("[data-gallery-preview]")) {
        if (entry.trigger?.isConnected) entry.trigger.click();
        else toast("图片预览暂时不可用，请刷新后重试", "error");
        return;
      }
      const editButton = event.target.closest("[data-gallery-edit]");
      if (editButton) {
        void prepareGeneratedImageEdit(entry, editButton);
        return;
      }
      if (event.target.closest("[data-gallery-download]")) {
        if (entriesNow.length < 2) {
          void downloadFile(entry.fileId);
        } else {
          const menu = gallery.querySelector("[data-gallery-download-menu]");
          setGeneratedGalleryDownloadMenu(gallery, menu?.hidden);
        }
        return;
      }
      if (event.target.closest("[data-gallery-download-current]")) {
        setGeneratedGalleryDownloadMenu(gallery, false);
        void downloadFile(entry.fileId);
        return;
      }
      const batchButton = event.target.closest("[data-gallery-download-all]");
      if (batchButton) {
        void downloadGeneratedGallery(entriesNow, batchButton).finally(() => {
          setGeneratedGalleryDownloadMenu(gallery, false);
        });
      }
    });
    gallery.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !gallery.querySelector("[data-gallery-download-menu]")?.hidden) {
        event.preventDefault();
        setGeneratedGalleryDownloadMenu(gallery, false);
        gallery.querySelector("[data-gallery-download]")?.focus();
        return;
      }
      const thumbnail = event.target.closest("[data-gallery-index]");
      if (!thumbnail || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const entryCount = gallery._turtleEntries?.length || 0;
      if (!entryCount) return;
      const current = Number(thumbnail.dataset.galleryIndex) || 0;
      const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
      const next = (current + direction + entryCount) % entryCount;
      setGeneratedGalleryActive(gallery, next);
      gallery.querySelector(`[data-gallery-index="${next}"]`)?.focus();
    });
    populateGeneratedGallery(gallery, entries);
    return gallery;
  };

  const generatedGallerySources = () => {
    const sources = new Set();
    document.querySelectorAll("#response-content-container p").forEach((paragraph) => {
      const mediaChildren = Array.from(paragraph.children).filter(
        (child) => generatedImageEntry(child),
      );
      const hasText = Array.from(paragraph.childNodes).some(
        (node) => node.nodeType === Node.TEXT_NODE && String(node.textContent || "").trim(),
      );
      if (mediaChildren.length && mediaChildren.length === paragraph.children.length && !hasText) {
        sources.add(paragraph);
      }
    });
    document.querySelectorAll("div.my-1.w-full.flex.flex-wrap").forEach((container) => {
      let assistantScope = container.parentElement;
      let belongsToAssistant = false;
      for (let depth = 0; assistantScope && depth < 4; depth += 1) {
        if (Array.from(assistantScope.children).some((child) => child.id === "response-content-container")) {
          belongsToAssistant = true;
          break;
        }
        assistantScope = assistantScope.parentElement;
      }
      if (!belongsToAssistant) return;
      const children = Array.from(container.children);
      const imageCards = children.filter((child) => generatedImageEntry(child));
      if (imageCards.length && imageCards.length === children.length) sources.add(container);
    });
    return Array.from(sources);
  };

  const decorateGeneratedGalleries = () => {
    const sources = generatedGallerySources();
    const activeSources = new Set(sources);
    document.querySelectorAll(".turtle-generated-gallery").forEach((gallery) => {
      const source = gallery._turtleSource;
      if (!source?.isConnected || !activeSources.has(source)) {
        source?.classList?.remove("turtle-generated-gallery-source");
        source?.removeAttribute?.("aria-hidden");
        if (source?._turtleGallery === gallery) source._turtleGallery = null;
        gallery.remove();
      }
    });
    sources.forEach((source) => {
      const entries = generatedImageEntries(source);
      if (!entries.length) return;
      const signature = entries.map((entry) => entry.fileId).join(":");
      const gallery = source._turtleGallery?.isConnected
        ? source._turtleGallery
        : createGeneratedGallery(source, entries);
      source.classList.add("turtle-generated-gallery-source");
      source.setAttribute("aria-hidden", "true");
      if (gallery.dataset.signature !== signature) {
        gallery.dataset.signature = signature;
        gallery.setAttribute("aria-label", `生成图片，共 ${entries.length} 张`);
        populateGeneratedGallery(gallery, entries);
      } else {
        gallery._turtleEntries = entries;
      }
    });
  };

  const decorateUnsupportedSandboxLinks = () => {
    document.querySelectorAll('a[href^="sandbox:" i]').forEach((link) => {
      const href = String(link.getAttribute("href") || "").trim();
      if (!href) return;
      link.dataset.turtleSandboxHref = href;
      link.classList.add("turtle-unsupported-sandbox-link");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("title", "GPT 未生成可下载文件，此临时链接已禁用");
      link.removeAttribute("href");
      link.removeAttribute("target");
      link.removeAttribute("rel");
      link.tabIndex = -1;
    });

    const affectedResponses = new Set();
    document.querySelectorAll(".turtle-unsupported-sandbox-link").forEach((link) => {
      const response = link.closest("#response-content-container");
      if (response) affectedResponses.add(response);
    });
    affectedResponses.forEach((response) => {
      if (response.querySelector("[data-turtle-sandbox-warning]")) return;
      const warning = document.createElement("aside");
      warning.className = "turtle-sandbox-warning";
      warning.dataset.turtleSandboxWarning = "";
      warning.setAttribute("role", "note");
      warning.setAttribute("aria-label", "系统提醒");
      warning.innerHTML = `
        <span class="turtle-sandbox-warning-icon">${mediaIcon("warning")}</span>
        <span class="turtle-sandbox-warning-copy">
          <strong>系统提醒</strong>
          <span>GPT 只返回了临时运行路径，没有生成本站可下载的文件。链接已禁用，请让 GPT 重新生成压缩包。</span>
        </span>`;
      response.append(warning);
    });
    document.querySelectorAll("[data-turtle-sandbox-warning]").forEach((warning) => {
      if (!warning.parentElement?.querySelector(".turtle-unsupported-sandbox-link")) warning.remove();
    });
  };

  const safeRichReferenceUrl = (value) => {
    if (typeof value !== "string" || value.length > 8192) return "";
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password || !url.hostname) return "";
      return url.href;
    } catch (_error) {
      return "";
    }
  };

  const boundedRichReferenceText = (value, limit = 240) =>
    typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, limit) : "";

  const richReferenceSearchUrl = (query, images = false) => {
    const value = boundedRichReferenceText(query, 400);
    if (!value) return "";
    const route = images ? "/images/search" : "/search";
    return `https://www.bing.com${route}?q=${encodeURIComponent(value)}`;
  };

  const parseRichReference = (link) => {
    try {
      const url = new URL(link.getAttribute("href") || "", window.location.origin);
      if (url.origin !== window.location.origin || !url.pathname.startsWith(RICH_REFERENCE_PREFIX)) {
        return null;
      }
      const kind = url.pathname.slice(RICH_REFERENCE_PREFIX.length);
      if (!/^[a-z0-9-]{1,32}$/.test(kind) || !url.hash || url.hash.length > 70_000) return null;
      const encoded = url.hash.slice(1).replace(/-/g, "+").replace(/_/g, "/");
      const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=");
      const bytes = Uint8Array.from(window.atob(padded), (character) => character.charCodeAt(0));
      const payload = JSON.parse(new TextDecoder().decode(bytes));
      if (!payload || typeof payload !== "object" || Array.isArray(payload) || payload.v !== 1) return null;
      return { kind, payload };
    } catch (_error) {
      return null;
    }
  };

  const richReferenceHeader = (iconKind, title, subtitle = "") => {
    const header = document.createElement("header");
    const icon = document.createElement("span");
    icon.className = "turtle-rich-reference-icon";
    icon.innerHTML = mediaIcon(iconKind);
    const copy = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = title;
    copy.append(strong);
    if (subtitle) {
      const small = document.createElement("small");
      small.textContent = subtitle;
      copy.append(small);
    }
    header.append(icon, copy);
    return header;
  };

  const createRichReferenceQueryLinks = (queries) => {
    const footer = document.createElement("footer");
    const label = document.createElement("span");
    label.textContent = "图片搜索";
    footer.append(label);
    queries.forEach((query) => {
      const href = richReferenceSearchUrl(query, true);
      if (!href) return;
      const link = document.createElement("a");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      link.textContent = query;
      footer.append(link);
    });
    return footer.childElementCount > 1 ? footer : null;
  };

  const createImageGroupReference = (payload) => {
    const queries = Array.isArray(payload.queries)
      ? payload.queries.map((query) => boundedRichReferenceText(query, 240)).filter(Boolean).slice(0, 8)
      : [];
    const rawImages = Array.isArray(payload.images) ? payload.images.slice(0, 12) : [];
    const images = rawImages
      .map((entry) => ({
        imageUrl: safeRichReferenceUrl(entry?.image_url),
        sourceUrl: safeRichReferenceUrl(entry?.source_url),
        title: boundedRichReferenceText(entry?.title, 240),
        sourceName: boundedRichReferenceText(entry?.source_name, 120),
        query: boundedRichReferenceText(entry?.query, 240),
      }))
      .filter((entry) => entry.imageUrl);
    const section = document.createElement("section");
    section.className = "turtle-image-reference-group";
    section.dataset.layout = ["bento", "grid"].includes(payload.layout) ? payload.layout : "carousel";
    section.setAttribute("aria-label", `参考图片，共 ${images.length} 张`);
    const ratio = /^(\d{1,2}):(\d{1,2})$/.exec(String(payload.aspect_ratio || ""));
    if (ratio && Number(ratio[1]) > 0 && Number(ratio[2]) > 0) {
      section.style.setProperty("--turtle-rich-image-ratio", `${ratio[1]} / ${ratio[2]}`);
    }
    section.append(
      richReferenceHeader(
        "image",
        "参考图片",
        queries.length ? queries.join(" · ") : `${images.length} 张图片`,
      ),
    );

    if (images.length) {
      const rail = document.createElement("div");
      rail.className = "turtle-image-reference-rail";
      let availableImages = images.length;
      images.forEach((entry, index) => {
        const card = document.createElement("a");
        card.className = "turtle-image-reference-card";
        card.href = entry.sourceUrl || entry.imageUrl;
        card.target = "_blank";
        card.rel = "noopener noreferrer";
        card.referrerPolicy = "no-referrer";
        card.setAttribute("aria-label", entry.title || `查看第 ${index + 1} 张参考图片`);
        const image = document.createElement("img");
        image.src = entry.imageUrl;
        image.alt = entry.title || entry.query || "";
        image.loading = "lazy";
        image.decoding = "async";
        image.referrerPolicy = "no-referrer";
        image.addEventListener(
          "error",
          () => {
            card.hidden = true;
            availableImages -= 1;
            if (!availableImages) section.dataset.imagesUnavailable = "true";
          },
          { once: true },
        );
        const captionText = entry.title || entry.sourceName || entry.query;
        if (captionText) {
          const caption = document.createElement("span");
          const strong = document.createElement("strong");
          strong.textContent = captionText;
          caption.append(strong);
          if (entry.sourceName && entry.sourceName !== captionText) {
            const small = document.createElement("small");
            small.textContent = entry.sourceName;
            caption.append(small);
          }
          card.append(image, caption);
        } else {
          card.append(image);
        }
        rail.append(card);
      });
      section.append(rail);
    } else {
      const empty = document.createElement("p");
      empty.className = "turtle-image-reference-empty";
      empty.textContent = queries.length
        ? "图片结果暂时不可用，可通过下方关键词继续查看。"
        : "图片结果暂时不可用。";
      section.append(empty);
    }
    const queryLinks = createRichReferenceQueryLinks(queries);
    if (queryLinks) section.append(queryLinks);
    return section;
  };

  const richEntityTypeLabel = (value, kind) => {
    const type = boundedRichReferenceText(value, 80).toLowerCase();
    const labels = {
      artist: "艺术家",
      city: "城市",
      event: "活动",
      location: "地点",
      organization: "机构",
      person: "人物",
      place: "地点",
      point_of_interest: "地点",
      product: "商品",
      restaurant: "餐厅",
    };
    return labels[type] || (kind === "product" ? "商品" : type.replace(/_/g, " ") || "实体");
  };

  const createEntityReference = (kind, payload) => {
    const name = boundedRichReferenceText(payload.name, 180);
    if (!name) return null;
    const description = boundedRichReferenceText(payload.description, 240);
    const href =
      safeRichReferenceUrl(payload.url) ||
      richReferenceSearchUrl([name, description].filter(Boolean).join(" "));
    const link = document.createElement("a");
    link.className = "turtle-entity-reference";
    if (href) {
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
    }
    const icon = document.createElement("span");
    icon.className = "turtle-rich-reference-icon";
    icon.innerHTML = mediaIcon(kind === "product" ? "file" : "chat");
    const copy = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = name;
    const small = document.createElement("small");
    const typeLabel = richEntityTypeLabel(payload.entity_type, kind);
    small.textContent = [typeLabel, description].filter(Boolean).join(" · ");
    copy.append(strong, small);
    link.append(icon, copy);
    link.setAttribute("aria-label", `查看${typeLabel}：${name}`);
    return link;
  };

  const createProductsReference = (payload) => {
    const items = Array.isArray(payload.items)
      ? payload.items
          .map((item) => ({
            name: boundedRichReferenceText(item?.name, 180),
            tag: boundedRichReferenceText(item?.tag, 160),
            url: safeRichReferenceUrl(item?.url),
          }))
          .filter((item) => item.name)
          .slice(0, 10)
      : [];
    if (!items.length) return null;
    const section = document.createElement("section");
    section.className = "turtle-products-reference";
    section.setAttribute("aria-label", `商品推荐，共 ${items.length} 项`);
    section.append(richReferenceHeader("file", "商品推荐", `${items.length} 项`));
    const list = document.createElement("div");
    items.forEach((item) => {
      const href = item.url || richReferenceSearchUrl(item.name);
      const card = document.createElement(href ? "a" : "span");
      card.className = "turtle-product-reference-card";
      if (href) {
        card.href = href;
        card.target = "_blank";
        card.rel = "noopener noreferrer";
        card.referrerPolicy = "no-referrer";
      }
      const strong = document.createElement("strong");
      strong.textContent = item.name;
      card.append(strong);
      if (item.tag) {
        const small = document.createElement("small");
        small.textContent = item.tag;
        card.append(small);
      }
      list.append(card);
    });
    section.append(list);
    return section;
  };

  const createReferenceList = (payload) => {
    const title = boundedRichReferenceText(payload.title, 120) || "相关内容";
    const items = Array.isArray(payload.items)
      ? payload.items
          .map((item) => ({
            name: boundedRichReferenceText(item?.name, 180),
            subtitle: boundedRichReferenceText(item?.subtitle, 240),
            url: safeRichReferenceUrl(item?.url),
            imageUrl: safeRichReferenceUrl(item?.image_url),
          }))
          .filter((item) => item.name)
          .slice(0, 12)
      : [];
    if (!items.length) return null;
    const section = document.createElement("section");
    section.className = "turtle-reference-list";
    section.setAttribute("aria-label", `${title}，共 ${items.length} 项`);
    const referenceType = boundedRichReferenceText(payload.reference_type, 80);
    const iconKind = /file/.test(referenceType) ? "file" : "cloud";
    section.append(richReferenceHeader(iconKind, title, `${items.length} 项`));
    const list = document.createElement("div");
    items.forEach((item) => {
      const card = document.createElement(item.url ? "a" : "span");
      card.className = "turtle-reference-list-card";
      if (item.url) {
        card.href = item.url;
        card.target = "_blank";
        card.rel = "noopener noreferrer";
        card.referrerPolicy = "no-referrer";
      }
      if (item.imageUrl) {
        const image = document.createElement("img");
        image.src = item.imageUrl;
        image.alt = "";
        image.loading = "lazy";
        image.decoding = "async";
        image.referrerPolicy = "no-referrer";
        card.append(image);
      }
      const copy = document.createElement("span");
      const strong = document.createElement("strong");
      strong.textContent = item.name;
      copy.append(strong);
      if (item.subtitle) {
        const small = document.createElement("small");
        small.textContent = item.subtitle;
        copy.append(small);
      }
      card.append(copy);
      list.append(card);
    });
    section.append(list);
    return section;
  };

  const replaceRichReference = (link, reference) => {
    let component = null;
    if (reference.kind === "image-group") component = createImageGroupReference(reference.payload);
    else if (reference.kind === "entity" || reference.kind === "product") {
      component = createEntityReference(reference.kind, reference.payload);
    } else if (reference.kind === "products") component = createProductsReference(reference.payload);
    else if (reference.kind === "reference-list") component = createReferenceList(reference.payload);
    if (!component) return false;
    const blockComponent = component.matches("section");
    const parent = link.parentElement;
    const standaloneParagraph =
      blockComponent &&
      parent?.tagName === "P" &&
      Array.from(parent.childNodes).every(
        (node) => node === link || (node.nodeType === Node.TEXT_NODE && !String(node.textContent || "").trim()),
      );
    if (standaloneParagraph) parent.replaceWith(component);
    else link.replaceWith(component);
    return true;
  };

  const decorateRichContentReferences = () => {
    document.querySelectorAll(`a[href*="${RICH_REFERENCE_PREFIX}"]`).forEach((link) => {
      const reference = parseRichReference(link);
      if (reference && replaceRichReference(link, reference)) return;
      link.classList.add("turtle-unsupported-rich-reference");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("title", "此富内容暂时无法显示");
      link.removeAttribute("href");
      link.removeAttribute("target");
      link.removeAttribute("rel");
      link.tabIndex = -1;
    });
  };

  const suppressRichReferenceNavigation = (event) => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest(`a[href*="${RICH_REFERENCE_PREFIX}"]`);
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    decorateRichContentReferences();
  };

  const decorateResponsePresentation = () => {
    document.querySelectorAll("#response-content-container").forEach((container) => {
      const response = container.firstElementChild;
      if (!(response instanceof HTMLElement)) return;
      response.classList.add("turtle-response-markdown");

      Array.from(response.children).forEach((section) => {
        if (!(section instanceof HTMLElement)) return;
        const disclosure = section.querySelector(
          ":scope > .cursor-pointer, :scope > [class*='cursor-pointer']",
        );
        const label = String(disclosure?.textContent || "").trim();
        if (!/思考|推理|Worked for|Thought for/i.test(label)) return;
        section.classList.add("turtle-reasoning-trace");
        disclosure.classList.add("turtle-reasoning-toggle");
        const reasoning = section.querySelector(":scope blockquote");
        if (!reasoning) return;
        reasoning.classList.add("turtle-reasoning-content");
        reasoning.querySelectorAll("p").forEach((paragraph) => {
          const text = String(paragraph.textContent || "").trim();
          paragraph.classList.toggle(
            "turtle-reasoning-progress",
            /正在(?:搜索|查看)/.test(text),
          );
        });
      });
    });
  };

  const decorateManagedOutputs = () => {
    decorateResponsePresentation();
    decorateRichContentReferences();
    decorateUnsupportedSandboxLinks();
    decorateGeneratedGalleries();

    document.querySelectorAll('a[href*="/api/v1/files/"]').forEach((link) => {
      if (link.dataset.turtleGeneratedFile || link.querySelector("img")) return;
      const name = generatedAttachmentName(link);
      const fileId = managedFileId(link.getAttribute("href") || link.href);
      if (!name || !fileId) return;
      const type = generatedAttachmentType(name);
      link.dataset.turtleGeneratedFile = "attachment";
      link.dataset.turtleFileId = fileId;
      link.dataset.turtleFileName = name;
      link.classList.add("turtle-generated-file-card");
      link.setAttribute("aria-label", `下载附件 ${name}`);
      link.removeAttribute("target");
      link.removeAttribute("rel");
      link.title = `下载附件 ${name}`;

      const icon = document.createElement("span");
      icon.className = "turtle-generated-file-icon";
      icon.innerHTML = mediaIcon("file");
      const copy = document.createElement("span");
      copy.className = "turtle-generated-file-copy";
      const strong = document.createElement("strong");
      strong.textContent = name;
      const small = document.createElement("small");
      small.textContent = type;
      copy.append(strong, small);
      const action = document.createElement("span");
      action.className = "turtle-generated-file-action";
      action.textContent = "下载";
      link.replaceChildren(icon, copy, action);
    });
  };

  const downloadManagedAttachmentOnPage = (event) => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest(".turtle-generated-file-card[data-turtle-file-id]");
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (link.dataset.turtleDownloadPending === "true") return;

    const fileId = String(link?.dataset.turtleFileId || "");
    const name = String(link?.dataset.turtleFileName || "生成附件.zip");
    if (!fileId) return;
    const action = link.querySelector(".turtle-generated-file-action");
    const originalCopy = action?.textContent || "下载";
    link.dataset.turtleDownloadPending = "true";
    link.setAttribute("aria-busy", "true");
    if (action) action.textContent = "准备中…";
    void downloadFile(fileId, name).then((started) => {
      if (started) toast("已开始下载", "success");
    }).finally(() => {
      delete link.dataset.turtleDownloadPending;
      link.removeAttribute("aria-busy");
      if (action?.isConnected) action.textContent = originalCopy;
    });
  };

  const suppressUnsupportedSandboxNavigation = (event) => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest(
      'a[href^="sandbox:" i], .turtle-unsupported-sandbox-link',
    );
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  };

  const suppressManagedImageSourceNavigation = (event) => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest("a[href]");
    if (!link?.querySelector(MANAGED_IMAGE_SELECTOR)) return;
    try {
      const source = new URL(link.href, window.location.origin);
      if (!source.pathname.startsWith("/v1/source/")) return;
    } catch (_error) {
      return;
    }
    // Open WebUI's preview button is nested inside the original generated-image
    // link. Cancel only the anchor's new-tab navigation and leave the preview
    // click handler untouched.
    event.preventDefault();
  };

  const isSearchSourceBlock = (element) =>
    element?.tagName === "BLOCKQUOTE"
    && !element.closest("details.turtle-search-sources")
    && /^\s*\[\d+\]\s*/.test(String(element.textContent || ""));

  const searchSourceParagraphs = (block) =>
    Array.from(block?.children || []).filter(
      (child) =>
        child.tagName === "P"
        && /^\s*\[\d+\]\s*/.test(String(child.textContent || "")),
    );

  const splitTrailingSearchAnswer = (block) => {
    const paragraphs = searchSourceParagraphs(block);
    const lastParagraph = paragraphs[paragraphs.length - 1];
    const links = Array.from(lastParagraph?.querySelectorAll("a") || []);
    const lastLink = links[links.length - 1];
    if (!lastParagraph || !lastLink || lastLink.parentElement !== lastParagraph) return;

    const trailingNodes = [];
    let node = lastLink.nextSibling;
    while (node) {
      const next = node.nextSibling;
      trailingNodes.push(node);
      node = next;
    }
    if (!trailingNodes.some((item) => String(item.textContent || "").trim())) return;

    const answer = document.createElement("p");
    trailingNodes.forEach((item) => answer.append(item));
    block.parentElement?.insertBefore(answer, block.nextSibling);
  };

  const compactSearchSources = () => {
    document.querySelectorAll("#response-content-container").forEach((container) => {
      const sourceBlocks = Array.from(container.querySelectorAll("blockquote"))
        .filter(isSearchSourceBlock);
      if (!sourceBlocks.length) return;

      const host = sourceBlocks[0].parentElement || container;
      let disclosure = Array.from(host.children).find(
        (child) => child.matches?.("details.turtle-search-sources"),
      );
      if (!disclosure) {
        disclosure = document.createElement("details");
        disclosure.className = "turtle-search-sources";
        disclosure.dataset.turtleSearchSources = "true";
        disclosure.innerHTML = "<summary>搜索来源</summary><div></div>";
        host.insertBefore(disclosure, sourceBlocks[0]);
      }

      const list = disclosure.querySelector("div");
      if (!list) return;
      sourceBlocks.forEach((block) => {
        splitTrailingSearchAnswer(block);
        const sourceItems = searchSourceParagraphs(block);
        if (sourceItems.length) {
          sourceItems.forEach((item) => {
            item.classList.add("turtle-search-source");
            list.append(item);
          });
          if (!String(block.textContent || "").trim()) block.remove();
          return;
        }
        block.classList.add("turtle-search-source");
        list.append(block);
      });
      const summary = disclosure.querySelector("summary");
      const sourceCount = list.querySelectorAll(".turtle-search-source").length;
      if (summary) summary.textContent = `搜索来源（${sourceCount}）`;
    });
  };

  const nativeReasoningLabel = (button) =>
    String(button?.innerText || button?.textContent || "").replace(/\s+/g, " ").trim();

  const syncNativeReasoningDisclosure = () => {
    document.querySelectorAll("#response-content-container").forEach((container) => {
      const buttons = Array.from(container.querySelectorAll('button[aria-expanded]'));
      const active = buttons.find((button) =>
        /^(?:正在思考|Thinking)(?:\.\.\.|…)?$/i.test(nativeReasoningLabel(button)),
      );
      if (active) {
        container.dataset.turtleNativeReasoningOpen = "true";
        if (active.getAttribute("aria-expanded") !== "true") active.click();
        return;
      }

      if (container.dataset.turtleNativeReasoningOpen !== "true") return;
      const completed = buttons.find((button) =>
        /^(?:思考用时|思考$|Thought(?:\s|$))/i.test(nativeReasoningLabel(button)),
      );
      if (!completed) return;
      if (completed.getAttribute("aria-expanded") === "true") completed.click();
      delete container.dataset.turtleNativeReasoningOpen;
    });
  };

  const nativePreviewScale = (modal) => {
    const image = modal?.querySelector("img");
    const transform = image?.parentElement
      ? window.getComputedStyle(image.parentElement).transform
      : "none";
    if (!transform || transform === "none") return 1;
    const matrix = transform.match(/^matrix\(([^)]+)\)$/)?.[1]
      ?.split(",")
      .map((value) => Number(value.trim()));
    return matrix?.length >= 2 && matrix.every(Number.isFinite)
      ? Math.hypot(matrix[0], matrix[1])
      : 1;
  };

  const nativePreviewSourceTrigger = (modal) => {
    const image = modal?.querySelector("img");
    const source = image?.currentSrc || image?.src || "";
    if (!source) return null;
    return Array.from(document.querySelectorAll('button[aria-label="显示图像预览"]')).find(
      (button) => {
        if (modal.contains(button)) return false;
        const candidate = button.querySelector("img");
        return candidate && (candidate.currentSrc || candidate.src || "") === source;
      },
    ) || null;
  };

  const resetNativeImagePreview = (modal) => {
    const closeButton = modal?.querySelector("[data-turtle-native-preview-close]");
    const sourceTrigger = modal?._turtleSourceTrigger || nativePreviewSourceTrigger(modal);
    if (!closeButton || !sourceTrigger?.isConnected) return;
    closeButton.click();
    requestAnimationFrame(() => {
      if (sourceTrigger.isConnected) sourceTrigger.click();
    });
  };

  const dispatchNativePreviewZoom = (modal, deltaY) => {
    const image = modal?.querySelector("img");
    const canvas = image?.parentElement;
    if (!image || !canvas) return;
    const rect = image.getBoundingClientRect();
    canvas.dispatchEvent(
      new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        deltaY,
        clientX: Math.max(0, Math.min(window.innerWidth, rect.left + rect.width / 2)),
        clientY: Math.max(0, Math.min(window.innerHeight, rect.top + rect.height / 2)),
      }),
    );
  };

  const nativePreviewZoomButton = (action, label, copy) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.turtleNativePreviewZoom = action;
    button.setAttribute("aria-label", label);
    button.title = label;
    button.textContent = copy;
    return button;
  };

  const enhanceNativeImagePreview = (modal) => {
    if (modal.dataset.turtleNativePreview === "ready") return;
    const image = modal.querySelector("img");
    const toolbar = Array.from(modal.children).find(
      (child) => child instanceof HTMLElement && child.querySelectorAll("button").length >= 2,
    );
    if (!image || !toolbar) return;

    const originalButtons = Array.from(toolbar.querySelectorAll("button"));
    const downloadButton =
      originalButtons.find((button) => button.querySelector('path[d^="M10.75"]')) ||
      originalButtons[1];
    const closeButton = originalButtons.find((button) => button !== downloadButton);
    if (!closeButton || !downloadButton) return;

    modal.dataset.turtleNativePreview = "ready";
    modal.classList.add("turtle-native-image-preview");
    toolbar.classList.add("turtle-native-preview-toolbar");
    closeButton.dataset.turtleNativePreviewClose = "";
    closeButton.setAttribute("aria-label", "关闭预览");
    closeButton.title = "关闭预览";
    downloadButton.dataset.turtleNativePreviewDownload = "";
    downloadButton.setAttribute("aria-label", "下载原图");
    downloadButton.title = "下载原图";
    modal._turtleSourceTrigger = nativePreviewSourceTrigger(modal);

    const zoomGroup = document.createElement("div");
    zoomGroup.className = "turtle-native-preview-zoom";
    const zoomOut = nativePreviewZoomButton("out", "缩小图片", "−");
    const zoomReset = nativePreviewZoomButton("reset", "适应窗口", "适应");
    const zoomIn = nativePreviewZoomButton("in", "放大图片", "+");
    zoomGroup.append(zoomOut, zoomReset, zoomIn);

    const actionGroup = document.createElement("div");
    actionGroup.className = "turtle-native-preview-actions";
    actionGroup.append(downloadButton, closeButton);
    toolbar.replaceChildren(zoomGroup, actionGroup);

    zoomOut.addEventListener("click", (event) => {
      event.stopPropagation();
      if (nativePreviewScale(modal) <= 1.35) resetNativeImagePreview(modal);
      else dispatchNativePreviewZoom(modal, 512);
    });
    zoomReset.addEventListener("click", (event) => {
      event.stopPropagation();
      resetNativeImagePreview(modal);
    });
    zoomIn.addEventListener("click", (event) => {
      event.stopPropagation();
      dispatchNativePreviewZoom(modal, -512);
    });
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeButton.click();
    });
    modal.addEventListener(
      "dblclick",
      (event) => {
        if (!(event.target instanceof Element) || !event.target.closest("img")) return;
        if (nativePreviewScale(modal) <= 1.05) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        resetNativeImagePreview(modal);
      },
      true,
    );
  };

  const enhanceNativeImagePreviews = () => {
    document.querySelectorAll("div.modal").forEach((modal) => {
      if (
        window.getComputedStyle(modal).position === "fixed" &&
        modal.querySelector("img.object-scale-down")
      ) {
        enhanceNativeImagePreview(modal);
      }
    });
  };

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  const bytes = (value) => {
    let amount = Math.max(0, Number(value) || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
  };

  const gb = (value) => ((Number(value) || 0) / 1024 ** 3).toFixed(2).replace(/\.00$/, "");
  const toBytes = (value) => Math.max(0, Math.round((Number(value) || 0) * 1024 ** 3));

  const localDateKey = (timestamp) => {
    const date = new Date(Number(timestamp) * 1000);
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  };

  const dateGroupLabel = (timestamp) => {
    const date = new Date(Number(timestamp) * 1000);
    const today = new Date();
    const dateStart = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const dayDifference = Math.round((todayStart.getTime() - dateStart.getTime()) / 86_400_000);
    if (dayDifference === 0) return "今天";
    if (dayDifference === 1) return "昨天";
    return date.toLocaleDateString("zh-CN", {
      ...(date.getFullYear() === today.getFullYear() ? {} : { year: "numeric" }),
      month: "long",
      day: "numeric",
      weekday: "short",
    });
  };

  const timeText = (timestamp) =>
    new Date(Number(timestamp) * 1000).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });

  const quotaPresentation = (quota = {}) => {
    const quotaBytes = Math.max(0, Number(quota.quota_bytes) || 0);
    const usedBytes = Math.max(0, Number(quota.used_bytes) || 0);
    const remainingBytes = Math.max(
      0,
      Number.isFinite(Number(quota.remaining_bytes)) ? Number(quota.remaining_bytes) : quotaBytes - usedBytes,
    );
    const ratio = Math.min(100, Math.max(0, quotaBytes ? (usedBytes / quotaBytes) * 100 : usedBytes ? 100 : 0));
    const percent = ratio === 0 ? "0%" : ratio < 0.1 ? "<0.1%" : ratio < 1 ? `${ratio.toFixed(1)}%` : `${Math.round(ratio)}%`;
    return { quotaBytes, usedBytes, remainingBytes, ratio, percent };
  };

  const quotaOverview = (quota = null, total = null) => {
    const ready = Boolean(quota && Number.isFinite(Number(quota.quota_bytes)));
    const values = quotaPresentation(quota || {});
    const tier = ready ? `${escapeHtml(quota.tier || "默认")} 会员空间` : "正在读取空间额度";
    return `<section class="turtle-space-overview" data-quota-ready="${String(ready)}" aria-label="存储用量">
      <div class="turtle-quota-ring" style="--turtle-usage:${values.ratio * 3.6}deg">
        <div><strong>${ready ? values.percent : "—"}</strong><span>已使用</span></div>
      </div>
      <div class="turtle-quota-summary">
        <span class="turtle-overline">${tier}</span>
        <div class="turtle-quota-value"><strong>${ready ? bytes(values.usedBytes) : "读取中"}</strong><span>${ready ? `/ ${bytes(values.quotaBytes)}` : ""}</span></div>
        <div class="turtle-quota-track"><i style="width:${values.ratio}%"></i></div>
        <div class="turtle-quota-meta">
          <span><small>剩余</small><strong>${ready ? bytes(values.remainingBytes) : "—"}</strong></span>
          <span><small>文件数</small><strong>${total == null ? "读取中" : Number(total)}</strong></span>
          <span><small>隐私</small><strong>仅自己</strong></span>
        </div>
      </div>
    </section>`;
  };

  const renderSpaceShell = (quota = null) => {
    const body = modalBody();
    if (!body) return;
    body.innerHTML = `${quotaOverview(quota)}
      <div class="turtle-space-toolbar">
        <div class="turtle-space-heading"><strong>媒体文件</strong><span>正在整理最新内容</span></div>
        <div class="turtle-kind-filters" aria-hidden="true">
          ${["全部", "图片", "视频", "文件"].map((label) => `<button type="button" disabled>${label}</button>`).join("")}
        </div>
      </div>
      <div class="turtle-space-loading-status" role="status"><span></span>正在加载文件列表…</div>
      <div class="turtle-file-grid turtle-file-skeleton" aria-hidden="true">
        ${Array.from({ length: 8 }, () => "<span></span>").join("")}
      </div>`;
  };

  const mediaIcon = (kind, className = "") => {
    const paths = {
      image: '<rect x="3" y="4" width="18" height="16" rx="3"></rect><circle cx="9" cy="10" r="2"></circle><path d="m5.5 17 4.2-4.2 3.1 3.1 2-2 3.7 3.1"></path>',
      video: '<rect x="3" y="5" width="14" height="14" rx="3"></rect><path d="m17 10 4-2v8l-4-2z"></path><path d="m8.5 9 4.5 3-4.5 3z"></path>',
      file: '<path d="M7 3h7l4 4v14H7z"></path><path d="M14 3v5h5"></path><path d="M10 13h5M10 17h5"></path>',
      cloud: '<path d="M7.5 18.5h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.55 8.8 4.8 4.8 0 0 0 7.5 18.5Z"></path><path d="m9.5 14 2.5-2.5 2.5 2.5M12 11.5V17"></path>',
      storage: '<ellipse cx="12" cy="5.5" rx="8" ry="3"></ellipse><path d="M4 5.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"></path><path d="M4 11.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"></path>',
      server: '<rect x="3" y="4" width="18" height="7" rx="2"></rect><rect x="3" y="13" width="18" height="7" rx="2"></rect><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"></path>',
      chat: '<path d="M5 5.5h14a2.5 2.5 0 0 1 2.5 2.5v7a2.5 2.5 0 0 1-2.5 2.5h-7l-4.5 3v-3H5A2.5 2.5 0 0 1 2.5 15V8A2.5 2.5 0 0 1 5 5.5Z"></path><path d="M7 10h10M7 13.5h6"></path>',
      warning: '<path d="M12 3 2.8 19h18.4L12 3Z"></path><path d="M12 9v4M12 16.5h.01"></path>',
      trash: '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"></path><path d="M10 11v5M14 11v5"></path>',
    };
    return `<svg class="${escapeHtml(className)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind] || paths.file}</svg>`;
  };

  const dismissConfirm = () => {
    const overlay = document.querySelector("#turtle-confirm-overlay");
    if (typeof overlay?._finish === "function") overlay._finish(false);
    else overlay?.remove();
  };

  const confirmAction = ({
    title,
    message,
    subject = "",
    confirmLabel = "确认",
    cancelLabel = "取消",
  }) =>
    new Promise((resolve) => {
      dismissConfirm();
      const returnFocus = document.activeElement;
      const overlay = document.createElement("div");
      overlay.id = "turtle-confirm-overlay";
      overlay.innerHTML = `
        <section id="turtle-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="turtle-confirm-title" aria-describedby="turtle-confirm-message">
          <span class="turtle-confirm-icon">${mediaIcon("trash")}</span>
          <div class="turtle-confirm-copy">
            <span class="turtle-confirm-eyebrow">永久删除</span>
            <h3 id="turtle-confirm-title">${escapeHtml(title)}</h3>
            ${subject ? `<strong class="turtle-confirm-subject" title="${escapeHtml(subject)}">${escapeHtml(subject)}</strong>` : ""}
            <p id="turtle-confirm-message">${escapeHtml(message)}</p>
          </div>
          <div class="turtle-confirm-actions">
            <button type="button" data-confirm-cancel>${escapeHtml(cancelLabel)}</button>
            <button type="button" data-confirm-accept>${escapeHtml(confirmLabel)}</button>
          </div>
        </section>`;

      let settled = false;
      const finish = (accepted) => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKeydown, true);
        overlay.remove();
        if (returnFocus?.isConnected) returnFocus.focus();
        resolve(Boolean(accepted));
      };
      const focusable = Array.from(overlay.querySelectorAll("button"));
      const onKeydown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopImmediatePropagation();
          finish(false);
          return;
        }
        if (event.key !== "Tab" || focusable.length < 2) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      };

      overlay._finish = finish;
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay || event.target.closest("[data-confirm-cancel]")) finish(false);
        else if (event.target.closest("[data-confirm-accept]")) finish(true);
      });
      document.body.append(overlay);
      document.addEventListener("keydown", onKeydown, true);
      requestAnimationFrame(() => overlay.querySelector("[data-confirm-cancel]")?.focus());
    });

  const toast = (message, tone = "info") => {
    document.querySelector("#turtle-storage-toast")?.remove();
    const element = document.createElement("div");
    element.id = "turtle-storage-toast";
    element.dataset.tone = tone;
    element.textContent = message;
    document.body.append(element);
    setTimeout(() => element.remove(), 3200);
  };

  const revokePreviews = () => {
    previewObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    previewObjectUrls.clear();
  };

  const trackedAbortController = () => {
    const controller = new AbortController();
    spaceAbortControllers.add(controller);
    controller.signal.addEventListener("abort", () => spaceAbortControllers.delete(controller), { once: true });
    return controller;
  };

  const releaseAbortController = (controller) => {
    if (controller) spaceAbortControllers.delete(controller);
  };

  const abortSpaceRequests = () => {
    thumbnailQueue = [];
    spaceAbortControllers.forEach((controller) => controller.abort());
    spaceAbortControllers.clear();
  };

  const disconnectSpaceObservers = () => {
    thumbnailObserver?.disconnect();
    sentinelObserver?.disconnect();
    thumbnailObserver = null;
    sentinelObserver = null;
  };

  const closePreview = () => {
    const preview = document.querySelector("#turtle-media-preview");
    preview?._abortController?.abort?.();
    preview?._cleanupViewport?.();
    preview?.querySelectorAll("img, video").forEach((media) => {
      media.removeAttribute("src");
      media.load?.();
    });
    preview?.remove();
  };

  const resetSpaceRuntime = () => {
    spaceSessionGeneration += 1;
    disconnectSpaceObservers();
    abortSpaceRequests();
    thumbnailQueue = [];
    spaceSession = null;
  };

  const closeModal = () => {
    dismissConfirm();
    closePreview();
    resetSpaceRuntime();
    revokePreviews();
    document.querySelector("#turtle-storage-overlay")?.remove();
    document.removeEventListener("keydown", onEscape);
    document.documentElement.classList.remove("turtle-storage-open");
    lastFocusedElement?.focus?.();
    lastFocusedElement = null;
  };

  const onEscape = (event) => {
    if (event.key !== "Escape") return;
    if (document.querySelector("#turtle-confirm-overlay")) return;
    if (document.querySelector("#turtle-media-preview")) closePreview();
    else closeModal();
  };

  const modalBody = () => document.querySelector("#turtle-storage-body");

  const renderLoading = (label = "正在读取空间…") => {
    const body = modalBody();
    if (body) body.innerHTML = `<div class="turtle-storage-loading"><span></span>${escapeHtml(label)}</div>`;
  };

  const dialogCopy = {
    space: {
      icon: "cloud",
      eyebrow: "MEDIA LIBRARY",
      title: "我的空间",
      description: "集中查看聊天上传与模型生成的媒体",
    },
    admin: {
      icon: "storage",
      eyebrow: "ADMIN · STORAGE",
      title: "存储管理",
      description: "配置对象存储、会员空间与用户额度",
    },
    chat: {
      icon: "chat",
      eyebrow: "ADMIN · CHAT",
      title: "聊天管理",
      description: "分配模型档位、次数窗口与思考权限",
    },
    project: {
      icon: "server",
      eyebrow: "PROJECT API",
      title: "API 密钥",
      description: "管理项目密钥、API 价格估算与 GPT 调用记录",
    },
  };

  const createDialog = (view) => {
    dismissConfirm();
    if (document.querySelector("#turtle-storage-overlay")) {
      closePreview();
      resetSpaceRuntime();
      revokePreviews();
      document.querySelector("#turtle-storage-overlay")?.remove();
    }
    document.removeEventListener("keydown", onEscape);
    const copy = dialogCopy[view] || dialogCopy.space;
    lastFocusedElement = document.activeElement;
    const overlay = document.createElement("div");
    overlay.id = "turtle-storage-overlay";
    overlay.innerHTML = `
      <section id="turtle-storage-dialog" data-view="${escapeHtml(view)}" role="dialog" aria-modal="true" aria-labelledby="turtle-storage-title">
        <header class="turtle-storage-header">
          <div class="turtle-storage-brand">
            <span class="turtle-storage-brand-icon">${mediaIcon(copy.icon)}</span>
            <div>
              <span class="turtle-storage-eyebrow">${escapeHtml(copy.eyebrow)}</span>
              <h2 id="turtle-storage-title">${escapeHtml(copy.title)}</h2>
              <p>${escapeHtml(copy.description)}</p>
            </div>
          </div>
          <div class="turtle-storage-header-actions">
            <button type="button" class="turtle-icon-button" data-storage-close aria-label="关闭">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"></path></svg>
            </button>
          </div>
        </header>
        <div id="turtle-storage-body"></div>
      </section>`;
    document.body.append(overlay);
    document.documentElement.classList.add("turtle-storage-open");
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay || event.target.closest("[data-storage-close]")) closeModal();
    });
    document.addEventListener("keydown", onEscape);
    overlay.querySelector("[data-storage-close]")?.focus();
    return overlay;
  };

  const openModal = async () => {
    createDialog("space");
    const cached = cachedSpacePage("all");
    renderSpaceShell(cached?.data?.quota || capabilityCache?.quota || null);
    await renderSpace("all");
  };

  const openAdminModal = async (view) => {
    const target = ["admin", "chat"].includes(view) ? view : "admin";
    createDialog(target);
    renderLoading(target === "chat" ? "正在读取聊天权限…" : "正在读取存储设置…");
    if (target === "chat") await renderChatAdmin();
    else await renderAdmin();
  };

  const projectNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  const projectUsd = (microUsd) => {
    if (microUsd == null) return "—";
    const value = Number(microUsd || 0) / 1_000_000;
    return `$${value < 0.01 ? value.toFixed(6) : value.toFixed(4)}`;
  };
  const projectRate = (value) =>
    value == null ? "—" : `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 4 })}`;
  const projectUsageSource = (source) =>
    source === "upstream_reported"
      ? "上游 usage"
      : source === "locally_estimated"
        ? "本地估算"
        : source === "not_charged"
          ? "未计费"
          : "请求兜底";

  const projectDateTime = (timestamp) =>
    timestamp
      ? new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })
      : "尚未调用";

  const projectUsagePath = () => {
    const query = new URLSearchParams({
      hours: String(projectPanelState.hours),
      model: "gpt-5-web",
      limit: String(projectPanelState.limit),
      offset: String(projectPanelState.offset),
    });
    if (projectPanelState.keyId) query.set("key_id", projectPanelState.keyId);
    if (projectPanelState.outcome) query.set("outcome", projectPanelState.outcome);
    return `/usage?${query.toString()}`;
  };

  const projectTabs = () => `
    <nav class="turtle-storage-tabs turtle-project-tabs" aria-label="API 密钥功能">
      ${[
        ["overview", "概览"],
        ["keys", "API 密钥"],
        ["records", "调用记录"],
      ].map(([value, label]) => `<button type="button" data-project-tab="${value}" data-active="${String(projectPanelState.tab === value)}">${label}</button>`).join("")}
    </nav>`;

  const renderProjectOverview = () => {
    const body = modalBody();
    if (!body) return;
    const keys = projectPanelState.bundle?.keys || [];
    const totals = projectPanelState.usage?.totals || {};
    const activeKeys = keys.filter((item) => item.status === "active").length;
    const priceProfiles = Object.values(projectPanelState.usage?.price_profiles || {});
    const lifetime = keys.reduce(
      (sum, item) => ({
        requests: sum.requests + Number(item.request_count || 0),
        tokens: sum.tokens + Number(item.total_tokens || 0),
        official: sum.official + Number(item.total_official_cost_microusd || 0),
        actual: sum.actual + Number(item.total_actual_cost_microusd || 0),
      }),
      { requests: 0, tokens: 0, official: 0, actual: 0 },
    );
    const multiplier = Number(projectPanelState.usage?.pricing_config?.cost_multiplier ?? 1);
    body.innerHTML = `
      <section class="turtle-project-overview-grid">
        <article><span>累计实际消耗</span><strong>${projectUsd(lifetime.actual)}</strong><small>官方参考 ${projectUsd(lifetime.official)}</small></article>
        <article><span>累计请求</span><strong>${projectNumber(lifetime.requests)}</strong><small>${projectNumber(totals.errors)} 条当前范围异常</small></article>
        <article><span>记录 Token</span><strong>${projectNumber(lifetime.tokens)}</strong><small>上游上报或本地估算</small></article>
        <article><span>有效密钥</span><strong>${projectNumber(activeKeys)} / ${projectNumber(projectPanelState.bundle?.max_keys || 5)}</strong><small>管理员设置的账号上限</small></article>
      </section>
      <section class="turtle-project-endpoint">
        <div><span class="turtle-overline">OPENAI COMPATIBLE</span><h3>项目调用地址</h3><p>每个项目使用独立密钥，便于追踪美元消耗、Token 和异常来源。</p></div>
        <dl>
          <div><dt>Base URL</dt><dd><code>${escapeHtml(`${window.location.origin}/api/project/v1`)}</code></dd></div>
          <div><dt>模型</dt><dd><code>gpt-5-web</code></dd></div>
          <div><dt>当前范围</dt><dd>最近 ${projectPanelState.hours === 24 ? "24 小时" : projectPanelState.hours === 168 ? "7 天" : "30 天"} · ${projectNumber(totals.requests)} 次 GPT 请求</dd></div>
        </dl>
      </section>
      <section class="turtle-project-rate-card">
        <div><span class="turtle-overline">OPENAI STANDARD · 每百万 Token</span><h3>官方 API 参考单价</h3><p>价格快照 ${escapeHtml(projectPanelState.usage?.price_card_version || "—")} · 当前实际倍率 ${multiplier.toFixed(2)}×；历史记录保存当时单价和倍率。</p></div>
        <div class="turtle-project-rate-grid">${priceProfiles.map((profile) => `<article><strong>${escapeHtml(profile.model)}</strong><span>输入 ${projectRate(profile.input_usd_per_million)}</span><span>缓存 ${projectRate(profile.cached_input_usd_per_million)}</span>${profile.cache_write_usd_per_million == null ? "" : `<span>写入 ${projectRate(profile.cache_write_usd_per_million)}</span>`}<span>输出 ${projectRate(profile.output_usd_per_million)}</span></article>`).join("")}</div>
      </section>
      <div class="turtle-project-note"><span>$</span><p><strong>这是美元消耗估算，不是真实账单</strong>先按官方参考单价计算，再应用管理员设置的倍率。上游没有 usage 时会显示“本地估算”。</p></div>`;
  };

  const renderProjectKeys = () => {
    const body = modalBody();
    if (!body) return;
    const keys = projectPanelState.bundle?.keys || [];
    const activeKeys = keys.filter((item) => item.status === "active").length;
    const maxKeys = Number(projectPanelState.bundle?.max_keys) || 5;
    const atLimit = activeKeys >= maxKeys;
    body.innerHTML = `
      <section class="turtle-project-section-heading"><div><span class="turtle-overline">PROJECT CREDENTIALS</span><h3>API 密钥</h3><p>密钥明文只在创建时显示一次，请按项目分别保存。</p></div><strong>${projectNumber(activeKeys)} / ${projectNumber(maxKeys)}</strong></section>
      ${projectPanelState.newSecret ? `<section class="turtle-project-secret"><div><span>新密钥已创建</span><strong>请立即复制，关闭面板后无法再次查看</strong></div><code>${escapeHtml(projectPanelState.newSecret)}</code><button type="button" data-project-copy-secret>复制密钥</button></section>` : ""}
      <form class="turtle-project-create" data-project-key-form>
        <label><span>项目名称</span><input name="name" maxlength="80" required ${atLimit ? "disabled" : ""} placeholder="例如：小说生成器"/></label>
        <button type="submit" ${atLimit ? "disabled" : ""}>${atLimit ? "已达上限" : "创建密钥"}</button>
      </form>
      <div class="turtle-project-key-list">
        ${keys.length ? keys.map((item) => `<article data-status="${escapeHtml(item.status)}">
          <div><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.key_prefix)}••••••••</code><small>${projectNumber(item.request_count)} 次调用 · ${projectUsd(item.total_actual_cost_microusd)} · ${escapeHtml(projectDateTime(item.last_used_at))}</small></div>
          ${item.status === "active" ? `<button type="button" data-project-revoke="${escapeHtml(item.id)}" data-project-name="${escapeHtml(item.name)}">撤销</button>` : "<span>已撤销</span>"}
        </article>`).join("") : '<div class="turtle-project-empty"><strong>还没有 API 密钥</strong><span>为小说项目或其他程序创建第一把独立密钥。</span></div>'}
      </div>`;
  };

  const renderProjectRecords = () => {
    const body = modalBody();
    if (!body) return;
    const keys = projectPanelState.bundle?.keys || [];
    const usage = projectPanelState.usage || {};
    const pagination = usage.pagination || {};
    const records = usage.recent || [];
    const currentPage = Math.floor(Number(pagination.offset || 0) / Number(pagination.limit || projectPanelState.limit)) + 1;
    body.innerHTML = `
      <section class="turtle-project-section-heading"><div><span class="turtle-overline">GPT USAGE</span><h3>调用记录</h3><p>只展示 GPT 项目调用，不保存提示词或回答内容。</p></div><strong>${projectNumber(pagination.total)} 条</strong></section>
      <div class="turtle-project-record-filters">
        <label><span>时间范围</span><select data-project-record-filter="hours"><option value="24" ${projectPanelState.hours === 24 ? "selected" : ""}>24 小时</option><option value="168" ${projectPanelState.hours === 168 ? "selected" : ""}>7 天</option><option value="720" ${projectPanelState.hours === 720 ? "selected" : ""}>30 天</option></select></label>
        <label><span>项目</span><select data-project-record-filter="key"><option value="">全部项目</option>${keys.map((item) => `<option value="${escapeHtml(item.id)}" ${projectPanelState.keyId === item.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>
        <label><span>调用结果</span><select data-project-record-filter="outcome"><option value="">全部状态</option><option value="success" ${projectPanelState.outcome === "success" ? "selected" : ""}>成功</option><option value="error" ${projectPanelState.outcome === "error" ? "selected" : ""}>失败</option><option value="cancelled" ${projectPanelState.outcome === "cancelled" ? "selected" : ""}>已取消</option></select></label>
      </div>
      <div class="turtle-project-table-wrap"><table><thead><tr><th>时间 / 项目</th><th>路由 / 计价模型</th><th>结果</th><th>Token 明细</th><th>实际消耗</th><th>耗时</th></tr></thead><tbody>
        ${records.length ? records.map((item) => `<tr><td><strong>${escapeHtml(projectDateTime(item.created_at))}</strong><small>${escapeHtml(item.project_name || "未知项目")} · ${escapeHtml(item.request_id)}</small></td><td><strong>${escapeHtml(item.pricing_profile || "GPT")}</strong><small>${escapeHtml(item.route || "默认")}</small></td><td data-outcome="${escapeHtml(item.outcome)}"><strong>${item.outcome === "success" ? "成功" : item.outcome === "cancelled" ? "已取消" : "失败"}</strong><small>HTTP ${projectNumber(item.status_code)}</small></td><td><strong>入 ${item.prompt_tokens == null ? "—" : projectNumber(item.prompt_tokens)} · 出 ${item.completion_tokens == null ? "—" : projectNumber(item.completion_tokens)}</strong><small>缓存 ${projectNumber(item.cached_tokens)} · ${projectUsageSource(item.usage_source)}</small></td><td><strong>${projectUsd(item.actual_cost_microusd)}</strong><small>官方 ${projectUsd(item.official_cost_microusd)} × ${Number(item.cost_multiplier ?? 1).toFixed(2)}</small></td><td>${projectNumber(item.latency_ms)} ms</td></tr>`).join("") : '<tr><td colspan="6"><div class="turtle-project-empty"><strong>当前条件没有调用记录</strong><span>调整筛选条件后再查看。</span></div></td></tr>'}
      </tbody></table></div>
      <footer class="turtle-project-pagination"><span>第 ${currentPage} 页 · 共 ${projectNumber(pagination.total)} 条</span><div><button type="button" data-project-page="-1" ${projectPanelState.offset <= 0 ? "disabled" : ""}>上一页</button><button type="button" data-project-page="1" ${pagination.has_more ? "" : "disabled"}>下一页</button></div></footer>`;
  };

  const renderProjectPanel = () => {
    document.querySelectorAll("[data-project-tab]").forEach((button) => {
      button.dataset.active = String(button.dataset.projectTab === projectPanelState.tab);
    });
    if (projectPanelState.tab === "keys") renderProjectKeys();
    else if (projectPanelState.tab === "records") renderProjectRecords();
    else renderProjectOverview();
    const body = modalBody();
    if (!body) return;
    body.onclick = async (event) => {
      const copy = event.target.closest("[data-project-copy-secret]");
      if (copy) {
        await navigator.clipboard.writeText(projectPanelState.newSecret);
        copy.textContent = "已复制";
        return;
      }
      const revoke = event.target.closest("[data-project-revoke]");
      if (revoke) {
        const confirmed = await confirmAction({
          title: "撤销 API 密钥",
          subject: revoke.dataset.projectName || "",
          message: "撤销后该项目会立即停止调用且不能恢复，历史记录仍会保留。",
          confirmLabel: "确认撤销",
        });
        if (!confirmed) return;
        try {
          await projectApiFetch(`/keys/${encodeURIComponent(revoke.dataset.projectRevoke)}`, { method: "DELETE" });
          projectPanelState.newSecret = "";
          await loadProjectPanel();
          toast("API 密钥已撤销");
        } catch (error) {
          toast(error?.message || "撤销失败", "error");
        }
        return;
      }
      const pager = event.target.closest("[data-project-page]");
      if (pager) {
        projectPanelState.offset = Math.max(0, projectPanelState.offset + Number(pager.dataset.projectPage) * projectPanelState.limit);
        await loadProjectUsage();
      }
    };
    body.onchange = async (event) => {
      const filter = event.target.closest("[data-project-record-filter]");
      if (!filter) return;
      if (filter.dataset.projectRecordFilter === "hours") projectPanelState.hours = Number(filter.value) || 24;
      else if (filter.dataset.projectRecordFilter === "key") projectPanelState.keyId = filter.value;
      else if (filter.dataset.projectRecordFilter === "outcome") projectPanelState.outcome = filter.value;
      projectPanelState.offset = 0;
      await loadProjectUsage();
    };
    body.onsubmit = async (event) => {
      const form = event.target.closest("[data-project-key-form]");
      if (!form) return;
      event.preventDefault();
      const name = String(new FormData(form).get("name") || "").trim();
      if (!name) return;
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;
      try {
        const created = await projectApiFetch("/keys", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        projectPanelState.newSecret = created.api_key;
        await loadProjectPanel();
        toast("API 密钥已创建");
      } catch (error) {
        toast(error?.message || "创建失败", "error");
        submit.disabled = false;
      }
    };
  };

  const loadProjectUsage = async () => {
    try {
      projectPanelState.usage = await projectApiFetch(projectUsagePath());
      renderProjectPanel();
    } catch (error) {
      const body = modalBody();
      if (body) body.innerHTML = `<div class="turtle-storage-error"><span>${escapeHtml(error?.message || "调用记录读取失败")}</span><button type="button" data-project-retry>重新加载</button></div>`;
      body?.querySelector("[data-project-retry]")?.addEventListener("click", () => void loadProjectPanel());
    }
  };

  const loadProjectPanel = async () => {
    const body = modalBody();
    if (!body) return;
    try {
      const bundle = await projectApiFetch(`/me?hours=${projectPanelState.hours}`);
      if (!bundle.enabled) throw new Error("管理员尚未为当前账号开通 API 密钥权限");
      projectPanelState.bundle = bundle;
      projectPanelState.usage = await projectApiFetch(projectUsagePath());
      renderProjectPanel();
    } catch (error) {
      if (body) body.innerHTML = `<div class="turtle-storage-error"><span>${escapeHtml(error?.message || "API 密钥面板读取失败")}</span><button type="button" data-project-retry>重新加载</button></div>`;
      body?.querySelector("[data-project-retry]")?.addEventListener("click", () => void loadProjectPanel());
    }
  };

  const openProjectApiModal = async () => {
    projectPanelState = {
      ...projectPanelState,
      tab: "overview",
      offset: 0,
      newSecret: "",
    };
    const overlay = createDialog("project");
    const actions = overlay.querySelector(".turtle-storage-header-actions");
    actions?.insertAdjacentHTML("afterbegin", projectTabs());
    actions?.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-project-tab]");
      if (!tab) return;
      projectPanelState.tab = tab.dataset.projectTab;
      renderProjectPanel();
    });
    renderLoading("正在读取 API 密钥与调用记录…");
    await loadProjectPanel();
  };

  const resolveMediaSource = async (fileId, variant, controller = null) => {
    const cacheKey = `${fileId}:${variant}`;
    const cached = mediaUrlCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now() + MEDIA_URL_EXPIRY_SKEW_MS) return cached.url;
    if (cached) mediaUrlCache.delete(cacheKey);

    const ownedController = controller == null;
    const requestController = controller || trackedAbortController();
    try {
      const response = await apiFetch(
        `/files/${encodeURIComponent(fileId)}/url?variant=${encodeURIComponent(variant)}`,
        { signal: requestController.signal },
      );
      if (!response.ok) throw new Error(await errorMessage(response, "预览地址获取失败"));
      const data = await response.json();
      if (data.direct) {
        const expiresIn = Math.max(1, Number(data.expires_in) || 300);
        mediaUrlCache.set(cacheKey, { url: data.url, expiresAt: Date.now() + expiresIn * 1000 });
        return data.url;
      }

      const fileResponse = await originalFetch(data.url, {
        headers: authHeaders(),
        credentials: "same-origin",
        signal: requestController.signal,
      });
      if (!fileResponse.ok) throw new Error("本地文件预览读取失败");
      const objectUrl = URL.createObjectURL(await fileResponse.blob());
      previewObjectUrls.add(objectUrl);
      return objectUrl;
    } finally {
      if (ownedController) releaseAbortController(requestController);
    }
  };

  const loadThumbnail = async (node, generation) => {
    if (!node?.isConnected || generation !== spaceSession?.generation) return;
    const controller = trackedAbortController();
    node.dataset.thumbnailState = "loading";
    try {
      const source = await resolveMediaSource(node.dataset.thumbnailId, "thumbnail", controller);
      await new Promise((resolve) => {
        let settled = false;
        const image = new Image();
        const finish = (loaded) => {
          if (settled) return;
          settled = true;
          if (loaded && node.isConnected && generation === spaceSession?.generation) {
            image.alt = "";
            image.decoding = "async";
            image.fetchPriority = "low";
            node.replaceChildren(image);
            node.dataset.thumbnailState = "loaded";
          } else if (node.isConnected && generation === spaceSession?.generation) {
            node.dataset.thumbnailState = "error";
          }
          resolve();
        };
        image.onload = () => finish(true);
        image.onerror = () => finish(false);
        controller.signal.addEventListener(
          "abort",
          () => {
            image.removeAttribute("src");
            finish(false);
          },
          { once: true },
        );
        image.src = source;
      });
    } catch (error) {
      if (error?.name !== "AbortError" && node.isConnected && generation === spaceSession?.generation) {
        node.dataset.thumbnailState = "error";
      }
    } finally {
      releaseAbortController(controller);
    }
  };

  const drainThumbnailQueue = () => {
    while (thumbnailActive < THUMBNAIL_CONCURRENCY && thumbnailQueue.length) {
      const job = thumbnailQueue.shift();
      if (!job.node?.isConnected || job.generation !== spaceSession?.generation) continue;
      thumbnailActive += 1;
      void loadThumbnail(job.node, job.generation).finally(() => {
        thumbnailActive = Math.max(0, thumbnailActive - 1);
        drainThumbnailQueue();
      });
    }
  };

  const enqueueThumbnail = (node, generation) => {
    if (!node?.isConnected || node.dataset.thumbnailState !== "idle") return;
    node.dataset.thumbnailState = "queued";
    thumbnailQueue.push({ node, generation });
    drainThumbnailQueue();
  };

  const observeSpaceThumbnails = (session) => {
    const body = modalBody();
    if (!body || session !== spaceSession) return;
    const nodes = body.querySelectorAll('[data-thumbnail-state="idle"]');
    if (typeof IntersectionObserver !== "function") {
      nodes.forEach((node) => enqueueThumbnail(node, session.generation));
      return;
    }
    if (!thumbnailObserver) {
      thumbnailObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            thumbnailObserver?.unobserve(entry.target);
            enqueueThumbnail(entry.target, session.generation);
          });
        },
        { root: body, rootMargin: "520px 0px", threshold: 0.01 },
      );
    }
    nodes.forEach((node) => thumbnailObserver.observe(node));
  };

  const emptyCollection = (kind) => {
    const emptyTitle = kind === "all"
      ? "空间还是空的"
      : `还没有${kind === "image" ? "图片" : kind === "video" ? "视频" : "文件"}`;
    return `<div class="turtle-file-grid" data-empty="true"><div class="turtle-storage-empty">
      <span class="turtle-empty-icon">${mediaIcon(kind === "all" ? "cloud" : kind)}</span>
      <strong>${emptyTitle}</strong>
      <span>聊天中上传或生成的内容会自动整理到这里。</span>
      <small>支持图片、视频和普通附件，文件只对当前账号可见。</small>
    </div></div>`;
  };

  const fileCard = (file) => {
    const previewable = file.kind === "image" || file.kind === "video";
    const previewAttributes = previewable
      ? `type="button" data-file-preview="${escapeHtml(file.id)}" aria-label="预览 ${escapeHtml(file.name)}"`
      : "";
    const thumbnailAttributes = file.kind === "image" && file.thumbnail_ready
      ? `data-thumbnail-id="${escapeHtml(file.id)}" data-thumbnail-state="idle"`
      : "";
    const previewTag = previewable ? "button" : "div";
    const downloadLabel = file.kind === "image" ? "下载原图" : "下载原文件";
    return `<article class="turtle-file-card" data-file-id="${escapeHtml(file.id)}">
      <${previewTag} class="turtle-file-preview" ${previewAttributes} ${thumbnailAttributes}>
        <span class="turtle-file-fallback">${mediaIcon(file.kind)}<small>${file.kind === "image" ? "图片" : file.kind === "video" ? "视频" : "文件"}</small></span>
      </${previewTag}>
      <div class="turtle-file-info">
        <strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong>
        <span>${bytes(file.size)} · ${escapeHtml(timeText(file.created_at))}</span>
      </div>
      <div class="turtle-file-actions">
        <button type="button" data-file-download="${escapeHtml(file.id)}" aria-label="下载 ${escapeHtml(file.name)}">${downloadLabel}</button>
        <button type="button" data-file-delete="${escapeHtml(file.id)}" aria-label="删除 ${escapeHtml(file.name)}">删除</button>
      </div>
    </article>`;
  };

  const appendSpaceItems = (session, items) => {
    const body = modalBody();
    const collection = body?.querySelector(".turtle-file-collection");
    if (!collection || session !== spaceSession) return;
    if (collection.dataset.empty === "true") collection.innerHTML = "";
    collection.dataset.empty = "false";

    const sorted = [...(items || [])].sort(
      (left, right) =>
        Number(right.created_at || 0) - Number(left.created_at || 0) ||
        String(right.id).localeCompare(String(left.id)),
    );
    sorted.forEach((file) => {
      if (session.itemById.has(file.id)) return;
      session.items.push(file);
      session.itemById.set(file.id, file);
      const dateKey = localDateKey(file.created_at);
      let group = collection.querySelector(`[data-date-key="${dateKey}"]`);
      if (!group) {
        group = document.createElement("section");
        group.className = "turtle-date-group";
        group.dataset.dateKey = dateKey;
        group.innerHTML = `<header class="turtle-date-heading">
          <strong>${escapeHtml(dateGroupLabel(file.created_at))}</strong><span data-date-count>0 项</span>
        </header><div class="turtle-file-grid"></div>`;
        collection.append(group);
      }
      group.querySelector(".turtle-file-grid")?.insertAdjacentHTML("beforeend", fileCard(file));
      const count = group.querySelectorAll(".turtle-file-card").length;
      const countNode = group.querySelector("[data-date-count]");
      if (countNode) countNode.textContent = `${count} 项`;
    });
    observeSpaceThumbnails(session);
  };

  const updateSpaceSummary = (session) => {
    const body = modalBody();
    if (!body || session !== spaceSession) return;
    const overview = body.querySelector(".turtle-space-overview");
    if (overview) overview.outerHTML = quotaOverview(session.quota, session.total);
    const summary = body.querySelector(".turtle-space-heading span");
    if (summary) {
      summary.textContent = session.total ? `共 ${session.total} 项` : "按类型自动归档";
    }
    const launcher = document.querySelector("#turtle-space-launcher");
    if (launcher && session.quota) updateLauncherUsage(launcher, { quota: session.quota });
  };

  const updateSpaceSentinel = (session) => {
    const sentinel = modalBody()?.querySelector(".turtle-space-sentinel");
    if (!sentinel || session !== spaceSession) return;
    sentinel.hidden = session.items.length === 0 && !session.hasMore;
    if (session.loading) {
      sentinel.dataset.state = "loading";
      sentinel.innerHTML = "<span></span><strong>正在加载更多缩略图…</strong>";
    } else if (session.loadError) {
      sentinel.dataset.state = "error";
      sentinel.innerHTML = `<strong>${escapeHtml(session.loadError)}</strong><button type="button" data-space-retry>重试</button>`;
    } else if (session.hasMore) {
      sentinel.dataset.state = "idle";
      sentinel.innerHTML = "<span></span><strong>继续向下滚动加载</strong>";
    } else {
      sentinel.dataset.state = "done";
      sentinel.innerHTML = `<strong>已加载全部 ${session.items.length} 项</strong>`;
    }
  };

  const maybeContinueInfiniteLoad = (session) => {
    window.requestAnimationFrame(() => {
      const body = modalBody();
      const sentinel = body?.querySelector(".turtle-space-sentinel");
      if (!body || !sentinel || session !== spaceSession || !session.hasMore || session.loading) return;
      if (sentinel.getBoundingClientRect().top <= body.getBoundingClientRect().bottom + 700) {
        void loadNextSpaceBatch(session);
      }
    });
  };

  const setupSentinelObserver = (session) => {
    const body = modalBody();
    const sentinel = body?.querySelector(".turtle-space-sentinel");
    if (!body || !sentinel || typeof IntersectionObserver !== "function") {
      maybeContinueInfiniteLoad(session);
      return;
    }
    sentinelObserver?.disconnect();
    sentinelObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadNextSpaceBatch(session);
      },
      { root: body, rootMargin: "700px 0px", threshold: 0 },
    );
    sentinelObserver.observe(sentinel);
  };

  const renderSpaceFrame = (session, data) => {
    const body = modalBody();
    if (!body || session !== spaceSession) return;
    session.quota = data.quota || capabilityCache?.quota || null;
    session.total = Number.isFinite(Number(data.total)) ? Number(data.total) : 0;
    session.nextCursor = data.next_cursor || null;
    session.hasMore = Boolean(data.has_more && data.next_cursor);
    body.dataset.spaceGeneration = String(session.generation);
    body.innerHTML = `${quotaOverview(session.quota, session.total)}
      <div class="turtle-space-toolbar">
        <div class="turtle-space-heading"><strong>媒体文件</strong><span>${session.total ? `共 ${session.total} 项` : "按类型自动归档"}</span></div>
        <div class="turtle-kind-filters">
          ${[["all", "全部"], ["image", "图片"], ["video", "视频"], ["file", "文件"]]
            .map(([value, label]) => `<button type="button" data-kind="${value}" data-active="${String(session.kind === value)}">${label}</button>`)
            .join("")}
        </div>
      </div>
      <div class="turtle-file-collection" data-empty="true"></div>
      <div class="turtle-space-sentinel" role="status" aria-live="polite"></div>`;
    appendSpaceItems(session, data.items || []);
    if (!session.items.length && !session.hasMore) {
      const collection = body.querySelector(".turtle-file-collection");
      collection.innerHTML = emptyCollection(session.kind);
      collection.dataset.empty = "true";
    }
    body.onclick = (event) => {
      const kindButton = event.target.closest("[data-kind]");
      if (kindButton) return void renderSpace(kindButton.dataset.kind);
      const previewButton = event.target.closest("[data-file-preview]");
      if (previewButton) {
        const file = session.itemById.get(previewButton.dataset.filePreview);
        if (file) void openFilePreview(file, session);
        return;
      }
      const downloadButton = event.target.closest("[data-file-download]");
      if (downloadButton) return void downloadFile(downloadButton.dataset.fileDownload);
      const deleteButton = event.target.closest("[data-file-delete]");
      if (deleteButton) return void deleteFile(deleteButton.dataset.fileDelete, session, deleteButton);
      if (event.target.closest("[data-space-retry]")) void loadNextSpaceBatch(session);
    };
    updateSpaceSentinel(session);
    setupSentinelObserver(session);
    maybeContinueInfiniteLoad(session);
  };

  const loadNextSpaceBatch = async (session) => {
    if (session !== spaceSession || session.loading || !session.hasMore || !session.nextCursor) return;
    session.loading = true;
    session.loadError = "";
    updateSpaceSentinel(session);
    const controller = trackedAbortController();
    try {
      const data = await fetchSpaceBatch(session.kind, session.nextCursor, false, { signal: controller.signal });
      if (session !== spaceSession || controller.signal.aborted) return;
      appendSpaceItems(session, data.items || []);
      session.nextCursor = data.next_cursor || null;
      session.hasMore = Boolean(data.has_more && data.next_cursor);
    } catch (error) {
      if (error?.name !== "AbortError" && session === spaceSession) {
        session.loadError = error?.message || "继续加载失败";
      }
    } finally {
      releaseAbortController(controller);
      if (session === spaceSession) {
        session.loading = false;
        updateSpaceSentinel(session);
        maybeContinueInfiniteLoad(session);
      }
    }
  };

  const renderSpace = async (kind = "all") => {
    closePreview();
    revokePreviews();
    resetSpaceRuntime();
    const generation = spaceSessionGeneration;
    const session = {
      generation,
      kind,
      items: [],
      itemById: new Map(),
      nextCursor: null,
      hasMore: true,
      loading: false,
      loadError: "",
      quota: null,
      total: 0,
    };
    spaceSession = session;
    const cached = cachedSpacePage(kind);
    if (!cached) renderSpaceShell(capabilityCache?.quota || null);
    const controller = trackedAbortController();
    try {
      const data = await fetchSpaceBatch(kind, null, false, { signal: controller.signal });
      if (
        session !== spaceSession ||
        controller.signal.aborted ||
        document.querySelector("#turtle-storage-dialog")?.dataset.view !== "space"
      ) return;
      renderSpaceFrame(session, data);
      if (cached && Date.now() - cached.cachedAt >= SPACE_CACHE_REVALIDATE_MS) {
        const refreshController = trackedAbortController();
        void fetchSpaceBatch(kind, null, true, { signal: refreshController.signal })
          .then((fresh) => {
            if (session !== spaceSession || refreshController.signal.aborted) return;
            session.quota = fresh.quota || session.quota;
            if (Number.isFinite(Number(fresh.total))) session.total = Number(fresh.total);
            updateSpaceSummary(session);
          })
          .catch(() => {})
          .finally(() => releaseAbortController(refreshController));
      }
    } catch (error) {
      const body = modalBody();
      if (error?.name !== "AbortError" && body && session === spaceSession) {
        body.innerHTML = `<div class="turtle-storage-error"><span>${escapeHtml(error.message)}</span><button type="button" data-space-initial-retry>重新加载</button></div>`;
        body.querySelector("[data-space-initial-retry]")?.addEventListener("click", () => void renderSpace(kind));
      }
    } finally {
      releaseAbortController(controller);
    }
  };

  const openFilePreview = async (file, session) => {
    if (session !== spaceSession || !["image", "video"].includes(file.kind)) return;
    closePreview();
    const preview = document.createElement("div");
    preview.id = "turtle-media-preview";
    preview.innerHTML = `<section role="dialog" aria-modal="true" aria-label="${escapeHtml(file.name)}">
      <header><div><strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong><span>${bytes(file.size)} · ${escapeHtml(timeText(file.created_at))}</span></div>
      <div><button type="button" data-preview-download>下载原图</button><button type="button" data-preview-close aria-label="关闭预览">×</button></div></header>
      <div class="turtle-media-preview-body" data-preview-body><div class="turtle-preview-loading"><span class="turtle-preview-spinner"></span><strong>正在加载清晰预览…</strong></div></div>
    </section>`;
    document.body.append(preview);
    const previewViewport = window.visualViewport || window;
    const syncPreviewViewport = () => {
      const viewportWidth = Math.max(1, Math.floor(window.visualViewport?.width || window.innerWidth));
      const viewportHeight = Math.max(1, Math.floor(window.visualViewport?.height || window.innerHeight));
      preview.style.setProperty("--turtle-preview-viewport-width", `${viewportWidth}px`);
      preview.style.setProperty("--turtle-preview-viewport-height", `${viewportHeight}px`);
    };
    syncPreviewViewport();
    previewViewport.addEventListener("resize", syncPreviewViewport, { passive: true });
    window.addEventListener("orientationchange", syncPreviewViewport, { passive: true });
    preview._cleanupViewport = () => {
      previewViewport.removeEventListener("resize", syncPreviewViewport);
      window.removeEventListener("orientationchange", syncPreviewViewport);
    };
    const controller = trackedAbortController();
    preview._abortController = controller;
    preview.addEventListener("click", (event) => {
      if (event.target === preview || event.target.closest("[data-preview-close]")) closePreview();
      if (event.target.closest("[data-preview-download]")) void downloadFile(file.id);
    });
    preview.querySelector("[data-preview-close]")?.focus();
    try {
      const variant = file.kind === "image" ? "preview" : "original";
      const source = await resolveMediaSource(file.id, variant, controller);
      const body = preview.querySelector("[data-preview-body]");
      if (!body || !preview.isConnected || controller.signal.aborted || session !== spaceSession) return;
      body.innerHTML = "";
      const media = document.createElement(file.kind === "image" ? "img" : "video");
      media.src = source;
      if (file.kind === "image") {
        media.alt = file.name;
        media.decoding = "async";
        media.draggable = false;
      } else {
        media.controls = true;
        media.preload = "metadata";
        media.playsInline = true;
      }
      media.addEventListener(
        "error",
        () => {
          if (body.isConnected) body.innerHTML = '<strong class="turtle-preview-error">预览内容加载失败</strong>';
        },
        { once: true },
      );
      body.append(media);
    } catch (error) {
      const body = preview.querySelector("[data-preview-body]");
      if (error?.name !== "AbortError" && body && preview.isConnected) {
        body.innerHTML = `<strong class="turtle-preview-error">${escapeHtml(error?.message || "预览加载失败")}</strong>`;
      }
    } finally {
      releaseAbortController(controller);
    }
  };

  const deleteFile = async (fileId, session, button = null) => {
    if (session !== spaceSession) return;
    const file = session.itemById.get(fileId);
    const confirmed = await confirmAction({
      title: "删除这个文件？",
      subject: file?.name || "未命名文件",
      message: "删除后将从“我的空间”永久移除，无法恢复。",
      confirmLabel: "删除文件",
      cancelLabel: "保留文件",
    });
    if (!confirmed || session !== spaceSession || !session.itemById.has(fileId)) return;
    if (button) button.disabled = true;
    try {
      const response = await originalFetch(`/api/v1/files/${encodeURIComponent(fileId)}`, {
        method: "DELETE",
        headers: authHeaders(),
        credentials: "same-origin",
      });
      if (!response.ok) return toast(await errorMessage(response, "删除失败"), "error");
      session.itemById.delete(fileId);
      session.items = session.items.filter((item) => item.id !== fileId);
      session.total = Math.max(0, Number(session.total || 0) - 1);
      if (file && session.quota) {
        const used = Math.max(0, Number(session.quota.used_bytes || 0) - Number(file.size || 0));
        session.quota = {
          ...session.quota,
          used_bytes: used,
          remaining_bytes: Math.max(0, Number(session.quota.quota_bytes || 0) - used),
        };
      }
      const body = modalBody();
      const card = Array.from(body?.querySelectorAll("[data-file-id]") || []).find(
        (node) => node.dataset.fileId === fileId,
      );
      const group = card?.closest(".turtle-date-group");
      card?.remove();
      if (group) {
        const count = group.querySelectorAll(".turtle-file-card").length;
        if (!count) group.remove();
        else {
          const countNode = group.querySelector("[data-date-count]");
          if (countNode) countNode.textContent = `${count} 项`;
        }
      }
      for (const key of mediaUrlCache.keys()) {
        if (key.startsWith(`${fileId}:`)) mediaUrlCache.delete(key);
      }
      if (!session.items.length && !session.hasMore) {
        const collection = body?.querySelector(".turtle-file-collection");
        if (collection) {
          collection.innerHTML = emptyCollection(session.kind);
          collection.dataset.empty = "true";
        }
      }
      updateSpaceSummary(session);
      updateSpaceSentinel(session);
      toast("文件已删除", "success");
      spacePageCache.clear();
      capabilityCache = null;
      capabilityCachedAt = 0;
      const refreshController = trackedAbortController();
      void fetchSpaceBatch(session.kind, null, true, { signal: refreshController.signal })
        .then((fresh) => {
          if (session !== spaceSession || refreshController.signal.aborted) return;
          session.quota = fresh.quota || session.quota;
          if (Number.isFinite(Number(fresh.total))) session.total = Number(fresh.total);
          updateSpaceSummary(session);
        })
        .catch(() => {})
        .finally(() => releaseAbortController(refreshController));
      maybeContinueInfiniteLoad(session);
    } catch (error) {
      toast(error?.message || "删除失败", "error");
    } finally {
      if (button?.isConnected) button.disabled = false;
    }
  };

  const downloadFile = async (fileId, fileName = "") => {
    try {
      const response = await apiFetch(
        `/files/${encodeURIComponent(fileId)}/url?variant=original&attachment=true`,
      );
      if (!response.ok) {
        toast(await errorMessage(response, "下载失败"), "error");
        return false;
      }
      const data = await response.json();
      if (data.direct) {
        const directUrl = new URL(data.url, window.location.origin);
        if (!["http:", "https:"].includes(directUrl.protocol)) throw new Error("下载地址无效");
        const frame = document.createElement("iframe");
        frame.className = "turtle-download-frame";
        frame.setAttribute("aria-hidden", "true");
        frame.src = directUrl.href;
        document.body.append(frame);
        window.setTimeout(() => frame.remove(), 120_000);
        return true;
      }
      const fileResponse = await originalFetch(data.url, { headers: authHeaders(), credentials: "same-origin" });
      if (!fileResponse.ok) {
        toast("下载失败", "error");
        return false;
      }
      const url = URL.createObjectURL(await fileResponse.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = fileName;
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return true;
    } catch (error) {
      toast(error?.message || "下载失败", "error");
      return false;
    }
  };

  const tierInputs = (tiers) =>
    Object.entries(tiers)
      .map(
        ([name, value]) => `
          <label class="turtle-tier-field">
            <span>${escapeHtml(name)}</span>
            <div><input type="number" min="0" step="0.5" data-tier-name="${escapeHtml(name)}" value="${gb(value)}" /><em>GB</em></div>
          </label>`,
      )
      .join("");

  const renderAdmin = async () => {
    try {
      const [configResponse, usersResponse] = await Promise.all([
        apiFetch("/admin/config"),
        apiFetch("/admin/users"),
      ]);
      if (!configResponse.ok) throw new Error(await errorMessage(configResponse, "配置读取失败"));
      if (!usersResponse.ok) throw new Error(await errorMessage(usersResponse, "用户额度读取失败"));
      const config = await configResponse.json();
      const users = await usersResponse.json();
      const cos = config.cos;
      const cdn = config.cdn || {};
      const media = config.media;
      const userRows = users.items
        .map(
          (target) => `
            <div class="turtle-user-quota" data-quota-user="${escapeHtml(target.id)}">
              <div class="turtle-user-identity">
                <span class="turtle-user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span>
                <div><strong>${escapeHtml(target.name)}</strong><span>${escapeHtml(target.email)} · 已用 ${bytes(target.used_bytes)}</span></div>
              </div>
              <label><span>会员等级</span><select data-user-tier>
                ${Object.keys(users.tiers).map((tier) => `<option value="${escapeHtml(tier)}" ${tier === target.tier ? "selected" : ""}>${escapeHtml(tier)}</option>`).join("")}
              </select></label>
              <label><span>个人额度（GB）</span><input data-user-quota-gb type="number" min="0" step="0.5" placeholder="跟随等级" value="${target.quota_override_bytes == null ? "" : gb(target.quota_override_bytes)}" /></label>
              <button type="button" data-save-user-quota>保存</button>
            </div>`,
        )
        .join("");

      modalBody().innerHTML = `
        <div class="turtle-admin-overview">
          <article><span class="turtle-admin-stat-icon">${mediaIcon(config.provider === "cos" ? "cloud" : "server")}</span><div><small>当前存储</small><strong>${config.provider === "cos" ? "腾讯云 COS" : "本地磁盘"}</strong></div><i data-tone="${config.provider === "cos" ? "cloud" : "local"}">${config.provider === "cos" ? "云端" : "本地"}</i></article>
          <article><span class="turtle-admin-stat-icon">${mediaIcon("storage")}</span><div><small>COS 连接</small><strong>${cos.configured ? "凭据已保存" : "尚未配置"}</strong></div><i data-tone="${cos.configured ? "ready" : "pending"}">${cos.configured ? "就绪" : "待配置"}</i></article>
          <article><span class="turtle-admin-stat-icon">${mediaIcon("file")}</span><div><small>默认额度</small><strong>${bytes(config.quota.default_bytes)}</strong></div><i data-tone="neutral">${users.items.length} 位用户</i></article>
        </div>
        <form id="turtle-storage-config-form">
          <div class="turtle-admin-layout">
            <section class="turtle-admin-section turtle-admin-storage-card">
              <div class="turtle-section-heading"><div><span>存储服务</span><h3>选择文件保存位置</h3><p>切换后新文件使用所选位置，已有文件保持原路径。</p></div></div>
              <div class="turtle-provider-grid">
                <label class="turtle-provider-choice">
                  <input type="radio" name="provider" value="local" ${config.provider === "local" ? "checked" : ""}/>
                  <span class="turtle-provider-icon">${mediaIcon("server")}</span>
                  <span><strong>本地存储</strong><small>零配置，适合本机和小规模使用</small></span>
                  <i></i>
                </label>
                <label class="turtle-provider-choice">
                  <input type="radio" name="provider" value="cos" ${config.provider === "cos" ? "checked" : ""}/>
                  <span class="turtle-provider-icon">${mediaIcon("cloud")}</span>
                  <span><strong>腾讯云 COS</strong><small>私有 Bucket、直传与预签名访问</small></span>
                  <i></i>
                </label>
              </div>

              <details class="turtle-admin-subpanel" data-cos-details ${config.provider === "cos" || cos.configured ? "open" : ""}>
                <summary><span><strong>COS 连接配置</strong><small>${cos.configured ? "凭据已加密保存，留空不会覆盖" : "选择 COS 前先完成连接信息"}</small></span><span class="turtle-status-pill" data-ready="${String(cos.configured)}">${cos.configured ? "已配置" : "待配置"}</span></summary>
                <div class="turtle-form-grid">
                  <label><span>地域</span><input name="region" placeholder="ap-tokyo" value="${escapeHtml(cos.region)}" /></label>
                  <label><span>Bucket</span><input name="bucket" placeholder="turtle-gpt-1250000000" value="${escapeHtml(cos.bucket)}" /></label>
                  <label class="turtle-span-2"><span>Endpoint</span><input name="endpoint" placeholder="https://cos.ap-tokyo.myqcloud.com" value="${escapeHtml(cos.endpoint_url)}" /></label>
                  <label><span>SecretId ${cos.secret_id_configured ? "· 已保存" : ""}</span><input name="secret_id" type="password" autocomplete="new-password" placeholder="${cos.secret_id_configured ? "留空保持不变" : "请输入 SecretId"}" /></label>
                  <label><span>SecretKey ${cos.secret_key_configured ? "· 已保存" : ""}</span><input name="secret_key" type="password" autocomplete="new-password" placeholder="${cos.secret_key_configured ? "留空保持不变" : "请输入 SecretKey"}" /></label>
                  <label><span>对象前缀</span><input name="prefix" value="${escapeHtml(cos.prefix)}" /></label>
                  <label><span>寻址方式</span><select name="addressing"><option value="virtual" ${cos.addressing_style === "virtual" ? "selected" : ""}>Virtual-hosted</option><option value="path" ${cos.addressing_style === "path" ? "selected" : ""}>Path</option></select></label>
                </div>
                <label class="turtle-switch"><input name="direct" type="checkbox" ${cos.direct_upload_enabled ? "checked" : ""}/><span><strong>浏览器直传 COS</strong><small>图片和视频跳过 WebUI 中转，需先配置 Bucket CORS</small></span></label>
                <div class="turtle-cors-note"><strong>Bucket CORS 要求</strong><span>允许 Turtle’s Chat 域名执行 PUT、GET、HEAD，并放行 Content-Type 请求头。</span></div>
                <div class="turtle-cors-note"><strong>主机媒体隔离</strong><span>${config.media_isolation?.pump_configured ? "外部 Media Pump 已配置；主机上传已关闭" : "Media Pump 尚未配置；为避免占用主机带宽，模型图片输入和生成媒体持久化将失败关闭"}</span></div>
              </details>

              <details class="turtle-admin-subpanel" ${cdn.enabled ? "open" : ""}>
                <summary><span><strong>媒体 CDN</strong><small>原文件与静态缩略图使用两个独立域名</small></span><span class="turtle-status-pill" data-ready="${String(Boolean(cdn.enabled && cdn.files_ready && cdn.images_ready))}">${cdn.enabled ? "已启用" : "未启用"}</span></summary>
                <div class="turtle-form-grid">
                  <label class="turtle-span-2"><span>原文件 CDN</span><input name="files_cdn_url" value="${escapeHtml(cdn.files_base_url || "https://files.chat.totools.cn")}" /></label>
                  <label class="turtle-span-2"><span>图片缩略图 CDN</span><input name="images_cdn_url" value="${escapeHtml(cdn.images_base_url || "https://img.chat.totools.cn")}" /></label>
                  <label><span>文件 Type A 密钥 ${cdn.files_auth_key_configured ? "· 已保存" : ""}</span><input name="files_cdn_key" type="password" autocomplete="new-password" placeholder="${cdn.files_auth_key_configured ? "留空保持不变" : "控制台鉴权主密钥"}" /></label>
                  <label><span>图片 Type A 密钥 ${cdn.images_auth_key_configured ? "· 已保存" : ""}</span><input name="images_cdn_key" type="password" autocomplete="new-password" placeholder="${cdn.images_auth_key_configured ? "留空保持不变" : "控制台鉴权主密钥"}" /></label>
                  <label><span>鉴权有效期（秒）</span><input name="cdn_auth_ttl" type="number" min="60" max="86400" value="${Number(cdn.auth_ttl_seconds) || 900}" /></label>
                </div>
                <label class="turtle-switch"><input name="cdn_enabled" type="checkbox" ${cdn.enabled ? "checked" : ""}/><span><strong>启用双 CDN 安全访问</strong><small>要求两个域名都开启腾讯 CDN Type A 鉴权，签名参数为 sign</small></span></label>
              </details>
            </section>

            <section class="turtle-admin-section turtle-admin-quota-card">
              <div class="turtle-section-heading"><div><span>会员空间</span><h3>默认与等级额度</h3><p>个人覆盖值优先于会员等级；额度只统计媒体文件。</p></div></div>
              <div class="turtle-tier-grid">
                <label class="turtle-tier-field turtle-tier-default"><span>默认</span><div><input name="default_quota" type="number" min="0" step="0.5" value="${gb(config.quota.default_bytes)}" /><em>GB</em></div></label>
                ${tierInputs(config.quota.tiers)}
              </div>
            </section>
          </div>

          <details class="turtle-admin-section turtle-advanced">
            <summary><span><strong>媒体处理与上传限制</strong><small>统一控制图片压缩质量和单文件大小</small></span></summary>
            <div class="turtle-form-grid">
              <label><span>图片最长边（px）</span><input name="max_dimension" type="number" min="512" max="8192" value="${media.max_image_dimension}" /></label>
              <label><span>WebP 质量（0.4–0.98）</span><input name="image_quality" type="number" min="0.4" max="0.98" step="0.01" value="${media.image_quality}" /></label>
              <label><span>图片上限（MB）</span><input name="max_image_mb" type="number" min="1" max="200" value="${Math.round(media.max_image_bytes / 1024 ** 2)}" /></label>
              <label><span>视频上限（MB）</span><input name="max_video_mb" type="number" min="1" max="5120" value="${Math.round(media.max_video_bytes / 1024 ** 2)}" /></label>
            </div>
          </details>

          <div class="turtle-admin-actions"><span>更改只影响新上传文件</span><button type="button" data-test-storage>测试已保存配置</button><button type="submit" class="turtle-primary-button">保存全部设置</button></div>
        </form>

        <section class="turtle-admin-section turtle-users-section">
          <div class="turtle-section-heading"><div><span>用户管理</span><h3>会员等级与个人额度</h3><p>个人额度留空时自动跟随上方会员等级。</p></div><small>${users.items.length} 位用户</small></div>
          <div class="turtle-user-quota-list">${userRows || '<div class="turtle-admin-empty">暂无可管理用户</div>'}</div>
        </section>`;

      document.querySelector("#turtle-storage-config-form").addEventListener("submit", saveAdminConfig);
      modalBody().querySelector("[data-test-storage]").addEventListener("click", testStorage);
      modalBody().querySelectorAll('input[name="provider"]').forEach((input) =>
        input.addEventListener("change", () => {
          if (input.value === "cos" && input.checked) modalBody().querySelector("[data-cos-details]").open = true;
        }),
      );
      modalBody().querySelectorAll("[data-save-user-quota]").forEach((button) =>
        button.addEventListener("click", () => saveUserQuota(button.closest("[data-quota-user]"))),
      );
    } catch (error) {
      modalBody().innerHTML = `<div class="turtle-storage-error">${escapeHtml(error.message)}</div>`;
    }
  };

  const chatResetLabel = (resetAt) => {
    if (!resetAt) return "";
    const seconds = Math.max(0, Number(resetAt) - Math.floor(Date.now() / 1000));
    if (seconds < 60) return "不到 1 分钟";
    if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`;
    if (seconds < 86400) return `${Math.ceil(seconds / 3600)} 小时`;
    return `${Math.ceil(seconds / 86400)} 天`;
  };

  const chatLaneLabel = (lane) => {
    if (!lane?.allowed) return "无权限";
    const reset = chatResetLabel(lane.reset_at);
    if (!lane.available) return reset ? `已用完 · ${reset}后恢复` : "已用完";
    if (lane.limit_count == null) return "不限次数";
    const base = `剩余 ${Number(lane.remaining_count || 0)}/${Number(lane.limit_count)}`;
    return reset ? `${base} · ${reset}后重置` : `${base} · 首次使用后计时`;
  };

  const chatWindowHours = (seconds) => {
    const hours = Number(seconds || 0) / 3600;
    return Number.isInteger(hours) ? String(hours) : String(Number(hours.toFixed(2)));
  };

  const normalizedChatGroupRule = (rule = {}) => {
    const enabled = Boolean(rule.enabled);
    const limit = enabled && rule.limit_count != null ? Number(rule.limit_count) : null;
    return {
      enabled,
      limit_count: limit,
      window_seconds: limit == null ? 0 : Number(rule.window_seconds || 0),
      fallback_key: limit == null ? null : rule.fallback_key || null,
    };
  };

  const groupMatchesChatPreset = (group, preset) => {
    if (!preset?.rules?.length) return false;
    const current = new Map((group.rules || []).map((rule) => [rule.selection_key, normalizedChatGroupRule(rule)]));
    return preset.rules.every((rule) => {
      const expected = normalizedChatGroupRule(rule);
      const actual = current.get(rule.selection_key) || normalizedChatGroupRule();
      return (
        actual.enabled === expected.enabled &&
        actual.limit_count === expected.limit_count &&
        actual.window_seconds === expected.window_seconds &&
        actual.fallback_key === expected.fallback_key
      );
    });
  };

  const chatPresetNote = (preset) => {
    if (!preset) {
      return `<span>选择方案后会把推荐次数、窗口、权限和降级目标填入当前表单；点击保存前不会生效。</span>`;
    }
    const sources = (preset.sources || [])
      .map(
        (source) =>
          `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.label)}</a>`,
      )
      .join(" · ");
    return `
      <span><strong>${escapeHtml(preset.official_note || "")}</strong>${escapeHtml(preset.recommendation_note || "")}</span>
      <small>已套用到当前表单，可继续逐项修改${sources ? ` · 官方来源：${sources}` : ""}</small>`;
  };

  const chatPresetEditor = (group, data) => {
    const presets = Array.isArray(data.presets) ? data.presets : [];
    if (!presets.length) return "";
    const active = presets.find((preset) => groupMatchesChatPreset(group, preset));
    const buttons = presets
      .map(
        (preset) => `
          <button type="button" data-apply-chat-preset="${escapeHtml(preset.id)}" aria-pressed="${String(active?.id === preset.id)}" title="${escapeHtml(preset.official_note || preset.label)}">
            <strong>${escapeHtml(preset.label)}</strong><small>${active?.id === preset.id ? "当前值" : "套用建议"}</small>
          </button>`,
      )
      .join("");
    return `
      <div class="turtle-chat-preset-editor">
        <div class="turtle-chat-preset-heading">
          <span><strong>订阅推荐配置</strong><small>Go、Plus、5× Pro、20× Pro</small></span>
          <i>可修改</i>
        </div>
        <div class="turtle-chat-preset-buttons">${buttons}</div>
        <div class="turtle-chat-preset-note" data-chat-preset-note>${chatPresetNote(active)}</div>
      </div>`;
  };

  const syncChatGroupRuleRow = (row) => {
    const enabled = row.querySelector("[data-group-rule-enabled]").checked;
    const limited = row.querySelector("[data-group-rule-limit]").value.trim() !== "";
    row.dataset.enabled = String(enabled);
    row.querySelector("[data-group-rule-limit]").disabled = !enabled;
    row.querySelector("[data-group-rule-window]").disabled = !enabled || !limited;
    row.querySelector("[data-group-rule-fallback]").disabled = !enabled || !limited;
  };

  const groupRuleEditor = (group, selection, selections) => {
    const rule = (group.rules || []).find((item) => item.selection_key === selection.key) || {};
    const fallbackOptions = [
      '<option value="">到限后停止</option>',
      ...selections
        .filter((item) => item.key !== selection.key && item.family === selection.family)
        .map(
          (item) =>
            `<option value="${escapeHtml(item.key)}" ${rule.fallback_key === item.key ? "selected" : ""}>${escapeHtml(item.version_label)} · ${escapeHtml(item.level_label)}</option>`,
        ),
    ].join("");
    return `
      <div class="turtle-chat-group-rule" data-chat-group-rule="${escapeHtml(selection.key)}">
        <label class="turtle-chat-rule-toggle">
          <input type="checkbox" data-group-rule-enabled ${rule.enabled ? "checked" : ""}/>
          <span><strong>${escapeHtml(selection.version_label)}</strong><small>${escapeHtml(selection.level_label)}${selection.verification_state === "pending" ? " · 待真实验证" : ""}</small></span>
          <i></i>
        </label>
        <label><span>次数</span><input data-group-rule-limit type="number" min="1" step="1" placeholder="不限" value="${rule.limit_count == null ? "" : Number(rule.limit_count)}"/></label>
        <label><span>窗口（小时）</span><input data-group-rule-window type="number" min="0.02" max="8784" step="0.5" placeholder="例如 3" value="${rule.limit_count == null ? "" : chatWindowHours(rule.window_seconds)}"/></label>
        <label class="turtle-chat-rule-fallback"><span>额度用完后</span><select data-group-rule-fallback>${fallbackOptions}</select></label>
      </div>`;
  };

  const groupEditor = (group, data, isNew = false) => {
    const rules = data.selections.map((item) => groupRuleEditor(group, item, data.selections)).join("");
    const presets = chatPresetEditor(group, data);
    const badge = isNew
      ? "新建"
      : group.default_role === "user"
        ? "默认用户组"
        : group.default_role === "admin"
          ? "默认管理员组"
          : `${Number(group.member_count || 0)} 位用户`;
    return `
      <details class="turtle-chat-group-card" data-chat-group="${isNew ? "new" : escapeHtml(group.id)}" ${isNew ? "" : group.default_role === "user" ? "open" : ""}>
        <summary><span><strong>${escapeHtml(group.name || "新建分组")}</strong><small>${escapeHtml(group.description || "配置模型权限、独立次数与自动降级")}</small></span><i>${escapeHtml(badge)}</i></summary>
        <div class="turtle-chat-group-body">
          ${presets}
          <div class="turtle-chat-group-meta">
            <label><span>分组名称</span><input data-chat-group-name maxlength="40" value="${escapeHtml(group.name || "")}" placeholder="例如：朋友组"/></label>
            <label><span>说明</span><input data-chat-group-description maxlength="200" value="${escapeHtml(group.description || "")}" placeholder="用途和分配原则"/></label>
          </div>
          <div class="turtle-chat-group-rule-list">${rules}</div>
          <div class="turtle-chat-group-actions">
            ${!isNew && !group.is_system ? '<button type="button" class="turtle-danger-button" data-delete-chat-group>删除分组</button>' : '<span>系统分组可修改，但不能删除</span>'}
            <button type="button" class="turtle-primary-button" data-save-chat-group>${isNew ? "创建分组" : "保存分组"}</button>
          </div>
        </div>
      </details>`;
  };

  const userQuotaCards = (target, selections) =>
    selections
      .map((item) => {
        const lane = target.quota.models?.[item.key] || {};
        const tone = !lane.allowed ? "forbidden" : lane.available ? "available" : "exhausted";
        return `
          <span class="turtle-chat-lane-status" data-state="${tone}">
            <strong>${escapeHtml(item.version_label)} · ${escapeHtml(item.level_label)}</strong>
            <small>${escapeHtml(chatLaneLabel(lane))}</small>
          </span>`;
      })
      .join("");

  const renderChatAdmin = async () => {
    try {
      const response = await chatApiFetch("/admin/users");
      if (!response.ok) throw new Error(await errorMessage(response, "聊天分组读取失败"));
      const data = await response.json();
      const template =
        (data.presets || []).find((preset) => preset.id === "plus") ||
        data.groups.find((group) => group.id === "basic") ||
        data.groups[0] ||
        { rules: [] };
      const newGroup = {
        id: "new",
        name: "",
        description: "",
        rules: (template.rules || []).map((rule) => ({ ...rule })),
      };
      const groupCards = [
        ...data.groups.map((group) => groupEditor(group, data)),
        groupEditor(newGroup, data, true),
      ].join("");
      const groupOptions = data.groups
        .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`)
        .join("");
      const userCards = data.items
        .map((target, index) => {
          const currentGroupId = target.policy.group?.id || "";
          const availableCount = Object.values(target.quota.models || {}).filter((lane) => lane.available).length;
          const selectOptions = `${currentGroupId ? "" : '<option value="" selected disabled>旧版自定义（请选择分组）</option>'}${groupOptions}`;
          return `
            <details class="turtle-chat-user-card" data-chat-user="${escapeHtml(target.id)}" ${index === 0 ? "open" : ""}>
              <summary>
                <span class="turtle-user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span>
                <span class="turtle-chat-user-copy"><strong>${escapeHtml(target.name)}</strong><small>${escapeHtml(target.email)} · ${escapeHtml(target.role)}</small></span>
                <span class="turtle-chat-balance"><strong>${escapeHtml(target.policy.group?.name || "旧版自定义")}</strong><small>${availableCount} 个档位可用 · ${Number(target.quota.request_count || 0)} 次有效请求</small></span>
              </summary>
              <div class="turtle-chat-user-body">
                <div class="turtle-chat-user-group-row">
                  <label><span>用户分组</span><select data-chat-user-group>${selectOptions}</select></label>
                  <button type="button" class="turtle-primary-button" data-save-chat-user-group>保存分组</button>
                  <button type="button" data-reset-chat-quota>重置该用户时间窗</button>
                </div>
                <div class="turtle-section-heading"><div><span>当前状态</span><h3>每个模型独立计数</h3><p>无权限和额度耗尽都会在用户模型菜单中置灰；耗尽档位按分组规则自动降级。</p></div></div>
                <div class="turtle-chat-lane-grid">${userQuotaCards(target, data.selections)}</div>
              </div>
            </details>`;
        })
        .join("");

      modalBody().innerHTML = `
        <div class="turtle-chat-admin-intro">
          <div><span>MODEL ALLOWANCE</span><h3>分组、模型次数与自动降级</h3><p>${escapeHtml(data.note || "")}</p></div>
          <i>用户独立计数</i>
        </div>
        <section class="turtle-chat-groups">
          <div class="turtle-section-heading"><div><span>分组模板</span><h3>先套用订阅建议，再按实际情况修改</h3><p>四个预设只填充表单；官方明确值、倍率和站内建议会分别标注，保存后才会影响用户。</p></div><small>${data.groups.length} 个分组</small></div>
          <div class="turtle-chat-group-list">${groupCards}</div>
        </section>
        <section class="turtle-chat-users">
          <div class="turtle-section-heading"><div><span>用户分配</span><h3>给用户选择分组并查看实时额度</h3><p>切换分组不会删除历史记录，也不会通过改组清空当前用户的模型时间窗。</p></div><small>${data.items.length} 位用户</small></div>
          <div class="turtle-chat-user-list">${userCards || '<div class="turtle-admin-empty">暂无可管理用户</div>'}</div>
        </section>`;

      modalBody().querySelectorAll("[data-chat-user-group]").forEach((select) => {
        const card = select.closest("[data-chat-user]");
        const target = data.items.find((item) => item.id === card.dataset.chatUser);
        if (target?.policy.group?.id) select.value = target.policy.group.id;
      });
      modalBody().querySelectorAll("[data-chat-group-rule]").forEach((row) => {
        row.querySelector("[data-group-rule-enabled]").addEventListener("change", () => syncChatGroupRuleRow(row));
        row.querySelector("[data-group-rule-limit]").addEventListener("input", () => syncChatGroupRuleRow(row));
        syncChatGroupRuleRow(row);
      });
      modalBody().querySelectorAll("[data-apply-chat-preset]").forEach((button) =>
        button.addEventListener("click", () => {
          const card = button.closest("[data-chat-group]");
          const preset = (data.presets || []).find((item) => item.id === button.dataset.applyChatPreset);
          if (card && preset) applyChatPlanPreset(card, preset);
        }),
      );
      modalBody().querySelectorAll("[data-save-chat-group]").forEach((button) =>
        button.addEventListener("click", () => saveChatGroup(button.closest("[data-chat-group]"))),
      );
      modalBody().querySelectorAll("[data-delete-chat-group]").forEach((button) =>
        button.addEventListener("click", () => deleteChatGroup(button.closest("[data-chat-group]"))),
      );
      modalBody().querySelectorAll("[data-save-chat-user-group]").forEach((button) =>
        button.addEventListener("click", () => saveChatUserGroup(button.closest("[data-chat-user]"))),
      );
      modalBody().querySelectorAll("[data-reset-chat-quota]").forEach((button) =>
        button.addEventListener("click", () => resetChatQuota(button.closest("[data-chat-user]"))),
      );
    } catch (error) {
      modalBody().innerHTML = `<div class="turtle-storage-error">${escapeHtml(error.message)}</div>`;
    }
  };

  const applyChatPlanPreset = (card, preset) => {
    const rules = new Map((preset.rules || []).map((rule) => [rule.selection_key, rule]));
    card.querySelectorAll("[data-chat-group-rule]").forEach((row) => {
      const rule = normalizedChatGroupRule(rules.get(row.dataset.chatGroupRule));
      row.querySelector("[data-group-rule-enabled]").checked = rule.enabled;
      row.querySelector("[data-group-rule-limit]").value = rule.limit_count == null ? "" : String(rule.limit_count);
      row.querySelector("[data-group-rule-window]").value =
        rule.limit_count == null ? "" : chatWindowHours(rule.window_seconds);
      row.querySelector("[data-group-rule-fallback]").value = rule.fallback_key || "";
      syncChatGroupRuleRow(row);
    });
    if (card.dataset.chatGroup === "new") {
      const name = card.querySelector("[data-chat-group-name]");
      const description = card.querySelector("[data-chat-group-description]");
      if (!name.value.trim()) name.value = preset.default_name || `${preset.label} 组`;
      if (!description.value.trim()) description.value = preset.default_description || "";
    }
    card.querySelectorAll("[data-apply-chat-preset]").forEach((button) => {
      const selected = button.dataset.applyChatPreset === preset.id;
      button.setAttribute("aria-pressed", String(selected));
      const status = button.querySelector("small");
      if (status) status.textContent = selected ? "已套用" : "套用建议";
    });
    const note = card.querySelector("[data-chat-preset-note]");
    if (note) note.innerHTML = chatPresetNote(preset);
    toast(`已套用 ${preset.label} 推荐值，可继续修改后保存`, "success");
  };

  const collectChatGroupRules = (card) => {
    const rules = [];
    card.querySelectorAll("[data-chat-group-rule]").forEach((row) => {
      const enabled = row.querySelector("[data-group-rule-enabled]").checked;
      const limitRaw = row.querySelector("[data-group-rule-limit]").value.trim();
      const hoursRaw = row.querySelector("[data-group-rule-window]").value.trim();
      const limit = enabled && limitRaw !== "" ? Number(limitRaw) : null;
      const hours = limit == null ? 0 : Number(hoursRaw);
      if (limit != null && (!Number.isInteger(limit) || limit < 1)) throw new Error("模型次数必须是正整数");
      if (limit != null && (!Number.isFinite(hours) || hours < 1 / 60)) throw new Error("有限额度必须填写至少 1 分钟的时间窗");
      rules.push({
        selection_key: row.dataset.chatGroupRule,
        enabled,
        limit_count: limit,
        window_seconds: limit == null ? 0 : Math.round(hours * 3600),
        fallback_key: limit == null ? null : row.querySelector("[data-group-rule-fallback]").value || null,
      });
    });
    if (!rules.some((rule) => rule.enabled)) throw new Error("分组至少开放一个模型档位");
    return rules;
  };

  const saveChatGroup = async (card) => {
    const button = card.querySelector("[data-save-chat-group]");
    const isNew = card.dataset.chatGroup === "new";
    const name = card.querySelector("[data-chat-group-name]").value.trim();
    const description = card.querySelector("[data-chat-group-description]").value.trim();
    if (!name) return toast("请填写分组名称", "error");
    let rules;
    try {
      rules = collectChatGroupRules(card);
    } catch (error) {
      return toast(error.message, "error");
    }
    button.disabled = true;
    button.textContent = isNew ? "创建中…" : "保存中…";
    try {
      const path = isNew ? "/admin/groups" : `/admin/groups/${encodeURIComponent(card.dataset.chatGroup)}`;
      const response = await chatApiFetch(path, {
        method: isNew ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description, rules }),
      });
      if (!response.ok) return toast(await errorMessage(response, "分组保存失败"), "error");
      window.dispatchEvent(new CustomEvent("turtle-chat-policy-updated"));
      toast(isNew ? "聊天分组已创建" : "聊天分组已更新", "success");
      await renderChatAdmin();
    } catch (error) {
      toast(error?.message || "分组保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = isNew ? "创建分组" : "保存分组";
    }
  };

  const deleteChatGroup = async (card) => {
    if (!window.confirm("确定删除这个聊天分组吗？只有未分配用户的自定义分组才能删除。")) return;
    const button = card.querySelector("[data-delete-chat-group]");
    button.disabled = true;
    try {
      const response = await chatApiFetch(`/admin/groups/${encodeURIComponent(card.dataset.chatGroup)}`, { method: "DELETE" });
      if (!response.ok) return toast(await errorMessage(response, "分组删除失败"), "error");
      toast("聊天分组已删除", "success");
      await renderChatAdmin();
    } catch (error) {
      toast(error?.message || "分组删除失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const saveChatUserGroup = async (card) => {
    const button = card.querySelector("[data-save-chat-user-group]");
    const groupId = card.querySelector("[data-chat-user-group]").value;
    if (!groupId) return toast("请先选择用户分组", "error");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const response = await chatApiFetch(`/admin/users/${encodeURIComponent(card.dataset.chatUser)}/group`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group_id: groupId }),
      });
      if (!response.ok) return toast(await errorMessage(response, "用户分组保存失败"), "error");
      window.dispatchEvent(new CustomEvent("turtle-chat-policy-updated"));
      toast("用户分组已更新", "success");
      await renderChatAdmin();
    } catch (error) {
      toast(error?.message || "用户分组保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存分组";
    }
  };

  const resetChatQuota = async (card) => {
    if (!window.confirm("确定重置该用户全部模型的当前时间窗吗？这会立即恢复其站内模型次数。")) return;
    const button = card.querySelector("[data-reset-chat-quota]");
    button.disabled = true;
    try {
      const response = await chatApiFetch(`/admin/users/${encodeURIComponent(card.dataset.chatUser)}/quota/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selection_key: null }),
      });
      if (!response.ok) return toast(await errorMessage(response, "额度重置失败"), "error");
      window.dispatchEvent(new CustomEvent("turtle-chat-policy-updated"));
      toast("该用户的模型时间窗已重置", "success");
      await renderChatAdmin();
    } catch (error) {
      toast(error?.message || "额度重置失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const saveAdminConfig = async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const submitButton = formElement.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    submitButton.textContent = "正在保存…";
    const form = new FormData(formElement);
    const region = String(form.get("region") || "").trim();
    const endpoint = String(form.get("endpoint") || "").trim() || (region ? `https://cos.${region}.myqcloud.com` : "");
    const tiers = {};
    formElement.querySelectorAll("[data-tier-name]").forEach((input) => {
      tiers[input.dataset.tierName] = toBytes(input.value);
    });
    const payload = {
      provider: form.get("provider"),
      cos: {
        region,
        bucket: form.get("bucket"),
        endpoint_url: endpoint,
        prefix: form.get("prefix"),
        addressing_style: form.get("addressing"),
        direct_upload_enabled: form.get("direct") === "on",
        secret_id: form.get("secret_id"),
        secret_key: form.get("secret_key"),
      },
      cdn: {
        enabled: form.get("cdn_enabled") === "on",
        files_base_url: form.get("files_cdn_url"),
        images_base_url: form.get("images_cdn_url"),
        files_auth_key: form.get("files_cdn_key"),
        images_auth_key: form.get("images_cdn_key"),
        auth_ttl_seconds: Number(form.get("cdn_auth_ttl")),
      },
      media: {
        max_image_dimension: Number(form.get("max_dimension")),
        image_quality: Number(form.get("image_quality")),
        max_image_bytes: Math.round(Number(form.get("max_image_mb")) * 1024 ** 2),
        max_video_bytes: Math.round(Number(form.get("max_video_mb")) * 1024 ** 2),
      },
      quota: { default_bytes: toBytes(form.get("default_quota")), tiers },
    };
    try {
      const response = await apiFetch("/admin/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) return toast(await errorMessage(response, "保存失败"), "error");
      invalidateSpaceData();
      toast("存储设置已保存", "success");
      await renderAdmin();
    } catch (error) {
      toast(error?.message || "保存失败", "error");
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "保存全部设置";
    }
  };

  const testStorage = async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在测试…";
    try {
      const response = await apiFetch("/admin/test", { method: "POST" });
      if (!response.ok) return toast(await errorMessage(response, "连接测试失败"), "error");
      const payload = await response.json();
      toast(payload.message || "连接成功", "success");
    } catch (error) {
      toast(error?.message || "连接测试失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "测试已保存配置";
    }
  };

  const saveUserQuota = async (row) => {
    const button = row.querySelector("[data-save-user-quota]");
    button.disabled = true;
    button.textContent = "保存中…";
    const value = row.querySelector("[data-user-quota-gb]").value.trim();
    try {
      const response = await apiFetch(`/admin/users/${encodeURIComponent(row.dataset.quotaUser)}/quota`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tier: row.querySelector("[data-user-tier]").value,
          quota_bytes: value === "" ? null : toBytes(value),
        }),
      });
      if (!response.ok) return toast(await errorMessage(response, "用户额度保存失败"), "error");
      invalidateSpaceData();
      toast("用户空间额度已更新", "success");
    } catch (error) {
      toast(error?.message || "用户额度保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存";
    }
  };

  const createLauncher = () => {
    const button = document.createElement("button");
    button.id = "turtle-space-launcher";
    button.type = "button";
    button.dataset.usageState = "loading";
    button.title = "我的空间 · 正在读取用量";
    button.innerHTML = `
      <span class="turtle-launcher-icon">${mediaIcon("cloud")}</span>
      <span class="turtle-launcher-copy"><strong>我的空间</strong><small data-launcher-usage>正在读取…</small></span>
      <svg class="turtle-launcher-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m9 6 6 6-6 6"></path></svg>`;
    button.addEventListener("click", () => void openModal());
    button.addEventListener("pointerenter", () => scheduleSpacePrefetch(0));
    button.addEventListener("focus", () => scheduleSpacePrefetch(0));
    return button;
  };

  const mountProjectApiMenuItem = () => {
    document.querySelector("#turtle-project-api-launcher")?.remove();
    let menuItem = document.querySelector("#turtle-project-api-menu-item");
    if (projectAccess !== true) {
      menuItem?.remove();
      if (projectAccess == null) {
        void projectApiAccess().then(() => queueMount()).catch(() => {});
      }
      return;
    }

    const menus = Array.from(
      document.querySelectorAll('[role="menu"], [data-radix-menu-content], [data-headlessui-menu-items]'),
    ).filter((candidate) => {
      const style = window.getComputedStyle(candidate);
      return style.display !== "none" && style.visibility !== "hidden";
    });
    const menu = menus.find((candidate) => {
      const text = candidate.textContent || "";
      return /设置|Settings/i.test(text) && /退出|登出|Sign out|Logout/i.test(text);
    });
    if (!menu) {
      menuItem?.remove();
      return;
    }

    if (menuItem && !menu.contains(menuItem)) {
      menuItem.remove();
      menuItem = null;
    }
    if (!menuItem) {
      menuItem = document.createElement("button");
      menuItem.id = "turtle-project-api-menu-item";
      menuItem.type = "button";
      menuItem.setAttribute("role", "menuitem");
      menuItem.innerHTML = `<span class="turtle-project-menu-icon">${mediaIcon("server")}</span><span>API 密钥</span>`;
      menuItem.addEventListener("click", () => void openProjectApiModal());
    }
    const candidates = Array.from(menu.querySelectorAll('a, button, [role="menuitem"]'));
    const settingsItem = candidates.find((candidate) => /^(设置|Settings)$/i.test((candidate.textContent || "").trim()));
    const logoutItem = candidates.find((candidate) => /退出|登出|Sign out|Logout/i.test((candidate.textContent || "").trim()));
    const reference = settingsItem || logoutItem;
    const host = reference?.parentElement || menu;
    if (reference?.className && typeof reference.className === "string") {
      menuItem.className = `${reference.className} turtle-project-api-menu-item`.trim();
    } else {
      menuItem.className = "turtle-project-api-menu-item";
    }
    if (menuItem.parentElement !== host || (reference && menuItem.nextElementSibling !== reference)) {
      host.insertBefore(menuItem, reference || null);
    }
  };

  const updateLauncherUsage = (button, cap) => {
    const quota = cap?.quota || {};
    const values = quotaPresentation(quota);
    const usage = `${bytes(values.usedBytes)} / ${bytes(values.quotaBytes)}`;
    const label = button.querySelector("[data-launcher-usage]");
    if (label && label.textContent !== usage) label.textContent = usage;
    button.dataset.usageState = "ready";
    button.title = `我的空间 · 已用 ${usage} · ${values.percent}`;
  };

  const scheduleLauncherRefresh = (delay = 0) => {
    if (launcherRefreshTimer != null) return;
    launcherRefreshTimer = window.setTimeout(() => {
      launcherRefreshTimer = null;
      void refreshLauncherUsage();
    }, delay);
  };

  const refreshLauncherUsage = async () => {
    try {
      const cap = await capabilities();
      const button = document.querySelector("#turtle-space-launcher");
      if (button) updateLauncherUsage(button, cap);
      launcherRetryCount = 0;
      scheduleSpacePrefetch();
    } catch (_error) {
      launcherRetryCount += 1;
      if (launcherRetryCount <= 4) scheduleLauncherRefresh(500 * 2 ** launcherRetryCount);
    }
  };

  const mountLauncher = () => {
    if (!storedToken() && !capturedAuthorization) {
      document.querySelector("#turtle-space-launcher")?.remove();
      return;
    }
    let button = document.querySelector("#turtle-space-launcher");
    if (!button) button = createLauncher();
    if (capabilityCache) {
      updateLauncherUsage(button, capabilityCache);
      scheduleSpacePrefetch();
    }

    const sidebar = document.querySelector("#sidebar");
    if (sidebar) {
      const sidebarShell = Array.from(sidebar.children).find(
        (child) =>
          child !== button &&
          child.classList?.contains("flex-col") &&
          child.querySelector?.('[aria-label="收起侧边栏"], [aria-label="展开侧边栏"]'),
      );
      const host = sidebarShell || sidebar;
      const footer = Array.from(host.children).find((child) => child.classList?.contains("bottom-0"));
      if (button.parentElement !== host || (footer && button.nextElementSibling !== footer)) {
        host.insertBefore(button, footer || null);
      }
      button.dataset.placement = "sidebar";
      const updateVisibility = () => {
        const expanded = Boolean(sidebar.querySelector('[aria-label="收起侧边栏"]'));
        button.hidden = !expanded;
        button.dataset.compact = String(!expanded);
      };
      updateVisibility();
      if (launcherObservedSidebar !== sidebar) {
        launcherResizeObserver?.disconnect();
        launcherObservedSidebar = sidebar;
        if (typeof ResizeObserver !== "undefined") {
          launcherResizeObserver = new ResizeObserver(updateVisibility);
          launcherResizeObserver.observe(sidebar);
        }
      }
    } else {
      if (button.parentElement !== document.body) document.body.append(button);
      button.dataset.placement = "detached";
      button.dataset.compact = "true";
      button.hidden = true;
      launcherResizeObserver?.disconnect();
      launcherObservedSidebar = null;
    }

    if (!capabilityCache || Date.now() - capabilityCachedAt >= 60_000) scheduleLauncherRefresh();
  };

  const mountAdminTools = () => {
    const existing = document.querySelector("#turtle-admin-tools");
    if (!window.location.pathname.startsWith("/admin/settings")) {
      existing?.remove();
      return;
    }
    const tabs = document.querySelector("#admin-settings-tabs-container");
    if (!tabs) return;

    let tools = existing;
    if (!tools) {
      tools = document.createElement("section");
      tools.id = "turtle-admin-tools";
      tools.setAttribute("aria-label", "Turtle’s Chat 设置");
      tools.innerHTML = `
        <a class="turtle-admin-settings-link" href="/admin#/overview">
          <span class="turtle-admin-settings-icon">${mediaIcon("server")}</span>
          <span>管理控制台</span>
        </a>`;
    }
    const before = tabs.querySelector("#evaluations");
    if (tools.parentElement !== tabs || tools.nextElementSibling !== before) {
      tabs.insertBefore(tools, before || null);
    }
  };

  const queueMount = () => {
    if (mountQueued) return;
    mountQueued = true;
    requestAnimationFrame(() => {
      mountQueued = false;
      mountLauncher();
      mountProjectApiMenuItem();
      mountAdminTools();
    });
  };

  const start = () => {
    document.documentElement.dataset.turtleStorage = "ready";
    document.addEventListener("click", suppressRichReferenceNavigation, true);
    document.addEventListener("click", suppressUnsupportedSandboxNavigation, true);
    document.addEventListener("click", suppressManagedImageSourceNavigation, true);
    document.addEventListener("click", downloadManagedAttachmentOnPage, true);
    document.addEventListener("click", dismissGeneratedGalleryDownloadMenus);
    new MutationObserver(() => {
      queueMount();
      scheduleManagedThumbnailScan();
      decorateManagedOutputs();
      enhanceNativeImagePreviews();
      compactSearchSources();
      syncNativeReasoningDisclosure();
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
    window.addEventListener("pageshow", () => {
      queueMount();
      scheduleManagedThumbnailScan();
      decorateManagedOutputs();
      enhanceNativeImagePreviews();
      compactSearchSources();
      syncNativeReasoningDisclosure();
    });
    window.addEventListener("popstate", () => {
      queueMount();
      scheduleManagedThumbnailScan();
      decorateManagedOutputs();
      compactSearchSources();
      syncNativeReasoningDisclosure();
    });
    window.addEventListener("focus", () => {
      queueMount();
      scheduleManagedThumbnailScan();
      decorateManagedOutputs();
      compactSearchSources();
      syncNativeReasoningDisclosure();
    });
    queueMount();
    scheduleManagedThumbnailScan();
    decorateManagedOutputs();
    enhanceNativeImagePreviews();
    compactSearchSources();
    syncNativeReasoningDisclosure();
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
