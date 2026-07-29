(() => {
  "use strict";

  // Saved selections are revalidated against the server allowlist on every load.
  const STORAGE_PREFIX = "turtle-model-controls-v2:";
  const MODEL_ENDPOINTS = new Set(["/api/models", "/api/openai/models"]);
  const CHAT_ENDPOINTS = new Set(["/api/chat/completions", "/api/openai/chat/completions"]);
  const CHAT_POLICY_ENDPOINT = "/api/v1/turtle/chat/capabilities";
  const ANNOUNCEMENTS_ENDPOINT = "/api/v1/turtle/chat/announcements";
  const PROVIDER_DISPLAY_ENDPOINT = "/api/v1/turtle/chat/provider-display";
  const CONVERSATION_INDEX_ENDPOINT = "/api/v1/turtle/chat/conversation-index";
  const AUTH_SECURITY_ENDPOINT = "/api/v1/turtle/auth/config";
  const SIGNUP_ENDPOINT = "/api/v1/auths/signup";
  const AUTH_SESSION_ENDPOINTS = new Set([
    "/api/v1/auths",
    "/api/v1/auths/",
    "/api/v1/auths/signin",
    "/api/v1/auths/signup",
    "/api/v1/auths/ldap",
  ]);
  const SIGNOUT_ENDPOINT = "/api/v1/auths/signout";
  const SESSION_ROLE_KEY = "turtle-session-role";
  const WORKSPACE_STORAGE_KEY = "turtle-provider-workspace-v1";
  const CLAUDE_WEB_SEARCH_STORAGE_KEY = "turtle-claude-web-search-v1";
  const PROVIDER_DISPLAY_CACHE_KEY = "turtle-provider-display-v1";
  const SPA_NAVIGATION_MODULE = "__TURTLE_SVELTEKIT_NAVIGATION_MODULE__";
  const CHAT_LIST_MODULE = "__TURTLE_CHAT_LIST_MODULE__";
  const CHAT_LIST_REFRESH_EXPORT = "__TURTLE_CHAT_LIST_REFRESH_EXPORT__";
  const PROVIDER_MODELS = Object.freeze({
    gpt: "gpt-5-web",
    claude: "claude-web",
  });
  const PROVIDER_ICONS = Object.freeze({
    gpt: "/static/turtle-provider-chatgpt.svg",
    claude: "/static/turtle-provider-claude.svg",
  });
  const PROVIDER_LABELS = {
    gpt: "GPT",
    claude: "Claude",
  };
  try {
    const cachedProviderDisplay = JSON.parse(
      sessionStorage.getItem(PROVIDER_DISPLAY_CACHE_KEY) || "{}",
    );
    Object.keys(PROVIDER_LABELS).forEach((provider) => {
      const label = String(cachedProviderDisplay?.[provider] || "").trim();
      if (label) PROVIDER_LABELS[provider] = label;
    });
  } catch (_error) {
    // A malformed optional display cache falls back to deployment labels.
  }
  const DEFAULT_ALLOWED = new Set([
    "gpt-5-5:instant",
    "latest:medium",
    "latest:high",
    "gpt-5-3:standard",
    "o3:standard",
  ]);
  const capabilities = new Map();
  const capabilitySources = new Map();
  let publishedProviderFamilies = new Set();
  let providerModelsCaptured = false;
  let allowedSelections = new Set(DEFAULT_ALLOWED);
  const selectionStates = new Map();
  let chatQuota = null;
  let chatSubscription = null;
  let chatPolicyIsAdmin = false;
  let fallbackNotice = null;
  let policyLoaded = false;
  let policyLoading = false;
  let policyLoadedAt = 0;
  let conversationProviders = new Map();
  let conversationCounts = { gpt: 0, claude: 0 };
  let conversationIndexLoaded = false;
  let conversationIndexRequest = null;
  let conversationIndexLoadedAt = 0;
  let providerDisplayRequest = null;
  let providerDisplayLoadedAt = 0;
  let authSecurity = {
    loaded: false,
    failed: false,
    registration_enabled: false,
    maintenance_enabled: false,
    maintenance_message: "系统正在维护，请稍后再试。",
    turnstile_enabled: false,
    turnstile_site_key: "",
    turnstile_action: "turtle_signup",
  };
  let authSecurityRequest = null;
  let turnstileScriptRequest = null;
  let turnstileWidgetId = null;
  let turnstileWidgetHost = null;
  let turnstileToken = "";
  let firstAdminSignup = false;
  let sessionRole = sessionStorage.getItem(SESSION_ROLE_KEY) || "";
  let announcements = [];
  let currentAnnouncement = null;
  let currentAnnouncementIndex = -1;
  let announcementView = "list";
  let announcementRequest = null;
  let announcementLoaded = false;
  let announcementLastFocus = null;
  let announcementDismissPending = false;
  let announcementLauncher = null;
  let spaNavigationRequest = null;
  let chatListModuleRequest = null;
  let clientReadCacheRefreshTimer = null;
  let workspaceNavigationRequest = null;
  let workspaceNavigationTarget = "";

  const fallbackGpt = {
    model_id: "gpt-5-web",
    name: "GPT",
    family: "gpt",
    family_label: "GPT",
    default_version: "gpt-5-5",
    version_field: "turtle_model_version",
    thinking_field: "turtle_thinking_level",
    picker: {
      style: "chatgpt",
      section_label: "智能",
      mode_order: [
        { selection_key: "gpt-5-5:instant", label: "极速", badge: "5.5" },
        { selection_key: "latest:medium", label: "中" },
        { selection_key: "latest:high", label: "高" },
        { selection_key: "latest:xhigh", label: "极高" },
        { selection_key: "latest:pro", label: "Pro" },
      ],
      model_order: ["latest", "gpt-5-5", "gpt-5-3", "o3"],
    },
    versions: [
      {
        id: "latest",
        label: "GPT-5.6 Sol",
        default_thinking_level: "medium",
        thinking_levels: [
          { id: "medium", label: "中" },
          { id: "high", label: "高" },
          { id: "xhigh", label: "极高" },
          { id: "pro", label: "Pro" },
        ],
      },
      {
        id: "gpt-5-5",
        label: "GPT-5.5",
        default_thinking_level: "instant",
        thinking_levels: [{ id: "instant", label: "极速" }],
      },
      {
        id: "gpt-5-3",
        label: "GPT-5.3",
        default_thinking_level: "standard",
        thinking_levels: [{ id: "standard", label: "标准" }],
      },
      {
        id: "o3",
        label: "o3",
        default_thinking_level: "standard",
        thinking_levels: [{ id: "standard", label: "推理" }],
      },
    ],
  };

  const normalize = (value) => String(value || "").trim().toLowerCase();
  const newRequestId = () => {
    if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
    const random = () => Math.floor(Math.random() * 16).toString(16);
    return `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`.replace(/[xy]/g, (value) => {
      const digit = Number.parseInt(random(), 16);
      return (value === "x" ? digit : (digit & 0x3) | 0x8).toString(16);
    });
  };

  const providerForModel = (modelId) => {
    const normalized = normalize(modelId);
    return Object.entries(PROVIDER_MODELS).find(([, id]) => normalize(id) === normalized)?.[0] || null;
  };

  // The upstream application only initializes its native model selection from
  // the query string on a fresh chat. New accounts do not have a saved native
  // selection yet, so make the current Turtle workspace explicit before the
  // deferred application bundle starts.
  const ensureInitialWorkspaceModelQuery = () => {
    if (window.location.pathname !== "/") return;
    const url = new URL(window.location.href);
    if (providerForModel(url.searchParams.get("model"))) return;
    const stored = normalize(localStorage.getItem(WORKSPACE_STORAGE_KEY));
    const provider = Object.hasOwn(PROVIDER_MODELS, stored) ? stored : "gpt";
    url.searchParams.set("model", PROVIDER_MODELS[provider]);
    window.history.replaceState(window.history.state, "", url);
  };

  ensureInitialWorkspaceModelQuery();

  const providerForProfileImage = (image) => {
    if (!(image instanceof HTMLImageElement)) return null;
    const source = image.getAttribute("src") || "";
    try {
      const url = new URL(source, window.location.origin);
      if (url.pathname === "/api/v1/models/model/profile/image") {
        return providerForModel(url.searchParams.get("id"));
      }
    } catch (_error) {
      // A malformed unrelated image is ignored below.
    }
    const provider = image.dataset.turtleProviderIcon;
    return Object.hasOwn(PROVIDER_ICONS, provider) && source === PROVIDER_ICONS[provider] ? provider : null;
  };

  const syncProviderIcons = () => {
    document.querySelectorAll('img[src*="/api/v1/models/model/profile/image"], img[data-turtle-provider-icon]').forEach((image) => {
      const provider = providerForProfileImage(image);
      const icon = provider ? PROVIDER_ICONS[provider] : null;
      if (!icon) return;
      image.dataset.turtleProviderIcon = provider;
      if (image.getAttribute("src") !== icon) image.setAttribute("src", icon);
    });
  };

  const syncProviderWorkspaceLinks = () => {
    const currentProvider = providerFromRoute();
    document.querySelectorAll('button[role="option"][data-value]').forEach((option) => {
      const provider = providerForModel(option.dataset.value);
      const existing = option.querySelector(
        ":scope > a[data-turtle-workspace-provider]",
      );
      if (!provider || provider === currentProvider) {
        existing?.remove();
        delete option.dataset.turtleWorkspaceOption;
        return;
      }

      option.dataset.turtleWorkspaceOption = provider;
      const link = existing || document.createElement("a");
      link.className = "turtle-provider-workspace-link";
      link.dataset.turtleWorkspaceProvider = provider;
      link.href = workspaceUrl(provider);
      link.setAttribute("aria-label", `切换到 ${PROVIDER_LABELS[provider]} 工作区`);
      link.setAttribute("data-sveltekit-preload-data", "hover");
      if (!existing) {
        link.addEventListener("pointerenter", () => void loadSpaNavigation(), { once: true });
        option.append(link);
      }
    });
  };

  const storedWorkspace = () => {
    const provider = normalize(localStorage.getItem(WORKSPACE_STORAGE_KEY));
    return Object.hasOwn(PROVIDER_MODELS, provider) ? provider : "gpt";
  };

  const rememberWorkspace = (provider) => {
    if (!Object.hasOwn(PROVIDER_MODELS, provider)) return;
    if (localStorage.getItem(WORKSPACE_STORAGE_KEY) !== provider) {
      localStorage.setItem(WORKSPACE_STORAGE_KEY, provider);
    }
    if (document.documentElement.dataset.turtleProvider !== provider) {
      document.documentElement.dataset.turtleProvider = provider;
    }
  };

  const claudeWebSearchEnabled = () => {
    try {
      const saved = localStorage.getItem(CLAUDE_WEB_SEARCH_STORAGE_KEY);
      return saved === null ? true : saved !== "false";
    } catch (_error) {
      return true;
    }
  };

  const rememberClaudeWebSearch = (enabled) => {
    try {
      localStorage.setItem(CLAUDE_WEB_SEARCH_STORAGE_KEY, String(Boolean(enabled)));
    } catch (_error) {
      // The in-request default remains enabled if browser storage is unavailable.
    }
  };

  const currentChatId = () => {
    const match = window.location.pathname.match(/^\/c\/([^/?#]+)/);
    if (!match) return null;
    try {
      return decodeURIComponent(match[1]);
    } catch (_error) {
      return match[1];
    }
  };

  const providerFromRoute = () => {
    const chatId = currentChatId();
    if (chatId && conversationProviders.has(chatId)) return conversationProviders.get(chatId);
    const queryProvider = providerForModel(new URLSearchParams(window.location.search).get("model"));
    if (queryProvider) return queryProvider;
    const capability = activeCapability();
    if (capability?.family && Object.hasOwn(PROVIDER_MODELS, capability.family)) return capability.family;
    return storedWorkspace();
  };

  const workspaceUrl = (provider) => {
    const url = new URL("/", window.location.origin);
    url.searchParams.set("model", PROVIDER_MODELS[provider]);
    return url.toString();
  };

  const renderClaudeWebSearchToggle = (button) => {
    const enabled = claudeWebSearchEnabled();
    const toggle = button.querySelector('[role="switch"]');
    button.setAttribute("aria-pressed", String(enabled));
    button.setAttribute(
      "aria-label",
      enabled
        ? "网页搜索已开启，由 Claude 自主判断何时搜索"
        : "网页搜索已关闭",
    );
    button.title = enabled
      ? "已将网页搜索能力交给 Claude，由模型按问题决定是否使用"
      : "开启后，Claude 会按问题自行判断是否需要搜索";
    if (toggle) {
      toggle.setAttribute("aria-checked", String(enabled));
      toggle.dataset.state = enabled ? "checked" : "unchecked";
    }
  };

  const syncClaudeWebSearchToggle = () => {
    const existingRows = Array.from(
      document.querySelectorAll("[data-turtle-claude-web-search-row]"),
    );
    if (providerFromRoute() !== "claude") {
      existingRows.forEach((row) => row.remove());
      return;
    }
    const menu = Array.from(document.querySelectorAll('[role="menu"]')).find(
      (candidate) =>
        candidate.getClientRects().length > 0
        && candidate.querySelector(
          'button[aria-label="Enable Image Generation"], button[aria-label="启用代码解释器"]',
        ),
    );
    const nativeButton = menu?.querySelector(
      'button[aria-label="Enable Image Generation"], button[aria-label="启用代码解释器"]',
    );
    const nativeRow = nativeButton?.parentElement;
    const host = nativeRow?.parentElement;
    if (!host) return;

    existingRows.forEach((row) => {
      if (row.parentElement !== host) row.remove();
    });
    let row = host.querySelector(":scope > [data-turtle-claude-web-search-row]");
    if (!row) {
      row = document.createElement("div");
      row.className = "turtle-claude-web-search-row";
      row.dataset.turtleClaudeWebSearchRow = "ready";
      row.innerHTML = `
        <button type="button" class="turtle-claude-web-search-button">
          <span class="turtle-claude-web-search-copy">
            <svg aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <circle cx="12" cy="12" r="9"></circle>
              <path d="M3 12h18M12 3a15 15 0 010 18M12 3a15 15 0 000 18"></path>
            </svg>
            <span>网页搜索</span>
          </span>
          <small>自动</small>
          <span class="turtle-claude-web-search-switch" role="switch" aria-checked="true" data-state="checked">
            <i></i>
          </span>
        </button>`;
      const button = row.querySelector("button");
      button?.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        rememberClaudeWebSearch(!claudeWebSearchEnabled());
        renderClaudeWebSearchToggle(button);
      });
      host.insertBefore(row, nativeRow);
    }
    const button = row.querySelector("button");
    if (button) renderClaudeWebSearchToggle(button);
  };

  const loadSpaNavigation = () => {
    if (spaNavigationRequest) return spaNavigationRequest;
    spaNavigationRequest = import(SPA_NAVIGATION_MODULE)
      .then((module) => (typeof module.g === "function" ? module.g : null))
      .catch(() => null);
    return spaNavigationRequest;
  };

  const loadChatListRefresh = () => {
    if (chatListModuleRequest) return chatListModuleRequest;
    chatListModuleRequest = import(CHAT_LIST_MODULE)
      .then((module) => (
        typeof module[CHAT_LIST_REFRESH_EXPORT] === "function"
          ? module[CHAT_LIST_REFRESH_EXPORT]
          : null
      ))
      .catch(() => null);
    return chatListModuleRequest;
  };

  const refreshWorkspaceChatList = async () => {
    const refresh = await loadChatListRefresh();
    if (!refresh) return false;
    try {
      const result = await refresh(storedToken(), { refreshPinned: true });
      return Boolean(result?.accepted);
    } catch (_error) {
      return false;
    }
  };

  window.addEventListener("turtle:client-read-cache-updated", () => {
    if (clientReadCacheRefreshTimer !== null) return;
    clientReadCacheRefreshTimer = window.setTimeout(() => {
      clientReadCacheRefreshTimer = null;
      void refreshWorkspaceChatList();
    }, 60);
  });

  const openWorkspace = (provider) => {
    if (!Object.hasOwn(PROVIDER_MODELS, provider)) return Promise.resolve(false);
    rememberWorkspace(provider);
    const target = workspaceUrl(provider);
    if (workspaceNavigationRequest && workspaceNavigationTarget === target) {
      return workspaceNavigationRequest;
    }
    if (workspaceNavigationRequest) return workspaceNavigationRequest;
    workspaceNavigationTarget = target;
    document.documentElement.dataset.turtleWorkspaceNavigating = provider;
    workspaceNavigationRequest = (async () => {
      const navigate = await loadSpaNavigation();
      if (navigate) {
        try {
          await navigate(target);
          await refreshWorkspaceChatList();
          void loadConversationIndex(true);
          queueMount();
          return true;
        } catch (_error) {
          // Fall through to the ordinary link if the pinned UI router changed.
        }
      }
      const newChatLink = document.querySelector("#sidebar-new-chat-button");
      if (newChatLink instanceof HTMLAnchorElement) {
        newChatLink.setAttribute("href", target);
        newChatLink.click();
        return true;
      }
      window.location.assign(target);
      return true;
    })().finally(() => {
      workspaceNavigationRequest = null;
      workspaceNavigationTarget = "";
      delete document.documentElement.dataset.turtleWorkspaceNavigating;
    });
    return workspaceNavigationRequest;
  };

  const dismissNativeWorkspacePicker = () => {
    const selector = document.querySelector(
      ':is(#model-selector-0-button, #model-selector-model-button)[aria-expanded="true"]',
    );
    if (!selector) return;
    const outside = document.querySelector("main");
    if (!(outside instanceof HTMLElement)) return;
    selector.blur();
    const view = outside.ownerDocument.defaultView;
    if (typeof view?.PointerEvent === "function") {
      outside.dispatchEvent(
        new view.PointerEvent("pointerdown", {
          bubbles: true,
          cancelable: true,
          composed: true,
        }),
      );
    }
    outside.click();
  };

  const chatIdFromLink = (link) => {
    try {
      const match = new URL(link.getAttribute("href") || "", window.location.origin).pathname.match(/^\/c\/([^/?#]+)/);
      return match ? decodeURIComponent(match[1]) : null;
    } catch (_error) {
      return null;
    }
  };

  const applyProviderDisplay = (value) => {
    if (!value || typeof value !== "object") return;
    const cache = {};
    Object.keys(PROVIDER_MODELS).forEach((provider) => {
      const next = String(value[provider] || "").trim();
      if (next) {
        PROVIDER_LABELS[provider] = next;
        cache[provider] = next;
      }
    });
    if (Object.keys(cache).length) {
      try {
        sessionStorage.setItem(PROVIDER_DISPLAY_CACHE_KEY, JSON.stringify(cache));
      } catch (_error) {
        // Display labels remain usable in memory when browser storage is full.
      }
    }
    fallbackGpt.name = PROVIDER_LABELS.gpt;
    fallbackGpt.family_label = PROVIDER_LABELS.gpt;
    queueMount();
  };

  const loadProviderDisplay = async (force = false) => {
    if (providerDisplayRequest) return providerDisplayRequest;
    if (!force && providerDisplayLoadedAt && Date.now() - providerDisplayLoadedAt < 30_000) return true;
    providerDisplayRequest = (async () => {
      try {
        const response = await originalFetch(PROVIDER_DISPLAY_ENDPOINT, {
          headers: policyHeaders(),
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) return false;
        const payload = await response.json();
        applyProviderDisplay(payload?.items);
        providerDisplayLoadedAt = Date.now();
        return true;
      } catch (_error) {
        return false;
      }
    })();
    try {
      return await providerDisplayRequest;
    } finally {
      providerDisplayRequest = null;
    }
  };

  const modelSelector = () =>
    document.querySelector("#model-selector-0-button, #model-selector-model-button");

  const conversationHeading = () =>
    Array.from(
      document.querySelectorAll(
        '#sidebar-folder-button, button[aria-controls="sidebar-chats-content"]',
      ),
    ).find((button) => {
      const label = normalize(button.textContent);
      return label.includes("对话") || label === "chats" || label === "chat history";
    }) || null;

  const syncWorkspaceContext = (provider) => {
    // The header already owns Provider switching. Keep the sidebar as a clear
    // history context instead of rendering a second, competing tab control.
    document.querySelector("#turtle-provider-workspaces")?.remove();
    const label = PROVIDER_LABELS[provider] || provider;
    const count = Number(conversationCounts[provider] || 0);
    const selector = modelSelector();
    if (selector) {
      const workspaceLabel = `${label} 工作区`;
      if (selector.dataset.turtleWorkspace !== provider) selector.dataset.turtleWorkspace = provider;
      if (selector.dataset.turtleWorkspaceLabel !== workspaceLabel) {
        selector.dataset.turtleWorkspaceLabel = workspaceLabel;
      }
      selector.setAttribute("aria-label", `切换对话工作区，当前：${label}`);
      selector.title = `切换 ${Object.values(PROVIDER_LABELS).join(" / ")} 工作区`;
      const visual = selector.firstElementChild;
      if (visual && visual.dataset.turtleWorkspaceLabel !== workspaceLabel) {
        visual.dataset.turtleWorkspaceLabel = workspaceLabel;
      }
    }

    const heading = conversationHeading();
    if (heading) {
      const headingLabel = `${label} 对话`;
      if (heading.dataset.turtleWorkspace !== provider) heading.dataset.turtleWorkspace = provider;
      heading.dataset.turtleChatHeading = "true";
      heading.setAttribute("aria-label", `${headingLabel}，共 ${count} 条`);
      const labelNode =
        (heading.matches('button[aria-controls="sidebar-chats-content"]')
          ? heading.querySelector(":scope > span")
          : heading.querySelector("button > div")) ||
        heading.querySelector(":scope > span");
      if (labelNode && labelNode.textContent !== headingLabel) labelNode.textContent = headingLabel;
      let countNode = heading.querySelector("[data-turtle-conversation-count]");
      if (!countNode) {
        countNode = document.createElement("small");
        countNode.dataset.turtleConversationCount = "true";
        countNode.setAttribute("aria-hidden", "true");
        heading.append(countNode);
      }
      if (countNode.textContent !== String(count)) countNode.textContent = String(count);
    }

    // Open WebUI handles same-origin links inside its SPA. Repoint the menu
    // link and use a full document navigation below so /admin always reaches
    // the standalone Turtle console rather than the legacy admin route.
    document.querySelectorAll('a[href="/admin"], a[href="/admin/"], a[href="/admin#/overview"]').forEach((link) => {
      if (link.getAttribute("href") !== "/admin#/overview") link.setAttribute("href", "/admin#/overview");
    });
  };

  const updateDateHeadings = (groups) => {
    const parents = new Set(groups.map((group) => group.parentElement).filter(Boolean));
    document.querySelectorAll('[data-turtle-date-header="true"]').forEach((header) => {
      delete header.dataset.turtleDateHeader;
      delete header.dataset.turtleProviderHidden;
    });
    parents.forEach((parent) => {
      const children = Array.from(parent.children);
      for (let index = 0; index < children.length; index += 1) {
        const candidate = children[index];
        const next = children[index + 1];
        if (
          next?.id !== "sidebar-chat-group" ||
          candidate.id === "sidebar-folder-button" ||
          candidate.querySelector?.("#sidebar-folder-button")
        ) {
          continue;
        }
        let visible = false;
        for (let cursor = index + 1; cursor < children.length && children[cursor].id === "sidebar-chat-group"; cursor += 1) {
          if (children[cursor].dataset.turtleProviderHidden !== "true") visible = true;
        }
        candidate.dataset.turtleDateHeader = "true";
        if (visible) delete candidate.dataset.turtleProviderHidden;
        else candidate.dataset.turtleProviderHidden = "true";
      }
    });
  };

  const syncProviderWorkspace = () => {
    const provider = providerFromRoute();
    rememberWorkspace(provider);
    syncWorkspaceContext(provider);

    const groups = Array.from(document.querySelectorAll("#sidebar-chat-group"));
    groups.forEach((group) => {
      const link = group.querySelector('a#sidebar-chat-item[href]') || group.querySelector('a[href]');
      const chatId = link ? chatIdFromLink(link) : null;
      const chatProvider =
        (chatId && conversationProviders.get(chatId)) ||
        (group.dataset.turtleChatProvider && Object.hasOwn(PROVIDER_MODELS, group.dataset.turtleChatProvider)
          ? group.dataset.turtleChatProvider
          : null) ||
        "gpt";
      if (group.dataset.turtleChatProvider !== chatProvider) group.dataset.turtleChatProvider = chatProvider;
      if (chatProvider === provider) delete group.dataset.turtleProviderHidden;
      else group.dataset.turtleProviderHidden = "true";
      if (link) {
        link.dataset.turtleChatProvider = chatProvider;
        if (chatProvider === provider) delete link.dataset.turtleProviderHidden;
        else link.dataset.turtleProviderHidden = "true";
      }
    });

    // Search results and future sidebar variants may render a conversation
    // link outside the standard ChatItem wrapper. Filter those links too and
    // fail closed to GPT for legacy/unknown IDs instead of inheriting the
    // currently selected workspace.
    document.querySelectorAll('a#sidebar-chat-item[href], #sidebar a[href]').forEach((link) => {
      const chatId = chatIdFromLink(link);
      if (!chatId) return;
      const chatProvider = conversationProviders.get(chatId) || "gpt";
      link.dataset.turtleChatProvider = chatProvider;
      if (chatProvider === provider) delete link.dataset.turtleProviderHidden;
      else link.dataset.turtleProviderHidden = "true";
      const row = link.closest("#sidebar-chat-group");
      if (!row) return;
      row.dataset.turtleChatProvider = chatProvider;
      if (chatProvider === provider) delete row.dataset.turtleProviderHidden;
      else row.dataset.turtleProviderHidden = "true";
    });
    updateDateHeadings(groups);
  };

  const loadConversationIndex = async (force = false) => {
    if (conversationIndexRequest) return conversationIndexRequest;
    if (!force && conversationIndexLoaded && Date.now() - conversationIndexLoadedAt < 15_000) return true;
    conversationIndexRequest = (async () => {
      try {
        const response = await originalFetch(CONVERSATION_INDEX_ENDPOINT, {
          headers: policyHeaders(),
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) return false;
        const payload = await response.json();
        const nextProviders = new Map();
        const nextCounts = { gpt: 0, claude: 0 };
        (Array.isArray(payload.items) ? payload.items : []).forEach((item) => {
          const id = String(item?.id || "");
          const provider = normalize(item?.provider);
          if (!id || !Object.hasOwn(PROVIDER_MODELS, provider)) return;
          nextProviders.set(id, provider);
          nextCounts[provider] += 1;
        });
        conversationProviders = nextProviders;
        conversationCounts = nextCounts;
        conversationIndexLoaded = true;
        conversationIndexLoadedAt = Date.now();
        document.documentElement.dataset.turtleConversationIndex = "ready";
        queueMount();
        return true;
      } catch (_error) {
        // Authentication can still be settling during startup; the sidebar
        // keeps legacy chats in GPT until the lightweight index succeeds.
        return false;
      }
    })();
    try {
      return await conversationIndexRequest;
    } finally {
      conversationIndexRequest = null;
    }
  };

  const isChatCollectionRequest = (path, method) => {
    if (method === "POST") return path === "/api/v1/chats/tags";
    if (method !== "GET") return false;
    if (
      path === "/api/v1/chats/" ||
      path === "/api/v1/chats/list" ||
      path === "/api/v1/chats/pinned" ||
      path === "/api/v1/chats/search"
    ) {
      return true;
    }
    return /^\/api\/v1\/chats\/folder\/[^/]+(?:\/list)?$/.test(path);
  };

  const rewriteChatCollection = (response, payload, providerForChat) => {
    const provider = providerFromRoute();
    const filtered = payload.filter((chat) => {
      const chatId = String(chat?.id || "");
      return chatId && providerForChat(chat, chatId) === provider;
    });
    const headers = new Headers(response.headers);
    headers.delete("Content-Length");
    headers.delete("Content-Encoding");
    headers.set("Content-Type", "application/json");
    return new Response(JSON.stringify(filtered), {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  };

  const providerFilteredResponse = async (response, path, method) => {
    if (!response.ok || !isChatCollectionRequest(path, method)) return response;
    try {
      const payload = await response.clone().json();
      if (!Array.isArray(payload)) return response;
      const embeddedProviders = new Map();
      const hasEmbeddedProviders = payload.every((chat) => {
        const chatId = String(chat?.id || "");
        if (!chatId) return true;
        const provider = normalize(chat?.provider);
        if (!Object.hasOwn(PROVIDER_MODELS, provider)) return false;
        embeddedProviders.set(chatId, provider);
        return true;
      });
      if (hasEmbeddedProviders) {
        // The ordinary sidebar endpoint now carries the immutable provider
        // enum. Filter its response immediately instead of serially waiting
        // for a second authenticated index request. The full index still
        // refreshes in the background for workspace totals and older routes.
        embeddedProviders.forEach((provider, chatId) => {
          conversationProviders.set(chatId, provider);
        });
        queueMount();
        void loadConversationIndex();
        return rewriteChatCollection(
          response,
          payload,
          (chat, chatId) => embeddedProviders.get(chatId) || normalize(chat?.provider),
        );
      }
      await loadConversationIndex();
      if (
        conversationIndexLoaded &&
        payload.some((chat) => String(chat?.id || "") && !conversationProviders.has(String(chat.id)))
      ) {
        // A just-created chat can reach the list before the scheduled index
        // refresh. Re-read once so the new Claude row is not hidden by the
        // legacy-GPT fallback until a later page reload.
        await loadConversationIndex(true);
      }
      if (!conversationIndexLoaded) return response;
      return rewriteChatCollection(
        response,
        payload,
        (_chat, chatId) => conversationProviders.get(chatId) || "gpt",
      );
    } catch (_error) {
      return response;
    }
  };

  const providerModelResponse = async (response, path, method) => {
    if (!response.ok || method !== "GET" || !MODEL_ENDPOINTS.has(path)) return response;
    try {
      // Cached/fallback labels are enough to release the model response.
      // Refresh custom names in parallel instead of serially delaying the
      // composer and model picker on every cold document load.
      void loadProviderDisplay();
      const payload = await response.clone().json();
      const source = Array.isArray(payload) ? payload : Array.isArray(payload?.data) ? payload.data : null;
      if (!source) return response;
      const models = source
        .filter((model) => normalize(model?.id || model?.name) !== "arena-model")
        .map((model) => {
          const provider = providerForModel(model?.id || model?.name);
          if (!provider) return model;
          const next = { ...model, name: PROVIDER_LABELS[provider] };
          if (model?.turtle && typeof model.turtle === "object") {
            next.turtle = { ...model.turtle, family_label: PROVIDER_LABELS[provider] };
          }
          return next;
        });
      const rewritten = Array.isArray(payload) ? models : { ...payload, data: models };
      const headers = new Headers(response.headers);
      headers.delete("Content-Length");
      headers.delete("Content-Encoding");
      headers.set("Content-Type", "application/json");
      return new Response(JSON.stringify(rewritten), {
        status: response.status,
        statusText: response.statusText,
        headers,
      });
    } catch (_error) {
      return response;
    }
  };

  const registerCapability = (model) => {
    if (!model || !model.id || !model.turtle || !Array.isArray(model.turtle.versions)) return;
    capabilitySources.set(model.id, model);
    const versions = model.turtle.versions
      .map((version) => ({
        ...version,
        thinking_levels: Array.isArray(version.thinking_levels)
          ? version.thinking_levels.map((level) => {
              const key = level.key || `${version.id}:${level.id}`;
              const state = selectionStates.get(key) || {
                selection_key: key,
                allowed: allowedSelections.has(key),
                available: allowedSelections.has(key),
                status: allowedSelections.has(key) ? "available" : "forbidden",
                limit_count: null,
                used_count: 0,
                reserved_count: 0,
                remaining_count: null,
                window_seconds: 0,
                reset_at: null,
                fallback_key: null,
                fallback_label: null,
              };
              return { ...level, key, ...state };
            })
          : [],
      }))
      .filter((version) => version.thinking_levels.length > 0)
      .map((version) => {
        const allowed = version.thinking_levels.some((level) => level.allowed);
        const available = version.thinking_levels.some((level) => level.available);
        return {
          ...version,
          allowed,
          available,
          status: available ? "available" : allowed ? "exhausted" : "forbidden",
        };
      });
    if (!versions.length) return;
    const defaultVersion = versions.some((version) => version.id === model.turtle.default_version)
      ? model.turtle.default_version
      : versions[0].id;
    capabilities.set(model.id, {
      ...model.turtle,
      versions,
      default_version: defaultVersion,
      model_id: model.id,
      name: model.name || model.turtle.family_label || model.id,
    });
  };

  registerCapability({ id: fallbackGpt.model_id, name: fallbackGpt.name, turtle: fallbackGpt });

  const capabilityForModel = (modelId) => {
    const normalized = normalize(modelId);
    for (const capability of capabilities.values()) {
      if (
        normalized === normalize(capability.model_id) ||
        normalized === normalize(capability.family) ||
        normalized === normalize(capability.name) ||
        normalized === normalize(capability.family_label)
      ) {
        return capability;
      }
    }
    return null;
  };

  const readSelection = (capability) => {
    const fallback = {
      version: capability.default_version,
      thinking: null,
      mode: capability.picker?.style === "chatgpt" ? "smart" : null,
    };
    try {
      const saved = JSON.parse(localStorage.getItem(`${STORAGE_PREFIX}${capability.family}`) || "null");
      if (!saved || typeof saved !== "object") return fallback;
      return {
        version: typeof saved.version === "string" ? saved.version : fallback.version,
        thinking: typeof saved.thinking === "string" ? saved.thinking : null,
        mode: saved.mode === "legacy" ? "legacy" : fallback.mode,
      };
    } catch (_error) {
      return fallback;
    }
  };

  const validSelection = (capability, selection) => {
    const modeKeys = new Set(
      (capability.picker?.mode_order || []).map((item) => item.selection_key),
    );
    const resolved = (version, thinking, requestedMode = selection.mode) => {
      const key = `${version}:${thinking}`;
      const mode = capability.picker?.style === "chatgpt"
        ? (requestedMode === "legacy" && version !== "latest" ? "legacy" : modeKeys.has(key) ? "smart" : "legacy")
        : null;
      return { version, thinking, mode };
    };
    const requestedVersion = capability.versions.find((item) => item.id === selection.version);
    const requestedThinking = requestedVersion?.thinking_levels.find((item) => item.id === selection.thinking);
    if (requestedThinking?.available) {
      return resolved(requestedVersion.id, requestedThinking.id);
    }

    const fallbackKey = requestedThinking?.fallback_key;
    if (fallbackKey) {
      for (const version of capability.versions) {
        const fallback = version.thinking_levels.find((item) => item.key === fallbackKey && item.available);
        if (fallback) return resolved(version.id, fallback.id, "smart");
      }
    }

    const version =
      capability.versions.find(
        (item) => item.id === selection.version && item.thinking_levels.some((level) => level.available),
      ) ||
      capability.versions.find(
        (item) => item.id === capability.default_version && item.thinking_levels.some((level) => level.available),
      ) ||
      capability.versions.find((item) => item.thinking_levels.some((level) => level.available)) ||
      capability.versions.find((item) => item.thinking_levels.some((level) => level.allowed)) ||
      capability.versions[0];
    const thinking =
      version.thinking_levels.find((item) => item.id === selection.thinking && item.available) ||
      version.thinking_levels.find((item) => item.id === version.default_thinking_level && item.available) ||
      version.thinking_levels.find((item) => item.available) ||
      version.thinking_levels.find((item) => item.allowed) ||
      version.thinking_levels[0];
    return resolved(version.id, thinking.id, "smart");
  };

  const saveSelection = (capability, selection) => {
    localStorage.setItem(`${STORAGE_PREFIX}${capability.family}`, JSON.stringify(selection));
  };

  const storedToken = () => {
    let value = localStorage.getItem("token") || "";
    if (!value) return "";
    try {
      const parsed = JSON.parse(value);
      if (typeof parsed === "string") value = parsed;
    } catch (_error) {
      // Open WebUI normally stores a raw token.
    }
    return value;
  };

  const policyHeaders = () => {
    const headers = new Headers();
    const token = storedToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return headers;
  };

  const sanitizeAnnouncementHtml = (value) => {
    const allowed = new Set([
      "A", "BLOCKQUOTE", "BR", "CODE", "DEL", "EM", "H1", "H2", "H3",
      "H4", "H5", "H6", "HR", "LI", "OL", "P", "PRE", "S", "STRONG",
      "TABLE", "TBODY", "TD", "TH", "THEAD", "TR", "UL",
    ]);
    const template = document.createElement("template");
    template.innerHTML = String(value || "");
    Array.from(template.content.querySelectorAll("*")).forEach((element) => {
      if (!allowed.has(element.tagName)) {
        element.replaceWith(...Array.from(element.childNodes));
        return;
      }
      const href = element.tagName === "A" ? element.getAttribute("href") : null;
      Array.from(element.attributes).forEach((attribute) => {
        element.removeAttribute(attribute.name);
      });
      if (element.tagName !== "A") return;
      try {
        const target = new URL(String(href || ""), window.location.origin);
        if (!["http:", "https:", "mailto:"].includes(target.protocol)) throw new Error("unsafe link");
        element.setAttribute("href", target.href);
        element.setAttribute("target", "_blank");
        element.setAttribute("rel", "noopener noreferrer nofollow");
      } catch (_error) {
        element.replaceWith(document.createTextNode(element.textContent || ""));
      }
    });
    return template.innerHTML;
  };

  const positionAnnouncementLauncher = (launcher) => {
    if (!launcher) return false;
    const temporaryChat = document.querySelector("#temporary-chat-button");
    const anchor = temporaryChat?.parentElement;
    const headerActions = anchor?.parentElement;
    if (!temporaryChat || !headerActions || !anchor) {
      // The native header is still hydrating (or has just been replaced by
      // Svelte). Keep the announcement action detached so it cannot flash as
      // a standalone control before its peers are ready.
      launcher.hidden = true;
      launcher.remove();
      delete launcher.dataset.placement;
      return false;
    }
    launcher.className = temporaryChat.className;
    const launcherIcon = launcher.querySelector("svg");
    const peerIcon = temporaryChat.querySelector("svg");
    if (launcherIcon && peerIcon) {
      const peerClass = peerIcon.getAttribute("class");
      const peerStrokeWidth = peerIcon.getAttribute("stroke-width");
      if (peerClass) launcherIcon.setAttribute("class", peerClass);
      if (peerStrokeWidth) launcherIcon.setAttribute("stroke-width", peerStrokeWidth);
    }
    if (launcher.parentElement !== headerActions || launcher.nextElementSibling !== anchor) {
      headerActions.insertBefore(launcher, anchor);
    }
    launcher.dataset.placement = "header";
    return true;
  };

  const ensureAnnouncementUi = () => {
    let launcher =
      document.querySelector("#turtle-announcement-launcher")
      || announcementLauncher;
    let shell = document.querySelector("#turtle-announcement-shell");
    if (!launcher) {
      launcher = document.createElement("button");
      launcher.id = "turtle-announcement-launcher";
      launcher.type = "button";
      launcher.className = "flex cursor-pointer px-2 py-2 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-850 transition";
      launcher.hidden = true;
      launcher.setAttribute("aria-label", "查看公告");
      launcher.innerHTML = `
        <svg class="size-4.5" aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path>
        </svg>
        <strong>公告</strong>
        <small data-announcement-unread hidden><i></i></small>`;
      launcher.addEventListener("click", () => showAnnouncement());
    }
    announcementLauncher = launcher;
    if (!shell) {
      shell = document.createElement("div");
      shell.id = "turtle-announcement-shell";
      shell.hidden = true;
      shell.innerHTML = `
        <button type="button" class="turtle-announcement-backdrop" data-announcement-dismiss aria-label="关闭公告"></button>
        <section class="turtle-announcement-dialog" role="dialog" aria-modal="true" aria-labelledby="turtle-announcement-title">
          <header>
            <div class="turtle-announcement-heading">
              <span class="turtle-announcement-heading-icon" aria-hidden="true">
                <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path>
                </svg>
              </span>
              <div>
                <h2 id="turtle-announcement-title">公告</h2>
                <small data-announcement-summary></small>
              </div>
            </div>
            <button type="button" class="turtle-announcement-close" data-announcement-dismiss aria-label="关闭公告">
              <svg aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"></path>
              </svg>
            </button>
          </header>
          <div class="turtle-announcement-list-view" data-announcement-list-view>
            <div class="turtle-announcement-list" data-announcement-list></div>
          </div>
          <div class="turtle-announcement-detail-view" data-announcement-detail-view hidden>
            <div class="turtle-announcement-detail-heading">
              <button type="button" class="turtle-announcement-back" data-announcement-back>
                <svg aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"></path>
                </svg>
                公告列表
              </button>
              <small data-announcement-date></small>
              <h3 data-announcement-detail-title></h3>
            </div>
            <div class="turtle-announcement-content">
              <div class="turtle-announcement-markdown" data-announcement-body></div>
            </div>
            <footer>
              <span data-announcement-version></span>
              <button type="button" class="turtle-announcement-confirm" data-announcement-confirm>标为已读</button>
            </footer>
          </div>
        </section>`;
      document.body.append(shell);
      shell.querySelectorAll("[data-announcement-dismiss]").forEach((button) => {
        button.addEventListener("click", () => void closeAnnouncement(false));
      });
      shell.querySelector("[data-announcement-confirm]")?.addEventListener(
        "click",
        () => void acknowledgeCurrentAnnouncement(),
      );
      shell.querySelector("[data-announcement-back]")?.addEventListener(
        "click",
        () => showAnnouncement(),
      );
      shell.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          void closeAnnouncement(false);
          return;
        }
        if (event.key !== "Tab") return;
        const focusable = Array.from(
          shell.querySelectorAll(
            ".turtle-announcement-dialog button:not([disabled]), .turtle-announcement-dialog a[href]",
          ),
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
    }
    const launcherMounted = positionAnnouncementLauncher(launcher);
    return { launcher, launcherMounted, shell };
  };

  const unreadAnnouncementCount = () =>
    announcements.filter((item) => item?.enabled && item?.should_show).length;

  const announcementRelativeTime = (createdAt) => {
    const timestamp = Number(createdAt || 0);
    if (!timestamp) return "";
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    if (elapsed < 60) return "刚刚";
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)}分钟前`;
    if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}小时前`;
    if (elapsed < 30 * 86400) return `${Math.floor(elapsed / 86400)}天前`;
    return new Date(timestamp * 1000).toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

  const announcementDateTime = (createdAt) => {
    const timestamp = Number(createdAt || 0);
    if (!timestamp) return "";
    return new Date(timestamp * 1000).toLocaleString("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const announcementStatusIcon = (unread) => unread
    ? `<span class="turtle-announcement-status is-unread" aria-hidden="true">
        <i></i>
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
      </span>`
    : `<span class="turtle-announcement-status" aria-hidden="true">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
      </span>`;

  const renderAnnouncementList = (shell) => {
    const list = shell.querySelector("[data-announcement-list]");
    if (!list) return;
    list.replaceChildren();
    announcements.forEach((item, index) => {
      const unread = Boolean(item.should_show);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "turtle-announcement-row";
      row.dataset.unread = String(unread);
      row.setAttribute("aria-label", `${item.title || "公告"}${unread ? "，未读" : ""}`);
      row.innerHTML = `${announcementStatusIcon(unread)}
        <span class="turtle-announcement-row-copy">
          <strong></strong>
          <small><time></time><em data-announcement-row-unread>未读</em></small>
        </span>
        <svg class="turtle-announcement-chevron" aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"></path>
        </svg>`;
      row.querySelector("strong").textContent = item.title || "公告";
      row.querySelector("time").textContent =
        announcementRelativeTime(item.created_at || item.updated_at);
      const unreadLabel = row.querySelector("[data-announcement-row-unread]");
      if (unreadLabel) unreadLabel.hidden = !unread;
      row.addEventListener("click", () => showAnnouncementDetail(index, true));
      list.append(row);
    });
  };

  const renderCurrentAnnouncement = () => {
    const { launcher, launcherMounted, shell } = ensureAnnouncementUi();
    const unreadBadge = launcher.querySelector("[data-announcement-unread]");
    const unreadCount = unreadAnnouncementCount();
    launcher.hidden = announcements.length === 0 || !launcherMounted;
    launcher.setAttribute(
      "aria-label",
      unreadCount ? `查看公告，${unreadCount} 条未读` : "查看公告",
    );
    if (unreadBadge) {
      unreadBadge.hidden = unreadCount === 0;
    }
    const summary = shell.querySelector("[data-announcement-summary]");
    if (summary) {
      summary.textContent = unreadCount
        ? `${unreadCount} 条未读 · 共 ${announcements.length} 条`
        : `共 ${announcements.length} 条公告`;
    }
    if (!announcements.length) {
      currentAnnouncement = null;
      currentAnnouncementIndex = -1;
      shell.hidden = true;
      delete document.documentElement.dataset.turtleAnnouncementOpen;
      return;
    }
    renderAnnouncementList(shell);
    if (
      currentAnnouncementIndex < 0
      || currentAnnouncementIndex >= announcements.length
    ) currentAnnouncementIndex = 0;
    currentAnnouncement = announcements[currentAnnouncementIndex];
    shell.querySelector("[data-announcement-detail-title]").textContent =
      currentAnnouncement.title || "公告";
    shell.querySelector("[data-announcement-version]").textContent =
      `第 ${currentAnnouncementIndex + 1}/${announcements.length} 条 · 版本 ${currentAnnouncement.revision || 1}`;
    shell.querySelector("[data-announcement-date]").textContent =
      announcementDateTime(currentAnnouncement.created_at || currentAnnouncement.updated_at);
    shell.querySelector("[data-announcement-body]").innerHTML =
      sanitizeAnnouncementHtml(currentAnnouncement.html);
    const listView = shell.querySelector("[data-announcement-list-view]");
    const detailView = shell.querySelector("[data-announcement-detail-view]");
    if (listView) listView.hidden = announcementView !== "list";
    if (detailView) detailView.hidden = announcementView !== "detail";
    const confirm = shell.querySelector("[data-announcement-confirm]");
    if (confirm) {
      confirm.disabled = announcementDismissPending;
      confirm.textContent =
        currentAnnouncement.should_show && sessionRole !== "admin"
          ? "标为已读"
          : "关闭";
    }
  };

  const showAnnouncement = () => {
    if (!announcements.length) return;
    const { shell } = ensureAnnouncementUi();
    announcementView = "list";
    renderCurrentAnnouncement();
    if (shell.hidden) announcementLastFocus = document.activeElement;
    shell.hidden = false;
    document.documentElement.dataset.turtleAnnouncementOpen = "true";
    shell.querySelector(".turtle-announcement-row, .turtle-announcement-close")?.focus();
  };

  const showAnnouncementDetail = (index, markOnOpen = false) => {
    if (!announcements.length || index < 0 || index >= announcements.length) return;
    const { shell } = ensureAnnouncementUi();
    currentAnnouncementIndex = index;
    currentAnnouncement = announcements[index];
    announcementView = "detail";
    renderCurrentAnnouncement();
    if (shell.hidden) announcementLastFocus = document.activeElement;
    shell.hidden = false;
    document.documentElement.dataset.turtleAnnouncementOpen = "true";
    shell.querySelector("[data-announcement-back]")?.focus();
    if (markOnOpen && currentAnnouncement.should_show) {
      void dismissAnnouncement(currentAnnouncement);
    }
  };

  const dismissAnnouncement = async (announcement = currentAnnouncement) => {
    if (
      !announcement?.enabled
      || announcement.dismissed
      || sessionRole === "admin"
    ) return true;
    const revision = Number(announcement.revision || 0);
    const announcementId = String(announcement.id || "");
    if (!revision || !announcementId || announcementDismissPending) return false;
    announcementDismissPending = true;
    renderCurrentAnnouncement();
    try {
      const headers = policyHeaders();
      headers.set("Content-Type", "application/json");
      const response = await originalFetch(
        `${ANNOUNCEMENTS_ENDPOINT}/${encodeURIComponent(announcementId)}/dismiss`,
        {
        method: "POST",
        headers,
        credentials: "same-origin",
        body: JSON.stringify({ revision }),
        },
      );
      if (response.status === 409) {
        announcementLoaded = false;
        await loadAnnouncements(true);
        return false;
      }
      if (!response.ok) return false;
      announcements = announcements.map((item) =>
        item.id === announcementId
          ? { ...item, dismissed: true, should_show: false }
          : item,
      );
      return true;
    } catch (_error) {
      // The user can still close the dialog. A failed receipt write means the
      // same version will be offered again on a later page entry.
      return false;
    } finally {
      announcementDismissPending = false;
      renderCurrentAnnouncement();
    }
  };

  const hideAnnouncement = () => {
    const shell = document.querySelector("#turtle-announcement-shell");
    if (!shell || shell.hidden) return;
    shell.hidden = true;
    delete document.documentElement.dataset.turtleAnnouncementOpen;
    announcementLastFocus?.focus?.();
    announcementLastFocus = null;
  };

  const closeAnnouncement = async (recordDismissal) => {
    if (recordDismissal) await dismissAnnouncement();
    hideAnnouncement();
  };

  const acknowledgeCurrentAnnouncement = async () => {
    const previousId = currentAnnouncement?.id;
    const acknowledged = await dismissAnnouncement();
    if (!acknowledged) return;
    const previousIndex = Math.max(
      0,
      announcements.findIndex((item) => item.id === previousId),
    );
    const nextUnreadOffset = Array.from(
      { length: announcements.length },
      (_value, offset) => (previousIndex + offset + 1) % announcements.length,
    ).find((index) => announcements[index]?.should_show);
    if (nextUnreadOffset === undefined) {
      hideAnnouncement();
      return;
    }
    currentAnnouncementIndex = nextUnreadOffset;
    announcementView = "detail";
    renderCurrentAnnouncement();
    document.querySelector(".turtle-announcement-confirm")?.focus();
  };

  const renderAnnouncements = (items) => {
    announcements = Array.isArray(items)
      ? items.filter((item) => item?.enabled && item?.id)
      : [];
    currentAnnouncementIndex = announcements.findIndex(
      (item) => item.should_show,
    );
    if (currentAnnouncementIndex < 0 && announcements.length) {
      currentAnnouncementIndex = 0;
    }
    renderCurrentAnnouncement();
    if (currentAnnouncement?.should_show) {
      showAnnouncementDetail(currentAnnouncementIndex, false);
    }
  };

  const resetAnnouncement = () => {
    announcements = [];
    currentAnnouncement = null;
    currentAnnouncementIndex = -1;
    announcementView = "list";
    announcementRequest = null;
    announcementLoaded = false;
    announcementDismissPending = false;
    const launcher = document.querySelector("#turtle-announcement-launcher");
    const shell = document.querySelector("#turtle-announcement-shell");
    if (launcher) launcher.hidden = true;
    if (shell) shell.hidden = true;
    delete document.documentElement.dataset.turtleAnnouncementOpen;
  };

  const loadAnnouncements = async (force = false) => {
    if (!storedToken() && !sessionRole) {
      resetAnnouncement();
      return;
    }
    if (!force && announcementLoaded) return;
    if (announcementRequest) return announcementRequest;
    announcementRequest = (async () => {
      try {
        const response = await originalFetch(ANNOUNCEMENTS_ENDPOINT, {
          headers: policyHeaders(),
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) return;
        const payload = await response.json();
        announcementLoaded = true;
        renderAnnouncements(payload.announcements || []);
      } catch (_error) {
        // Announcement failure must never block the chat page.
      } finally {
        announcementRequest = null;
      }
    })();
    return announcementRequest;
  };

  const syncAnnouncement = () => {
    if (window.location.pathname.startsWith("/auth")) {
      if (announcementLoaded || currentAnnouncement) resetAnnouncement();
      return;
    }
    if (sessionRole) {
      // Open WebUI replaces the entire chat header after creating a
      // conversation. Recreate the lightweight launcher if Svelte removed it,
      // then reuse the cached list without reopening an acknowledged item.
      const announcementUiMissing =
        !document.querySelector("#turtle-announcement-launcher")
        || !document.querySelector("#turtle-announcement-shell");
      ensureAnnouncementUi();
      if (announcementLoaded && announcementUiMissing) renderCurrentAnnouncement();
      void loadAnnouncements();
    }
  };

  const refreshRegisteredCapabilities = () => {
    Array.from(capabilitySources.values()).forEach((model) => registerCapability(model));
  };

  const loadChatPolicy = async (force = false) => {
    if (policyLoading) return;
    if (!force && policyLoaded && Date.now() - policyLoadedAt < 30_000) return;
    policyLoading = true;
    try {
      const response = await originalFetch(CHAT_POLICY_ENDPOINT, {
        headers: policyHeaders(),
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const payload = await response.json();
      if (!Array.isArray(payload.allowed) || !payload.model) return;
      applyProviderDisplay(payload.provider_display);
      allowedSelections = new Set(payload.allowed);
      chatQuota = payload.quota || null;
      chatSubscription = payload.subscription || null;
      chatPolicyIsAdmin = payload.is_admin === true;
      selectionStates.clear();
      Object.entries(payload.quota?.models || {}).forEach(([key, value]) => {
        selectionStates.set(key, value);
      });
      policyLoaded = true;
      policyLoadedAt = Date.now();
      refreshRegisteredCapabilities();
      registerCapability(payload.model);
      document.documentElement.dataset.turtleChatPolicy = "ready";
      const controls = document.querySelector("#turtle-runtime-controls");
      const active = activeCapability();
      if (controls && active) controls.configure(active);
      queueMount();
    } catch (_error) {
      // Login may not be ready yet; the short retry keeps the conservative fallback.
    } finally {
      policyLoading = false;
    }
  };

  const option = (item) => {
    const element = document.createElement("option");
    element.value = item.id;
    element.textContent = item.label;
    element.disabled = item.available === false;
    return element;
  };

  const displayVersionLabel = (label) => String(label || "").replace(/^最新\s*[·・]\s*/, "");

  const formatRemainingTime = (resetAt) => {
    if (!resetAt) return "";
    const seconds = Math.max(0, Number(resetAt) - Math.floor(Date.now() / 1000));
    if (seconds < 60) return "不到 1 分钟";
    if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} 分钟`;
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      return minutes && hours < 6 ? `${hours} 小时 ${minutes} 分钟` : `${Math.max(1, hours)} 小时`;
    }
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    return hours && days < 7 ? `${days} 天 ${hours} 小时` : `${Math.max(1, days)} 天`;
  };

  const formatWindowDuration = (value) => {
    const seconds = Math.max(0, Number(value || 0));
    if (!seconds) return "";
    if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟`;
    if (seconds < 86400) return `${Math.max(1, Math.round(seconds / 3600))} 小时`;
    return `${Math.max(1, Math.round(seconds / 86400))} 天`;
  };

  const laneRefreshText = (lane) => {
    if (!lane?.allowed) return "当前套餐未开放";
    if (lane.limit_count == null && chatPolicyIsAdmin) return "不受站内次数限制";
    if (lane.limit_count == null) return "以上游动态额度为准";
    const reset = formatRemainingTime(lane.reset_at);
    if (reset) return `${reset}后刷新`;
    const window = formatWindowDuration(lane.window_seconds);
    return window ? `首次使用后 ${window} 刷新` : "使用后开始计时";
  };

  const laneStatusText = (lane) => {
    if (!lane?.allowed) return "当前分组不可用";
    const refresh = laneRefreshText(lane);
    if (!lane.available) return `已用完 · ${refresh}`;
    if (lane.limit_count == null && chatPolicyIsAdmin) return "管理员不限额";
    if (lane.limit_count == null) return "动态额度 · 以上游为准";
    const remaining = Number(lane.remaining_count || 0);
    const base = `剩余 ${remaining}/${Number(lane.limit_count)}`;
    return `${base} · ${refresh}`;
  };

  const quotaIndicator = (lane) => {
    if (!lane) return null;
    const indicator = document.createElement("span");
    indicator.className = "turtle-runtime-option-quota";
    indicator.dataset.state = lane.status || (lane.allowed ? "available" : "forbidden");
    indicator.dataset.quotaResetAt = String(Number(lane.reset_at || 0));
    indicator.dataset.quotaWindowSeconds = String(Number(lane.window_seconds || 0));

    const value = document.createElement("strong");
    const refresh = document.createElement("small");
    refresh.dataset.quotaRefresh = "true";
    refresh.textContent = laneRefreshText(lane);

    if (!lane.allowed) {
      value.textContent = "未开放";
      indicator.append(value, refresh);
      return indicator;
    }
    if (lane.limit_count == null) {
      if (chatPolicyIsAdmin) {
        indicator.dataset.kind = "unlimited";
        value.textContent = "管理员不限额";
        indicator.append(value, refresh);
        return indicator;
      }
      indicator.dataset.kind = "dynamic";
      value.textContent = "动态额度";
      indicator.append(value, refresh);
      return indicator;
    }

    const limit = Math.max(0, Number(lane.limit_count || 0));
    const remaining = Math.max(0, Number(lane.remaining_count || 0));
    indicator.dataset.kind = "fixed";
    value.textContent = `${remaining} / ${limit}`;
    const track = document.createElement("i");
    const fill = document.createElement("b");
    const percentage = limit ? Math.max(0, Math.min(100, (remaining / limit) * 100)) : 0;
    fill.style.setProperty("--turtle-runtime-quota-fill", `${percentage}%`);
    track.append(fill);
    indicator.append(value, refresh, track);
    return indicator;
  };

  const refreshQuotaCountdowns = () => {
    let expired = false;
    document.querySelectorAll(".turtle-runtime-option-quota").forEach((indicator) => {
      const refresh = indicator.querySelector("[data-quota-refresh]");
      const resetAt = Number(indicator.dataset.quotaResetAt || 0);
      if (!refresh || !resetAt) return;
      if (resetAt <= Math.floor(Date.now() / 1000)) {
        refresh.textContent = "正在刷新额度…";
        expired = true;
      } else {
        refresh.textContent = `${formatRemainingTime(resetAt)}后刷新`;
      }
    });
    return expired;
  };

  const laneForSelection = (capability, selection) => {
    const version = capability.versions.find((item) => item.id === selection.version);
    return version?.thinking_levels.find((item) => item.id === selection.thinking) || null;
  };

  const allowedLanesForCapability = (capability) =>
    capability?.versions.flatMap((version) =>
      version.thinking_levels
        .filter((lane) => lane.allowed)
        .map((lane) => ({ version, lane })),
    ) || [];

  const hasSingleStableLane = (capability) => {
    if (!policyLoaded) return false;
    const allowed = allowedLanesForCapability(capability);
    return allowed.length === 1 && allowed[0].lane.available;
  };

  const chevron = (direction = "right") => `
    <svg class="turtle-runtime-row-chevron" data-direction="${direction}" aria-hidden="true" viewBox="0 0 20 20">
      <path d="${direction === "down" ? "m6.5 8 3.5 3.5L13.5 8" : "m8 6.5 3.5 3.5L8 13.5"}"></path>
    </svg>`;

  const createControls = () => {
    const controls = document.createElement("div");
    controls.id = "turtle-runtime-controls";
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "模型版本与思考等级");
    controls.innerHTML = `
      <span class="turtle-runtime-state" aria-hidden="true">
        <select id="turtle-version-select" tabindex="-1" aria-label="模型版本"></select>
        <select id="turtle-thinking-select" tabindex="-1" aria-label="思考等级"></select>
      </span>
      <button class="turtle-runtime-trigger" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="turtle-runtime-menu">
        <span data-runtime-trigger-label>中</span>
        ${chevron("down")}
      </button>
      <div id="turtle-runtime-menu" class="turtle-runtime-menu" role="menu" aria-label="模型与思考设置" hidden>
        <div class="turtle-runtime-official-picker" data-runtime-official hidden>
          <div class="turtle-runtime-section-heading">
            <span class="turtle-runtime-section-label" data-runtime-section-label>智能</span>
            <small>本站剩余 / 总额 · 刷新时间</small>
          </div>
          <div class="turtle-runtime-smart-options" data-runtime-options="smart"></div>
          <div class="turtle-runtime-divider" role="separator"></div>
          <button class="turtle-runtime-menu-row turtle-runtime-model-launcher" type="button" role="menuitem" data-runtime-open="model" aria-haspopup="menu" aria-expanded="false">
            <strong data-runtime-official-model-label>GPT-5.6 Sol</strong>${chevron()}
          </button>
        </div>
        <div data-runtime-generic>
          <button class="turtle-runtime-menu-row" type="button" role="menuitem" data-runtime-open="model" aria-haspopup="menu" aria-expanded="false">
            <span>模型</span><strong data-runtime-model-label>GPT</strong>${chevron()}
          </button>
          <button class="turtle-runtime-menu-row" type="button" role="menuitem" data-runtime-open="thinking" aria-haspopup="menu" aria-expanded="false">
            <span>思考强度</span><strong data-runtime-thinking-label>中</strong>${chevron()}
          </button>
        </div>
        <div class="turtle-runtime-divider" role="separator"></div>
        <button class="turtle-runtime-menu-row turtle-runtime-advanced-toggle" type="button" role="menuitem" aria-expanded="false">
          <span>高级</span>${chevron("down")}
        </button>
        <div class="turtle-runtime-advanced" hidden>
          <span>聊天额度</span>
          <strong data-runtime-quota-primary>正在同步…</strong>
          <small data-runtime-quota-secondary>只显示站内分配额度</small>
          <small data-runtime-upstream-fallback></small>
        </div>
      </div>
      <div class="turtle-runtime-submenu" data-runtime-submenu="model" role="menu" aria-label="选择模型" hidden>
        <button class="turtle-runtime-submenu-back" type="button" data-runtime-back>${chevron()}<span>模型</span></button>
        <div data-runtime-options="model"></div>
      </div>
      <div class="turtle-runtime-submenu" data-runtime-submenu="thinking" role="menu" aria-label="选择思考强度" hidden>
        <button class="turtle-runtime-submenu-back" type="button" data-runtime-back>${chevron()}<span>思考强度</span></button>
        <div data-runtime-options="thinking"></div>
      </div>
    `;

    const versionSelect = controls.querySelector("#turtle-version-select");
    const thinkingSelect = controls.querySelector("#turtle-thinking-select");
    const trigger = controls.querySelector(".turtle-runtime-trigger");
    const menu = controls.querySelector("#turtle-runtime-menu");
    const advancedToggle = controls.querySelector(".turtle-runtime-advanced-toggle");
    const advanced = controls.querySelector(".turtle-runtime-advanced");

    const currentCapability = () => capabilityForModel(controls.dataset.modelId);

    const closeSubmenus = () => {
      controls.dataset.submenuOpen = "false";
      delete controls.dataset.activeMenu;
      controls.querySelectorAll("[data-runtime-open]").forEach((button) => {
        button.dataset.active = "false";
        button.setAttribute("aria-expanded", "false");
      });
      controls.querySelectorAll("[data-runtime-submenu]").forEach((submenu) => {
        submenu.hidden = true;
      });
    };

    const closeMenus = (restoreFocus = false) => {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      controls.dataset.open = "false";
      closeSubmenus();
      advanced.hidden = true;
      advancedToggle.setAttribute("aria-expanded", "false");
      advancedToggle.querySelector("svg")?.setAttribute("data-direction", "down");
      if (restoreFocus) trigger.focus();
    };

    const updateSubmenuPlacement = () => {
      const submenu = controls.querySelector(`[data-runtime-submenu="${controls.dataset.activeMenu || ""}"]`);
      if (!submenu || submenu.hidden || menu.hidden) return;
      const menuRect = menu.getBoundingClientRect();
      const width = submenu.getBoundingClientRect().width || 252;
      controls.dataset.submenuSide = menuRect.right + 8 + width <= window.innerWidth - 12 ? "right" : "left";
    };

    const openSubmenu = (name) => {
      advanced.hidden = true;
      advancedToggle.setAttribute("aria-expanded", "false");
      advancedToggle.querySelector("svg")?.setAttribute("data-direction", "down");
      controls.dataset.activeMenu = name;
      controls.dataset.submenuOpen = "true";
      controls.querySelectorAll("[data-runtime-open]").forEach((button) => {
        const active = button.dataset.runtimeOpen === name;
        button.dataset.active = String(active);
        button.setAttribute("aria-expanded", String(active));
      });
      controls.querySelectorAll("[data-runtime-submenu]").forEach((submenu) => {
        submenu.hidden = submenu.dataset.runtimeSubmenu !== name;
      });
      requestAnimationFrame(updateSubmenuPlacement);
    };

    const renderQuota = () => {
      const primary = controls.querySelector("[data-runtime-quota-primary]");
      const secondary = controls.querySelector("[data-runtime-quota-secondary]");
      const upstreamFallback = controls.querySelector("[data-runtime-upstream-fallback]");
      if (!policyLoaded || !chatQuota) {
        primary.textContent = "正在同步…";
        secondary.textContent = "只显示站内分配额度";
        upstreamFallback.textContent = "";
        return;
      }
      const capability = currentCapability();
      const selection = {
        version: versionSelect.value,
        thinking: thinkingSelect.value,
      };
      const lane = capability ? laneForSelection(capability, selection) : null;
      if (fallbackNotice && Date.now() - fallbackNotice.at < 20_000) {
        primary.textContent = `已切换到 ${fallbackNotice.to}`;
        secondary.textContent = fallbackNotice.reason;
        return;
      }
      fallbackNotice = null;
      primary.textContent = lane ? laneStatusText(lane) : "正在同步…";
      const groupName = chatQuota.provider_groups?.[capability?.family]?.name || "未分配模型组";
      secondary.textContent = `${groupName} · 已完成 ${Number(chatQuota.request_count || 0)} 次`;
      upstreamFallback.textContent = capability?.family === "gpt"
        ? "官方透明兜底：Instant 到限可切 GPT-5.5 Instant mini，推理到限可切 GPT-5.4 Thinking mini；Mini 不可手选。"
        : "Claude 官方额度按会话和周用量动态计算；这里显示的是本站逐档公平额度。";
    };

    const menuOption = ({
      label,
      badge,
      value,
      selected,
      disabled,
      status,
      tone,
      quotaLane,
      onSelect,
    }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "turtle-runtime-option";
      button.dataset.selected = String(selected);
      button.dataset.state = tone || "available";
      button.setAttribute("role", "menuitemradio");
      button.setAttribute("aria-checked", String(selected));
      button.setAttribute("aria-disabled", String(Boolean(disabled)));
      button.disabled = Boolean(disabled);
      button.dataset.value = value;
      const copy = document.createElement("span");
      copy.className = "turtle-runtime-option-copy";
      const text = document.createElement("strong");
      text.textContent = label;
      copy.append(text);
      if (badge) {
        const badgeElement = document.createElement("span");
        badgeElement.className = "turtle-runtime-option-badge";
        badgeElement.textContent = badge;
        copy.append(badgeElement);
      }
      if (status) {
        const detail = document.createElement("small");
        detail.textContent = status;
        copy.append(detail);
      }
      button.append(copy);
      const quota = quotaIndicator(quotaLane);
      if (quota) button.append(quota);
      if (!disabled) button.addEventListener("click", onSelect);
      return button;
    };

    const renderOptions = (capability) => {
      const modelOptions = controls.querySelector('[data-runtime-options="model"]');
      const smartOptions = controls.querySelector('[data-runtime-options="smart"]');
      const officialPicker = capability.picker?.style === "chatgpt";
      const currentKey = `${versionSelect.value}:${thinkingSelect.value}`;
      const pickerMode = controls.dataset.pickerMode || "smart";
      const laneByKey = (key) => {
        for (const version of capability.versions) {
          const lane = version.thinking_levels.find((item) => item.key === key);
          if (lane) return { version, lane };
        }
        return null;
      };
      const selectLane = (version, lane, mode) => {
        controls.dataset.pickerMode = mode;
        versionSelect.value = version.id;
        updateThinkingOptions(capability, lane.id);
        closeMenus(true);
      };

      if (officialPicker) {
        const modeItems = (capability.picker.mode_order || [])
          .map((item) => ({ item, target: laneByKey(item.selection_key) }))
          .filter(({ target }) => target?.lane?.allowed);
        smartOptions.replaceChildren(
          ...modeItems.map(({ item, target }) =>
            menuOption({
              label: item.label,
              badge: item.badge,
              value: item.selection_key,
              selected: pickerMode === "smart" && currentKey === item.selection_key,
              disabled: !target.lane.available,
              tone: target.lane.status,
              status: target.lane.available ? "" : laneStatusText(target.lane),
              quotaLane: target.lane,
              onSelect: () => selectLane(target.version, target.lane, "smart"),
            }),
          ),
        );
        const orderedVersions = (capability.picker.model_order || [])
          .map((id) => capability.versions.find((version) => version.id === id))
          .filter((version) => version?.allowed);
        modelOptions.replaceChildren(
          ...orderedVersions.map((version) => {
            const lane = version.thinking_levels.find((item) => item.available)
              || version.thinking_levels.find((item) => item.allowed)
              || version.thinking_levels[0];
            const selected = pickerMode === "smart"
              ? version.id === "latest"
              : version.id === versionSelect.value;
            return menuOption({
              label: displayVersionLabel(version.label),
              value: version.id,
              selected,
              disabled: !version.available,
              tone: version.status,
              status: version.available
                ? ""
                : version.status === "exhausted"
                  ? "额度已用完"
                  : "当前套餐不可用",
              quotaLane: lane,
              onSelect: () => selectLane(
                version,
                (
                  version.id === "latest"
                    ? version.thinking_levels.find((item) => item.id === "medium" && item.available)
                    : null
                ) || lane,
                version.id === "latest" ? "smart" : "legacy",
              ),
            });
          }),
        );
        controls.querySelector('[data-runtime-submenu="model"] [data-runtime-back] span').textContent = "模型";
      } else {
        smartOptions.replaceChildren();
        modelOptions.replaceChildren(
          ...capability.versions.map((version) =>
            menuOption({
              label: displayVersionLabel(version.label),
              value: version.id,
              selected: version.id === versionSelect.value,
              disabled: !version.available,
              tone: version.status,
              status:
                version.status === "forbidden"
                  ? "当前分组不可用"
                  : version.status === "exhausted"
                    ? "该版本额度已用完"
                    : `${version.thinking_levels.filter((item) => item.available).length} 个档位可用`,
              quotaLane:
                version.thinking_levels.find((item) => item.id === version.default_thinking_level)
                || version.thinking_levels[0],
              onSelect: () => {
                versionSelect.value = version.id;
                versionSelect.dispatchEvent(new Event("change", { bubbles: true }));
                closeMenus(true);
              },
            }),
          ),
        );
      }

      const version = capability.versions.find((item) => item.id === versionSelect.value);
      const thinkingOptions = controls.querySelector('[data-runtime-options="thinking"]');
      thinkingOptions.replaceChildren(
        ...(version?.thinking_levels || []).map((thinking) =>
          menuOption({
            label: thinking.label,
            value: thinking.id,
            selected: thinking.id === thinkingSelect.value,
            disabled: !thinking.available,
            tone: thinking.status,
            status: thinking.available ? "" : laneStatusText(thinking),
            quotaLane: thinking,
            onSelect: () => {
              thinkingSelect.value = thinking.id;
              thinkingSelect.dispatchEvent(new Event("change", { bubbles: true }));
              closeMenus(true);
            },
          }),
        ),
      );
    };

    const syncPresentation = (capability) => {
      const version = capability.versions.find((item) => item.id === versionSelect.value);
      const thinking = version?.thinking_levels.find((item) => item.id === thinkingSelect.value);
      const versionLabel = displayVersionLabel(version?.label || "GPT");
      const thinkingLabel = thinking?.label || "思考";
      const officialPicker = capability.picker?.style === "chatgpt";
      const pickerMode = controls.dataset.pickerMode || "smart";
      const singleSelection = hasSingleStableLane(capability);
      controls.querySelector("[data-runtime-official]").hidden = !officialPicker;
      controls.querySelector("[data-runtime-generic]").hidden = officialPicker;
      controls.querySelector("[data-runtime-model-label]").textContent = versionLabel;
      controls.querySelector("[data-runtime-thinking-label]").textContent = thinkingLabel;
      if (officialPicker) {
        const currentKey = `${versionSelect.value}:${thinkingSelect.value}`;
        const mode = (capability.picker.mode_order || []).find(
          (item) => item.selection_key === currentKey,
        );
        const triggerLabel = pickerMode === "smart" ? (mode?.label || thinkingLabel) : versionLabel;
        controls.querySelector("[data-runtime-trigger-label]").textContent = triggerLabel;
        controls.querySelector("[data-runtime-section-label]").textContent =
          capability.picker.section_label || "智能";
        controls.querySelector("[data-runtime-official-model-label]").textContent =
          pickerMode === "smart"
            ? displayVersionLabel(
              capability.versions.find((item) => item.id === "latest")?.label || "GPT-5.6 Sol",
            )
            : versionLabel;
        trigger.title = pickerMode === "smart"
          ? `${triggerLabel} · ${versionLabel}`
          : versionLabel;
        trigger.setAttribute("aria-label", `模型选择：${triggerLabel}`);
      } else {
        controls.querySelector("[data-runtime-trigger-label]").textContent = thinkingLabel;
        trigger.title = `${versionLabel} · ${thinkingLabel}`;
        trigger.setAttribute("aria-label", `模型与思考设置：${versionLabel}，${thinkingLabel}`);
      }
      controls.dataset.singleSelection = String(singleSelection);
      trigger.disabled = singleSelection;
      trigger.setAttribute("aria-disabled", String(singleSelection));
      if (singleSelection) {
        trigger.removeAttribute("aria-haspopup");
        trigger.removeAttribute("aria-controls");
        closeMenus();
      } else {
        trigger.setAttribute("aria-haspopup", "menu");
        trigger.setAttribute("aria-controls", "turtle-runtime-menu");
      }
      controls.dataset.thinkingLevel = thinkingSelect.value;
      renderOptions(capability);
      renderQuota();
    };

    const updateThinkingOptions = (capability, preferred) => {
      const version = capability.versions.find((item) => item.id === versionSelect.value);
      if (!version) return;
      thinkingSelect.replaceChildren(...version.thinking_levels.map((item) => option(item)));
      const selected = validSelection(capability, {
        version: version.id,
        thinking: preferred,
        mode: controls.dataset.pickerMode,
      });
      thinkingSelect.value = selected.thinking;
      controls.dataset.pickerMode = selected.mode || "legacy";
      thinkingSelect.disabled = version.thinking_levels.filter((item) => item.available).length <= 1;
      saveSelection(capability, selected);
      syncPresentation(capability);
    };

    versionSelect.addEventListener("change", () => {
      const capability = currentCapability();
      if (!capability) return;
      updateThinkingOptions(capability, null);
    });

    thinkingSelect.addEventListener("change", () => {
      const capability = currentCapability();
      if (!capability) return;
      saveSelection(capability, {
        version: versionSelect.value,
        thinking: thinkingSelect.value,
        mode: controls.dataset.pickerMode || null,
      });
      syncPresentation(capability);
    });

    trigger.addEventListener("click", () => {
      if (!menu.hidden) return closeMenus(false);
      void loadChatPolicy(true);
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      controls.dataset.open = "true";
      renderQuota();
    });

    controls.querySelectorAll("[data-runtime-open]").forEach((button) =>
      button.addEventListener("click", () => openSubmenu(button.dataset.runtimeOpen)),
    );
    controls.querySelectorAll("[data-runtime-back]").forEach((button) =>
      button.addEventListener("click", closeSubmenus),
    );
    advancedToggle.addEventListener("click", () => {
      const expanded = advanced.hidden;
      closeSubmenus();
      advanced.hidden = !expanded;
      advancedToggle.setAttribute("aria-expanded", String(expanded));
      advancedToggle.querySelector("svg")?.setAttribute("data-direction", expanded ? "up" : "down");
      renderQuota();
    });
    document.addEventListener("pointerdown", (event) => {
      if (!menu.hidden && !controls.contains(event.target)) closeMenus(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !menu.hidden) closeMenus(true);
    });
    window.addEventListener("resize", updateSubmenuPlacement);

    controls.configure = (capability) => {
      controls.hidden = false;
      controls.dataset.modelId = capability.model_id;
      versionSelect.replaceChildren(...capability.versions.map((item) => option(item)));
      const requested = readSelection(capability);
      const selected = validSelection(capability, requested);
      controls.dataset.pickerMode = selected.mode || "legacy";
      if (requested.thinking && (requested.version !== selected.version || requested.thinking !== selected.thinking)) {
        const previous = laneForSelection(capability, requested);
        const next = laneForSelection(capability, selected);
        const nextVersion = capability.versions.find((item) => item.id === selected.version);
        if (next && nextVersion) {
          const reset = formatRemainingTime(previous?.reset_at);
          fallbackNotice = {
            at: Date.now(),
            to: `${displayVersionLabel(nextVersion.label)} · ${next.label}`,
            reason: previous?.status === "exhausted"
              ? `原档位额度已用完${reset ? `，${reset}后恢复` : ""}`
              : "原档位不属于当前分组",
          };
        }
      }
      versionSelect.value = selected.version;
      updateThinkingOptions(capability, selected.thinking);
    };
    controls.close = closeMenus;
    controls.refreshQuotaPresentation = renderQuota;

    return controls;
  };

  const activeCapability = () => {
    const selector = modelSelector();
    if (!selector) return null;
    const identity = `${selector.textContent || ""} ${selector.getAttribute("aria-label") || ""}`;
    for (const capability of capabilities.values()) {
      if (
        normalize(identity).includes(normalize(capability.model_id)) ||
        normalize(identity).includes(normalize(capability.name)) ||
        normalize(identity).includes(normalize(capability.family_label))
      ) {
        return capability;
      }
    }
    return null;
  };

  const allowedProviderFamilies = () => {
    if (!policyLoaded || !providerModelsCaptured) return [];
    return Array.from(capabilities.values())
      .filter((capability) => allowedLanesForCapability(capability).length > 0)
      .map((capability) => capability.family)
      .filter((family, index, values) =>
        Object.hasOwn(PROVIDER_MODELS, family)
        && publishedProviderFamilies.has(family)
        && values.indexOf(family) === index,
      );
  };

  const syncSingleModelSelector = () => {
    const selector = modelSelector();
    if (!selector) return;
    const allowedFamilies = allowedProviderFamilies();
    const currentProvider = providerFromRoute();
    const singleModel =
      allowedFamilies.length === 1 && allowedFamilies[0] === currentProvider;

    if (singleModel) {
      if (selector.dataset.turtleSingleModel !== "true") {
        selector.dataset.turtleSingleModelWasDisabled = String(Boolean(selector.disabled));
      }
      selector.dataset.turtleSingleModel = "true";
      selector.disabled = true;
      selector.setAttribute("aria-disabled", "true");
      selector.setAttribute(
        "aria-label",
        `${PROVIDER_LABELS[currentProvider] || currentProvider}（唯一可用模型）`,
      );
      selector.setAttribute("title", "当前账号只有这一个可用模型");
      return;
    }

    if (selector.dataset.turtleSingleModel === "true") {
      if (selector.dataset.turtleSingleModelWasDisabled === "false") selector.disabled = false;
      delete selector.dataset.turtleSingleModel;
      delete selector.dataset.turtleSingleModelWasDisabled;
      selector.removeAttribute("aria-disabled");
      selector.removeAttribute("title");
      const label = selector.dataset.turtleWorkspaceLabel;
      if (label) selector.setAttribute("aria-label", label);
    }
  };

  const STOP_ICON_PATH_PREFIX = "M2.25 12c0-5.385";

  const stopButtonWithin = (root) =>
    root
      ? Array.from(root.querySelectorAll("button")).find((button) =>
          Array.from(button.querySelectorAll("svg path")).some((path) =>
            String(path.getAttribute("d") || "").startsWith(STOP_ICON_PATH_PREFIX),
          ),
        )
      : null;

  const controlsLocation = (input) => {
    const voiceButton = input.querySelector("#voice-input-button");
    const voiceWrapper = voiceButton?.parentElement;
    const rightTools = voiceWrapper?.parentElement;
    if (rightTools && input.contains(rightTools)) {
      return { host: rightTools, before: voiceWrapper, placement: "voice" };
    }

    // While a response is active Open WebUI removes the voice button and wraps
    // the stop button one level deeper. Keep the runtime trigger in that same
    // right-hand action row instead of falling back to the upper toolbar.
    const stopButton = stopButtonWithin(input);
    const stopSlot = stopButton?.parentElement?.parentElement;
    const stopTools = stopSlot?.parentElement;
    if (stopTools && input.contains(stopTools)) {
      return { host: stopTools, before: stopSlot, placement: "stop" };
    }

    const toolbar = Array.from(input.children).find((child) => child.querySelector?.("#camera-input"));
    return { host: toolbar || input, before: null, placement: "fallback" };
  };

  const enhanceChatChrome = () => {
    const composer = document.querySelector("#message-input-container");
    const activeStopButton = stopButtonWithin(composer);

    document.querySelectorAll(".turtle-stop-button").forEach((button) => {
      if (button !== activeStopButton) button.classList.remove("turtle-stop-button");
    });
    if (activeStopButton) {
      activeStopButton.classList.add("turtle-stop-button");
      activeStopButton.setAttribute("aria-label", "停止回答");
      activeStopButton.setAttribute("title", "停止回答");
    }

    document.querySelectorAll('button[aria-label^="追问："], button[aria-label^="Follow-up:"]').forEach((button) => {
      const section = button.closest("div.mt-4");
      if (!section) return;
      section.classList.add("turtle-follow-ups-hidden");
      section.setAttribute("aria-hidden", "true");
    });
  };

  const syncChatHome = () => {
    // v0.11 already supplies a compact, accessible welcome state and native
    // suggestions. Keep that upstream hierarchy instead of mounting a second
    // Turtle hero and another set of prompt cards above the same composer.
    document.querySelector("#turtle-chat-home")?.remove();
  };

  const mountControls = () => {
    const input = document.querySelector("#message-input-container");
    if (!input) return;
    const location = controlsLocation(input);
    let controls = document.querySelector("#turtle-runtime-controls");
    if (!controls) {
      controls = createControls();
      location.host.insertBefore(controls, location.before);
    } else if (controls.parentElement !== location.host || controls.nextElementSibling !== location.before) {
      location.host.insertBefore(controls, location.before);
    }
    controls.dataset.placement = location.placement;

    const capability = activeCapability();
    if (!capability) {
      controls.close?.();
      controls.hidden = true;
      return;
    }
    if (controls.dataset.modelId !== capability.model_id || controls.hidden) {
      controls.configure(capability);
    }
  };

  const syncAuthLanding = (signup, firstAdmin) => {
    const authPage = document.querySelector("#auth-page");
    if (!authPage) {
      document.querySelector("#turtle-auth-intro")?.remove();
      return;
    }
    authPage.dataset.turtleAuthMode = signup ? (firstAdmin ? "admin" : "signup") : "login";
    let intro = document.querySelector("#turtle-auth-intro");
    if (!intro) {
      intro = document.createElement("aside");
      intro.id = "turtle-auth-intro";
      intro.setAttribute("aria-label", "Turtle’s Chat 产品介绍");
      intro.innerHTML = `
        <div class="turtle-auth-intro-kicker"><i></i><span>PRIVATE AI WORKSPACE</span></div>
        <h1>让每一次思考，<br />都有更好的下一步。</h1>
        <p>在一个界面中切换 GPT 与 Claude，延续对话、整理资料并完成复杂任务。</p>
        <div class="turtle-auth-intro-points">
          <div><span>01</span><strong>双模型工作区</strong><small>GPT 与 Claude 对话分区归档</small></div>
          <div><span>02</span><strong>连续的工作上下文</strong><small>历史、文件与模型选择始终清晰</small></div>
          <div><span>03</span><strong>专属成员空间</strong><small>按成员配置可见模型与使用额度</small></div>
        </div>
        <footer><span>Turtle’s Chat</span><i></i><small>清晰、专注、持续</small></footer>
      `;
      authPage.append(intro);
    }
  };

  const rememberSessionRole = (role) => {
    const normalized = normalize(role);
    const previousRole = sessionRole;
    sessionRole = ["pending", "user", "admin"].includes(normalized) ? normalized : "";
    if (sessionRole !== previousRole) {
      policyLoaded = false;
      policyLoadedAt = 0;
      chatQuota = null;
      chatSubscription = null;
      chatPolicyIsAdmin = false;
      resetAnnouncement();
    }
    if (sessionRole) sessionStorage.setItem(SESSION_ROLE_KEY, sessionRole);
    else {
      sessionStorage.removeItem(SESSION_ROLE_KEY);
    }
    document.documentElement.dataset.turtleSessionRole = sessionRole || "anonymous";
    queueMount();
  };

  const chatAccessBlock = () => {
    if (sessionRole === "admin") return null;
    if (sessionRole === "pending") {
      return {
        code: "pending",
        title: "账户等待管理员激活",
        message: "你可以浏览页面和设置；激活前发送按钮保持关闭，也不会向模型发起请求。",
      };
    }
    if (storedToken() && !policyLoaded) {
      return {
        code: "checking",
        title: "正在确认订阅状态",
        message: "订阅校验完成后即可发送，通常只需要片刻。",
      };
    }
    if (chatSubscription?.active) {
      const expiresAt = Number(chatSubscription?.expires_at || 0);
      if (!expiresAt || Math.floor(Date.now() / 1000) <= expiresAt) return null;
    }
    const status = (
      Number(chatSubscription?.expires_at || 0) > 0
      && Math.floor(Date.now() / 1000) > Number(chatSubscription.expires_at)
    ) ? "expired" : String(chatSubscription?.status || "inactive");
    const copy = {
      expired: {
        title: "订阅已到期",
        message: "你仍可查看页面、历史对话和设置；续订前不能发送新消息。",
      },
      cancelled: {
        title: "订阅已停止",
        message: "你仍可查看页面、历史对话和设置；请联系管理员重新开通。",
      },
      scheduled: {
        title: "订阅尚未开始",
        message: "到达订阅开始时间后发送功能会自动开放。",
      },
      inactive: {
        title: "尚未开通订阅",
        message: "你仍可浏览页面和设置；请联系管理员开通使用权限。",
      },
    }[status] || {
      title: "当前不能发送消息",
      message: "请联系管理员检查订阅状态。",
    };
    return { code: status, ...copy };
  };

  const removeSiteAccessOverlay = () => {
    document.querySelector("#turtle-maintenance-overlay")?.remove();
  };

  const syncSiteAccess = () => {
    if (document.querySelector("#auth-page")) {
      removeSiteAccessOverlay();
      document.querySelector("#turtle-pending-banner")?.remove();
      return;
    }

    const maintenance =
      authSecurity.loaded &&
      !authSecurity.failed &&
      authSecurity.maintenance_enabled &&
      sessionRole &&
      sessionRole !== "admin";
    if (maintenance) {
      let overlay = document.querySelector("#turtle-maintenance-overlay");
      if (!overlay) {
        overlay = document.createElement("section");
        overlay.id = "turtle-maintenance-overlay";
        overlay.setAttribute("role", "status");
        overlay.setAttribute("aria-live", "polite");
        overlay.innerHTML = `
          <div class="turtle-maintenance-card">
            <img src="/static/turtle-gpt-logo.webp" alt="" />
            <span>SYSTEM MAINTENANCE</span>
            <h1>暂时休息一下</h1>
            <p data-turtle-maintenance-message></p>
            <small>管理员账号仍可进入系统处理维护工作</small>
          </div>`;
        document.body.append(overlay);
      }
      overlay.querySelector("[data-turtle-maintenance-message]").textContent =
        authSecurity.maintenance_message || "系统正在维护，请稍后再试。";
    } else {
      removeSiteAccessOverlay();
    }

    const input = document.querySelector("#message-input-container");
    const send = document.querySelector("#send-message-button");
    const block = maintenance ? null : chatAccessBlock();
    if (send) {
      if (block) {
        if (send.dataset.turtleAccessDisabled !== "true") {
          send.dataset.turtleAccessWasDisabled = String(Boolean(send.disabled));
        } else if (!send.disabled) {
          // Open WebUI may enable the button after the user types while access is
          // blocked. Remember that state so a later subscription refresh can
          // restore the button instead of leaving it disabled indefinitely.
          send.dataset.turtleAccessWasDisabled = "false";
        }
        send.disabled = true;
        send.dataset.turtleAccessDisabled = "true";
        send.setAttribute("aria-disabled", "true");
        send.title = block.title;
      } else if (send.dataset.turtleAccessDisabled === "true") {
        if (send.dataset.turtleAccessWasDisabled === "false") send.disabled = false;
        delete send.dataset.turtleAccessDisabled;
        delete send.dataset.turtleAccessWasDisabled;
        send.removeAttribute("aria-disabled");
        send.removeAttribute("title");
      }
    }
    if (!block || !input) {
      document.querySelector("#turtle-pending-banner")?.remove();
      return;
    }
    let banner = document.querySelector("#turtle-pending-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "turtle-pending-banner";
      banner.setAttribute("role", "status");
      banner.innerHTML = "<strong></strong><span></span>";
      input.parentElement?.insertBefore(banner, input);
    }
    banner.dataset.state = block.code;
    banner.querySelector("strong").textContent = block.title;
    banner.querySelector("span").textContent = block.message;
  };

  const loadAuthSecurity = async (force = false) => {
    if (authSecurityRequest) return authSecurityRequest;
    if (!force && authSecurity.loaded) return authSecurity;
    authSecurityRequest = (async () => {
      try {
        const response = await originalFetch(AUTH_SECURITY_ENDPOINT, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) throw new Error("auth-security-config");
        const payload = await response.json();
        authSecurity = {
          loaded: true,
          failed: false,
          registration_enabled: Boolean(payload?.registration_enabled),
          maintenance_enabled: Boolean(payload?.maintenance_enabled),
          maintenance_message: String(
            payload?.maintenance_message || "系统正在维护，请稍后再试。",
          ).trim(),
          turnstile_enabled: Boolean(payload?.turnstile_enabled),
          turnstile_site_key: String(payload?.turnstile_site_key || "").trim(),
          turnstile_action: String(payload?.turnstile_action || "turtle_signup"),
        };
      } catch (_error) {
        authSecurity = {
          ...authSecurity,
          loaded: true,
          failed: true,
        };
      }
      queueMount();
      return authSecurity;
    })();
    try {
      return await authSecurityRequest;
    } finally {
      authSecurityRequest = null;
    }
  };

  const loadTurnstileScript = () => {
    if (window.turnstile?.render) return Promise.resolve(window.turnstile);
    if (turnstileScriptRequest) return turnstileScriptRequest;
    turnstileScriptRequest = new Promise((resolve, reject) => {
      let script = document.querySelector('script[data-turtle-turnstile]');
      if (!script) {
        script = document.createElement("script");
        script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
        script.async = true;
        script.defer = true;
        script.referrerPolicy = "no-referrer";
        script.dataset.turtleTurnstile = "true";
        document.head.append(script);
      }
      script.addEventListener(
        "load",
        () => (window.turnstile?.render ? resolve(window.turnstile) : reject(new Error("turnstile-unavailable"))),
        { once: true },
      );
      script.addEventListener("error", () => reject(new Error("turnstile-load-failed")), { once: true });
    }).catch((error) => {
      turnstileScriptRequest = null;
      throw error;
    });
    return turnstileScriptRequest;
  };

  const signupSubmitButton = (form) => form?.querySelector('button[type="submit"]') || null;

  const blockSignupSubmit = (form, blocked) => {
    const button = signupSubmitButton(form);
    if (!button) return;
    if (blocked) {
      if (!button.disabled) button.dataset.turtleTurnstileDisabled = "true";
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
      return;
    }
    if (button.dataset.turtleTurnstileDisabled === "true") {
      button.disabled = false;
      button.removeAttribute("aria-disabled");
      delete button.dataset.turtleTurnstileDisabled;
    }
  };

  const setTurnstileStatus = (panel, message, state = "loading") => {
    const status = panel?.querySelector("[data-turtle-turnstile-status]");
    if (!status) return;
    status.textContent = message;
    status.dataset.state = state;
  };

  const releaseTurnstileWidget = () => {
    if (turnstileWidgetId !== null && window.turnstile?.remove) {
      try {
        window.turnstile.remove(turnstileWidgetId);
      } catch (_error) {
        // A replaced Svelte form may already have removed the iframe.
      }
    }
    turnstileWidgetId = null;
    turnstileWidgetHost = null;
    turnstileToken = "";
  };

  const resetTurnstileWidget = (message = "请重新完成人机验证") => {
    turnstileToken = "";
    const panel = document.querySelector("#turtle-turnstile-panel");
    const form = panel?.closest("form");
    if (form) blockSignupSubmit(form, true);
    setTurnstileStatus(panel, message, "waiting");
    if (turnstileWidgetId !== null && window.turnstile?.reset) {
      try {
        window.turnstile.reset(turnstileWidgetId);
      } catch (_error) {
        releaseTurnstileWidget();
        queueMount();
      }
    }
  };

  const ensureTurnstilePanel = (form) => {
    let panel = form.querySelector("#turtle-turnstile-panel");
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "turtle-turnstile-panel";
    panel.className = "turtle-turnstile-panel";
    panel.innerHTML = `
      <div class="turtle-turnstile-heading"><span>安全验证</span><small data-turtle-turnstile-status data-state="loading">正在连接 Cloudflare…</small></div>
      <div class="turtle-turnstile-widget" data-turtle-turnstile-widget></div>`;
    const submit = signupSubmitButton(form);
    const submitSection = submit?.closest(".mt-5") || submit?.parentElement;
    if (submitSection && submitSection.parentElement === form) {
      form.insertBefore(panel, submitSection);
    } else {
      form.append(panel);
    }
    return panel;
  };

  const syncTurnstilePanel = () => {
    const form = document.querySelector("#auth-login-card form");
    const signup = Boolean(form?.querySelector("#name"));
    if (!form || !signup) {
      document.querySelector("#turtle-turnstile-panel")?.remove();
      releaseTurnstileWidget();
      return;
    }

    if (!authSecurity.loaded) {
      const panel = ensureTurnstilePanel(form);
      setTurnstileStatus(panel, "正在读取安全设置…", "loading");
      blockSignupSubmit(form, true);
      void loadAuthSecurity();
      return;
    }
    if (authSecurity.failed) {
      const panel = ensureTurnstilePanel(form);
      setTurnstileStatus(panel, "安全设置暂时无法读取，请刷新页面", "error");
      blockSignupSubmit(form, true);
      return;
    }
    if (!authSecurity.turnstile_enabled) {
      document.querySelector("#turtle-turnstile-panel")?.remove();
      releaseTurnstileWidget();
      blockSignupSubmit(form, false);
      return;
    }

    const panel = ensureTurnstilePanel(form);
    const widgetHost = panel.querySelector("[data-turtle-turnstile-widget]");
    blockSignupSubmit(form, !turnstileToken);
    if (!authSecurity.turnstile_site_key) {
      setTurnstileStatus(panel, "Turnstile 尚未完成配置，请联系管理员", "error");
      return;
    }

    if (turnstileWidgetHost && turnstileWidgetHost !== widgetHost) {
      releaseTurnstileWidget();
    }
    if (turnstileWidgetId !== null || widgetHost.dataset.rendering === "true") {
      setTurnstileStatus(
        panel,
        turnstileToken ? "验证已通过，本次注册可继续" : "请完成下方验证",
        turnstileToken ? "ready" : "waiting",
      );
      return;
    }

    widgetHost.dataset.rendering = "true";
    setTurnstileStatus(panel, "正在连接 Cloudflare…", "loading");
    void loadTurnstileScript()
      .then((turnstile) => {
        if (!widgetHost.isConnected || !document.querySelector("#auth-login-card #name")) return;
        turnstileWidgetHost = widgetHost;
        turnstileWidgetId = turnstile.render(widgetHost, {
          sitekey: authSecurity.turnstile_site_key,
          action: authSecurity.turnstile_action,
          theme: document.documentElement.classList.contains("dark") ? "dark" : "light",
          language: "zh-CN",
          size: "flexible",
          appearance: "always",
          retry: "auto",
          "refresh-expired": "auto",
          callback: (token) => {
            turnstileToken = String(token || "");
            setTurnstileStatus(panel, "验证已通过，本次注册可继续", "ready");
            blockSignupSubmit(form, !turnstileToken);
          },
          "expired-callback": () => resetTurnstileWidget("验证已过期，请重新完成"),
          "timeout-callback": () => resetTurnstileWidget("验证超时，请重新完成"),
          "error-callback": () => {
            turnstileToken = "";
            setTurnstileStatus(panel, "验证加载失败，请稍后重试", "error");
            blockSignupSubmit(form, true);
          },
        });
        delete widgetHost.dataset.rendering;
        setTurnstileStatus(panel, "请完成下方验证", "waiting");
      })
      .catch(() => {
        delete widgetHost.dataset.rendering;
        setTurnstileStatus(panel, "无法连接 Cloudflare，请检查网络后刷新", "error");
        blockSignupSubmit(form, true);
      });
  };

  const activateFirstAdminSignup = () => {
    if (!authSecurity.loaded || authSecurity.failed || !authSecurity.registration_enabled) return;
    const authPage = document.querySelector("#auth-page");
    const onboardingHost = authPage?.previousElementSibling;
    const getStarted = onboardingHost?.querySelector("button[aria-label]");
    if (!(getStarted instanceof HTMLButtonElement)) return;
    if (getStarted.dataset.turtleFirstAdminSignup === "requested") return;
    // Turtle hides Open WebUI's promotional onboarding screen. Trigger its
    // native transition once so an empty installation opens the real
    // create-admin form instead of leaving users on a sign-in-only form.
    getStarted.dataset.turtleFirstAdminSignup = "requested";
    firstAdminSignup = true;
    getStarted.click();
  };

  const syncAuthCopy = () => {
    const heading = document.querySelector("#auth-login-card form > .mb-1");
    if (!heading) return;
    const form = heading.closest("form");
    const signup = Boolean(form?.querySelector("#name"));
    if (
      signup &&
      authSecurity.loaded &&
      !authSecurity.failed &&
      !authSecurity.registration_enabled &&
      !firstAdminSignup
    ) {
      const signInToggle = Array.from(
        form.querySelectorAll('button[type="button"]'),
      ).find((button) => /登录|sign in/i.test(button.textContent || ""));
      if (signInToggle) {
        signInToggle.click();
        return;
      }
    }
    const firstAdmin = signup && firstAdminSignup;
    const title = heading.firstElementChild;
    let subtitle = heading.querySelector("[data-turtle-auth-copy]");
    if (!subtitle) {
      subtitle = document.createElement("div");
      heading.append(subtitle);
    }
    subtitle.className = "mt-1 text-sm font-normal text-gray-500 dark:text-gray-400";
    subtitle.dataset.turtleAuthCopy = "ready";
    const titleCopy = signup ? (firstAdmin ? "创建管理员账号" : "创建账号") : "欢迎回来";
    const copy = signup
      ? firstAdmin
        ? "设置管理员资料，随后配置模型、成员与安全选项"
        : "创建账号后，等待管理员审批即可使用"
      : "继续你的对话与工作";
    if (title && title.textContent?.trim() !== titleCopy) title.textContent = titleCopy;
    if (subtitle.textContent?.trim() !== copy) subtitle.textContent = copy;
    const submit = signupSubmitButton(form);
    const submitCopy = signup ? (firstAdmin ? "创建管理员账号" : "创建账号") : "登录";
    if (submit && submit.textContent?.trim() !== submitCopy) submit.textContent = submitCopy;
    syncAuthLanding(signup, firstAdmin);
  };

  let mountQueued = false;
  const queueMount = () => {
    if (mountQueued) return;
    mountQueued = true;
    requestAnimationFrame(() => {
      mountQueued = false;
      mountControls();
      enhanceChatChrome();
      syncChatHome();
      syncProviderWorkspace();
      syncProviderIcons();
      syncProviderWorkspaceLinks();
      syncClaudeWebSearchToggle();
      syncSingleModelSelector();
      activateFirstAdminSignup();
      syncAuthCopy();
      syncTurnstilePanel();
      syncSiteAccess();
      syncAnnouncement();
    });
  };

  const captureCapabilities = async (response) => {
    try {
      const payload = await response.clone().json();
      const models = Array.isArray(payload) ? payload : payload?.data;
      if (!Array.isArray(models)) return;
      publishedProviderFamilies = new Set(
        models
          .map((model) => providerForModel(model?.id || model?.name))
          .filter(Boolean),
      );
      providerModelsCaptured = true;
      models.forEach(registerCapability);
      queueMount();
    } catch (_error) {
      // The normal Open WebUI request will surface its own error; controls keep the safe fallback.
    }
  };

  const urlPath = (input) => {
    try {
      const raw = typeof input === "string" ? input : input?.url;
      return new URL(raw, window.location.origin).pathname;
    } catch (_error) {
      return "";
    }
  };

  const bodyText = async (input, init) => {
    if (typeof init?.body === "string") return init.body;
    if (typeof Request !== "undefined" && input instanceof Request) {
      return input.clone().text();
    }
    return null;
  };

  const jsonResponseLike = (response, payload) => {
    const headers = new Headers(response.headers);
    headers.delete("Content-Length");
    headers.delete("Content-Encoding");
    headers.set("Content-Type", "application/json");
    return new Response(JSON.stringify(payload), {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  };

  const captureSessionResponse = async (response, path) => {
    if (!AUTH_SESSION_ENDPOINTS.has(path)) return response;
    if (!response.ok) {
      if (path === "/api/v1/auths/") rememberSessionRole("");
      return response;
    }
    try {
      const payload = await response.clone().json();
      if (!payload || typeof payload !== "object") return response;
      const actualRole = normalize(payload.turtle_role || payload.role);
      rememberSessionRole(actualRole);
      if (actualRole) scheduleAuthenticatedWarmup();
      if (actualRole !== "pending") return response;
      return jsonResponseLike(response, {
        ...payload,
        role: "user",
        turtle_role: "pending",
      });
    } catch (_error) {
      return response;
    }
  };

  const blockedChatResponse = (message) =>
    new Response(
      JSON.stringify({
        detail: message,
      }),
      {
        status: 403,
        headers: { "Content-Type": "application/json" },
      },
    );

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const path = urlPath(input);
    const method = String(init?.method || input?.method || "GET").toUpperCase();
    let nextInput = input;
    let nextInit = init;

    const chatBlock = chatAccessBlock();
    if (
      method === "POST" &&
      CHAT_ENDPOINTS.has(path) &&
      (chatBlock ||
        (sessionRole &&
          sessionRole !== "admin" &&
          authSecurity.loaded &&
          authSecurity.maintenance_enabled))
    ) {
      return blockedChatResponse(
        chatBlock
          ? chatBlock.title
          : authSecurity.maintenance_message || "系统正在维护，请稍后再试。",
      );
    }

    if (method === "POST" && path === SIGNUP_ENDPOINT && authSecurity.turnstile_enabled) {
      try {
        const raw = await bodyText(input, init);
        const payload = raw ? JSON.parse(raw) : null;
        if (payload && typeof payload === "object") {
          payload.turnstile_token = turnstileToken;
          const serialized = JSON.stringify(payload);
          if (typeof Request !== "undefined" && input instanceof Request) {
            nextInput = new Request(input, { ...init, body: serialized });
            nextInit = undefined;
          } else {
            nextInit = { ...init, body: serialized };
          }
        }
      } catch (_error) {
        // The server remains authoritative and will reject a missing token.
      }
    }

    if (method === "POST" && CHAT_ENDPOINTS.has(path)) {
      try {
        const raw = await bodyText(input, init);
        const payload = raw ? JSON.parse(raw) : null;
        let capability = payload ? capabilityForModel(payload.model) : null;
        if (payload && !capability) {
          // A new Open WebUI account can briefly submit an empty native model
          // before its selector has persisted a value. The visible Turtle
          // workspace is already authoritative, so recover only this browser
          // request to the deployment-managed Provider alias.
          const provider = providerFromRoute();
          const modelId = PROVIDER_MODELS[provider];
          payload.model = modelId;
          capability = capabilityForModel(modelId);
        }
        if (payload && capability) {
          const controls = document.querySelector("#turtle-runtime-controls");
          const selection = validSelection(
            capability,
            controls && controls.dataset.modelId === capability.model_id
              ? {
                  version: controls.querySelector("#turtle-version-select")?.value,
                  thinking: controls.querySelector("#turtle-thinking-select")?.value,
                }
              : readSelection(capability),
          );
          payload[capability.version_field] = selection.version;
          payload[capability.thinking_field] = selection.thinking;
          if (capability.family === "claude") {
            // Enabling the tool does not force a search. It gives Claude the
            // same choice as the official chat toggle on every prompt.
            payload.web_search = claudeWebSearchEnabled();
          }
          const queueRequestId = newRequestId();
          payload.turtle_queue_request_id = queueRequestId;
          const serialized = JSON.stringify(payload);

          if (typeof Request !== "undefined" && input instanceof Request) {
            nextInput = new Request(input, { ...init, body: serialized });
            nextInit = undefined;
          } else {
            nextInit = { ...init, body: serialized };
          }
        }
      } catch (_error) {
        // Preserve the original request if it is not a JSON chat completion.
      }
    }

    let response;
    try {
      response = await originalFetch(nextInput, nextInit);
    } catch (error) {
      throw error;
    }
    if (path === SIGNOUT_ENDPOINT && response.ok) rememberSessionRole("");
    response = await captureSessionResponse(response, path);
    response = await providerFilteredResponse(response, path, method);
    response = await providerModelResponse(response, path, method);
    if (method === "GET" && MODEL_ENDPOINTS.has(path)) {
      void captureCapabilities(response);
      void loadChatPolicy();
    }
    if (
      method === "POST" &&
      CHAT_ENDPOINTS.has(path) &&
      (response.ok || response.status === 429 || response.status === 403 || response.status === 409)
    ) {
      if (!response.ok) void loadChatPolicy(true);
      setTimeout(() => void loadChatPolicy(true), 3500);
      if (response.ok) {
        setTimeout(() => void loadConversationIndex(true), 1200);
        setTimeout(() => void loadConversationIndex(true), 5000);
      }
    }
    if (response.ok && method !== "GET" && path.startsWith("/api/v1/chats")) {
      setTimeout(() => void loadConversationIndex(true), 350);
    }
    if (method === "POST" && path === SIGNUP_ENDPOINT) {
      if (!response.ok) resetTurnstileWidget();
      else turnstileToken = "";
    }
    return response;
  };

  const handleWorkspaceNavigation = (event) => {
    if (!(event.target instanceof Element)) return;
    if (chatAccessBlock() && event.target.closest("#send-message-button")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      syncSiteAccess();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

    const workspaceLink = event.target.closest("a[data-turtle-workspace-provider]");
    if (workspaceLink) {
      const targetProvider = normalize(workspaceLink.dataset.turtleWorkspaceProvider);
      if (!Object.hasOwn(PROVIDER_MODELS, targetProvider)) return;
      // The overlay anchor lives inside Open WebUI's native option button.
      // Own this click completely so one gesture cannot invoke both routers.
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      dismissNativeWorkspacePicker();
      void openWorkspace(targetProvider).finally(dismissNativeWorkspacePicker);
      return;
    }

    const adminConsoleLink = event.target.closest('a[href]');
    if (adminConsoleLink) {
      try {
        const target = new URL(adminConsoleLink.getAttribute("href") || "", window.location.origin);
        if (target.origin === window.location.origin && target.pathname === "/admin") {
          event.preventDefault();
          event.stopImmediatePropagation();
          window.location.assign("/admin#/overview");
          return;
        }
      } catch (_error) {
        // Ignore malformed unrelated links and preserve the native handler.
      }
    }

    const newChatButton = event.target.closest("#sidebar-new-chat-button");
    if (newChatButton) {
      const provider = providerFromRoute();
      rememberWorkspace(provider);
      if (newChatButton.tagName === "A") {
        newChatButton.setAttribute("href", workspaceUrl(provider));
      }
      // Preserve Open WebUI's native client-side navigation. The former hard
      // navigation here caused every new chat to reload the app.
      return;
    }

    const chatLink = event.target.closest("a[href]");
    const linkedChatId = chatLink ? chatIdFromLink(chatLink) : null;
    if (linkedChatId) {
      const linkedProvider = conversationProviders.get(linkedChatId) || "gpt";
      if (linkedProvider !== providerFromRoute()) {
        event.preventDefault();
        event.stopImmediatePropagation();
        syncProviderWorkspace();
        return;
      }
    }

    const modelOption = event.target.closest('button[role="option"][data-value]');
    const targetProvider = modelOption ? providerForModel(modelOption.dataset.value) : null;
    if (!targetProvider || targetProvider === providerFromRoute()) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    dismissNativeWorkspacePicker();
    void openWorkspace(targetProvider).finally(dismissNativeWorkspacePicker);
  };

  let authenticatedWarmupScheduled = false;
  const scheduleAuthenticatedWarmup = () => {
    if (authenticatedWarmupScheduled || (!storedToken() && !sessionRole)) return;
    authenticatedWarmupScheduled = true;
    const warm = () => {
      authenticatedWarmupScheduled = false;
      void loadChatPolicy(true);
      void loadProviderDisplay();
      void loadConversationIndex();
    };
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(warm, { timeout: 900 });
    } else {
      window.setTimeout(warm, 120);
    }
  };

  const start = () => {
    document.documentElement.dataset.turtleModelControls = "ready";
    new MutationObserver(queueMount).observe(document.body, {
      attributes: true,
      // Open WebUI v0.11 pre-mounts popovers; aria-expanded is the reliable
      // signal that their live content is ready for Turtle enhancements.
      attributeFilter: ["src", "disabled", "aria-expanded"],
      childList: true,
      subtree: true,
    });
    document.addEventListener("click", handleWorkspaceNavigation, true);
    document.addEventListener(
      "submit",
      (event) => {
        if (
          chatAccessBlock() &&
          event.target instanceof HTMLFormElement &&
          event.target.querySelector("#message-input-container")
        ) {
          event.preventDefault();
          event.stopImmediatePropagation();
          syncSiteAccess();
        }
      },
      true,
    );
    document.addEventListener("input", queueMount, true);
    window.addEventListener("turtle-chat-policy-updated", () => void loadChatPolicy(true));
    window.addEventListener("popstate", () => {
      queueMount();
      setTimeout(() => void refreshWorkspaceChatList(), 0);
    });
    window.addEventListener("focus", () => void loadConversationIndex());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void loadConversationIndex();
    });
    void loadAuthSecurity(true);
    scheduleAuthenticatedWarmup();
    setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void loadAuthSecurity(true);
      const quotaExpired = refreshQuotaCountdowns();
      document.querySelector("#turtle-runtime-controls")?.refreshQuotaPresentation?.();
      if (storedToken()) void loadChatPolicy(quotaExpired);
    }, 30_000);
    queueMount();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
