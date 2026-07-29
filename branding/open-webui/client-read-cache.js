(() => {
  "use strict";

  const SIDEBAR_CACHE_PREFIX = "turtle-sidebar-read-cache-v1:";
  const SIDEBAR_SCOPE_KEY = "turtle-sidebar-read-cache-scope-v1";
  const CONVERSATION_SESSION_PREFIX = "turtle-conversation-read-cache-v1:";
  const SIDEBAR_REVALIDATE_AFTER_MS = 15_000;
  const SIDEBAR_MAX_STALE_MS = 6 * 60 * 60 * 1000;
  const CONVERSATION_CACHE_TTL_MS = 10 * 60 * 1000;
  const TASK_CACHE_TTL_MS = 30_000;
  const CONVERSATION_CACHE_LIMIT = 36;
  const CONVERSATION_SESSION_LIMIT = 12;
  const MAX_CACHE_BODY_BYTES = 512 * 1024;
  const SIDEBAR_UPDATED_EVENT = "turtle:client-read-cache-updated";
  const CHAT_COMPLETION_PATHS = new Set([
    "/api/chat/completions",
    "/api/openai/chat/completions",
  ]);
  const RESERVED_CHAT_SEGMENTS = new Set([
    "all",
    "archive",
    "folder",
    "list",
    "new",
    "pinned",
    "search",
    "tags",
  ]);

  const originalFetch = window.fetch.bind(window);
  const sidebarInflight = new Map();
  const sidebarRevalidating = new Map();
  const sidebarFreshDeliveries = new Map();
  const conversationInflight = new Map();
  const conversationCache = new Map();
  const historyPageInflight = new Map();
  const historyPageState = new Map();

  let sidebarGeneration = 0;
  let conversationGeneration = 0;
  let scopeToken = "";
  let scopePromise = null;
  const stats = {
    sidebarHits: 0,
    sidebarMisses: 0,
    sidebarRevalidations: 0,
    sidebarSingleFlightJoins: 0,
    conversationHits: 0,
    conversationSessionHits: 0,
    conversationMisses: 0,
    conversationPrefetches: 0,
    historyPageLoads: 0,
    historyPageSingleFlightJoins: 0,
  };

  const storedToken = () => {
    try {
      return localStorage.getItem("token") || "";
    } catch (_error) {
      return "";
    }
  };

  const removeSidebarEntries = () => {
    try {
      for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (key?.startsWith(SIDEBAR_CACHE_PREFIX)) sessionStorage.removeItem(key);
      }
    } catch (_error) {
      // Storage can be unavailable in hardened/private browser contexts.
    }
  };

  const removeConversationStorageEntries = (chatId = "") => {
    try {
      for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (!key?.startsWith(CONVERSATION_SESSION_PREFIX)) continue;
        if (!chatId) {
          sessionStorage.removeItem(key);
          continue;
        }
        try {
          const entry = JSON.parse(sessionStorage.getItem(key) || "null");
          if (!entry || entry.chatId === chatId) sessionStorage.removeItem(key);
        } catch (_error) {
          sessionStorage.removeItem(key);
        }
      }
    } catch (_error) {
      // Storage cleanup is best effort; memory invalidation still succeeds.
    }
  };

  const resetConversationMemory = ({ clearStorage = false } = {}) => {
    conversationGeneration += 1;
    conversationCache.clear();
    conversationInflight.clear();
    historyPageState.clear();
    if (clearStorage) removeConversationStorageEntries();
  };

  const invalidateSidebarSnapshots = ({ clearScope = false } = {}) => {
    sidebarGeneration += 1;
    sidebarInflight.clear();
    sidebarRevalidating.clear();
    sidebarFreshDeliveries.clear();
    removeSidebarEntries();
    if (clearScope) {
      try {
        sessionStorage.removeItem(SIDEBAR_SCOPE_KEY);
      } catch (_error) {
        // The in-memory caches are still cleared when storage is unavailable.
      }
      scopeToken = "";
      scopePromise = null;
    }
  };

  const clearAllReadCaches = () => {
    invalidateSidebarSnapshots({ clearScope: true });
    resetConversationMemory({ clearStorage: true });
  };

  const tokenScope = async () => {
    const token = storedToken();
    if (!token || !globalThis.crypto?.subtle) return "";
    if (scopeToken === token && scopePromise) return scopePromise;

    scopeToken = token;
    const candidate = token;
    scopePromise = globalThis.crypto.subtle
      .digest("SHA-256", new TextEncoder().encode(candidate))
      .then((digest) => {
        if (storedToken() !== candidate) {
          scopeToken = "";
          scopePromise = null;
          return tokenScope();
        }

        const scope = Array.from(new Uint8Array(digest).slice(0, 16))
          .map((value) => value.toString(16).padStart(2, "0"))
          .join("");
        try {
          const previousScope = sessionStorage.getItem(SIDEBAR_SCOPE_KEY) || "";
          if (previousScope && previousScope !== scope) {
            removeSidebarEntries();
            resetConversationMemory({ clearStorage: true });
          }
          sessionStorage.setItem(SIDEBAR_SCOPE_KEY, scope);
        } catch (_error) {
          // The cache remains an optional acceleration only.
        }
        return scope;
      })
      .catch(() => "");
    return scopePromise;
  };

  const requestMetadata = (input, init) => {
    try {
      const request =
        typeof Request !== "undefined" && input instanceof Request ? input : null;
      const url = new URL(request?.url || input, window.location.href);
      const method = String(init?.method || request?.method || "GET").toUpperCase();
      const headers = new Headers(request?.headers || {});
      if (init?.headers) {
        new Headers(init.headers).forEach((value, key) => headers.set(key, value));
      }
      return { request, url, method, headers };
    } catch (_error) {
      return null;
    }
  };

  const authorizedScope = async (metadata) => {
    if (!metadata || metadata.url.origin !== window.location.origin) return "";
    const token = storedToken();
    if (!token || metadata.headers.get("Authorization") !== `Bearer ${token}`) return "";
    return tokenScope();
  };

  const sidebarKind = (metadata) => {
    if (!metadata || metadata.method !== "GET") return "";
    const { pathname, searchParams } = metadata.url;
    if (pathname === "/api/v1/chats/" && searchParams.get("page") === "1") {
      return "chats";
    }
    if (pathname === "/api/v1/chats/pinned") return "pinned";
    if (pathname === "/api/v1/chats/all/tags") return "tags";
    if (pathname === "/api/v1/folders/") return "folders";
    if (pathname === "/api/v1/folders/shared") return "shared-folders";
    if (pathname === "/api/v1/notes/pinned") return "pinned-notes";
    return "";
  };

  const decodePathSegment = (value) => {
    try {
      return decodeURIComponent(value);
    } catch (_error) {
      return "";
    }
  };

  const conversationResource = (metadata) => {
    if (!metadata || metadata.method !== "GET" || metadata.url.search) return null;
    const path = metadata.url.pathname;

    const taskMatch = path.match(/^\/api\/tasks\/chat\/([^/]+)$/);
    if (taskMatch) {
      return {
        chatId: decodePathSegment(taskMatch[1]),
        part: "tasks",
        ttlMs: TASK_CACHE_TTL_MS,
      };
    }

    const tagMatch = path.match(/^\/api\/v1\/chats\/([^/]+)\/tags$/);
    if (tagMatch) {
      return {
        chatId: decodePathSegment(tagMatch[1]),
        part: "tags",
        ttlMs: CONVERSATION_CACHE_TTL_MS,
      };
    }

    const chatMatch = path.match(/^\/api\/v1\/chats\/([^/]+)$/);
    if (!chatMatch) return null;
    const chatId = decodePathSegment(chatMatch[1]);
    if (!chatId || RESERVED_CHAT_SEGMENTS.has(chatId)) return null;
    return {
      chatId,
      part: "chat",
      ttlMs: CONVERSATION_CACHE_TTL_MS,
    };
  };

  const responseEntry = async (response, kind) => {
    if (!response.ok) return null;
    const contentType = response.headers.get("Content-Type") || "";
    if (!contentType.toLowerCase().includes("application/json")) return null;

    try {
      const body = await response.clone().text();
      if (new TextEncoder().encode(body).byteLength > MAX_CACHE_BODY_BYTES) return null;
      const payload = JSON.parse(body);
      if (kind === "object") {
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
      } else if (kind === "array" && !Array.isArray(payload)) {
        return null;
      }
      return {
        version: 1,
        savedAt: Date.now(),
        status: response.status,
        statusText: response.statusText,
        contentType,
        body,
      };
    } catch (_error) {
      return null;
    }
  };

  const responseFromEntry = (entry, cacheState) =>
    new Response(entry.body, {
      status: entry.status,
      statusText: entry.statusText,
      headers: {
        "Content-Type": entry.contentType || "application/json",
        "X-Turtle-Client-Cache": cacheState,
      },
    });

  const normalizedHistoryPage = (value) => {
    if (!value || typeof value !== "object") return null;
    const rangeStart = Number(value.rangeStart);
    const rangeEnd = Number(value.rangeEnd);
    if (
      !Number.isInteger(rangeStart) ||
      !Number.isInteger(rangeEnd) ||
      rangeStart < 0 ||
      rangeEnd < rangeStart ||
      typeof value.hasMore !== "boolean"
    ) {
      return null;
    }
    return {
      rangeStart,
      rangeEnd,
      hasMore: value.hasMore,
      span: Number.isInteger(Number(value.span)) ? Number(value.span) : null,
      revision: Number.isInteger(Number(value.revision))
        ? Number(value.revision)
        : null,
      messageCount: Number.isInteger(Number(value.messageCount))
        ? Number(value.messageCount)
        : null,
    };
  };

  const rememberHistoryPageFromEntry = (chatId, entry) => {
    try {
      const payload = JSON.parse(entry?.body || "null");
      const page = normalizedHistoryPage(payload?.chat?.history?.turtlePage);
      if (page) historyPageState.set(chatId, page);
    } catch (_error) {
      // A missing page sidecar only disables upward pagination for this read.
    }
  };

  const sidebarStorageKey = (scope, metadata) =>
    `${SIDEBAR_CACHE_PREFIX}${scope}:${encodeURIComponent(
      `${metadata.url.pathname}${metadata.url.search}`,
    )}`;

  const readSidebarEntry = (key) => {
    try {
      const entry = JSON.parse(sessionStorage.getItem(key) || "null");
      if (
        !entry ||
        entry.version !== 1 ||
        typeof entry.body !== "string" ||
        !Number.isFinite(entry.savedAt) ||
        Date.now() - entry.savedAt > SIDEBAR_MAX_STALE_MS
      ) {
        sessionStorage.removeItem(key);
        return null;
      }
      return entry;
    } catch (_error) {
      try {
        sessionStorage.removeItem(key);
      } catch (_storageError) {
        // Ignore optional cache cleanup failures.
      }
      return null;
    }
  };

  const writeSidebarEntry = (key, entry) => {
    try {
      sessionStorage.setItem(key, JSON.stringify(entry));
      return true;
    } catch (_error) {
      removeSidebarEntries();
      try {
        sessionStorage.setItem(key, JSON.stringify(entry));
        return true;
      } catch (_retryError) {
        return false;
      }
    }
  };

  const dispatchSidebarUpdate = (kind) => {
    try {
      window.dispatchEvent(
        new CustomEvent(SIDEBAR_UPDATED_EVENT, {
          detail: { kind },
        }),
      );
    } catch (_error) {
      // A current-page refresh is optional; the next navigation still gets fresh data.
    }
  };

  const revalidateSidebarEntry = (key, kind, input, init, previousEntry) => {
    if (sidebarRevalidating.has(key)) return;
    const generation = sidebarGeneration;
    stats.sidebarRevalidations += 1;
    const requestInput =
      typeof Request !== "undefined" && input instanceof Request ? input.clone() : input;
    const pending = originalFetch(requestInput, init)
      .then(async (response) => {
        if (generation !== sidebarGeneration) return;
        const nextEntry = await responseEntry(response, "array");
        if (!nextEntry || generation !== sidebarGeneration) return;
        writeSidebarEntry(key, nextEntry);
        if (
          nextEntry.body !== previousEntry.body &&
          (kind === "chats" || kind === "pinned")
        ) {
          sidebarFreshDeliveries.set(key, nextEntry);
          dispatchSidebarUpdate(kind);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (sidebarRevalidating.get(key) === pending) sidebarRevalidating.delete(key);
      });
    sidebarRevalidating.set(key, pending);
  };

  const fetchSidebarResponse = async (metadata, input, init, scope, kind) => {
    const key = sidebarStorageKey(scope, metadata);
    const delivery = sidebarFreshDeliveries.get(key);
    if (delivery) {
      sidebarFreshDeliveries.delete(key);
      return responseFromEntry(delivery, "revalidated");
    }

    const cached = readSidebarEntry(key);
    if (cached) {
      stats.sidebarHits += 1;
      if (Date.now() - cached.savedAt > SIDEBAR_REVALIDATE_AFTER_MS) {
        revalidateSidebarEntry(key, kind, input, init, cached);
      }
      return responseFromEntry(cached, "hit");
    }

    stats.sidebarMisses += 1;
    let pending = sidebarInflight.get(key);
    if (pending) {
      stats.sidebarSingleFlightJoins += 1;
    } else {
      const generation = sidebarGeneration;
      pending = originalFetch(input, init)
        .then(async (response) => {
          const entry = await responseEntry(response, "array");
          if (entry && generation === sidebarGeneration) writeSidebarEntry(key, entry);
          return response;
        })
        .finally(() => {
          if (sidebarInflight.get(key) === pending) sidebarInflight.delete(key);
        });
      sidebarInflight.set(key, pending);
    }
    return (await pending).clone();
  };

  const conversationKey = (scope, resource) =>
    `${scope}:${resource.chatId}:${resource.part}`;

  const conversationStorageKey = (scope, resource) =>
    `${CONVERSATION_SESSION_PREFIX}${scope}:${encodeURIComponent(resource.chatId)}:${
      resource.part
    }`;

  const pruneConversationStorage = () => {
    try {
      const now = Date.now();
      const entries = [];
      for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (!key?.startsWith(CONVERSATION_SESSION_PREFIX)) continue;
        try {
          const entry = JSON.parse(sessionStorage.getItem(key) || "null");
          if (
            !entry ||
            entry.version !== 1 ||
            !Number.isFinite(entry.expiresAt) ||
            entry.expiresAt <= now
          ) {
            sessionStorage.removeItem(key);
            continue;
          }
          entries.push({
            key,
            accessedAt: Number(entry.accessedAt) || Number(entry.savedAt) || 0,
          });
        } catch (_error) {
          sessionStorage.removeItem(key);
        }
      }
      entries.sort((left, right) => left.accessedAt - right.accessedAt);
      while (entries.length > CONVERSATION_SESSION_LIMIT) {
        sessionStorage.removeItem(entries.shift().key);
      }
    } catch (_error) {
      // Storage pruning is optional.
    }
  };

  const readConversationStorageEntry = (scope, resource) => {
    const storageKey = conversationStorageKey(scope, resource);
    try {
      const entry = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      if (
        !entry ||
        entry.version !== 1 ||
        entry.chatId !== resource.chatId ||
        entry.part !== resource.part ||
        typeof entry.body !== "string" ||
        !Number.isFinite(entry.savedAt) ||
        !Number.isFinite(entry.expiresAt) ||
        entry.expiresAt <= Date.now()
      ) {
        sessionStorage.removeItem(storageKey);
        return null;
      }
      entry.accessedAt = Date.now();
      try {
        sessionStorage.setItem(storageKey, JSON.stringify(entry));
      } catch (_error) {
        // A read hit remains usable when the LRU timestamp cannot be updated.
      }
      return entry;
    } catch (_error) {
      try {
        sessionStorage.removeItem(storageKey);
      } catch (_storageError) {
        // Ignore optional cleanup failures.
      }
      return null;
    }
  };

  const writeConversationStorageEntry = (scope, resource, entry) => {
    const storageKey = conversationStorageKey(scope, resource);
    const storedEntry = {
      ...entry,
      chatId: resource.chatId,
      part: resource.part,
      expiresAt: entry.savedAt + resource.ttlMs,
      accessedAt: Date.now(),
    };
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(storedEntry));
      pruneConversationStorage();
      return true;
    } catch (_error) {
      removeConversationStorageEntries();
      try {
        sessionStorage.setItem(storageKey, JSON.stringify(storedEntry));
        return true;
      } catch (_retryError) {
        return false;
      }
    }
  };

  const writeConversationMemoryEntry = (key, entry) => {
    conversationCache.delete(key);
    conversationCache.set(key, entry);
    while (conversationCache.size > CONVERSATION_CACHE_LIMIT) {
      const oldestKey = conversationCache.keys().next().value;
      if (oldestKey === undefined) break;
      conversationCache.delete(oldestKey);
    }
  };

  const readConversationEntry = (key, scope, resource) => {
    const entry = conversationCache.get(key);
    if (!entry || Date.now() - entry.savedAt > resource.ttlMs) {
      conversationCache.delete(key);
    } else {
      conversationCache.delete(key);
      conversationCache.set(key, entry);
      return { entry, cacheState: "memory" };
    }

    const storedEntry = readConversationStorageEntry(scope, resource);
    if (!storedEntry) return null;
    writeConversationMemoryEntry(key, storedEntry);
    return { entry: storedEntry, cacheState: "session" };
  };

  const fetchConversationResponse = async (input, init, scope, resource) => {
    const key = conversationKey(scope, resource);
    const cached = readConversationEntry(key, scope, resource);
    if (cached) {
      stats.conversationHits += 1;
      if (cached.cacheState === "session") stats.conversationSessionHits += 1;
      if (resource.part === "chat") {
        rememberHistoryPageFromEntry(resource.chatId, cached.entry);
      }
      return responseFromEntry(cached.entry, cached.cacheState);
    }

    stats.conversationMisses += 1;
    let pending = conversationInflight.get(key);
    if (!pending) {
      const generation = conversationGeneration;
      let requestInput = input;
      let requestInit = init;
      if (resource.part === "chat") {
        const pageUrl = new URL(
          `/api/v1/turtle/chat/history/${encodeURIComponent(resource.chatId)}/initial`,
          window.location.origin,
        );
        if (typeof Request !== "undefined" && input instanceof Request) {
          requestInput = new Request(pageUrl, input);
        } else {
          requestInput = pageUrl.toString();
        }
      }
      pending = originalFetch(requestInput, requestInit)
        .then(async (response) => {
          const entry = await responseEntry(
            response,
            resource.part === "tags" ? "array" : "object",
          );
          if (entry && generation === conversationGeneration) {
            if (resource.part === "chat") {
              rememberHistoryPageFromEntry(resource.chatId, entry);
            }
            writeConversationMemoryEntry(key, entry);
            writeConversationStorageEntry(scope, resource, entry);
          }
          return response;
        })
        .finally(() => {
          if (conversationInflight.get(key) === pending) conversationInflight.delete(key);
        });
      conversationInflight.set(key, pending);
    }
    return (await pending).clone();
  };

  const rebuildLoadedHistoryLinks = (history) => {
    const messages =
      history?.messages && typeof history.messages === "object" ? history.messages : {};
    const loadedIds = new Set(Object.keys(messages));
    for (const [messageId, message] of Object.entries(messages)) {
      if (!message || typeof message !== "object") {
        delete messages[messageId];
        loadedIds.delete(messageId);
        continue;
      }
      message.id = messageId;
      message.childrenIds = [];
    }
    const ordered = Object.entries(messages).sort((left, right) => {
      const leftTimestamp = Number(left[1]?.timestamp) || 0;
      const rightTimestamp = Number(right[1]?.timestamp) || 0;
      return leftTimestamp - rightTimestamp || left[0].localeCompare(right[0]);
    });
    for (const [messageId, message] of ordered) {
      const parentId = message?.parentId;
      if (!parentId || !loadedIds.has(parentId)) continue;
      const parent = messages[parentId];
      if (!parent.childrenIds.includes(messageId)) parent.childrenIds.push(messageId);
    }
    history.messages = messages;
  };

  const loadOlderHistoryRange = async (chatId, history) => {
    const normalizedId = String(chatId || "").trim();
    const inlinePage = normalizedHistoryPage(history?.turtlePage);
    const page = inlinePage || historyPageState.get(normalizedId);
    if (!inlinePage && page && history && typeof history === "object") {
      history.turtlePage = { ...page };
    }
    const beforeDepth = Number(page?.rangeStart);
    if (
      !normalizedId ||
      normalizedId.includes("/") ||
      !page?.hasMore ||
      !Number.isInteger(beforeDepth) ||
      beforeDepth <= 0
    ) {
      return false;
    }

    const token = storedToken();
    if (!token) return false;
    const key = `${normalizedId}:${beforeDepth}`;
    let pending = historyPageInflight.get(key);
    if (pending) {
      stats.historyPageSingleFlightJoins += 1;
      return pending;
    }

    stats.historyPageLoads += 1;
    pending = originalFetch(
      `/api/v1/turtle/chat/history/${encodeURIComponent(
        normalizedId,
      )}/range?before_depth=${encodeURIComponent(beforeDepth)}`,
      {
        method: "GET",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
      },
    )
      .then(async (response) => {
        if (!response.ok) return false;
        const payload = await response.json();
        if (
          !payload ||
          typeof payload !== "object" ||
          !payload.messages ||
          typeof payload.messages !== "object" ||
          !payload.page ||
          typeof payload.page !== "object"
        ) {
          return false;
        }
        history.messages =
          history.messages && typeof history.messages === "object"
            ? history.messages
            : {};
        for (const [messageId, message] of Object.entries(payload.messages)) {
          if (message && typeof message === "object") {
            history.messages[messageId] = message;
          }
        }
        history.turtlePage = payload.page;
        const nextPage = normalizedHistoryPage(payload.page);
        if (nextPage) historyPageState.set(normalizedId, nextPage);
        rebuildLoadedHistoryLinks(history);
        return true;
      })
      .catch(() => false)
      .finally(() => {
        if (historyPageInflight.get(key) === pending) historyPageInflight.delete(key);
      });
    historyPageInflight.set(key, pending);
    return pending;
  };

  const invalidateConversation = async (chatId = "") => {
    conversationGeneration += 1;
    conversationInflight.clear();
    if (!chatId) {
      conversationCache.clear();
      historyPageState.clear();
      removeConversationStorageEntries();
      return;
    }
    historyPageState.delete(chatId);
    const suffixes = [`:${chatId}:chat`, `:${chatId}:tags`, `:${chatId}:tasks`];
    for (const key of conversationCache.keys()) {
      if (suffixes.some((suffix) => key.endsWith(suffix))) conversationCache.delete(key);
    }
    removeConversationStorageEntries(chatId);
  };

  const requestBody = async (input, init) => {
    if (typeof init?.body === "string") return init.body;
    if (typeof Request !== "undefined" && input instanceof Request) {
      try {
        return await input.clone().text();
      } catch (_error) {
        return "";
      }
    }
    return "";
  };

  const mutationChatId = async (metadata, input, init) => {
    if (!metadata || metadata.method === "GET") return "";
    if (CHAT_COMPLETION_PATHS.has(metadata.url.pathname)) {
      try {
        const raw = await requestBody(input, init);
        const payload = raw ? JSON.parse(raw) : null;
        return String(payload?.chat_id || payload?.turtle_chat_id || "");
      } catch (_error) {
        return "";
      }
    }
    const match = metadata.url.pathname.match(/^\/api\/v1\/chats\/([^/]+)(?:\/|$)/);
    if (!match || RESERVED_CHAT_SEGMENTS.has(match[1])) return "";
    return decodePathSegment(match[1]);
  };

  const shouldInvalidateSidebar = (metadata) =>
    metadata?.method !== "GET" &&
    (metadata.url.pathname.startsWith("/api/v1/chats") ||
      metadata.url.pathname.startsWith("/api/v1/folders") ||
      metadata.url.pathname.startsWith("/api/v1/notes"));

  const pagedChatMutationRequest = (metadata, input, init) => {
    if (
      metadata?.method !== "POST" ||
      !/^\/api\/v1\/chats\/[^/]+$/.test(metadata.url.pathname)
    ) {
      return { input, init };
    }
    const headers = new Headers(metadata.headers);
    headers.set("X-Turtle-History-Response", "paged");
    if (typeof Request !== "undefined" && input instanceof Request) {
      return {
        input: new Request(input, { ...(init || {}), headers }),
        init: undefined,
      };
    }
    return {
      input,
      init: { ...(init || {}), headers },
    };
  };

  const prefetchConversation = async (chatId) => {
    const token = storedToken();
    const normalizedId = String(chatId || "").trim();
    if (!token || !normalizedId || normalizedId.includes("/")) return false;
    const encodedId = encodeURIComponent(normalizedId);
    const headers = {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    };
    stats.conversationPrefetches += 1;
    const requests = [
      `/api/v1/chats/${encodedId}`,
      `/api/v1/chats/${encodedId}/tags`,
      `/api/tasks/chat/${encodedId}`,
    ];
    await Promise.allSettled(
      requests.map((url) =>
        window.fetch(url, {
          method: "GET",
          headers,
          credentials: "same-origin",
        }),
      ),
    );
    return true;
  };

  const chatIdFromTarget = (target) => {
    if (!(target instanceof Element)) return "";
    const link = target.closest('a#sidebar-chat-item[href], a[href^="/c/"]');
    if (!link) return "";
    try {
      const url = new URL(link.href, window.location.href);
      const match = url.pathname.match(/^\/c\/([^/]+)$/);
      return match ? decodePathSegment(match[1]) : "";
    } catch (_error) {
      return "";
    }
  };

  const prefetchFromEvent = (event) => {
    const chatId = chatIdFromTarget(event.target);
    if (chatId) void prefetchConversation(chatId);
  };

  document.addEventListener("pointerdown", prefetchFromEvent, { passive: true });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") prefetchFromEvent(event);
  });

  window.fetch = async (input, init) => {
    const metadata = requestMetadata(input, init);
    const scope = await authorizedScope(metadata);
    const kind = scope ? sidebarKind(metadata) : "";
    const resource = scope ? conversationResource(metadata) : null;
    const changedChatId = await mutationChatId(metadata, input, init);
    const request = scope
      ? pagedChatMutationRequest(metadata, input, init)
      : { input, init };

    let response;
    if (scope && kind) {
      response = await fetchSidebarResponse(
        metadata,
        request.input,
        request.init,
        scope,
        kind,
      );
    } else if (scope && resource) {
      response = await fetchConversationResponse(
        request.input,
        request.init,
        scope,
        resource,
      );
    } else {
      response = await originalFetch(request.input, request.init);
    }

    if (
      metadata?.url.pathname === "/api/v1/auths/" &&
      (response.status === 401 || response.status === 403)
    ) {
      clearAllReadCaches();
    }
    if (metadata?.url.pathname === "/api/v1/auths/signout" && response.ok) {
      clearAllReadCaches();
    }
    if (response.ok && shouldInvalidateSidebar(metadata)) {
      invalidateSidebarSnapshots();
    }
    if (
      response.ok &&
      metadata?.method !== "GET" &&
      (changedChatId || CHAT_COMPLETION_PATHS.has(metadata?.url.pathname))
    ) {
      await invalidateConversation(changedChatId);
    }
    return response;
  };

  window.__turtleClientReadCache = Object.freeze({
    clear: clearAllReadCaches,
    prefetchConversation,
    stats: () => ({ ...stats }),
  });
  window.__turtleHistoryPager = Object.freeze({
    loadOlder: loadOlderHistoryRange,
  });
})();
