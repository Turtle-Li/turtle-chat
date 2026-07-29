(() => {
  "use strict";

  const ADMIN_API = "/api/v1/turtle/admin";
  const AUTH_API = "/api/v1/turtle/auth";
  const CHAT_API = "/api/v1/turtle/chat";
  const STORAGE_API = "/api/v1/turtle/storage";
  const PROJECT_API = "/api/v1/turtle/project-api";
  const STATIC_THUMBNAIL = {
    max_dimension: 480,
    quality: 0.72,
    content_type: "image/webp",
    max_bytes: 2 * 1024 * 1024,
  };
  const ROUTES = {
    overview: { title: "管理总览", eyebrow: "OPERATIONS" },
    announcements: { title: "公告", eyebrow: "USER ONBOARDING" },
    operations: { title: "运维监控", eyebrow: "SERVICE OBSERVABILITY" },
    projectApi: { title: "项目 API", eyebrow: "PROJECT ATTRIBUTION" },
    providers: { title: "Provider", eyebrow: "AI ROUTING" },
    users: { title: "用户", eyebrow: "USER DIRECTORY" },
    subscriptions: { title: "订阅管理", eyebrow: "SUBSCRIPTION CONTROL" },
    access: { title: "分组策略", eyebrow: "GROUP POLICY" },
    storage: { title: "存储", eyebrow: "MEDIA STORAGE" },
    system: { title: "系统", eyebrow: "SYSTEM BOUNDARY" },
  };

  const app = document.querySelector("#turtle-admin-app");
  const content = document.querySelector("#admin-content");
  const drawer = document.querySelector("#admin-drawer");
  const drawerBody = document.querySelector("#drawer-body");
  const drawerFooter = document.querySelector("#drawer-footer");
  const modal = document.querySelector("#admin-modal");
  const modalBody = document.querySelector("#modal-body");
  const modalFooter = document.querySelector("#modal-footer");
  const originalFetch = window.fetch.bind(window);
  const cache = new Map();
  const inflight = new Map();
  const state = {
    route: "overview",
    userQuery: "",
    userRole: "all",
    subscriptionQuery: "",
    subscriptionStatus: "all",
    subscriptionResourceGroup: "",
    subscriptionGptGroup: "",
    subscriptionClaudeGroup: "",
    subscriptionSelectedUserIds: new Set(),
    usersBundle: null,
    chatAdmin: null,
    storageUsers: null,
    providerData: null,
    accountPools: null,
    activeGroup: null,
    forceRefresh: false,
    lastFocus: null,
    operationsHours: 1,
    projectApiHours: 24,
    projectApiOwner: "",
    projectApiKey: "",
    projectApiModel: "",
    projectApiOutcome: "",
    projectApiOffset: 0,
    projectApiUsers: [],
    projectApiUserSearchTimer: null,
    projectApiUserSearchGeneration: 0,
    modalResolve: null,
    modalLastFocus: null,
    lastOverviewAutoAt: 0,
    renderId: 0,
    prefetchScheduled: false,
    providerPollTimer: null,
    loginSessions: new Map(),
    loginWindows: new Map(),
    thumbnailBackfillRunning: false,
    announcementPreviewTimer: null,
    announcementPreviewGeneration: 0,
    announcementSelectedId: "",
    announcementCreateMode: false,
  };

  class RequestError extends Error {
    constructor(message, code = "request", status = 0) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  const clone = (value) => JSON.parse(JSON.stringify(value));

  const storedToken = () => {
    let value = localStorage.getItem("token") || "";
    if (!value) return "";
    try {
      const parsed = JSON.parse(value);
      if (typeof parsed === "string") value = parsed;
    } catch (_error) {
      // Open WebUI normally stores the token as a plain string.
    }
    return value;
  };

  const authHeaders = (extra = {}) => {
    const headers = new Headers(extra);
    const token = storedToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return headers;
  };

  const responseMessage = async (response, fallback) => {
    try {
      const payload = await response.clone().json();
      const detail = payload?.detail;
      if (typeof detail === "string") return detail;
      if (detail?.message) return detail.message;
      if (payload?.message) return payload.message;
      if (payload?.error?.message) return payload.error.message;
    } catch (_error) {
      // Keep the fallback concise.
    }
    return `${fallback}（HTTP ${response.status}）`;
  };

  const requestJson = async (path, init = {}) => {
    const response = await originalFetch(path, {
      ...init,
      headers: authHeaders(init.headers),
      credentials: "same-origin",
    });
    if (response.status === 401) throw new RequestError("请先登录后再进入管理后台", "unauthorized", 401);
    if (response.status === 403) throw new RequestError("当前账号没有管理员权限", "forbidden", 403);
    if (!response.ok) throw new RequestError(await responseMessage(response, "请求失败"), "request", response.status);
    if (response.status === 204) return null;
    return response.json();
  };

  const cached = async (key, loader, maxAge = 15_000) => {
    const entry = cache.get(key);
    if (!state.forceRefresh && entry && Date.now() - entry.at < maxAge) return entry.value;
    if (inflight.has(key)) return inflight.get(key);
    const request = Promise.resolve()
      .then(loader)
      .then((value) => {
        cache.set(key, { value, at: Date.now() });
        return value;
      })
      .finally(() => {
        if (inflight.get(key) === request) inflight.delete(key);
      });
    inflight.set(key, request);
    return request;
  };

  const clearCache = (...keys) => {
    if (!keys.length) {
      cache.clear();
      state.accountPools = null;
    } else {
      keys.forEach((key) => cache.delete(key));
      if (keys.includes("account-pools")) state.accountPools = null;
    }
  };

  const bytes = (value) => {
    let amount = Math.max(0, Number(value) || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    const precision = index === 0 || amount >= 10 ? 0 : 1;
    return `${amount.toFixed(precision)} ${units[index]}`;
  };

  const canvasBlob = async (bitmap, width, height) => {
    if (typeof OffscreenCanvas !== "undefined") {
      const canvas = new OffscreenCanvas(width, height);
      canvas.getContext("2d", { alpha: true }).drawImage(bitmap, 0, 0, width, height);
      return canvas.convertToBlob({
        type: STATIC_THUMBNAIL.content_type,
        quality: STATIC_THUMBNAIL.quality,
      });
    }
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d", { alpha: true }).drawImage(bitmap, 0, 0, width, height);
    return new Promise((resolve) => canvas.toBlob(
      resolve,
      STATIC_THUMBNAIL.content_type,
      STATIC_THUMBNAIL.quality,
    ));
  };

  const createStaticThumbnail = async (source) => {
    if (typeof createImageBitmap !== "function") throw new Error("当前浏览器不支持缩略图回填");
    let bitmap;
    try {
      try {
        bitmap = await createImageBitmap(source, { imageOrientation: "from-image" });
      } catch (_error) {
        bitmap = await createImageBitmap(source);
      }
      const scale = Math.min(1, STATIC_THUMBNAIL.max_dimension / Math.max(bitmap.width, bitmap.height));
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const blob = await canvasBlob(bitmap, width, height);
      if (
        !blob
        || blob.type !== STATIC_THUMBNAIL.content_type
        || blob.size <= 0
        || blob.size > STATIC_THUMBNAIL.max_bytes
      ) {
        throw new Error("浏览器生成的缩略图不符合限制");
      }
      return { blob, width, height };
    } finally {
      bitmap?.close?.();
    }
  };

  const toBytes = (gigabytes) => Math.max(0, Math.round((Number(gigabytes) || 0) * 1024 ** 3));
  const toGigabytes = (value) => ((Number(value) || 0) / 1024 ** 3).toFixed(2).replace(/\.00$/, "");
  const numberText = (value) => new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  const projectUsd = (microUsd) => {
    if (microUsd == null) return "—";
    const value = Number(microUsd || 0) / 1_000_000;
    return `$${value < 0.01 ? value.toFixed(6) : value.toFixed(4)}`;
  };
  const projectUsageSource = (source) =>
    source === "upstream_reported"
      ? "上游 usage"
      : source === "locally_estimated"
        ? "本地估算"
        : source === "not_charged"
          ? "未计费"
          : "请求兜底";

  const dateTime = (timestamp) => {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  };

  const subscriptionDateTime = (timestamp) => {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  };

  const beijingDateTimeInput = (timestamp) => {
    if (!timestamp) return "";
    const shifted = new Date(Number(timestamp) * 1000 + 8 * 60 * 60 * 1000);
    const pad = (value) => String(value).padStart(2, "0");
    return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`;
  };

  const beijingInputTimestamp = (value) => {
    const matched = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
    if (!matched) return null;
    const [, year, month, day, hour, minute] = matched.map(Number);
    return Math.floor(Date.UTC(year, month - 1, day, hour - 8, minute, 59) / 1000);
  };

  const defaultSubscriptionExpiry = (days = 30, baseTimestamp = Math.floor(Date.now() / 1000)) => {
    const shifted = new Date(Number(baseTimestamp) * 1000 + 8 * 60 * 60 * 1000);
    return Math.floor(Date.UTC(
      shifted.getUTCFullYear(),
      shifted.getUTCMonth(),
      shifted.getUTCDate() + Number(days || 30),
      15,
      59,
      59,
    ) / 1000);
  };

  const relativeTime = (timestamp) => {
    if (!timestamp) return "尚无记录";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000) - Number(timestamp));
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  };

  const safeLoginSession = (runtime) => {
    if (runtime?.login_mode !== "remote_browser") return null;
    const expiresAt = Number(runtime.login_session_expires_at) || 0;
    if (expiresAt <= Date.now() / 1000) return null;
    try {
      const url = new URL(String(runtime.login_session_url || ""), window.location.origin);
      const localHttp = url.origin === window.location.origin
        && ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
      const capability = /^#token=[A-Za-z0-9_-]{32,256}$/.test(url.hash);
      if (
        (url.protocol !== "https:" && !localHttp)
        || url.username
        || url.password
        || url.search
        || !capability
      ) return null;
      return { url: url.href, expiresAt };
    } catch (_error) {
      return null;
    }
  };

  const rememberLoginSession = (accountId, runtime) => {
    const session = safeLoginSession(runtime);
    if (session) state.loginSessions.set(String(accountId), session);
    return session;
  };

  const loginSessionFor = (accountId) => {
    const key = String(accountId || "");
    const session = state.loginSessions.get(key);
    if (!session) return null;
    if (session.expiresAt <= Date.now() / 1000) {
      state.loginSessions.delete(key);
      return null;
    }
    return session;
  };

  const loginExpiryText = (expiresAt) => {
    const remaining = Math.max(0, Number(expiresAt) - Date.now() / 1000);
    if (!remaining) return "链接已过期";
    const minutes = Math.floor(remaining / 60);
    const seconds = Math.floor(remaining % 60);
    return `${minutes}:${String(seconds).padStart(2, "0")} 后失效`;
  };

  const loginSessionLink = (accountId, className = "button") => {
    const session = loginSessionFor(accountId);
    if (!session) return "";
    return `<a class="${escapeHtml(className)}" href="${escapeHtml(session.url)}" target="_blank" rel="noopener noreferrer">打开安全登录页</a>`;
  };

  const remoteLoginExpected = (accountId = "") => {
    const accounts = accountPoolData().accounts || [];
    const selected = accounts.find((account) => String(account.id) === String(accountId));
    if (selected?.login_runtime?.login_mode === "remote_browser") return true;
    return accounts.some((account) => account.login_runtime?.login_mode === "remote_browser");
  };

  const reserveLoginWindow = (expected) => {
    if (!expected) return null;
    const popup = window.open("about:blank", "_blank");
    if (!popup) return null;
    try {
      popup.document.title = "正在准备安全登录 · Turtle’s Chat";
      popup.document.body.innerHTML = `
        <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:#071016;color:#e8f5f3;font:16px/1.6 system-ui,sans-serif">
          <div style="max-width:420px;padding:32px;text-align:center">
            <strong style="display:block;font-size:20px">正在准备安全登录页面</strong>
            <span style="display:block;margin-top:8px;color:#9fb7b5">请稍候，不要关闭这个窗口。</span>
          </div>
        </main>`;
    } catch (_error) {
      // The reserved window can still be navigated even when its document is unavailable.
    }
    return popup;
  };

  const failReservedLoginWindow = (popup) => {
    try {
      if (!popup || popup.closed) return;
      popup.document.title = "安全登录页未能打开 · Turtle’s Chat";
      popup.document.body.innerHTML = `
        <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:#071016;color:#e8f5f3;font:16px/1.6 system-ui,sans-serif">
          <div style="max-width:440px;padding:32px;text-align:center">
            <strong style="display:block;font-size:20px">安全登录页未能打开</strong>
            <span style="display:block;margin-top:8px;color:#9fb7b5">请返回 Provider 页面，根据错误提示重试。</span>
          </div>
        </main>`;
    } catch (_error) {
      // Keep the tab open when the browser no longer allows document access.
    }
  };

  const closeReservedLoginWindow = (popup) => {
    try {
      if (popup && !popup.closed) popup.close();
    } catch (_error) {
      // A browser may revoke the opener handle after creating the tab.
    }
  };

  const rememberLoginWindow = (accountId, popup) => {
    const key = String(accountId || "");
    if (!key || !popup || popup.closed) return;
    const previous = state.loginWindows.get(key);
    if (previous && previous !== popup) closeReservedLoginWindow(previous);
    state.loginWindows.set(key, popup);
  };

  const closeLoginWindow = (accountId) => {
    const key = String(accountId || "");
    const popup = state.loginWindows.get(key);
    closeReservedLoginWindow(popup);
    state.loginWindows.delete(key);
  };

  const openReservedLoginWindow = (popup, runtime, session) => {
    if (runtime?.login_mode !== "remote_browser") {
      closeReservedLoginWindow(popup);
      return false;
    }
    if (!session) {
      failReservedLoginWindow(popup);
      return false;
    }
    try {
      if (popup && !popup.closed) {
        popup.location.replace(session.url);
        return true;
      }
    } catch (_error) {
      failReservedLoginWindow(popup);
    }
    return false;
  };

  const updateLoginCountdowns = () => {
    document.querySelectorAll("[data-login-expires-at]").forEach((element) => {
      const expiresAt = Number(element.dataset.loginExpiresAt) || 0;
      element.textContent = loginExpiryText(expiresAt);
      if (expiresAt <= Date.now() / 1000) element.closest(".remote-login-session")?.classList.add("is-expired");
    });
  };

  const windowText = (seconds) => {
    const value = Number(seconds) || 0;
    if (!value) return "—";
    if (value % 86400 === 0) return `${value / 86400} 天`;
    if (value % 3600 === 0) return `${value / 3600} 小时`;
    return `${Math.ceil(value / 60)} 分钟`;
  };

  const resourceGroupDetail = (group) =>
    group
      ? `媒体空间 ${bytes(group.storage_quota_bytes)}；资源组并发 ${numberText(group.max_concurrency)}；用户默认并发 ${numberText(group.default_user_concurrency)}`
      : "未找到资源组详情";

  const modelGroupDetail = (group, selections = [], bundle = null) => {
    if (!group) return "未找到额度组详情";
    const labels = new Map(selections.map((item) => [item.key, `${item.version_label} · ${item.level_label}`]));
    const preset = modelGroupPreset(bundle, group);
    const metadata = new Map((preset?.rules || []).map((rule) => [rule.selection_key, rule]));
    const enabled = (group.rules || []).filter((rule) => rule.enabled);
    if (!enabled.length) return "未开放任何档位";
    return enabled.map((rule) => {
      const source = metadata.get(rule.selection_key)?.source;
      const limit = rule.limit_count == null
        ? source === "official_dynamic"
          ? "动态额度 · 以上游为准"
          : source === "official_multiplier"
            ? "套餐倍率 · 以上游为准"
            : "本站不设硬上限"
        : `${numberText(rule.limit_count)} 次 / ${windowText(rule.window_seconds)}`;
      return `${labels.get(rule.selection_key) || rule.selection_key}：${limit}`;
    }).join("；");
  };

  const modelGroupPreset = (bundle, group) =>
    (bundle?.presetsByProvider?.[group?.provider_family] || [])
      .find((preset) => preset.id === group?.template_preset_id) || null;

  const modelGroupSourceText = (source) => ({
    official_published: "官方固定值",
    official_dynamic: "官方动态额度",
    official_multiplier: "官方套餐倍率",
    published_approximation: "公开值 · 近似路由",
    not_in_plan: "套餐不包含",
    site_rule: "站内规则",
  })[source] || "站内自定义";

  const modelGroupRuleValue = (rule, meta) => {
    if (!rule?.enabled) return "未开放";
    if (rule.limit_count != null) {
      return `${numberText(rule.limit_count)} 次 / ${windowText(rule.window_seconds)}`;
    }
    if (meta?.source === "official_dynamic") return "动态 · 以上游为准";
    if (meta?.source === "official_multiplier") return "套餐倍率 · 动态";
    return "本站不设硬上限";
  };

  const modelGroupVisual = (group, bundle) => {
    if (!group) return '<div class="model-group-visual-empty">未选择额度组</div>';
    const selections = (bundle?.selections || []).filter(
      (selection) => selection.family === group.provider_family,
    );
    const ruleMap = new Map((group.rules || []).map((rule) => [rule.selection_key, rule]));
    const preset = modelGroupPreset(bundle, group);
    const metaMap = new Map((preset?.rules || []).map((rule) => [rule.selection_key, rule]));
    const lanes = selections.map((selection) => {
      const rule = ruleMap.get(selection.key) || {};
      const meta = metaMap.get(selection.key) || {};
      const source = modelGroupSourceText(meta.source);
      return `<div class="model-group-visual-lane" data-enabled="${String(Boolean(rule.enabled))}">
        <span><strong>${escapeHtml(selection.level_label)}</strong><small>${escapeHtml(selection.version_label)}</small></span>
        <b>${escapeHtml(modelGroupRuleValue(rule, meta))}</b>
        <em>${escapeHtml(source)}</em>
      </div>`;
    }).join("");
    const note = preset
      ? `<p><strong>${escapeHtml(preset.official_note || "")}</strong><span>${escapeHtml(preset.recommendation_note || "")}</span></p>`
      : `<p><strong>自定义额度组</strong><span>${escapeHtml(group.description || "由管理员维护逐档权限与时间窗。")}</span></p>`;
    return `<div class="model-group-visual">
      <header><div><span>${escapeHtml(familyText(group.provider_family))} 套餐明细</span><strong>${escapeHtml(group.name)}</strong></div>${group.is_plan_template ? '<i>官方套餐模板</i>' : '<i>自定义</i>'}</header>
      <div class="model-group-visual-grid">${lanes}</div>
      ${note}
    </div>`;
  };

  const modelGroupPicker = (family, groups, current, bundle) => {
    const ordered = groups
      .filter((group) => !group.is_retired || group.id === current)
      .sort((left, right) =>
      Number(left.sort_order || 800) - Number(right.sort_order || 800)
      || String(left.name || "").localeCompare(String(right.name || ""), "zh-CN")
      );
    const selected = ordered.find((group) => group.id === current) || null;
    const choices = ordered.map((group) => {
      const enabled = (group.rules || []).filter((rule) => rule.enabled);
      const fixed = enabled.filter((rule) => rule.limit_count != null);
      const preset = modelGroupPreset(bundle, group);
      const title = preset?.label || group.name;
      return `<label class="model-group-choice" data-selected="${String(group.id === current)}">
        <input type="radio" name="model_group_${escapeHtml(family)}" value="${escapeHtml(group.id)}" data-model-group-choice data-family="${escapeHtml(family)}" ${group.id === current ? "checked" : ""}/>
        <span class="model-group-choice-mark" aria-hidden="true"></span>
        <span class="model-group-choice-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(group.name)}</small></span>
        <span class="model-group-choice-metrics"><b>${numberText(enabled.length)}</b> 档${fixed.length ? ` · ${numberText(fixed.length)} 个固定窗` : " · 动态"}</span>
      </label>`;
    }).join("");
    return `<fieldset class="subscription-model-group-picker" data-model-group-picker="${escapeHtml(family)}">
      <legend>${escapeHtml(familyText(family))} 额度组</legend>
      ${selected ? "" : '<div class="legacy-policy-warning">当前仍是旧版单用户配置；选择套餐后才会迁移。</div>'}
      <div class="model-group-choice-list">${choices}</div>
      <div data-model-group-detail="${escapeHtml(family)}">${modelGroupVisual(selected, bundle)}</div>
    </fieldset>`;
  };

  const roleText = (role) => ({ admin: "管理员", user: "用户", pending: "待审批" })[role] || role || "未知";
  const subscriptionStatusText = (status) => ({
    active: "生效中",
    scheduled: "待生效",
    expired: "已过期",
    cancelled: "已停止",
    inactive: "未开通",
    pending: "待激活",
    unlimited: "管理员不限期",
  })[status] || "状态未知";
  const subscriptionBadge = (subscription) =>
    statusBadge(
      subscription?.status || "inactive",
      subscriptionStatusText(subscription?.status || "inactive"),
    );
  const familyText = (family) => ({ gpt: "GPT", claude: "Claude" })[family] || family || "其他";
  const providerStateText = (provider) => provider?.message || ({
    ready: "连接正常",
    planned: "尚未接入",
    auth_required: "需要重新认证",
    verification_required: "等待模型验证",
    offline: "服务不可达",
    degraded: "上游异常",
    misconfigured: "配置无效",
  })[provider?.state] || "状态未知";

  const accountStateText = (account) => {
    if (account?.status === "disabled" && account?.session_state === "valid" && account?.health_status === "healthy") {
      return "检测通过 · 待启用";
    }
    return ({
      ready: "可调度",
      disabled: "已停用",
      cooldown: "冷却中",
      reauth_required: "需要重新登录",
    })[account?.status] || "尚未验证";
  };

  const quotaSourceText = (source) => ({
    official_published: "官方公开值",
    official_dynamic: "官方动态额度",
    official_multiplier: "官方套餐倍率",
    published_approximation: "公开值 · 本站近似路由",
    turtle_recommendation: "Turtle 保守调度预算",
    untracked: "未跟踪上游额度",
  })[source] || "本地调度预算";

  const quotaLaneStateText = (lane) => ({
    available: "可调度",
    reserve: "安全保留区",
    exhausted: "本窗预算已用完",
    cooldown: "上游 429 冷却",
    disabled: "模板未开放",
    dynamic: "动态额度 · 上游为准",
    untracked: "只统计，不限额",
  })[lane?.state] || "未知";

  const statusBadge = (stateValue, label) =>
    `<span class="status-badge" data-state="${escapeHtml(stateValue)}">${escapeHtml(label)}</span>`;

  const roleBadge = (role) =>
    `<span class="role-badge" data-role="${escapeHtml(role)}">${escapeHtml(roleText(role))}</span>`;

  const familyBadge = (family) =>
    `<span class="family-badge" data-family="${escapeHtml(family)}">${escapeHtml(familyText(family))}</span>`;

  const toast = (message, tone = "success") => {
    const region = document.querySelector("#admin-toast-region");
    const element = document.createElement("div");
    element.className = "toast";
    element.dataset.tone = tone;
    element.setAttribute("role", tone === "error" ? "alert" : "status");
    const symbols = { success: "✓", info: "i", error: "!" };
    element.innerHTML = `<b aria-hidden="true">${symbols[tone] || "i"}</b><span>${escapeHtml(message)}</span><button type="button" aria-label="关闭提示">×</button>`;
    element.querySelector("button").addEventListener("click", () => element.remove(), { once: true });
    region.append(element);
    window.setTimeout(() => element.remove(), 4600);
  };

  const loadingView = (route = state.route) => `
    <section class="page-loading" aria-live="polite">
      <div class="loading-message"><span aria-hidden="true"></span><div><strong>正在加载${escapeHtml(ROUTES[route]?.title || "管理数据")}</strong><small>页面框架已经就绪，各数据模块正在异步读取</small></div></div>
      <div class="loading-title"></div>
      <div class="loading-stats"><span></span><span></span><span></span><span></span></div>
      <div class="loading-grid"><span></span><span></span></div>
    </section>`;

  const inlineLoading = (label) => `
    <div class="module-loading" aria-live="polite">
      <span aria-hidden="true"></span><div><strong>${escapeHtml(label)}</strong><small>不影响页面其他区域使用</small></div>
    </div>`;

  const pageIntro = (title, copy, actions = "") => `
    <section class="page-intro">
      <div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(copy)}</p></div>
      ${actions ? `<div class="page-intro-actions">${actions}</div>` : ""}
    </section>`;

  const accessGate = (error) => {
    const forbidden = error?.code === "forbidden";
    content.innerHTML = `
      <section class="access-gate">
        <div>
          <span class="gate-mark">${forbidden ? "限" : "登"}</span>
          <h2>${forbidden ? "需要管理员权限" : "请先登录"}</h2>
          <p>${escapeHtml(error?.message || "验证登录状态后才能读取管理数据。")}</p>
          <a class="button" href="/?redirect=%2Fadmin">${forbidden ? "返回聊天页面" : "前往登录"}</a>
        </div>
      </section>`;
  };

  const errorView = (error) => {
    if (error?.code === "unauthorized" || error?.code === "forbidden") return accessGate(error);
    content.innerHTML = `
      <section class="error-state">
        <div><span class="gate-mark">!</span><h2>页面暂时无法读取</h2>
        <p>${escapeHtml(error?.message || "请稍后重试。")}</p>
        <button class="button" type="button" data-action="refresh">重新加载</button></div>
      </section>`;
  };

  const routeFromLocation = () => {
    const candidate = String(window.location.hash || "").replace(/^#\/?/, "").split("/", 1)[0];
    return ROUTES[candidate] ? candidate : "overview";
  };

  const updateRouteChrome = (route) => {
    const meta = ROUTES[route];
    document.querySelector("#page-title").textContent = meta.title;
    document.querySelector("#page-eyebrow").textContent = meta.eyebrow;
    document.title = `${meta.title} · Turtle’s Chat`;
    document.querySelectorAll("[data-route]").forEach((button) => {
      if (button.dataset.route === route) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  };

  const updateGlobalStatus = (overview) => {
    if (!overview) return;
    const viewer = overview.viewer || {};
    const identity = document.querySelector("#admin-identity");
    identity.querySelector("span").textContent = String(viewer.name || "管").trim().slice(0, 1).toUpperCase();
    identity.querySelector("strong").textContent = viewer.name || "管理员";
    identity.querySelector("small").textContent = roleText(viewer.role);
    const pending = Number(overview.users?.roles?.pending || 0);
    const pendingBadge = document.querySelector("#pending-nav-badge");
    pendingBadge.textContent = pending;
    pendingBadge.hidden = pending === 0;
    const providers = overview.providers || [];
    const health = document.querySelector("#sidebar-health");
    if (!providers.length) {
      health.dataset.state = "loading";
      health.querySelector("strong").textContent = "Provider 后台检测中";
      health.querySelector("small").textContent = "其他管理数据已经可用";
      return;
    }
    const unhealthy = providers.filter((provider) => provider.state !== "ready");
    health.dataset.state = unhealthy.length ? "warning" : "ready";
    health.querySelector("strong").textContent = unhealthy.length ? `${unhealthy.length} 项需要关注` : "核心服务正常";
    health.querySelector("small").textContent = `${providers.filter((item) => item.state === "ready").length}/${providers.length || 0} Provider 就绪`;
  };

  const loadOverview = () => cached("overview", () => requestJson(`${ADMIN_API}/overview`), 30_000);
  const loadChatAdmin = () => cached("chat-admin", () => requestJson(`${CHAT_API}/admin/users`), 30_000);
  const loadAnnouncementAdmin = () =>
    cached("announcement-admin", () => requestJson(`${CHAT_API}/admin/announcements`), 30_000);
  const loadStorageConfig = () => cached("storage-config", () => requestJson(`${STORAGE_API}/admin/config`), 60_000);
  const loadStorageUsers = () => cached("storage-users", () => requestJson(`${STORAGE_API}/admin/users`), 30_000);
  const loadAuthSecurityConfig = () =>
    cached("auth-security-config", () => requestJson(`${AUTH_API}/admin/config`), 30_000);
  const loadUpstreamCleanup = () =>
    cached("upstream-cleanup", () => requestJson(`${ADMIN_API}/upstream-cleanup`), 30_000);
  const loadModels = () => cached("models", () => requestJson("/api/models"), 60_000);
  const loadVersion = () => cached("version", () => requestJson("/api/version"), 60_000);
  const loadProviders = () => cached("providers", () => requestJson(`${ADMIN_API}/providers`), 15_000);
  const loadAccountPools = () => cached("account-pools", () => requestJson(`${ADMIN_API}/account-pools`), 15_000);
  const loadOperations = (hours = state.operationsHours) => cached(
    `operations-${hours}`,
    () => requestJson(`${ADMIN_API}/operations?hours=${hours}`),
    5_000,
  );
  const loadProjectApiUsers = () => cached(
    "project-api-users",
    () => requestJson(`${PROJECT_API}/admin/users`),
    15_000,
  );
  const projectApiQuery = () => {
    const query = new URLSearchParams({ hours: String(state.projectApiHours) });
    if (state.projectApiOwner) query.set("owner_user_id", state.projectApiOwner);
    if (state.projectApiKey) query.set("key_id", state.projectApiKey);
    if (state.projectApiModel) query.set("model", state.projectApiModel);
    if (state.projectApiOutcome) query.set("outcome", state.projectApiOutcome);
    query.set("limit", "100");
    query.set("offset", String(state.projectApiOffset));
    return query.toString();
  };
  const loadProjectApiUsage = () => requestJson(`${PROJECT_API}/admin/usage?${projectApiQuery()}`);
  const loadProjectApiKeys = () => requestJson(
    `${PROJECT_API}/admin/keys${state.projectApiOwner ? `?owner_user_id=${encodeURIComponent(state.projectApiOwner)}` : ""}`,
  );
  const loadProjectApiConfig = () => requestJson(`${PROJECT_API}/admin/config`);

  const schedulePrefetch = () => {
    if (state.prefetchScheduled || state.forceRefresh || !storedToken()) return;
    state.prefetchScheduled = true;
    const warm = () => {
      state.prefetchScheduled = false;
      void Promise.allSettled([
        loadOverview(),
        loadChatAdmin(),
        loadAnnouncementAdmin(),
        loadStorageConfig(),
        loadStorageUsers(),
        loadModels(),
        loadAccountPools(),
        loadOperations(),
      ]);
    };
    if ("requestIdleCallback" in window) window.requestIdleCallback(warm, { timeout: 1200 });
    else window.setTimeout(warm, 180);
  };

  const normalizeModels = (payload) => {
    const items = Array.isArray(payload) ? payload : Array.isArray(payload?.data) ? payload.data : [];
    return items.map((model) => {
      const id = String(model?.id || model?.name || "");
      const declaredFamily = String(model?.turtle?.family || "").toLowerCase();
      const family = declaredFamily === "claude" || id.toLowerCase().includes("claude")
        ? "claude"
        : declaredFamily === "gpt" || id.toLowerCase().includes("gpt")
          ? "gpt"
          : "other";
      return { id, name: String(model?.name || id), family, turtle: model?.turtle || null };
    });
  };

  const PROVIDER_DEFINITIONS = [
    { key: "gpt", label: "ChatGPT", kind: "ChatGPT Web", copy: "固定 Gateway 与网页账号上游" },
    { key: "claude", label: "Claude", kind: "Claude Web", copy: "独立网页登录与真实路由验证" },
  ];

  const providerMini = (provider) => `
    <div class="provider-mini">
      <i>${escapeHtml((provider.label || provider.key || "P").slice(0, 2))}</i>
      <div><strong>${escapeHtml(provider.label || "Provider")}</strong><small>${escapeHtml(provider.kind || provider.public_model || "AI 连接")}</small></div>
      ${statusBadge(provider.state || "planned", providerStateText(provider))}
    </div>`;

  const overviewAttention = (overview, providers) => {
    const attention = [];
    const userData = overview.users || {};
    const storage = overview.storage || {};
    if (Number(userData.roles?.pending || 0) > 0) {
      attention.push({ mark: "订", title: `${userData.roles.pending} 位新用户待激活`, copy: "在订阅管理中分配功能、有效期并激活", route: "subscriptions" });
    }
    providers.filter((item) => !["ready", "planned"].includes(item.state)).forEach((provider) => {
      attention.push({ mark: "源", title: `${provider.label} ${providerStateText(provider)}`, copy: "Provider 未通过当前健康检查", route: "providers" });
    });
    if (storage.strict_external_media && !storage.media_pump_configured) {
      attention.push({ mark: "存", title: "Media Pump 尚未就绪", copy: "严格隔离会阻止模型媒体回存", route: "storage" });
    }
    if (!attention.length && providers.length) {
      attention.push({ mark: "✓", title: "没有需要立即处理的项目", copy: "核心状态和权限策略均处于可用范围", route: "system" });
    }
    return attention;
  };

  const attentionMarkup = (items) => items.map((item) => `
    <button type="button" class="attention-item" data-route="${escapeHtml(item.route)}"><i>${escapeHtml(item.mark)}</i><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.copy)}</small></div></button>`).join("");

  const renderTrend = (daily) => {
    const byDate = new Map((Array.isArray(daily) ? daily : []).map((item) => [String(item.date), item]));
    const items = Array.from({ length: 14 }, (_value, index) => {
      const date = new Date();
      date.setHours(12, 0, 0, 0);
      date.setDate(date.getDate() - (13 - index));
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
      return byDate.get(key) || { date: key, requests: 0 };
    });
    const maxValue = Math.max(1, ...items.map((item) => Number(item.requests) || 0));
    return `<div class="trend-chart" aria-label="最近请求趋势">${items
      .map((item, index) => {
        const value = Number(item.requests) || 0;
        // Leave enough headroom for the real tooltip even when only one or two
        // days contain data; otherwise a single sample becomes a giant block
        // and the panel clips its value label.
        const height = Math.max(value ? 7 : 2, (value / maxValue) * 112);
        const date = String(item.date || "");
        const shortDate = date.slice(5).replace("-", "/");
        const edge = index < 2 ? "start" : index > items.length - 3 ? "end" : "center";
        const label = `${shortDate} · ${numberText(value)} 次请求`;
        return `<div class="trend-day"><span class="trend-bar" data-edge="${edge}" style="height:${height}px" tabindex="0" role="img" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}"><span class="trend-tooltip" role="tooltip">${escapeHtml(label)}</span></span><small>${escapeHtml(date.slice(5))}</small></div>`;
      })
      .join("")}</div>`;
  };

  const renderOverview = async (renderId = state.renderId) => {
    const [overview, chatAdmin] = await Promise.all([loadOverview(), loadChatAdmin()]);
    if (renderId !== state.renderId || state.route !== "overview") return;
    updateGlobalStatus(overview);
    state.chatAdmin = chatAdmin;
    const usersById = new Map((chatAdmin.items || []).map((item) => [item.id, item]));
    const chat = overview.chat || {};
    const userData = overview.users || {};
    const storage = overview.storage || {};
    const providers = [...(overview.providers || [])];

    const attention = overviewAttention(overview, providers);

    const recentRows = (chat.recent || []).map((item) => {
      const target = usersById.get(item.user_id);
      return `<tr>
        <td><div class="user-cell"><span class="user-avatar">${escapeHtml(String(target?.name || "?").slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target?.name || "已删除用户")}</strong><small>${escapeHtml(target?.email || item.user_id || "")}</small></div></div></td>
        <td>${familyBadge(item.family)}</td>
        <td><strong>${escapeHtml(item.version_label)}</strong><br/><small>${escapeHtml(item.level_label)}</small></td>
        <td>${item.status === "committed" ? statusBadge("ready", "已完成") : statusBadge("degraded", item.status === "reserved" ? "处理中" : "已释放")}</td>
        <td>${escapeHtml(relativeTime(item.created_at))}</td>
      </tr>`;
    }).join("");

    content.innerHTML = `
      ${pageIntro("今天的运行情况，一眼看清", "日常监控集中在这里；需要调整时再进入对应模块，不再从聊天页寻找设置。")}
      <section class="stat-grid">
        <article class="stat-card"><header><span>过去 24 小时请求</span><i>24H</i></header><strong>${numberText(chat.requests_24h)}</strong><footer>近 7 天 <b>${numberText(chat.requests_7d)} 次</b></footer></article>
        <article class="stat-card"><header><span>注册用户</span><i>USR</i></header><strong>${numberText(userData.total)}</strong><footer>今日活跃 <b>${numberText(userData.active_today)} 位</b> · 待审批 ${numberText(userData.roles?.pending)}</footer></article>
        <article class="stat-card"><header><span>累计有效请求</span><i>ALL</i></header><strong>${numberText(chat.all_time_requests)}</strong><footer>近 7 天自动降级 <b>${numberText(chat.fallbacks_7d)} 次</b></footer></article>
        <article class="stat-card"><header><span>媒体存储</span><i>OBJ</i></header><strong>${escapeHtml(bytes(storage.used_bytes))}</strong><footer>${storage.provider === "cos" ? "腾讯云 COS" : "本地存储"} · ${storage.direct_upload ? "直传已开" : "服务端上传"}</footer></article>
      </section>

      <section class="dashboard-grid">
        <article class="panel">
          <header class="panel-heading"><div><h3>请求趋势</h3><p>只统计有效完成，不保存提示词或回答</p></div><span>近 14 天</span></header>
          ${renderTrend(chat.daily)}
          <div class="panel-body"><div class="provider-mini-list" data-overview-providers aria-busy="${String(!providers.length)}">${providers.length ? providers.map(providerMini).join("") : inlineLoading("Provider 状态后台检测中")}</div></div>
        </article>
        <div class="quick-list">
          <article class="panel">
            <header class="panel-heading"><div><h3>需要关注</h3><p>按影响优先排列</p></div><span data-overview-attention-count>${attention.length || "…"}</span></header>
            <div class="panel-body attention-list" data-overview-attention>${attention.length ? attentionMarkup(attention) : inlineLoading("正在合并 Provider 状态")}</div>
          </article>
          <article class="panel">
            <header class="panel-heading"><div><h3>常用操作</h3><p>直接进入最常用管理任务</p></div></header>
            <div class="panel-body quick-list">
              <button type="button" class="quick-item" data-route="announcements"><i>告</i><div><strong>发布使用公告</strong><small>Markdown、订阅说明与首次进入提醒</small></div></button>
              <button type="button" class="quick-item" data-route="users"><i>人</i><div><strong>查看用户资料</strong><small>账号身份与系统角色</small></div></button>
              <button type="button" class="quick-item" data-route="subscriptions"><i>订</i><div><strong>激活与续订</strong><small>有效期、分组和并发权限</small></div></button>
              <button type="button" class="quick-item" data-route="access"><i>额</i><div><strong>调整模型额度</strong><small>按 Provider 管理次数与降级链</small></div></button>
              <button type="button" class="quick-item" data-route="operations"><i>运</i><div><strong>查看运维监控</strong><small>并发、排队、时延、异常与资源</small></div></button>
              <button type="button" class="quick-item" data-route="providers"><i>源</i><div><strong>检查 Provider</strong><small>查看 ChatGPT 与 Claude 接入状态</small></div></button>
            </div>
          </article>
        </div>
      </section>

      <article class="panel" style="margin-top:18px">
        <header class="panel-heading"><div><h3>最近请求</h3><p>仅展示路由、状态和时间，不读取消息内容</p></div><span>${(chat.recent || []).length} 条</span></header>
        <div class="table-wrap"><table class="data-table"><thead><tr><th>用户</th><th>Provider</th><th>模型档位</th><th>状态</th><th>时间</th></tr></thead><tbody>${recentRows || '<tr><td colspan="5">暂无请求记录</td></tr>'}</tbody></table></div>
      </article>`;

    void loadProviders().then((payload) => {
      if (renderId !== state.renderId || state.route !== "overview") return;
      const currentProviders = payload.items || [];
      const providerRegion = content.querySelector("[data-overview-providers]");
      if (providerRegion) {
        providerRegion.setAttribute("aria-busy", "false");
        providerRegion.innerHTML = currentProviders.length
          ? currentProviders.map(providerMini).join("")
          : '<p class="muted-copy">尚未配置 Provider</p>';
      }
      const currentAttention = overviewAttention(overview, currentProviders);
      const attentionRegion = content.querySelector("[data-overview-attention]");
      const attentionCount = content.querySelector("[data-overview-attention-count]");
      if (attentionRegion) attentionRegion.innerHTML = attentionMarkup(currentAttention);
      if (attentionCount) attentionCount.textContent = String(currentAttention.length);
      updateGlobalStatus({ ...overview, providers: currentProviders });
    }).catch(() => {
      if (renderId !== state.renderId || state.route !== "overview") return;
      const providerRegion = content.querySelector("[data-overview-providers]");
      if (providerRegion) {
        providerRegion.setAttribute("aria-busy", "false");
        providerRegion.innerHTML = '<p class="muted-copy">Provider 状态暂时无法读取，可前往 Provider 页面重试。</p>';
      }
    });
  };

  const durationText = (value) => {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    const milliseconds = Math.max(0, Number(value));
    if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
    if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
    return `${(milliseconds / 60_000).toFixed(1)} min`;
  };

  const percentageText = (value) => value == null ? "—" : `${Number(value).toFixed(Number(value) < 10 ? 1 : 0)}%`;

  const bitRateText = (value) => {
    let amount = Math.max(0, Number(value) || 0);
    const units = ["bps", "Kbps", "Mbps", "Gbps"];
    let index = 0;
    while (amount >= 1000 && index < units.length - 1) {
      amount /= 1000;
      index += 1;
    }
    return `${amount.toFixed(amount >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  };

  const operationsBars = (buckets) => {
    const items = Array.isArray(buckets) ? buckets : [];
    const maximum = Math.max(1, ...items.map((item) => Number(item.requests) || 0));
    return `<div class="operations-bars" aria-label="请求与异常趋势">${items.map((item, index) => {
      const requests = Number(item.requests) || 0;
      const errors = Number(item.errors) || 0;
      const height = Math.max(requests ? 8 : 2, (requests / maximum) * 100);
      const errorHeight = requests ? Math.min(100, (errors / requests) * 100) : 0;
      const showLabel = index === 0 || index === items.length - 1 || index % Math.max(1, Math.floor(items.length / 6)) === 0;
      const label = new Date(Number(item.at) * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
      return `<span title="${escapeHtml(label)} · ${requests} 次 · ${errors} 异常"><i style="height:${height}%"><b style="height:${errorHeight}%"></b></i><small>${showLabel ? escapeHtml(label) : ""}</small></span>`;
    }).join("")}</div>`;
  };

  const resourceBars = (series, field, maximum = null) => {
    const items = (Array.isArray(series) ? series : []).slice(-120);
    const maxValue = Math.max(1, Number(maximum) || 0, ...items.map((item) => Number(item[field]) || 0));
    return `<div class="resource-bars" aria-hidden="true">${items.map((item) => `<i style="height:${Math.max(2, ((Number(item[field]) || 0) / maxValue) * 100)}%"></i>`).join("") || "<i style=\"height:2%\"></i>"}</div>`;
  };

  const renderOperations = async (renderId = state.renderId) => {
    const data = await loadOperations();
    if (renderId !== state.renderId || state.route !== "operations") return;
    const requests = data.requests || {};
    const latency = requests.latency || {};
    const concurrency = data.concurrency || {};
    const globalConcurrency = concurrency.global || {};
    const resources = data.resources || {};
    const current = resources.current || {};
    const series = resources.series || [];
    const providerRows = (concurrency.providers || []).map((item) => {
      const limit = Number(item.limit) || 0;
      const active = Number(item.active) || 0;
      const width = limit ? Math.min(100, (active / limit) * 100) : 0;
      return `<div class="concurrency-row"><div><strong>${escapeHtml(familyText(item.family))}</strong><small>${numberText(item.queued)} 排队</small></div><span><i style="width:${width}%"></i></span><b>${numberText(active)} / ${limit ? numberText(limit) : "—"}</b></div>`;
    }).join("");
    const accountPoolRows = (concurrency.account_pools || []).map((item) => {
      const limit = Number(item.limit) || 0;
      const active = Number(item.active) || 0;
      const width = limit ? Math.min(100, (active / limit) * 100) : 0;
      return `<div class="concurrency-row"><div><strong>${escapeHtml(item.pool_name || item.pool_id || "ChatGPT 账号组")}</strong><small>${numberText(item.queued)} 排队</small></div><span><i style="width:${width}%"></i></span><b>${numberText(active)} / ${numberText(limit)}</b></div>`;
    }).join("");
    const groupRows = (concurrency.groups || []).map((item) => {
      const limit = Number(item.limit) || 0;
      const active = Number(item.active) || 0;
      const width = limit ? Math.min(100, (active / limit) * 100) : 0;
      return `<div class="concurrency-row"><div><strong>${escapeHtml(item.group_name || item.group_id)}</strong><small>${numberText(item.queued)} 排队</small></div><span><i style="width:${width}%"></i></span><b>${numberText(active)} / ${limit ? numberText(limit) : "—"}</b></div>`;
    }).join("");
    const errors = (requests.recent_errors || []).map((item) => `<tr>
      <td>${familyBadge(item.family)}</td>
      <td><strong>${escapeHtml(item.error_type || "unknown")}</strong><br/><small>${escapeHtml(item.error_phase || "unknown")}</small></td>
      <td>${item.http_status == null ? "—" : numberText(item.http_status)}</td>
      <td>${escapeHtml(durationText(item.total_ms))}</td>
      <td>${escapeHtml(relativeTime(item.created_at))}</td>
    </tr>`).join("");

    content.innerHTML = `
      ${pageIntro("从排队到服务器资源的完整视图", "连接耗时、首字时间、异常类型、并发队列与服务器资源使用同一时间范围；不保存提示词、回答或上游错误正文。", `<div class="range-switch" role="group" aria-label="监控时间范围">${[1, 6, 24].map((hours) => `<button type="button" data-operations-hours="${hours}" aria-pressed="${String(state.operationsHours === hours)}">${hours} 小时</button>`).join("")}</div>`)}
      ${concurrency.state === "unavailable" ? `<div class="danger-banner"><i>!</i><div><strong>并发协调层不可用</strong><br/>${escapeHtml(concurrency.message || "Redis 暂时不可达；新请求会安全失败关闭。")}</div></div>` : ""}
      <section class="stat-grid operations-stat-grid">
        <article class="stat-card"><header><span>实时并发</span><i>RUN</i></header><strong>${numberText(globalConcurrency.active)} / ${globalConcurrency.limit == null ? "—" : numberText(globalConcurrency.limit)}</strong><footer>当前排队 <b>${numberText(globalConcurrency.queued)} 个</b> · ${escapeHtml(concurrency.backend || "未知")}</footer></article>
        <article class="stat-card"><header><span>异常率</span><i>ERR</i></header><strong>${percentageText((Number(requests.error_rate) || 0) * 100)}</strong><footer>${numberText(requests.errors)} 异常 / ${numberText(requests.completed)} 已结束</footer></article>
        <article class="stat-card"><header><span>连接耗时 P95</span><i>CON</i></header><strong>${escapeHtml(durationText(latency.connect_p95_ms))}</strong><footer>P50 ${escapeHtml(durationText(latency.connect_p50_ms))}</footer></article>
        <article class="stat-card"><header><span>首字时间 P95</span><i>TTF</i></header><strong>${escapeHtml(durationText(latency.ttft_p95_ms))}</strong><footer>总耗时 P95 ${escapeHtml(durationText(latency.total_p95_ms))}</footer></article>
      </section>

      <section class="operations-layout">
        <article class="panel operations-wide">
          <header class="panel-heading"><div><h3>请求与异常趋势</h3><p>红色部分为异常；空时间桶保持细线</p></div><span>${numberText(requests.requests)} 次</span></header>
          ${operationsBars(requests.buckets)}
          <div class="latency-grid">
            <span><small>排队 P50 / P95</small><strong>${escapeHtml(durationText(latency.queue_p50_ms))} / ${escapeHtml(durationText(latency.queue_p95_ms))}</strong></span>
            <span><small>连接 P50 / P95</small><strong>${escapeHtml(durationText(latency.connect_p50_ms))} / ${escapeHtml(durationText(latency.connect_p95_ms))}</strong></span>
            <span><small>首字 P50 / P95</small><strong>${escapeHtml(durationText(latency.ttft_p50_ms))} / ${escapeHtml(durationText(latency.ttft_p95_ms))}</strong></span>
            <span><small>总耗时 P50 / P95</small><strong>${escapeHtml(durationText(latency.total_p50_ms))} / ${escapeHtml(durationText(latency.total_p95_ms))}</strong></span>
          </div>
        </article>

        <article class="panel">
          <header class="panel-heading"><div><h3>并发与排队</h3><p>用户 → 资源组 → 账号组 → Provider → 全局五层服务端约束</p></div><span>实时</span></header>
          <div class="panel-body concurrency-list">${providerRows || '<p class="muted-copy">暂无 Provider 并发</p>'}${accountPoolRows ? `<div class="concurrency-divider">ChatGPT 账号组</div>${accountPoolRows}` : ""}${groupRows ? `<div class="concurrency-divider">资源组</div>${groupRows}` : ""}</div>
        </article>
      </section>

      <section class="resource-grid">
        <article class="resource-card"><header><span>CPU</span><strong>${percentageText(current.cpu_percent)}</strong></header>${resourceBars(series, "cpu_percent", 100)}<footer>Load ${current.load_1 == null ? "—" : Number(current.load_1).toFixed(2)} / ${current.load_5 == null ? "—" : Number(current.load_5).toFixed(2)}</footer></article>
        <article class="resource-card"><header><span>内存</span><strong>${percentageText(current.memory_percent)}</strong></header>${resourceBars(series, "memory_percent", 100)}<footer>${escapeHtml(bytes(current.memory_used_bytes))} / ${current.memory_limit_bytes ? escapeHtml(bytes(current.memory_limit_bytes)) : "无限制"}</footer></article>
        <article class="resource-card"><header><span>磁盘</span><strong>${percentageText(current.disk_percent)}</strong></header>${resourceBars(series, "disk_percent", 100)}<footer>${escapeHtml(bytes(current.disk_used_bytes))} / ${escapeHtml(bytes(current.disk_total_bytes))}</footer></article>
        <article class="resource-card"><header><span>带宽</span><strong>↓ ${escapeHtml(bitRateText(current.network_rx_bps))}</strong></header>${resourceBars(series, "network_rx_bps")}<footer>↑ ${escapeHtml(bitRateText(current.network_tx_bps))} · 累计 ↓ ${escapeHtml(bytes(current.network_rx_bytes))}</footer></article>
      </section>
      <div class="info-banner"><i>域</i><div><strong>${resources.scope === "host" ? "整机资源范围" : "Turtle 容器/本机可见范围"}</strong><br/>${escapeHtml(resources.scope_note || "监控范围由部署方式决定。")}</div></div>

      <article class="panel">
        <header class="panel-heading"><div><h3>最近异常请求</h3><p>只保留分类、阶段、状态码和耗时</p></div><span>${(requests.recent_errors || []).length} 条</span></header>
        <div class="table-wrap"><table class="data-table"><thead><tr><th>Provider</th><th>异常</th><th>HTTP</th><th>耗时</th><th>时间</th></tr></thead><tbody>${errors || '<tr><td colspan="5">当前范围内没有异常请求</td></tr>'}</tbody></table></div>
      </article>`;
  };

  const renderProjectApi = async (renderId = state.renderId) => {
    const [userPayload, usage, keyPayload, pricingConfig] = await Promise.all([
      loadProjectApiUsers(),
      loadProjectApiUsage(),
      loadProjectApiKeys(),
      loadProjectApiConfig(),
    ]);
    if (renderId !== state.renderId || state.route !== "projectApi") return;
    const users = userPayload.items || [];
    state.projectApiUsers = users;
    const keys = keyPayload.items || [];
    const directory = userPayload.directory || users;
    const userById = new Map(directory.map((item) => [item.id, item]));
    const relevantOwnerIds = new Set([...users.map((item) => item.id), ...keys.map((item) => item.owner_user_id)]);
    const filterUsers = directory.filter((item) => relevantOwnerIds.has(item.id));
    const totals = usage.totals || {};
    const pagination = usage.pagination || {};
    const lifetimeActualCost = users.reduce((sum, item) => sum + Number(item.actual_cost_microusd || 0), 0);
    const lifetimeOfficialCost = users.reduce((sum, item) => sum + Number(item.official_cost_microusd || 0), 0);
    const multiplier = Number(pricingConfig.cost_multiplier ?? 1);
    const enabledUsers = users.filter((item) => item.enabled).length;
    const permissionRows = users.map((target) => `<tr>
      <td><div class="user-cell"><span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>${escapeHtml(target.email || "")}</small></div></div></td>
      <td><label class="permission-switch"><input type="checkbox" ${target.enabled ? "checked" : ""} data-project-api-permission-switch data-user-id="${escapeHtml(target.id)}" aria-label="${target.enabled ? "关闭" : "开启"} ${escapeHtml(target.name || target.email || "该账号")} 的 API 密钥权限"/><span aria-hidden="true"></span><em>${target.enabled ? "已启用" : "已停用"}</em></label></td>
      <td><strong>${numberText(target.active_keys)} / ${numberText(target.max_keys || 5)}</strong><br/><small>有效密钥 / 上限</small></td>
      <td><strong>${numberText(target.request_count)}</strong><br/><small>${numberText(target.total_tokens)} token</small></td>
      <td><strong>${projectUsd(target.actual_cost_microusd)}</strong><br/><small>官方参考 ${projectUsd(target.official_cost_microusd)}</small></td>
      <td><strong>${target.balance_microusd == null ? "未启用预付额度" : projectUsd(target.balance_microusd)}</strong><br/><small>${target.balance_microusd == null ? "当前按历史兼容模式调用" : `占用 ${projectUsd(target.reserved_microusd)}`}</small></td>
      <td><div class="row-actions"><button type="button" class="text-button" data-action="grant-project-api-credit" data-user-id="${escapeHtml(target.id)}">增加额度</button><button type="button" class="text-button" data-action="edit-project-api-user" data-user-id="${escapeHtml(target.id)}">编辑</button><button type="button" class="text-button danger-text" data-action="delete-project-api-user" data-user-id="${escapeHtml(target.id)}">删除</button></div></td>
    </tr>`).join("");
    const recordRows = (usage.recent || []).map((item) => {
      const owner = userById.get((usage.projects || []).find((project) => project.key_id === item.key_id)?.owner_user_id);
      return `<tr>
        <td><strong>${escapeHtml(item.project_name || "未知项目")}</strong><br/><small>${escapeHtml(owner?.name || owner?.email || "未知账号")} · ${escapeHtml(item.request_id)}</small></td>
        <td><strong>${escapeHtml(item.model || "—")}</strong><br/><small>${escapeHtml(item.route || "默认")}</small></td>
        <td>${item.outcome === "success" ? statusBadge("ready", "成功") : statusBadge("unavailable", item.outcome === "cancelled" ? "已取消" : "失败")}<br/><small>HTTP ${numberText(item.status_code)}</small></td>
        <td><strong>入 ${item.prompt_tokens == null ? "—" : numberText(item.prompt_tokens)} · 出 ${item.completion_tokens == null ? "—" : numberText(item.completion_tokens)}</strong><br/><small>缓存 ${numberText(item.cached_tokens)} · ${projectUsageSource(item.usage_source)}</small></td>
        <td><strong>${projectUsd(item.actual_cost_microusd)}</strong><br/><small>官方 ${projectUsd(item.official_cost_microusd)} × ${Number(item.cost_multiplier ?? 1).toFixed(2)}</small></td>
        <td><strong>${escapeHtml(durationText(item.latency_ms))}</strong><br/><small>${escapeHtml(dateTime(item.created_at))}</small></td>
      </tr>`;
    }).join("");
    content.innerHTML = `
      ${pageIntro("按账号归属每一个项目调用", "管理员负责授权账号和设置密钥上限；用户自行创建 API 密钥。这里只保存路由、状态、usage 与耗时，不保存提示词和回答。")}
      <section class="stat-grid operations-stat-grid">
        <article class="stat-card"><header><span>已开通账号</span><i>USR</i></header><strong>${numberText(enabledUsers)}</strong><footer>当前授权账号</footer></article>
        <article class="stat-card"><header><span>累计实际消耗</span><i>USD</i></header><strong>${projectUsd(lifetimeActualCost)}</strong><footer>官方参考 ${projectUsd(lifetimeOfficialCost)}</footer></article>
        <article class="stat-card"><header><span>筛选范围请求</span><i>REQ</i></header><strong>${numberText(totals.requests)}</strong><footer>${numberText(totals.errors)} 条异常</footer></article>
        <article class="stat-card"><header><span>记录 Token</span><i>TOK</i></header><strong>${numberText(totals.total_tokens)}</strong><footer>${numberText(totals.locally_estimated_requests)} 条本地估算 · ${numberText(totals.fallback_requests)} 条兜底</footer></article>
      </section>
      <article class="panel project-cost-config">
        <header class="panel-heading"><div><h3>消耗金额配置</h3><p>默认按官方参考价估算；倍率只影响保存后的新记录，历史记录保持原值</p></div><span>${multiplier.toFixed(2)}×</span></header>
        <form class="project-cost-config-form" data-project-cost-config>
          <label><span>实际消耗倍率</span><div class="project-multiplier-input"><input type="number" min="0" max="100" step="0.01" value="${escapeHtml(multiplier.toFixed(2))}" data-project-cost-multiplier/><b>×</b></div><small>例如官方估算 $1.00，倍率 1.20 时记录为 $1.20。</small></label>
          <div class="project-cost-preview"><small>当前计算示例</small><strong>$1.00 × ${multiplier.toFixed(2)} = ${projectUsd(multiplier * 1_000_000)}</strong></div>
          <button type="submit" class="button">保存倍率</button>
        </form>
      </article>
      <article class="panel">
        <header class="panel-heading"><div><h3>账号权限</h3><p>显示所有已添加账号；关闭开关会暂停密钥，但账号仍保留在列表中</p></div><div class="panel-heading-actions"><span>${numberText(enabledUsers)} / ${numberText(users.length)} 已启用</span><button type="button" class="button" data-action="open-project-api-user-picker"><span aria-hidden="true">＋</span> 添加用户</button></div></header>
        <div class="table-wrap"><table class="data-table"><thead><tr><th>账号</th><th>API 权限</th><th>密钥</th><th>历史调用</th><th>实际消耗</th><th>额度余额</th><th>操作</th></tr></thead><tbody>${permissionRows || '<tr><td colspan="7"><div class="project-empty"><strong>还没有授权账号</strong><small>点击“添加用户”搜索账号并开通 API 密钥权限</small></div></td></tr>'}</tbody></table></div>
      </article>
      <article class="panel">
        <header class="panel-heading"><div><h3>用量与逐条记录</h3><p>项目、模型、结果和时间范围可以组合筛选</p></div><span>${numberText(pagination.total)} 条</span></header>
        <div class="project-filter-shell">
          <div class="project-filter-heading"><div><strong>筛选调用记录</strong><small>条件会实时组合应用</small></div><div class="range-switch" role="group" aria-label="时间范围">${[[24,"24 小时"],[168,"7 天"],[720,"30 天"]].map(([value,label]) => `<button type="button" data-action="project-api-hours" data-hours="${value}" aria-pressed="${String(state.projectApiHours === value)}">${label}</button>`).join("")}</div></div>
          <div class="project-filter-grid">
            <label><span>账号</span><select data-project-api-filter="owner"><option value="">全部账号</option>${filterUsers.map((target) => `<option value="${escapeHtml(target.id)}" ${state.projectApiOwner === target.id ? "selected" : ""}>${escapeHtml(target.name || target.email || target.id)}</option>`).join("")}</select></label>
            <label><span>项目</span><select data-project-api-filter="key"><option value="">全部项目</option>${keys.map((item) => `<option value="${escapeHtml(item.id)}" ${state.projectApiKey === item.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>
            <label><span>模型</span><select data-project-api-filter="model"><option value="">全部模型</option><option value="gpt-5-web" ${state.projectApiModel === "gpt-5-web" ? "selected" : ""}>gpt-5-web</option></select></label>
            <label><span>调用结果</span><select data-project-api-filter="outcome"><option value="">全部状态</option><option value="success" ${state.projectApiOutcome === "success" ? "selected" : ""}>成功</option><option value="error" ${state.projectApiOutcome === "error" ? "selected" : ""}>失败</option><option value="cancelled" ${state.projectApiOutcome === "cancelled" ? "selected" : ""}>已取消</option></select></label>
            <button type="button" class="project-filter-reset" data-action="reset-project-api-filters">重置筛选</button>
          </div>
        </div>
        <div class="table-wrap"><table class="data-table"><thead><tr><th>项目 / 账号</th><th>模型 / 路由</th><th>状态</th><th>Token 明细</th><th>实际消耗</th><th>耗时 / 时间</th></tr></thead><tbody>${recordRows || '<tr><td colspan="6">当前筛选范围没有调用记录</td></tr>'}</tbody></table></div>
        <div class="form-actions"><span>第 ${Math.floor(Number(pagination.offset || 0) / Number(pagination.limit || 100)) + 1} 页 · 共 ${numberText(pagination.total)} 条</span><div><button type="button" class="button-secondary" data-action="project-api-page" data-direction="-1" ${state.projectApiOffset <= 0 ? "disabled" : ""}>上一页</button> <button type="button" class="button-secondary" data-action="project-api-page" data-direction="1" ${pagination.has_more ? "" : "disabled"}>下一页</button></div></div>
      </article>
      <div class="info-banner"><i>$</i><div><strong>按 OpenAI 标准 API 单价做版本化估算</strong><br/>输入、缓存输入、缓存写入和输出分别计价，再应用记录当时的倍率。它不是实际 API 账单，也不是 ChatGPT 订阅余额。</div></div>`;
  };

  const saveProjectApiConfig = async (form) => {
    const input = form.querySelector("[data-project-cost-multiplier]");
    const multiplier = Number(input?.value);
    if (!Number.isFinite(multiplier) || multiplier < 0 || multiplier > 100) {
      showToast("倍率必须在 0 到 100 之间", "error");
      input?.focus();
      return;
    }
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    try {
      await requestJson(`${PROJECT_API}/admin/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cost_multiplier: multiplier }),
      });
      showToast(`实际消耗倍率已更新为 ${multiplier.toFixed(2)}×`);
      await renderProjectApi();
    } catch (error) {
      showToast(error.message || "保存倍率失败", "error");
    } finally {
      if (button) button.disabled = false;
    }
  };

  const updateProjectApiPermission = async (userId, payload) =>
    requestJson(`${PROJECT_API}/admin/users/${encodeURIComponent(userId)}/permission`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

  const toggleProjectApiPermission = async (input) => {
    const target = state.projectApiUsers.find((item) => item.id === input.dataset.userId);
    const enabled = input.checked;
    if (!enabled) {
      const confirmed = await confirmAction({
        title: "关闭 API 密钥权限",
        message: `关闭后，“${target?.name || target?.email || "该账号"}”的所有现有密钥会立即停止调用；重新开启后可恢复使用。`,
        confirmLabel: "确认关闭",
        danger: true,
      });
      if (!confirmed) {
        input.checked = true;
        return;
      }
    }
    input.disabled = true;
    try {
      await updateProjectApiPermission(input.dataset.userId, { enabled });
      clearCache("project-api-users");
      await renderProjectApi();
      toast(enabled ? "API 密钥权限已开启" : "API 密钥权限已关闭");
    } catch (error) {
      input.checked = !enabled;
      toast(error?.message || "项目 API 权限更新失败", "error");
    } finally {
      input.disabled = false;
    }
  };

  const renderProjectApiUserSearch = async (query = "") => {
    const region = modalBody.querySelector("#project-api-user-results");
    if (!region) return;
    const normalized = query.trim();
    if (!normalized) {
      region.innerHTML = '<div class="project-search-hint">输入姓名或邮箱，停顿 1 秒后显示匹配账号</div>';
      return;
    }
    const generation = ++state.projectApiUserSearchGeneration;
    region.innerHTML = inlineLoading("正在搜索用户");
    try {
      const payload = await requestJson(`${PROJECT_API}/admin/users/search?q=${encodeURIComponent(normalized)}`);
      if (!modalBody.querySelector("#project-api-user-results") || generation !== state.projectApiUserSearchGeneration) return;
      const items = payload.items || [];
      region.innerHTML = items.length ? items.map((target) => `
        <button type="button" class="user-search-result" data-action="add-project-api-user" data-user-id="${escapeHtml(target.id)}">
          <span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span>
          <span><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>${escapeHtml(target.email || "")}</small></span>
          <em>添加</em>
        </button>`).join("") : '<div class="project-empty"><strong>没有可添加的账号</strong><small>换一个姓名或邮箱关键词试试</small></div>';
    } catch (error) {
      region.innerHTML = `<div class="project-empty error"><strong>搜索失败</strong><small>${escapeHtml(error?.message || "请稍后重试")}</small></div>`;
    }
  };

  const openProjectApiUserPicker = () => {
    window.clearTimeout(state.projectApiUserSearchTimer);
    state.projectApiUserSearchGeneration += 1;
    openModal({
      eyebrow: "账号授权",
      title: "添加 API 密钥用户",
      body: `
        <div class="project-user-combobox"><label class="field"><span>搜索用户</span><input type="search" data-project-api-user-search placeholder="输入姓名或邮箱" autocomplete="off" aria-controls="project-api-user-results"/></label>
        <div id="project-api-user-results" class="user-search-results"><div class="project-search-hint">输入姓名或邮箱，停顿 1 秒后显示匹配账号</div></div></div>
        <label class="field compact-limit-field"><span>初始密钥上限</span><input type="number" min="1" max="100" value="5" data-project-api-new-max/><small>只统计有效密钥；撤销后的密钥不占用数量。</small></label>
        `,
      footer: '<span class="modal-note">选择用户后立即开通权限</span><button type="button" class="button-secondary" data-close-modal>完成</button>',
    });
  };

  const addProjectApiUser = async (button) => {
    const maxKeys = Number(modalBody.querySelector("[data-project-api-new-max]")?.value || 5);
    if (!Number.isInteger(maxKeys) || maxKeys < 1 || maxKeys > 100) {
      return toast("密钥上限必须为 1 到 100 的整数", "error");
    }
    button.disabled = true;
    try {
      await updateProjectApiPermission(button.dataset.userId, { enabled: true, max_keys: maxKeys });
      clearCache("project-api-users");
      closeModal();
      await renderProjectApi();
      toast("用户已获得 API 密钥权限");
    } catch (error) {
      toast(error?.message || "添加用户失败", "error");
      button.disabled = false;
    }
  };

  const openProjectApiUserEditor = (button) => {
    const target = state.projectApiUsers.find((item) => item.id === button.dataset.userId);
    if (!target) return toast("账号数据已变化，请刷新后重试", "error");
    openModal({
      eyebrow: "权限设置",
      title: `编辑 ${target.name || target.email || "账号"}`,
      body: `
        <div class="modal-user-summary"><span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>${escapeHtml(target.email || "")}</small></div></div>
        <label class="field"><span>最多有效 API 密钥数量</span><input type="number" min="1" max="100" value="${Number(target.max_keys) || 5}" data-project-api-edit-max/><small>当前已有 ${numberText(target.active_keys)} 个有效密钥；降低上限不会自动撤销已有密钥，但会阻止继续创建。</small></label>`,
      footer: `<button type="button" class="button-secondary" data-close-modal>取消</button><button type="button" class="button" data-action="save-project-api-user" data-user-id="${escapeHtml(target.id)}">保存设置</button>`,
    });
  };

  const openProjectApiCreditGrant = (button) => {
    const target = state.projectApiUsers.find((item) => item.id === button.dataset.userId);
    if (!target) return toast("账号数据已变化，请刷新后重试", "error");
    const idempotencyKey = globalThis.crypto?.randomUUID?.()
      || `credit-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    openModal({
      eyebrow: "API 额度账本",
      title: `给 ${target.name || target.email || "账号"} 增加额度`,
      body: `
        <div class="modal-user-summary"><span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>当前余额：${target.balance_microusd == null ? "尚未启用预付额度" : projectUsd(target.balance_microusd)}</small></div></div>
        <label class="field"><span>增加金额（USD）</span><input type="number" min="0.000001" max="1000000" step="0.000001" value="10" data-project-api-credit-amount required/><small>保存后立即写入不可变额度流水；首次增加会启用预付额度。</small></label>
        <label class="field"><span>原因</span><input type="text" maxlength="200" value="管理员发放项目 API 额度" data-project-api-credit-reason required/><small>用于审计，不会显示在 API 响应正文中。</small></label>`,
      footer: `<button type="button" class="button-secondary" data-close-modal>取消</button><button type="button" class="button" data-action="submit-project-api-credit" data-user-id="${escapeHtml(target.id)}" data-idempotency-key="${escapeHtml(idempotencyKey)}">确认增加</button>`,
    });
  };

  const submitProjectApiCredit = async (button) => {
    const usd = Number(modalBody.querySelector("[data-project-api-credit-amount]")?.value);
    const reason = String(modalBody.querySelector("[data-project-api-credit-reason]")?.value || "").trim();
    const amountMicrousd = Math.round(usd * 1_000_000);
    if (!Number.isSafeInteger(amountMicrousd) || amountMicrousd <= 0) {
      return toast("增加金额必须大于 0，且最多保留 6 位小数", "error");
    }
    if (reason.length < 2) return toast("请填写至少 2 个字符的发放原因", "error");
    button.disabled = true;
    try {
      await requestJson(`${PROJECT_API}/admin/users/${encodeURIComponent(button.dataset.userId)}/credits`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount_microusd: amountMicrousd,
          reason,
          idempotency_key: button.dataset.idempotencyKey,
        }),
      });
      clearCache("project-api-users");
      closeModal();
      await renderProjectApi();
      toast(`已增加 ${projectUsd(amountMicrousd)} API 额度`);
    } catch (error) {
      toast(error?.message || "增加额度失败", "error");
      button.disabled = false;
    }
  };

  const saveProjectApiUser = async (button) => {
    const maxKeys = Number(modalBody.querySelector("[data-project-api-edit-max]")?.value);
    if (!Number.isInteger(maxKeys) || maxKeys < 1 || maxKeys > 100) {
      return toast("密钥上限必须为 1 到 100 的整数", "error");
    }
    button.disabled = true;
    try {
      await updateProjectApiPermission(button.dataset.userId, { enabled: true, max_keys: maxKeys });
      clearCache("project-api-users");
      closeModal();
      await renderProjectApi();
      toast("账号密钥上限已更新");
    } catch (error) {
      toast(error?.message || "保存失败", "error");
      button.disabled = false;
    }
  };

  const deleteProjectApiUser = async (button) => {
    const target = state.projectApiUsers.find((item) => item.id === button.dataset.userId);
    const confirmed = await confirmAction({
      title: "删除账号 API 权限",
      message: `删除后，“${target?.name || target?.email || "该账号"}”的所有有效密钥都会被永久撤销，历史调用记录仍会保留。此操作不可恢复。`,
      confirmLabel: "删除并撤销密钥",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`${PROJECT_API}/admin/users/${encodeURIComponent(button.dataset.userId)}/permission`, {
        method: "DELETE",
      });
      clearCache("project-api-users");
      await renderProjectApi();
      toast("账号权限与有效密钥已删除");
    } catch (error) {
      toast(error?.message || "删除失败", "error");
    }
  };

  const changeProjectApiPage = async (button) => {
    state.projectApiOffset = Math.max(0, state.projectApiOffset + Number(button.dataset.direction || 0) * 100);
    await renderProjectApi();
  };

  const resetProjectApiFilters = () => {
    state.projectApiHours = 24;
    state.projectApiOwner = "";
    state.projectApiKey = "";
    state.projectApiModel = "";
    state.projectApiOutcome = "";
    state.projectApiOffset = 0;
    void renderProjectApi();
  };

  const renderProviders = async (_force = false, renderId = state.renderId, preserve = false) => {
    const accountPoolPromise = loadAccountPools()
      .then((value) => ({ value, error: null }))
      .catch((error) => ({ value: null, error }));
    const loadingCards = PROVIDER_DEFINITIONS.map((definition) => `<article class="provider-card provider-card-loading">
      <div class="provider-card-head">
        <span class="provider-logo">${escapeHtml(definition.label.slice(0, 2))}</span>
        <div><h3>${escapeHtml(definition.label)}</h3><p>${escapeHtml(definition.copy)}</p></div>
        ${statusBadge("checking", "检测中")}
      </div>
      ${inlineLoading(`${definition.label} 健康状态读取中`)}
    </article>`).join("");
    if (!preserve) {
      content.innerHTML = `
        ${pageIntro("Provider 与模型发布", "页面框架会先显示；ChatGPT 与 Claude 的健康探测在后台独立完成。", '<button type="button" class="button-secondary" disabled>正在读取状态…</button>')}
        <div class="info-banner"><i>异</i><div><strong>健康检查不会再阻塞其他管理模块</strong><br/>检测期间保留明确状态，完成后自动替换为检测时间和耗时。</div></div>
        <section class="provider-grid" aria-busy="true">${loadingCards}</section>`;
    }

    const [providerData, modelsPayload] = await Promise.all([
      loadProviders(),
      loadModels().catch(() => ({ data: [] })),
    ]);
    if (renderId !== state.renderId || state.route !== "providers") return;
    state.providerData = providerData;
    const models = normalizeModels(modelsPayload);
    const healthByKey = new Map((providerData.items || []).map((item) => [item.key, item]));
    const displayByKey = new Map((providerData.display || []).map((item) => [item.provider_family, item]));

    const cards = PROVIDER_DEFINITIONS.map((definition) => {
      const health = healthByKey.get(definition.key) || (providerData.probing
        ? { state: "checking", message: "后台检测中" }
        : { state: "planned", message: "尚未配置连接" });
      const display = displayByKey.get(definition.key) || {
        provider_family: definition.key,
        display_name: definition.key === "gpt" ? "GPT" : "Claude",
      };
      const providerModels = models.filter((model) => model.family === definition.key);
      const routeCount = providerModels.reduce((count, model) => count + (model.turtle?.versions?.length || 0), 0);
      return `<article class="provider-card" data-provider-key="${escapeHtml(definition.key)}">
        <div class="provider-card-head">
          <span class="provider-logo">${escapeHtml(definition.label.slice(0, 2))}</span>
          <div><h3>${escapeHtml(definition.label)}</h3><p>${escapeHtml(definition.copy)}</p></div>
          ${statusBadge(health.state, providerStateText(health))}
        </div>
        <div class="provider-details">
          <span><small>公开模型</small><strong>${escapeHtml(health.public_model || providerModels[0]?.id || "未发布")}</strong></span>
          <span><small>页面模型</small><strong>${providerModels.length} 个</strong></span>
          <span><small>已验证路由</small><strong>${health.verified_route_count ?? routeCount ?? 0}</strong></span>
          <span><small>检测耗时</small><strong>${health.latency_ms == null ? "—" : `${numberText(health.latency_ms)} ms`}</strong></span>
          <span><small>检测时间</small><strong>${health.checked_at ? dateTime(health.checked_at) : "尚未检测"}</strong></span>
        </div>
        <div class="model-list">${providerModels.length ? providerModels.map(() => `<span>${escapeHtml(display.display_name)}</span>`).join("") : "<span>暂无已发布模型</span>"}</div>
        <form class="provider-display-form" data-provider-display="${escapeHtml(definition.key)}">
          <label><span>聊天页展示名称</span><input name="display_name" maxlength="40" required value="${escapeHtml(display.display_name)}"/></label>
          <button type="button" class="button-secondary" data-action="save-provider-display">保存名称</button>
          <small>只修改可见名称；内部 ID ${escapeHtml(health.public_model || providerModels[0]?.id || (definition.key === "gpt" ? "gpt-5-web" : "claude-web"))} 保持不变。</small>
        </form>
      </article>`;
    }).join("");

    const accountPool = state.accountPools || { pools: [], accounts: [], backend: "loading" };
    const accountPoolMarkup = (accountPool.pools || []).map((pool) => {
      const poolProvider = pool.provider === "claude" ? "claude" : "gpt";
      const poolProviderLabel = familyText(poolProvider);
      const accounts = (accountPool.accounts || []).filter((account) => account.pool_id === pool.id);
      const rows = accounts.map((account) => {
        const runtime = account.login_runtime || {};
        const loginConfigured = Boolean(runtime.configured);
        const loginOpen = ["manual", "ready"].includes(runtime.browser_state);
        const loginPending = account.status === "reauth_required" && loginOpen;
        const remoteLogin = runtime.login_mode === "remote_browser";
        const remoteSession = loginSessionFor(account.id);
        const upstreamIdentity = account.upstream_display_name
          ? `${poolProviderLabel} · ${account.upstream_display_name}`
          : `${poolProviderLabel} 身份将在登录验证后确认`;
        const credentialText = ({
          stored: "登录状态已安全保存",
          empty: "等待首次登录",
          invalid: "登录状态异常",
        })[runtime.credential_state] || "登录状态待确认";
        const loginState = !loginConfigured
          ? runtime.control_state === "unavailable" ? "登录服务暂不可用" : "尚未准备登录环境"
          : loginPending ? "等待你完成登录" : loginOpen ? "登录窗口已打开" : credentialText;
        const quota = account.quota || {};
        const tightestLane = quota.tightest_lane;
        const quotaSummary = quota.tracked
          ? tightestLane
            ? `${quota.profile_label} · 最紧 ${tightestLane.label} ${numberText(tightestLane.safe_remaining_count)} / ${numberText(tightestLane.dispatch_budget_count)}`
            : `${quota.profile_label} · ${numberText(quota.available_lane_count)} / ${numberText(quota.enabled_lane_count)} 档可调度`
          : "额度模板未设置 · 当前仅做均衡统计";
        const loginActions = loginPending
          ? `${remoteSession
            ? loginSessionLink(account.id, "quiet-button")
            : remoteLogin
              ? `<button type="button" class="quiet-button" data-action="start-account-reauth" data-reauth-resume="true" data-account-id="${escapeHtml(account.id)}">重新生成登录页</button>`
              : ""}<button type="button" class="quiet-button" data-action="verify-account-reauth" data-account-id="${escapeHtml(account.id)}">我已登录，验证</button><button type="button" class="quiet-button" data-action="cancel-account-reauth" data-account-id="${escapeHtml(account.id)}">取消登录</button>`
          : loginConfigured
            ? `<button type="button" class="quiet-button" data-action="start-account-reauth" data-account-id="${escapeHtml(account.id)}">重新登录</button>`
            : `<button type="button" class="quiet-button" data-action="prepare-account-runtime" data-account-id="${escapeHtml(account.id)}">登录</button>`;
        return `<div class="account-row">
          <div class="account-identity"><strong>${escapeHtml(account.name)}</strong><small class="account-upstream-identity">${escapeHtml(upstreamIdentity)}</small><small class="account-login-state">${escapeHtml(loginState)}</small><small class="account-quota-state">${escapeHtml(quotaSummary)}</small></div>
          ${statusBadge(account.status === "ready" && account.available ? "ready" : account.status || "planned", accountStateText(account))}
          <span class="account-capacity">${numberText(account.active)} / ${numberText(account.max_concurrency)} 并发 · ${numberText(account.sticky_chat_count)} 个粘性会话</span>
          <span class="account-health">${account.last_health_at ? `${relativeTime(account.last_health_at)}检测` : "尚未检测"}</span>
          <div class="account-actions">
            ${account.status === "reauth_required" ? "" : `<button type="button" class="quiet-button" data-action="probe-account" data-account-id="${escapeHtml(account.id)}">检测</button>`}
            ${loginActions}
            <button type="button" class="quiet-button" data-action="open-account" data-account-id="${escapeHtml(account.id)}" data-pool-id="${escapeHtml(pool.id)}">管理</button>
          </div>
        </div>`;
      }).join("");
      return `<article class="account-pool-card" data-provider="${escapeHtml(poolProvider)}">
        <header><div><h3>${escapeHtml(pool.name)}</h3><p>${escapeHtml(pool.description || "管理员备注为空；不影响账号调度")}</p></div><div>${familyBadge(poolProvider)} ${statusBadge(pool.enabled ? "ready" : "disabled", pool.enabled ? "已启用" : "已停用")}</div></header>
        <div class="account-pool-stats"><span><small>账号</small><strong>${numberText(pool.account_count)}</strong></span><span><small>可调度</small><strong>${numberText(pool.ready_count)}</strong></span><span><small>活动请求</small><strong>${numberText(pool.active)}</strong></span><span><small>准入并发</small><strong>${numberText(pool.admission_capacity)}</strong></span><span><small>当前空闲</small><strong>${numberText(pool.available_slots)}</strong></span><span><small>配置总量</small><strong>${numberText(pool.capacity)}</strong></span><span><small>粘性会话</small><strong>${numberText(pool.sticky_chat_count)}</strong></span><span><small>累计迁移</small><strong>${numberText(pool.affinity_migration_count)}</strong></span></div>
        <div class="account-list">${rows || `<p class="muted-copy account-empty-copy">还没有 ${escapeHtml(poolProviderLabel)} 账号。点击“添加账号”，系统会自动准备独立运行环境并打开官方登录页。</p>`}</div>
        <footer><button type="button" class="button-secondary" data-action="probe-account-pool" data-pool-id="${escapeHtml(pool.id)}">检测</button><button type="button" class="button" data-action="open-account" data-account-id="new" data-pool-id="${escapeHtml(pool.id)}">添加 ${escapeHtml(poolProviderLabel)} 账号</button><button type="button" class="quiet-button" data-action="open-account-pool" data-pool-id="${escapeHtml(pool.id)}">设置账号池</button></footer>
      </article>`;
    }).join("");

    content.innerHTML = `
      ${pageIntro("Provider 与模型发布", "账号池负责登录身份与独立 Worker；额度组只负责把用户绑定到同 Provider 的池并配置档位规则。", `<button type="button" class="button-secondary" data-action="open-account-pool" data-pool-id="new" data-provider="gpt">新建 GPT 账号池</button><button type="button" class="button-secondary" data-action="open-account-pool" data-pool-id="new" data-provider="claude">新建 Claude 账号池</button><button type="button" class="button-secondary" data-action="refresh-providers" ${providerData.probing ? "disabled" : ""}>${providerData.probing ? "检测中…最长 15 秒" : "重新检测"}</button>`)}
      <div class="info-banner"><i>密</i><div><strong>部署 Secret 是唯一凭据来源</strong><br/>后台只读取 ChatGPT 与 Claude 的脱敏健康状态；检测最长等待 15 秒，按钮会持续显示执行状态。</div></div>
      <section class="provider-grid">${cards}</section>
      <div class="info-banner"><i>池</i><div><strong>GPT 与 Claude 各自拥有账号池</strong><br/>每个账号都有隔离登录目录、浏览器和 Worker；模型额度组只能关联同 Provider 的账号池。</div></div>
      <section class="account-pool-section" data-account-pool-section aria-busy="${String(accountPool.backend === "loading")}">
        <header class="panel-heading"><div><h3>Provider 账号池</h3><p>登录在池内完成；分组只引用池，不保存账号身份。</p></div><span>${escapeHtml(accountPool.backend || "未配置")}</span></header>
        ${accountPool.backend === "loading" ? inlineLoading("账号池与容量读取中") : accountPool.backend === "unavailable" ? '<div class="warning-banner"><i>!</i><div><strong>Provider 账号池暂时无法读取</strong><br/>账号池配置恢复后再进行登录与调度设置。</div></div>' : accountPoolMarkup || '<p class="muted-copy">尚未创建 Provider 账号池。</p>'}
      </section>
      <section class="settings-grid" style="margin-top:14px">
        <article class="settings-card"><header><h3>接入顺序</h3><p>避免“配置了模型名但没有可用上游”</p></header><div class="system-list">
          <div class="system-row"><span class="count-pill">01</span><div><strong>添加账号并登录</strong><small>系统自动准备每个账号的独立环境；管理员无需填写部署参数</small></div></div>
          <div class="system-row"><span class="count-pill">02</span><div><strong>完成协议与真实路由验证</strong><small>非流式、SSE、多轮、重启持久化和错误映射均需通过</small></div></div>
          <div class="system-row"><span class="count-pill">03</span><div><strong>发布模型并配置用户策略</strong><small>模型进入页面后，再在“额度策略”中分配权限和窗口</small></div></div>
        </div></article>
        <article class="settings-card"><header><h3>当前配置边界</h3><p>日常可见，敏感项不可见</p></header><div class="system-list">
          <div class="system-row"><span class="family-badge" data-family="gpt">可见</span><div><strong>模型、路由数量与健康状态</strong><small>用于日常监控和发布判断</small></div></div>
          <div class="system-row"><span class="family-badge" data-family="claude">脱敏</span><div><strong>登录 / 重登 / 待验证状态</strong><small>不会显示 Cookie、Token、认证文件或消息正文</small></div></div>
          <div class="system-row"><span class="count-pill">部署</span><div><strong>URL、Secret 与进程参数</strong><small>由部署环境管理，防止网页误改导致全站中断</small></div></div>
        </div></article>
      </section>`;
    if (providerData.probing) scheduleProviderPoll(renderId);
    if (!state.accountPools) {
      void accountPoolPromise.then(({ value, error }) => {
        if (renderId !== state.renderId || state.route !== "providers") return;
        state.accountPools = value || {
          enabled: false,
          backend: "unavailable",
          pools: [],
          accounts: [],
          message: error?.message || "账号池状态暂时无法读取",
        };
        return renderProviders(false, renderId, true);
      });
    }
  };

  const scheduleProviderPoll = (renderId) => {
    if (state.providerPollTimer != null) return;
    state.providerPollTimer = window.setTimeout(async () => {
      state.providerPollTimer = null;
      if (renderId !== state.renderId || state.route !== "providers") return;
      clearCache("providers");
      try {
        await renderProviders(false, renderId, true);
      } catch (_error) {
        // The manual button remains available after the background probe ends.
      }
    }, 700);
  };

  const accountPoolData = () => state.accountPools || state.providerData?.account_pools || { pools: [], accounts: [] };

  const openAccountPoolDrawer = (poolId = "new", requestedProvider = "gpt") => {
    const source = poolId === "new"
      ? {
          id: "new",
          provider: requestedProvider === "claude" ? "claude" : "gpt",
          name: "",
          description: "",
          enabled: true,
        }
      : accountPoolData().pools.find((pool) => pool.id === poolId);
    if (!source) return toast("Provider 账号池数据已变化，请刷新", "error");
    const provider = source.provider === "claude" ? "claude" : "gpt";
    const providerLabel = familyText(provider);
    const isDefaultPool = poolId === "gpt-default" || poolId === "claude-default";
    const hasAccounts = Number(source.account_count || 0) > 0;
    const deleteButton = poolId !== "new" && !isDefaultPool
      ? `<button type="button" class="button-danger" data-action="delete-account-pool" ${hasAccounts ? 'disabled title="池内仍有账号，不能删除"' : ""}>删除账号池</button>`
      : "";
    openDrawer({
      eyebrow: poolId === "new" ? "NEW ACCOUNT POOL" : "ACCOUNT POOL",
      title: poolId === "new" ? `新建 ${providerLabel} 账号池` : source.name,
      body: `<form id="account-pool-form" data-pool-id="${escapeHtml(poolId)}" data-provider="${escapeHtml(provider)}">
        <div class="drawer-section"><div class="field-grid">
          <label class="field"><span>Provider</span><input value="${escapeHtml(providerLabel)}" disabled/><small>账号池创建后不能更换 Provider。</small></label>
          <label class="field"><span>账号池名称</span><input name="name" maxlength="60" required value="${escapeHtml(source.name)}" placeholder="例如：${escapeHtml(providerLabel)} 主池"/></label>
          <label class="field"><span>管理员备注（可选）</span><input name="description" maxlength="200" value="${escapeHtml(source.description || "")}" placeholder="例如：日常账号、备用账号、测试账号"/><small>只帮助你识别用途，不参与调度或额度计算。</small></label>
          <label class="choice-card"><input type="checkbox" name="enabled" ${source.enabled ? "checked" : ""}/><span><strong>启用这个 ${escapeHtml(providerLabel)} 账号池</strong><small>关闭后停止接收新请求；正在生成的内容会自然完成，账号和登录状态不会删除。</small></span></label>
        </div></div>
        <div class="info-banner"><i>史</i><div><strong>${escapeHtml(providerLabel)} 账号池不会影响聊天历史</strong><br/>用户、对话和消息始终保存在 Turtle PostgreSQL；切换或停用上游账号不会让历史记录消失。</div></div>
      </form>`,
      footer: `${deleteButton}<button type="button" class="button-secondary" data-close-drawer>取消</button><button type="button" class="button" data-action="save-account-pool">保存账号池</button>`,
    });
  };

  const saveAccountPool = async (button) => {
    const form = document.querySelector("#account-pool-form");
    if (!form) return;
    const values = new FormData(form);
    const payload = {
      provider: form.dataset.provider === "claude" ? "claude" : "gpt",
      name: String(values.get("name") || "").trim(),
      description: String(values.get("description") || "").trim(),
      enabled: values.get("enabled") === "on",
    };
    if (!payload.name) return toast("请填写账号池名称", "error");
    button.disabled = true;
    button.textContent = "正在保存…";
    try {
      const poolId = form.dataset.poolId;
      await requestJson(
        poolId === "new" ? `${ADMIN_API}/account-pools` : `${ADMIN_API}/account-pools/${encodeURIComponent(poolId)}`,
        { method: poolId === "new" ? "POST" : "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
      );
      closeDrawer();
      clearCache("providers", "account-pools", "overview");
      await renderProviders(false, state.renderId, true);
      toast(`${familyText(payload.provider)} 账号池已保存`);
    } catch (error) {
      toast(error?.message || "Provider 账号池保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存账号池";
    }
  };

  const deleteAccountPool = async (button) => {
    const form = document.querySelector("#account-pool-form");
    if (!form || form.dataset.poolId === "new") return;
    const poolId = form.dataset.poolId;
    const source = accountPoolData().pools.find((pool) => pool.id === poolId);
    if (!source) return toast("Provider 账号池数据已变化，请刷新", "error");
    if (Number(source.account_count || 0) > 0) {
      return toast("账号池仍有账号，不能删除", "error");
    }
    if (!await confirmAction({
      title: "删除账号池",
      message: `确定删除“${source.name}”吗？只有空池且未被任何分组引用时才会删除。`,
      confirmLabel: "确认删除",
      danger: true,
    })) return;
    button.disabled = true;
    button.textContent = "正在删除…";
    try {
      await requestJson(`${ADMIN_API}/account-pools/${encodeURIComponent(poolId)}`, {
        method: "DELETE",
      });
      closeDrawer();
      clearCache("providers", "account-pools", "overview", "access");
      await renderProviders(false, state.renderId, true);
      toast("账号池已删除");
    } catch (error) {
      toast(error?.message || "账号池删除失败", "error");
      button.disabled = false;
      button.textContent = "删除账号池";
    }
  };

  const openAccountDrawer = (accountId, poolId) => {
    const selectedPool = accountPoolData().pools.find((pool) => pool.id === poolId);
    const source = accountId === "new"
      ? {
          id: "new",
          pool_id: poolId,
          provider: selectedPool?.provider || "gpt",
          name: "",
        }
      : accountPoolData().accounts.find((account) => account.id === accountId);
    if (!source) return toast("账号数据已变化，请刷新", "error");
    const provider = source.provider === "claude" || selectedPool?.provider === "claude"
      ? "claude"
      : "gpt";
    const providerLabel = familyText(provider);
    if (accountId === "new") {
      openDrawer({
        eyebrow: `ADD ${provider.toUpperCase()} ACCOUNT`,
        title: `添加 ${providerLabel} 账号`,
        body: `<form id="provider-account-form" data-account-id="new" data-pool-id="${escapeHtml(source.pool_id || poolId)}">
          <div class="drawer-section"><div class="field-grid">
            <label class="field"><span>账号备注名</span><input name="name" maxlength="60" required autofocus placeholder="例如：备用账号 1"/><small>只用于后台识别，不需要填写真实邮箱。</small></label>
          </div></div>
          <div class="onboarding-steps" aria-label="账号添加步骤">
            <div><b>1</b><span><strong>自动准备安全登录环境</strong><small>系统自动完成全部技术配置，你不需要填写地址、端口或健康检查。</small></span></div>
            <div><b>2</b><span><strong>打开 ${escapeHtml(providerLabel)} 官方登录页</strong><small>本地会打开专用 Chrome；服务器会生成一次性安全页面。后台不接收密码或验证码。</small></span></div>
            <div><b>3</b><span><strong>验证身份并保存</strong><small>${provider === "claude" ? "系统会逐一验证已发布的 Claude 档位，因此这一步可能需要几分钟。" : "系统会确认账号身份，并验证 Worker 重启后仍可使用。"}</small></span></div>
          </div>
          <div class="info-banner"><i>隐</i><div><strong>认证内容不会写入数据库</strong><br/>Cookie 和令牌只留在该账号的服务器受限目录；数据库仅保存管理员备注和脱敏运行状态。</div></div>
        </form>`,
        footer: '<button type="button" class="button-secondary" data-close-drawer>取消</button><button type="button" class="button" data-action="save-account">添加并开始登录</button>',
      });
      return;
    }
    const runtime = source.login_runtime || {};
    const loginConfigured = Boolean(runtime.configured);
    const loginOpen = ["manual", "ready"].includes(runtime.browser_state);
    const loginPending = source.status === "reauth_required" && loginOpen;
    const remoteLogin = runtime.login_mode === "remote_browser";
    const remoteSession = loginSessionFor(accountId);
    const identityText = source.upstream_display_name || "登录验证后显示";
    const quotaProfiles = accountPoolData().quota_profiles_by_provider?.[provider]
      || accountPoolData().quota_profiles
      || [];
    const quota = source.quota || { lanes: [], profile_id: source.quota_profile || "untracked" };
    const quotaProfileOptions = quotaProfiles.map((profile) =>
      `<option value="${escapeHtml(profile.id)}" ${profile.id === (source.quota_profile || quota.profile_id || "untracked") ? "selected" : ""}>${escapeHtml(profile.label)}</option>`
    ).join("");
    const quotaLaneMarkup = (quota.lanes || []).map((lane) => {
      const publishedWindow = Number(lane.published_window_seconds || lane.window_seconds || 0);
      const published = lane.published_min != null
        ? lane.published_min === lane.published_max
          ? `公开 ${numberText(lane.published_min)} / ${windowText(publishedWindow)}`
          : `公开 ${numberText(lane.published_min)}–${numberText(lane.published_max)} / ${windowText(publishedWindow)}`
        : lane.source === "official_multiplier"
          ? "官方套餐倍率 · 单模型固定次数未公开"
          : lane.source === "official_dynamic"
            ? `官方动态${publishedWindow ? ` · ${windowText(publishedWindow)}窗口` : ""}`
        : lane.dispatch_budget_count == null
          ? "官方实时余量不可读"
          : `调度预算 ${numberText(lane.dispatch_budget_count)} / ${windowText(lane.window_seconds)}`;
      const usage = lane.dispatch_budget_count == null
        ? `近窗成功 ${numberText(lane.used_count)} · 活动 ${numberText(lane.active_count)}`
        : `已用 ${numberText(lane.used_count)} · 预留 ${numberText(lane.active_count)} · 安全可用 ${numberText(lane.safe_remaining_count)}`;
      const recovery = lane.blocked_until
        ? `冷却至 ${dateTime(lane.blocked_until)}`
        : lane.reset_at
          ? `最早释放 ${dateTime(lane.reset_at)}`
          : ["official_dynamic", "official_multiplier"].includes(lane.source)
            ? "恢复时间以上游提示为准"
            : "尚未开始计时";
      return `<div class="account-quota-lane" data-state="${escapeHtml(lane.state || "unknown")}">
        <div><strong>${escapeHtml(lane.label)}</strong><small>${escapeHtml(quotaSourceText(lane.source))}</small></div>
        <span><small>${escapeHtml(published)}</small><strong>${escapeHtml(usage)}</strong></span>
        <span><small>${escapeHtml(recovery)}</small><strong>${escapeHtml(quotaLaneStateText(lane))}</strong></span>
      </div>`;
    }).join("");
    const credentialText = ({
      stored: "登录状态已保存",
      empty: "尚未完成首次登录",
      invalid: "登录状态异常",
    })[runtime.credential_state] || (loginConfigured ? "登录状态待检测" : "尚未准备登录环境");
    // A successful probe deliberately keeps a disabled account in
    // status=disabled until the administrator enables it. Requiring ready here
    // creates a circular gate because ready is only assigned after enablement.
    const canEnable = source.session_state === "valid" && source.health_status === "healthy";
    const loginButtons = loginPending
      ? `${remoteSession
        ? loginSessionLink(accountId, "button-secondary")
        : remoteLogin
          ? `<button type="button" class="button-secondary" data-action="start-account-reauth" data-reauth-resume="true" data-account-id="${escapeHtml(accountId)}">重新生成安全登录页</button>`
          : ""}<button type="button" class="button-secondary" data-action="cancel-account-reauth" data-account-id="${escapeHtml(accountId)}">取消登录</button><button type="button" class="button" data-action="verify-account-reauth" data-account-id="${escapeHtml(accountId)}">我已登录，开始验证</button>`
      : loginConfigured
        ? `<button type="button" class="button-secondary" data-action="start-account-reauth" data-account-id="${escapeHtml(accountId)}">重新登录</button>`
        : `<button type="button" class="button" data-action="prepare-account-runtime" data-account-id="${escapeHtml(accountId)}">准备登录</button>`;
    openDrawer({
      eyebrow: `${provider.toUpperCase()} ACCOUNT`,
      title: source.name,
      body: `<form id="provider-account-form" data-account-id="${escapeHtml(accountId)}" data-pool-id="${escapeHtml(source.pool_id || poolId)}">
        <div class="drawer-section"><div class="field-grid">
          <label class="field"><span>账号备注名</span><input name="name" maxlength="60" required value="${escapeHtml(source.name || "")}" placeholder="例如：主账号、备用账号 1"/><small>只用于后台识别，不需要填写真实邮箱。</small></label>
          <label class="field"><span>账号额度调度模板</span><select name="quota_profile">${quotaProfileOptions || '<option value="untracked">未设置</option>'}</select><small>由管理员按实际订阅选择；逐档成功请求会统计，模板是本地安全预算，不是 ${provider === "gpt" ? "OpenAI" : "Anthropic"} 实时余额。</small></label>
          <label class="field"><span>单账号安全并发</span><input name="max_concurrency" type="number" min="1" max="20" step="1" required value="${numberText(source.max_concurrency || 1)}"/><small>账号池会实时汇总所有健康账号的安全并发；调整不会启动额外 Worker。</small></label>
          <div class="account-runtime-summary"><span><small>${escapeHtml(providerLabel)} 登录身份</small><strong>${escapeHtml(identityText)}</strong></span><span><small>认证状态</small><strong>${escapeHtml(credentialText)}</strong></span><span><small>当前活动</small><strong>${numberText(source.active || 0)} / ${numberText(source.max_concurrency || 1)}</strong></span><span><small>登录方式</small><strong>${remoteLogin ? "服务器安全页面" : loginConfigured ? "本机专用窗口" : "等待准备"}</strong></span></div>
          <label class="choice-card"><input type="checkbox" name="enabled" ${source.enabled ? "checked" : ""} ${!source.enabled && !canEnable ? "disabled" : ""}/><span><strong>允许这个账号接收请求</strong><small>${canEnable || source.enabled ? "关闭后只停止新请求，不影响已有聊天记录。" : "完成登录并检测通过后才能启用。"}</small></span></label>
        </div></div>
        <div class="drawer-section account-quota-section"><div class="section-heading"><div><h3>逐档额度与调度余量</h3><p>成功请求才计数；失败和取消释放预留。公开值、建议预算和 429 证据分开显示。</p></div><span>${escapeHtml(quota.profile_label || "未设置")}</span></div><div class="account-quota-lanes">${quotaLaneMarkup || '<p class="muted-copy">保存额度模板后显示逐档预算。</p>'}</div></div>
        <div class="info-banner"><i>登</i><div><strong>${loginPending ? remoteLogin ? "请在安全登录页面完成登录" : "请完成专用窗口中的登录" : loginConfigured ? "登录状态由系统安全维护" : "点击“准备登录”即可继续"}</strong><br/>${loginPending ? "完成后回到这里点击“我已登录，开始验证”。系统会关闭登录会话、重启账号 Worker，并再次进行真实检测。" : "认证内容只保存在账号自己的受限目录；过期时页面会提示重新登录。"}</div></div>
        ${remoteSession ? `<div class="remote-login-session"><div><strong>服务器安全登录链接</strong><small>只用于这个账号和本次登录，请勿转发。</small></div>${loginSessionLink(accountId)}<time data-login-expires-at="${escapeHtml(remoteSession.expiresAt)}">${escapeHtml(loginExpiryText(remoteSession.expiresAt))}</time></div>` : ""}
      </form>`,
      footer: `<button type="button" class="button-secondary" data-close-drawer>关闭</button>${source.status === "reauth_required" ? "" : `<button type="button" class="button-secondary" data-action="probe-account" data-account-id="${escapeHtml(accountId)}">检测状态</button>`}${loginButtons}<button type="button" class="button" data-action="save-account">保存</button>`,
    });
  };

  const saveAccount = async (button) => {
    const form = document.querySelector("#provider-account-form");
    if (!form) return;
    const values = new FormData(form);
    const accountId = form.dataset.accountId;
    const isNew = accountId === "new";
    const payload = isNew
      ? { name: String(values.get("name") || "").trim() }
      : {
          name: String(values.get("name") || "").trim(),
          enabled: values.get("enabled") === "on",
          quota_profile: String(values.get("quota_profile") || "untracked"),
          max_concurrency: Number(values.get("max_concurrency")),
        };
    if (!payload.name) return toast("请填写账号备注名", "error");
    if (!isNew && (!Number.isInteger(payload.max_concurrency) || payload.max_concurrency < 1 || payload.max_concurrency > 20)) {
      return toast("单账号安全并发必须是 1–20 的整数", "error");
    }
    const loginPopup = isNew ? reserveLoginWindow(remoteLoginExpected()) : null;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = isNew ? "正在准备隔离环境…" : "正在保存…";
    try {
      const path = isNew
        ? `${ADMIN_API}/account-pools/${encodeURIComponent(form.dataset.poolId)}/accounts/onboard`
        : `${ADMIN_API}/accounts/${encodeURIComponent(accountId)}/settings`;
      const result = await requestJson(path, { method: isNew ? "POST" : "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const remoteSession = isNew ? rememberLoginSession(result.account_id, result.runtime) : null;
      const loginOpened = isNew ? openReservedLoginWindow(loginPopup, result.runtime, remoteSession) : false;
      if (isNew && loginOpened) rememberLoginWindow(result.account_id, loginPopup);
      closeDrawer();
      clearCache("providers", "account-pools", "overview");
      state.accountPools = await loadAccountPools();
      await renderProviders(false, state.renderId, true);
      if (isNew) {
        const remote = result.runtime?.login_mode === "remote_browser";
        toast(remote
          ? loginOpened
            ? "安全登录页面已打开；完成登录后回到这里验证"
            : "浏览器拦截了新窗口；请点击账号卡中的“打开安全登录页”"
          : "登录窗口已打开；完成登录后回到这里验证", "info");
        openAccountDrawer(result.account_id, form.dataset.poolId);
      } else {
        toast("账号设置已保存");
      }
    } catch (error) {
      failReservedLoginWindow(loginPopup);
      toast(error?.message || "账号保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const prepareAccountRuntime = async (button) => {
    const accountId = button.dataset.accountId;
    if (!accountId) return;
    const loginPopup = reserveLoginWindow(remoteLoginExpected(accountId));
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "正在准备…";
    try {
      const result = await requestJson(`${ADMIN_API}/accounts/${encodeURIComponent(accountId)}/runtime/prepare`, { method: "POST" });
      const remoteSession = rememberLoginSession(accountId, result.runtime);
      const loginOpened = openReservedLoginWindow(loginPopup, result.runtime, remoteSession);
      if (loginOpened) rememberLoginWindow(accountId, loginPopup);
      if (!drawer.hidden) closeDrawer();
      clearCache("providers", "account-pools", "overview");
      state.accountPools = await loadAccountPools();
      await renderProviders(false, state.renderId, true);
      openAccountDrawer(accountId, "");
      toast(result.runtime?.login_mode === "remote_browser"
        ? loginOpened
          ? "安全登录页面已打开；完成登录后点击验证"
          : "浏览器拦截了新窗口；请点击账号卡中的“打开安全登录页”"
        : "登录窗口已打开；完成登录后点击验证", "info");
    } catch (error) {
      failReservedLoginWindow(loginPopup);
      toast(error?.message || "登录环境准备失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const probeAccount = async (button) => {
    const accountId = button.dataset.accountId;
    if (!accountId) return;
    const activeForm = document.querySelector("#provider-account-form");
    const reopenDrawer = !drawer.hidden && activeForm?.dataset.accountId === accountId;
    const poolId = activeForm?.dataset.poolId || "";
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "检测中…";
    try {
      const result = await requestJson(`${ADMIN_API}/accounts/${encodeURIComponent(accountId)}/probe`, { method: "POST" });
      clearCache("providers", "account-pools", "overview");
      state.accountPools = await loadAccountPools();
      await renderProviders(false, state.renderId, true);
      if (reopenDrawer) openAccountDrawer(accountId, poolId);
      toast(
        result.ok
          ? `账号检测通过（${numberText(result.latency_ms)} ms）；结果已保存，现在可以勾选启用`
          : "账号仍不可用，请完成登录后重试；仍失败时查看账号状态",
        result.ok ? "success" : "error",
      );
    } catch (error) {
      toast(error?.message || "账号检测失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const accountReauth = async (button, action) => {
    const accountId = button.dataset.accountId;
    if (!accountId || !["start", "verify", "cancel"].includes(action)) return;
    const resumeExistingLogin = button.dataset.reauthResume === "true";
    if (action === "start" && !resumeExistingLogin && !await confirmAction({
      title: "重新登录 Provider 账号",
      message: "重新登录会立即暂停这个账号接收新请求；已有活动请求时系统会拒绝操作。",
      confirmLabel: "暂停并继续",
      danger: true,
    })) return;
    // An already-paused remote session does not need a second confirmation.
    // Reserve its tab directly from the click so the browser keeps user activation.
    const loginPopup = action === "start"
      ? reserveLoginWindow(remoteLoginExpected(accountId))
      : null;
    const labels = {
      start: "正在打开登录窗口…",
      verify: "正在验证并重启…",
      cancel: "正在关闭登录窗口…",
    };
    const original = button.textContent;
    button.disabled = true;
    button.textContent = labels[action];
    try {
      const result = await requestJson(
        `${ADMIN_API}/accounts/${encodeURIComponent(accountId)}/reauth/${action}`,
        { method: "POST" },
      );
      const remoteSession = action === "start"
        ? rememberLoginSession(accountId, result.runtime)
        : null;
      const loginOpened = action === "start"
        ? openReservedLoginWindow(loginPopup, result.runtime, remoteSession)
        : false;
      if (action === "start" && loginOpened) rememberLoginWindow(accountId, loginPopup);
      if (action !== "start") {
        state.loginSessions.delete(String(accountId));
        closeLoginWindow(accountId);
      }
      if (!drawer.hidden) closeDrawer();
      clearCache("providers", "account-pools", "overview");
      await renderProviders(false, state.renderId, true);
      if (action === "start") {
        const account = accountPoolData().accounts.find((item) => item.id === accountId);
        openAccountDrawer(accountId, account?.pool_id || "");
        toast(result.runtime?.login_mode === "remote_browser"
          ? loginOpened
            ? "安全登录页面已打开；完成登录后点击验证"
            : "浏览器拦截了新窗口；请点击账号卡中的“打开安全登录页”"
          : "独立 Chrome 已打开；手工登录后点击验证", "info");
      } else if (action === "verify") {
        toast(`账号重新登录验证通过：${result.upstream_display_name || "身份已确认"}（${numberText(result.latency_ms)} ms）`, "success");
      } else {
        toast("登录窗口已关闭；账号继续保持暂停调度");
      }
    } catch (error) {
      failReservedLoginWindow(loginPopup);
      if (!drawer.hidden) closeDrawer();
      clearCache("providers", "account-pools", "overview");
      await renderProviders(false, state.renderId, true).catch(() => {});
      toast(error?.message || "账号重新登录操作失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const probeAccountPool = async (button) => {
    const poolId = button.dataset.poolId;
    if (!poolId) return;
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "整组检测中…";
    try {
      const result = await requestJson(`${ADMIN_API}/account-pools/${encodeURIComponent(poolId)}/probe`, { method: "POST" });
      clearCache("providers", "account-pools", "overview");
      await renderProviders(false, state.renderId, true);
      const ready = (result.items || []).filter((item) => item.ok).length;
      const pool = accountPoolData().pools.find((item) => item.id === poolId);
      toast(`${familyText(pool?.provider || "gpt")} 账号池检测完成：${ready}/${(result.items || []).length} 可用`, result.ok ? "success" : "error");
    } catch (error) {
      toast(error?.message || "Provider 账号池检测失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const refreshProviders = async (button) => {
    button.disabled = true;
    button.textContent = "检测中…最长 15 秒";
    content.dataset.providerChecking = "true";
    content.querySelectorAll(".provider-card .status-badge").forEach((badge) => {
      badge.dataset.state = "checking";
      badge.textContent = "检测中";
    });
    try {
      clearCache("providers", "overview");
      const result = await requestJson(`${ADMIN_API}/providers?force=true`);
      cache.set("providers", { value: result, at: Date.now() });
      await renderProviders(false, state.renderId, true);
      const items = result.items || [];
      const failed = items.filter((item) => item.state !== "ready").length;
      const slowest = Math.max(0, ...items.map((item) => Number(item.latency_ms) || 0));
      toast(
        failed
          ? `检测完成：${failed} 个 Provider 需要关注，最慢 ${numberText(slowest)} ms`
          : `检测完成：ChatGPT 与 Claude 均正常，最慢 ${numberText(slowest)} ms`,
        failed ? "error" : "success",
      );
    } catch (error) {
      toast(error?.message || "Provider 检测失败", "error");
      clearCache("providers");
      await renderProviders(false, state.renderId, true).catch(() => {});
    } finally {
      content.dataset.providerChecking = "false";
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = "重新检测";
      }
    }
  };

  const saveProviderDisplay = async (button) => {
    const form = button.closest("form[data-provider-display]");
    if (!form) return;
    const provider = form.dataset.providerDisplay;
    const displayName = String(new FormData(form).get("display_name") || "").trim();
    if (!["gpt", "claude"].includes(provider)) return;
    if (!displayName) return toast("请填写聊天页展示名称", "error");
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "保存中…";
    try {
      await requestJson(`${ADMIN_API}/providers/${encodeURIComponent(provider)}/display`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      });
      clearCache("providers", "models", "overview");
      await renderProviders(false, state.renderId, true);
      toast("展示名称已保存；聊天页刷新后会使用新名称");
    } catch (error) {
      toast(error?.message || "展示名称保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const buildUsersBundle = async () => {
    const [chat, storage] = await Promise.all([loadChatAdmin(), loadStorageUsers()]);
    state.chatAdmin = chat;
    state.storageUsers = storage;
    const storageById = new Map((storage.items || []).map((item) => [item.id, item]));
    const items = (chat.items || []).map((item) => ({ ...item, storage: storageById.get(item.id) || null }));
    state.usersBundle = {
      items,
      groups: chat.resource_groups || chat.groups || [],
      resourceGroups: chat.resource_groups || chat.groups || [],
      modelGroups: chat.model_groups || [],
      presetsByProvider: chat.presets_by_provider || {},
      selections: chat.selections || [],
    };
    return state.usersBundle;
  };

  const filteredUsers = () => {
    const query = state.userQuery.trim().toLowerCase();
    return (state.usersBundle?.items || []).filter((item) => {
      if (state.userRole !== "all" && item.role !== state.userRole) return false;
      if (!query) return true;
      return [item.name, item.email, item.id, roleText(item.role)]
        .some((value) => String(value || "").toLowerCase().includes(query));
    });
  };

  const userTable = () => {
    const users = filteredUsers();
    if (!users.length) return `<div class="empty-state"><div><span class="empty-mark">人</span><h2>没有匹配的用户</h2><p>调整搜索词或角色筛选后再试。</p></div></div>`;
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>用户</th><th>系统角色</th><th>订阅状态</th><th>对话数据</th><th>媒体空间</th><th></th></tr></thead><tbody>${users.map((target) => {
      const used = Number(target.storage?.used_bytes || 0);
      const quota = Number(target.storage?.quota_bytes || 0);
      return `<tr>
        <td><div class="user-cell"><span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>${escapeHtml(target.email || "")}</small></div></div></td>
        <td>${roleBadge(target.role)}</td>
        <td>${subscriptionBadge(target.subscription)}<br/><small>${target.subscription?.expires_at ? `至 ${escapeHtml(subscriptionDateTime(target.subscription.expires_at))}` : "无到期时间"}</small></td>
        <td><strong>${numberText(target.quota?.request_count)} 次有效请求</strong><br/><small>功能与额度请到“订阅管理”调整</small></td>
        <td><strong>${escapeHtml(bytes(used))} / ${escapeHtml(bytes(quota))}</strong><br/><small>继承 ${escapeHtml(target.storage?.group_name || target.policy?.resource_group?.name || "资源组")} · ${quota ? Math.round((used / quota) * 100) : 0}%</small></td>
        <td><button type="button" class="button-secondary" data-action="open-user" data-user-id="${escapeHtml(target.id)}">管理资料</button></td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
  };

  const renderUserTableRegion = () => {
    const region = document.querySelector("#user-table-region");
    if (region) region.innerHTML = userTable();
    const count = document.querySelector("#user-filter-count");
    if (count) count.textContent = `${filteredUsers().length} 位`;
  };

  const renderUsers = async (renderId = state.renderId) => {
    const bundle = await buildUsersBundle();
    if (renderId !== state.renderId || state.route !== "users") return;
    const roleCounts = bundle.items.reduce((counts, item) => ({ ...counts, [item.role]: (counts[item.role] || 0) + 1 }), {});
    content.innerHTML = `
      ${pageIntro("用户资料与系统角色", "这里只管理账号身份和角色；激活、有效期、资源组、模型组与并发统一放在“订阅管理”。", '<button type="button" class="button" data-route="subscriptions">打开订阅管理</button>')}
      <div class="filter-bar">
        <label class="search-field"><input type="search" data-user-search placeholder="搜索姓名、邮箱或用户 ID" value="${escapeHtml(state.userQuery)}" aria-label="搜索用户"/></label>
        <div class="filter-chips" role="group" aria-label="按角色筛选">
          ${[["all", `全部 ${bundle.items.length}`], ["pending", `待审批 ${roleCounts.pending || 0}`], ["user", `用户 ${roleCounts.user || 0}`], ["admin", `管理员 ${roleCounts.admin || 0}`]].map(([key, label]) => `<button type="button" data-user-filter="${key}" aria-pressed="${String(state.userRole === key)}">${escapeHtml(label)}</button>`).join("")}
        </div>
        <span class="count-pill" id="user-filter-count">${filteredUsers().length} 位</span>
      </div>
      <article class="panel" id="user-table-region">${userTable()}</article>`;
  };

  const openDrawer = ({ eyebrow, title, body, footer = "" }) => {
    if (drawer.hidden) state.lastFocus = document.activeElement;
    document.querySelector("#drawer-eyebrow").textContent = eyebrow;
    document.querySelector("#drawer-title").textContent = title;
    drawerBody.innerHTML = body;
    drawerFooter.innerHTML = footer;
    drawer.hidden = false;
    document.documentElement.style.overflow = "hidden";
    drawer.querySelector(".drawer-close")?.focus();
  };

  const closeDrawer = () => {
    if (drawer.hidden) return;
    drawer.hidden = true;
    drawerBody.innerHTML = "";
    drawerFooter.innerHTML = "";
    document.documentElement.style.overflow = "";
    state.lastFocus?.focus?.();
    state.lastFocus = null;
    state.activeGroup = null;
  };

  const openModal = ({ eyebrow = "PROJECT API", title, body, footer = "", resolve = null }) => {
    if (modal.hidden) state.modalLastFocus = document.activeElement;
    document.querySelector("#modal-eyebrow").textContent = eyebrow;
    document.querySelector("#modal-title").textContent = title;
    modalBody.innerHTML = body;
    modalFooter.innerHTML = footer;
    state.modalResolve = resolve;
    modal.hidden = false;
    document.documentElement.style.overflow = "hidden";
    modal.querySelector(".modal-close")?.focus();
  };

  const closeModal = (result = false) => {
    if (modal.hidden) return;
    const resolve = state.modalResolve;
    modal.hidden = true;
    modalBody.innerHTML = "";
    modalFooter.innerHTML = "";
    state.modalResolve = null;
    document.documentElement.style.overflow = drawer.hidden ? "" : "hidden";
    state.modalLastFocus?.focus?.();
    state.modalLastFocus = null;
    resolve?.(result);
  };

  const confirmAction = ({ title, message, confirmLabel = "确认", danger = false }) =>
    new Promise((resolve) => {
      openModal({
        eyebrow: "操作确认",
        title,
        body: `<div class="confirm-copy"><span aria-hidden="true">${danger ? "!" : "?"}</span><p>${escapeHtml(message)}</p></div>`,
        footer: `<button type="button" class="button-secondary" data-close-modal>取消</button><button type="button" class="${danger ? "button-danger" : "button"}" data-action="confirm-modal">${escapeHtml(confirmLabel)}</button>`,
        resolve,
      });
    });

  const openUserDrawer = (userId) => {
    const bundle = state.usersBundle;
    const target = bundle?.items.find((item) => item.id === userId);
    if (!target) return toast("用户数据已变化，请刷新后重试", "error");
    const storage = target.storage || {};
    const pending = target.role === "pending";
    openDrawer({
      eyebrow: pending ? "PENDING USER" : "USER PROFILE",
      title: target.name || target.email || "用户管理",
      body: `
        ${pending ? '<div class="warning-banner"><i>订</i><div><strong>该用户尚未激活订阅</strong><br/>待审批账号的功能分配和激活已统一移到“订阅管理”，这里不会绕过有效期直接批准。</div></div>' : ""}
        <form id="user-control-form" data-user-id="${escapeHtml(target.id)}">
          <div class="drawer-section">
            <div class="drawer-section-heading"><div><h3>账号资料</h3><p>只展示 Open WebUI 账号数据，不读取或重置用户密码</p></div>${roleBadge(target.role)}</div>
            <div class="field-grid">
              <div class="read-only-field"><span>姓名</span><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>用户自行维护的展示名称</small></div>
              <div class="read-only-field"><span>邮箱</span><strong>${escapeHtml(target.email || "—")}</strong><small>用户 ID：${escapeHtml(target.id)}</small></div>
              ${pending
                ? `<div class="read-only-field"><span>系统角色</span><strong>待审批</strong><small>请在订阅管理完成激活</small></div>`
                : `<label class="field"><span>系统角色</span><select name="role">
                    <option value="user" ${target.role === "user" ? "selected" : ""}>普通用户</option>
                    <option value="admin" ${target.role === "admin" ? "selected" : ""}>管理员</option>
                  </select><small>管理员不受订阅有效期限制；角色变更后需重新登录刷新权限。</small></label>`}
              <div class="read-only-field"><span>订阅状态</span><strong>${escapeHtml(subscriptionStatusText(target.subscription?.status))}</strong><small>${target.subscription?.expires_at ? `北京时间 ${escapeHtml(subscriptionDateTime(target.subscription.expires_at))} 到期` : "管理员账号不限期或尚未开通"}</small></div>
            </div>
          </div>

          <div class="drawer-section">
            <div class="drawer-section-heading"><div><h3>账号数据概览</h3><p>功能分配只读展示；修改请进入订阅管理</p></div><span class="count-pill">已用 ${escapeHtml(bytes(storage.used_bytes))}</span></div>
            <div class="field-grid">
              <div class="read-only-field"><span>资源组</span><strong>${escapeHtml(target.policy?.resource_group?.name || target.policy?.group?.name || "默认资源组")}</strong><small>媒体空间 ${escapeHtml(bytes(storage.used_bytes))} / ${escapeHtml(bytes(storage.quota_bytes))}</small></div>
              <div class="read-only-field"><span>模型组</span><strong>${["gpt", "claude"].map((family) => `${familyText(family)}：${escapeHtml(target.policy?.provider_groups?.[family]?.name || "旧版自定义")}`).join(" · ")}</strong><small>当前并发 ${numberText(target.concurrency?.user_max_concurrency)} 个</small></div>
            </div>
          </div>

        </form>`,
      footer: pending
        ? `<button type="button" class="button-secondary" data-close-drawer>关闭</button><button type="button" class="button" data-route="subscriptions">前往订阅管理</button>`
        : `<button type="button" class="button-secondary" data-close-drawer>取消</button><button type="button" class="button" data-action="save-user">保存角色</button>`,
    });
  };

  const saveUser = async (button) => {
    const form = document.querySelector("#user-control-form");
    if (!form) return;
    const target = state.usersBundle?.items.find((item) => item.id === form.dataset.userId);
    if (!target) return;
    if (target.role === "pending") {
      return toast("待审批用户请在订阅管理中激活", "error");
    }
    const values = new FormData(form);
    const nextRole = String(values.get("role") || "user");
    if (nextRole !== target.role) {
      const action = `${nextRole === "admin" ? "提升" : "调整"}该用户角色`;
      const confirmed = await confirmAction({
        title: action,
        message: `确定${action}吗？角色权限会立即生效。`,
        confirmLabel: "确认调整",
      });
      if (!confirmed) return;
    }
    button.disabled = true;
    button.textContent = "正在保存…";
    try {
      if (nextRole !== target.role) {
        await requestJson(`${ADMIN_API}/users/${encodeURIComponent(target.id)}/role`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: nextRole }),
        });
      }
      clearCache("overview", "chat-admin", "storage-users");
      closeDrawer();
      await renderUsers();
      updateGlobalStatus(await loadOverview());
      toast("用户角色已保存");
    } catch (error) {
      toast(error?.message || "用户设置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存角色";
    }
  };

  const subscriptionRemainingText = (subscription) => {
    if (!subscription?.active || subscription.status === "unlimited") return "";
    const remaining = Math.max(
      0,
      Number(subscription.expires_at || 0) - Math.floor(Date.now() / 1000),
    );
    if (remaining < 60) return "不足 1 分钟";
    if (remaining < 3600) return `剩余 ${Math.ceil(remaining / 60)} 分钟`;
    if (remaining < 86400) return `剩余 ${Math.ceil(remaining / 3600)} 小时`;
    return `剩余 ${Math.ceil(remaining / 86400)} 天`;
  };

  const filteredSubscriptions = () => {
    const query = state.subscriptionQuery.trim().toLowerCase();
    return (state.usersBundle?.items || []).filter((item) => {
      const statusValue = item.subscription?.status || "inactive";
      if (state.subscriptionStatus !== "all" && statusValue !== state.subscriptionStatus) return false;
      const resourceGroupId = item.policy?.resource_group?.id || item.policy?.group?.id || "";
      if (
        state.subscriptionResourceGroup
        && resourceGroupId !== state.subscriptionResourceGroup
      ) return false;
      if (
        state.subscriptionGptGroup
        && item.policy?.provider_groups?.gpt?.id !== state.subscriptionGptGroup
      ) return false;
      if (
        state.subscriptionClaudeGroup
        && item.policy?.provider_groups?.claude?.id !== state.subscriptionClaudeGroup
      ) return false;
      if (!query) return true;
      const providerNames = Object.values(item.policy?.provider_groups || {}).map((group) => group?.name);
      return [
        item.name,
        item.email,
        item.policy?.resource_group?.name,
        subscriptionStatusText(statusValue),
        ...providerNames,
      ].some((value) => String(value || "").toLowerCase().includes(query));
    });
  };

  const selectedSubscriptionUsers = () => {
    const selected = state.subscriptionSelectedUserIds;
    return (state.usersBundle?.items || []).filter(
      (item) => item.role !== "admin" && selected.has(item.id),
    );
  };

  const subscriptionBatchBar = () => {
    const visible = filteredSubscriptions().filter((item) => item.role !== "admin");
    const selected = selectedSubscriptionUsers();
    return `<div class="subscription-batch-bar" data-has-selection="${String(selected.length > 0)}">
      <div><strong>批量迁移分组</strong><small>当前已选 ${numberText(selected.length)} 位；只调整明确选择的资源/GPT/Claude 组</small></div>
      <div class="row-actions">
        <button type="button" class="text-button" data-action="select-visible-subscriptions" ${visible.length ? "" : "disabled"}>选择当前 ${numberText(visible.length)} 位</button>
        <button type="button" class="text-button" data-action="clear-subscription-selection" ${selected.length ? "" : "disabled"}>清空</button>
        <button type="button" class="button-secondary" data-action="open-bulk-groups" ${selected.length ? "" : "disabled"}>调整所选分组</button>
      </div>
    </div>`;
  };

  const subscriptionTable = () => {
    const users = filteredSubscriptions();
    if (!users.length) {
      return `<div class="empty-state"><div><span class="empty-mark">订</span><h2>没有匹配的订阅</h2><p>调整搜索词或状态筛选后再试。</p></div></div>`;
    }
    const selectable = users.filter((target) => target.role !== "admin");
    const selectedVisible = selectable.filter((target) => state.subscriptionSelectedUserIds.has(target.id));
    const allSelected = selectable.length > 0 && selectedVisible.length === selectable.length;
    const partlySelected = selectedVisible.length > 0 && !allSelected;
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th class="selection-cell"><input type="checkbox" data-subscription-select-all aria-label="选择当前筛选用户" aria-checked="${partlySelected ? "mixed" : String(allSelected)}" ${allSelected ? "checked" : ""} ${selectable.length ? "" : "disabled"}/></th><th>用户</th><th>订阅状态</th><th>有效期（北京时间）</th><th>资源 / 模型组</th><th>并发</th><th></th></tr></thead><tbody>${users.map((target) => {
      const subscription = target.subscription || {};
      const validity = subscription.status === "unlimited"
        ? "<strong>不限期</strong><br/><small>管理员绕过订阅门禁</small>"
        : subscription.expires_at
          ? `<strong>${escapeHtml(subscriptionDateTime(subscription.expires_at))}</strong><br/><small>${escapeHtml(subscriptionRemainingText(subscription) || (subscription.status === "expired" ? "已超过到期时间" : "精确到分钟校验"))}</small>`
          : "<strong>尚未设置</strong><br/><small>默认开通 30 天</small>";
      return `<tr>
        <td class="selection-cell"><input type="checkbox" data-subscription-select value="${escapeHtml(target.id)}" aria-label="选择 ${escapeHtml(target.name || target.email || "该用户")}" ${state.subscriptionSelectedUserIds.has(target.id) ? "checked" : ""} ${target.role === "admin" ? "disabled" : ""}/></td>
        <td><div class="user-cell"><span class="user-avatar">${escapeHtml(String(target.name || target.email || "?").trim().slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(target.name || "未命名用户")}</strong><small>${escapeHtml(target.email || "")}</small></div></div></td>
        <td>${subscriptionBadge(subscription)}<br/><small>${escapeHtml(roleText(target.role))}</small></td>
        <td>${validity}</td>
        <td><strong>${escapeHtml(target.policy?.resource_group?.name || target.policy?.group?.name || "默认资源组")}</strong><br/><small>${["gpt", "claude"].map((family) => `${familyText(family)}：${escapeHtml(target.policy?.provider_groups?.[family]?.name || "旧版自定义")}`).join(" · ")}</small></td>
        <td><strong>${numberText(target.concurrency?.user_max_concurrency)} 个</strong><br/><small>${target.concurrency?.user_override == null ? "继承资源组" : "个人覆盖"} · 组上限 ${numberText(target.concurrency?.group_max_concurrency)}</small></td>
        <td><button type="button" class="button-secondary" data-action="open-subscription" data-user-id="${escapeHtml(target.id)}">${target.role === "admin" ? "查看" : "管理订阅"}</button></td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
  };

  const renderSubscriptionTableRegion = () => {
    const region = document.querySelector("#subscription-table-region");
    if (region) region.innerHTML = subscriptionTable();
    const batch = document.querySelector("#subscription-batch-region");
    if (batch) batch.innerHTML = subscriptionBatchBar();
    const count = document.querySelector("#subscription-filter-count");
    if (count) count.textContent = `${filteredSubscriptions().length} 位`;
    const selectAll = document.querySelector("[data-subscription-select-all]");
    if (selectAll) selectAll.indeterminate = selectAll.getAttribute("aria-checked") === "mixed";
  };

  const renderSubscriptions = async (renderId = state.renderId) => {
    const bundle = await buildUsersBundle();
    if (renderId !== state.renderId || state.route !== "subscriptions") return;
    const counts = bundle.items.reduce((result, item) => {
      const key = item.subscription?.status || "inactive";
      result[key] = (result[key] || 0) + 1;
      return result;
    }, {});
    const filters = [
      ["all", `全部 ${bundle.items.length}`],
      ["pending", `待激活 ${counts.pending || 0}`],
      ["active", `生效中 ${counts.active || 0}`],
      ["expired", `已过期 ${counts.expired || 0}`],
      ["cancelled", `已停止 ${counts.cancelled || 0}`],
      ["unlimited", `管理员 ${counts.unlimited || 0}`],
    ];
    const resourceGroupOptions = (bundle.resourceGroups || [])
      .map((group) => `<option value="${escapeHtml(group.id)}" ${state.subscriptionResourceGroup === group.id ? "selected" : ""}>${escapeHtml(group.name)}${group.is_retired ? "（旧版待迁移）" : ""}</option>`)
      .join("");
    const modelGroupFilter = (family, value) => (bundle.modelGroups || [])
      .filter((group) => group.provider_family === family)
      .map((group) => `<option value="${escapeHtml(group.id)}" ${value === group.id ? "selected" : ""}>${escapeHtml(group.name)}${group.is_retired ? "（旧版待迁移）" : ""}</option>`)
      .join("");
    content.innerHTML = `
      ${pageIntro("订阅、功能和时间统一管理", "普通用户默认 30 天，到期精确到分钟；无需延迟队列，每次读取能力和发送消息时都会即时校验。")}
      <div class="info-banner"><i>时</i><div><strong>默认规则：激活日起第 30 天 23:59:59 到期（北京时间）</strong><br/>过期用户仍能登录、查看页面与设置，但前端发送按钮关闭，服务端也会拒绝绕过请求；管理员不限期。</div></div>
      <div class="filter-bar">
        <label class="search-field"><input type="search" data-subscription-search placeholder="搜索姓名、邮箱或分组" value="${escapeHtml(state.subscriptionQuery)}" aria-label="搜索订阅"/></label>
        <div class="filter-chips" role="group" aria-label="按订阅状态筛选">
          ${filters.map(([key, label]) => `<button type="button" data-subscription-filter="${key}" aria-pressed="${String(state.subscriptionStatus === key)}">${escapeHtml(label)}</button>`).join("")}
        </div>
        <span class="count-pill" id="subscription-filter-count">${filteredSubscriptions().length} 位</span>
      </div>
      <div class="subscription-group-filters" aria-label="按分组筛选用户">
        <label><span>资源组</span><select data-subscription-group-filter="resource"><option value="">全部资源组</option>${resourceGroupOptions}</select></label>
        <label><span>GPT 组</span><select data-subscription-group-filter="gpt"><option value="">全部 GPT 组</option>${modelGroupFilter("gpt", state.subscriptionGptGroup)}</select></label>
        <label><span>Claude 组</span><select data-subscription-group-filter="claude"><option value="">全部 Claude 组</option>${modelGroupFilter("claude", state.subscriptionClaudeGroup)}</select></label>
        <button type="button" class="text-button" data-action="reset-subscription-group-filters">重置分组筛选</button>
      </div>
      <div id="subscription-batch-region">${subscriptionBatchBar()}</div>
      <article class="panel" id="subscription-table-region">${subscriptionTable()}</article>`;
    const selectAll = document.querySelector("[data-subscription-select-all]");
    if (selectAll) selectAll.indeterminate = selectAll.getAttribute("aria-checked") === "mixed";
  };

  const openBulkGroupModal = () => {
    const targets = selectedSubscriptionUsers();
    if (!targets.length) return toast("请先选择需要调整的用户", "error");
    const bundle = state.usersBundle;
    const resourceOptions = (bundle?.resourceGroups || [])
      .filter((group) => !group.is_retired)
      .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`)
      .join("");
    const providerOptions = (family) => (bundle?.modelGroups || [])
      .filter((group) => group.provider_family === family && !group.is_retired)
      .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`)
      .join("");
    openModal({
      eyebrow: "BULK GROUP MIGRATION",
      title: `批量调整 ${targets.length} 位用户`,
      body: `
        <div class="info-banner"><i>组</i><div><strong>只修改明确选择的分组</strong><br/>“保持不变”的项目不会被覆盖；订阅有效期、并发、角色和模型时间窗均不变。整批写入使用同一数据库事务。</div></div>
        <form id="bulk-group-form">
          <div class="field-grid">
            <label class="field"><span>资源组</span><select name="resource_group_id"><option value="">保持不变</option>${resourceOptions}</select></label>
            <label class="field"><span>GPT 额度组</span><select name="gpt_model_group_id"><option value="">保持不变</option>${providerOptions("gpt")}</select></label>
            <label class="field"><span>Claude 额度组</span><select name="claude_model_group_id"><option value="">保持不变</option>${providerOptions("claude")}</select></label>
          </div>
        </form>`,
      footer: '<button type="button" class="button-secondary" data-close-modal>取消</button><button type="button" class="button" data-action="apply-bulk-groups">确认批量调整</button>',
    });
  };

  const applyBulkGroups = async (button) => {
    const form = document.querySelector("#bulk-group-form");
    const targets = selectedSubscriptionUsers();
    if (!form || !targets.length) return;
    const values = new FormData(form);
    const payload = {
      user_ids: targets.map((target) => target.id),
      resource_group_id: String(values.get("resource_group_id") || "") || null,
      gpt_model_group_id: String(values.get("gpt_model_group_id") || "") || null,
      claude_model_group_id: String(values.get("claude_model_group_id") || "") || null,
    };
    if (!payload.resource_group_id && !payload.gpt_model_group_id && !payload.claude_model_group_id) {
      return toast("请至少选择一个需要调整的分组", "error");
    }
    button.disabled = true;
    button.textContent = "正在批量调整…";
    try {
      const result = await requestJson(`${CHAT_API}/admin/users/bulk-groups`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.subscriptionSelectedUserIds.clear();
      clearCache("overview", "chat-admin", "storage-users");
      closeModal(true);
      await renderSubscriptions();
      toast(`已批量调整 ${numberText(result.updated || targets.length)} 位用户`);
    } catch (error) {
      toast(error?.message || "批量调整分组失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "确认批量调整";
    }
  };

  const filterUsersByGroup = (button) => {
    const kind = button.dataset.groupKind;
    const groupId = button.dataset.groupId || "";
    state.subscriptionQuery = "";
    state.subscriptionStatus = "all";
    state.subscriptionResourceGroup = kind === "resource" ? groupId : "";
    state.subscriptionGptGroup = kind === "gpt" ? groupId : "";
    state.subscriptionClaudeGroup = kind === "claude" ? groupId : "";
    state.subscriptionSelectedUserIds.clear();
    closeDrawer();
    navigate("subscriptions");
  };

  const openSubscriptionDrawer = (userId) => {
    const bundle = state.usersBundle;
    const target = bundle?.items.find((item) => item.id === userId);
    if (!target) return toast("订阅数据已变化，请刷新后重试", "error");
    const subscription = target.subscription || {};
    if (target.role === "admin") {
      openDrawer({
        eyebrow: "UNLIMITED ADMIN",
        title: target.name || target.email || "管理员订阅",
        body: `
          <div class="info-banner"><i>管</i><div><strong>管理员账号不限期</strong><br/>管理员需要持续进入后台处理用户与系统设置，因此不会被订阅有效期阻断。</div></div>
          <div class="drawer-section"><div class="drawer-section-heading"><div><h3>账号</h3><p>${escapeHtml(target.email || "")}</p></div>${roleBadge(target.role)}</div>
          <div class="field-grid">
            <div class="read-only-field"><span>订阅状态</span><strong>管理员不限期</strong><small>服务端直接豁免</small></div>
            <div class="read-only-field"><span>功能调整</span><strong>请先在用户模块改为普通用户</strong><small>改回普通用户后会自动获得默认 30 天订阅</small></div>
          </div></div>`,
        footer: '<button type="button" class="button-secondary" data-close-drawer>关闭</button>',
      });
      return;
    }

    const currentResourceGroup = target.policy?.resource_group?.id || target.policy?.group?.id || "";
    const selectedResourceGroup = (bundle.resourceGroups || []).find((group) => group.id === currentResourceGroup);
    const resourceGroupOptions = (bundle.resourceGroups || [])
      .filter((group) => !group.is_retired || group.id === currentResourceGroup)
      .map((group) =>
      `<option value="${escapeHtml(group.id)}" title="${escapeHtml(resourceGroupDetail(group))}" ${group.id === currentResourceGroup ? "selected" : ""}>${escapeHtml(group.name)}</option>`
      ).join("");
    const providerGroupSelectors = ["gpt", "claude"].map((family) => {
      const current = target.policy?.provider_groups?.[family]?.id || "";
      const groups = (bundle.modelGroups || []).filter((group) => group.provider_family === family);
      return modelGroupPicker(family, groups, current, bundle);
    }).join("");
    const now = Math.floor(Date.now() / 1000);
    const preserveWindow = ["active", "scheduled"].includes(subscription.status)
      || (
        subscription.status === "pending"
        && subscription.configured
        && Number(subscription.expires_at || 0) > now
      );
    const startsAt = preserveWindow && subscription.starts_at ? Number(subscription.starts_at) : now;
    const expiresAt = preserveWindow && subscription.expires_at
      ? Number(subscription.expires_at)
      : defaultSubscriptionExpiry(30, startsAt);
    openDrawer({
      eyebrow: target.role === "pending" ? "ACTIVATE SUBSCRIPTION" : "SUBSCRIPTION CONTROL",
      title: target.name || target.email || "订阅管理",
      body: `
        ${target.role === "pending" ? '<div class="warning-banner"><i>审</i><div><strong>保存后才会激活该用户</strong><br/>系统先写入资源组、GPT/Claude 组、并发和有效期，全部成功后才把角色改为普通用户。</div></div>' : ""}
        ${["expired", "cancelled", "inactive"].includes(subscription.status) ? `<div class="danger-banner"><i>!</i><div><strong>${escapeHtml(subscriptionStatusText(subscription.status))}</strong><br/>用户仍可浏览页面和设置，但发送按钮与服务端请求均已关闭；保存新有效期即可重新开通。</div></div>` : ""}
        <form id="subscription-control-form" data-user-id="${escapeHtml(target.id)}">
          <input type="hidden" name="starts_at" value="${startsAt}"/>
          <div class="drawer-section">
            <div class="drawer-section-heading"><div><h3>有效期</h3><p>${escapeHtml(target.email || "")} · 当前状态：${escapeHtml(subscriptionStatusText(subscription.status))}</p></div>${subscriptionBadge(subscription)}</div>
            <div class="field-grid">
              <div class="read-only-field"><span>开始时间</span><strong data-subscription-start-label>${escapeHtml(subscriptionDateTime(startsAt))}</strong><small>北京时间；重新开通时从当前时间开始</small></div>
              <label class="field"><span>到期时间（北京时间）</span><input name="expires_at" type="datetime-local" step="60" required value="${escapeHtml(beijingDateTimeInput(expiresAt))}"/><small>精确到分钟；到点后无需后台任务，下一次发送会立即被拦截。</small></label>
            </div>
            <div class="subscription-presets" aria-label="快速设置有效期">
              <span>从今天起：</span>
              <button type="button" class="button-secondary" data-action="apply-subscription-preset" data-days="30">30 天</button>
              <button type="button" class="button-secondary" data-action="apply-subscription-preset" data-days="90">90 天</button>
              <button type="button" class="button-secondary" data-action="apply-subscription-preset" data-days="365">365 天</button>
            </div>
          </div>

          <div class="drawer-section">
            <div class="drawer-section-heading"><div><h3>功能分配</h3><p>资源、GPT 与 Claude 相互独立；套餐按从小到大排列，选中后直接展示逐档数值</p></div><span class="count-pill">默认 30 天</span></div>
            <div class="field-grid">
              <label class="field"><span>资源组</span><select name="resource_group_id" data-group-detail-select>${resourceGroupOptions}</select><small data-group-detail>${escapeHtml(resourceGroupDetail(selectedResourceGroup))}</small></label>
              ${providerGroupSelectors}
              <label class="field"><span>用户最大并发</span><input name="user_concurrency" type="number" min="1" max="${numberText(target.concurrency?.group_max_concurrency || 1)}" step="1" placeholder="继承 ${numberText(target.concurrency?.default_user_concurrency || 1)}" value="${target.concurrency?.user_override == null ? "" : numberText(target.concurrency.user_override)}"/><small>留空继承资源组；当前生效 ${numberText(target.concurrency?.user_max_concurrency)}，不能超过资源组上限。</small></label>
            </div>
          </div>
        </form>
        `,
      footer: `<button type="button" class="button-secondary" data-close-drawer>取消</button>${subscription.configured ? '<button type="button" class="button-danger" data-action="cancel-subscription">停止订阅</button>' : ""}<button type="button" class="button-secondary" data-action="reset-user-quota">重置模型窗</button>${subscription.configured && subscription.status === "active" ? '<button type="button" class="button-secondary" data-action="extend-subscription" data-days="30">续订 30 天</button>' : ""}<button type="button" class="button" data-action="save-subscription">${target.role === "pending" ? "分配并激活" : "保存订阅"}</button>`,
    });
  };

  const saveSubscription = async (button) => {
    const form = document.querySelector("#subscription-control-form");
    if (!form) return;
    const target = state.usersBundle?.items.find((item) => item.id === form.dataset.userId);
    if (!target || target.role === "admin") return;
    const values = new FormData(form);
    const startsAt = Number(values.get("starts_at") || Math.floor(Date.now() / 1000));
    const expiresAt = beijingInputTimestamp(values.get("expires_at"));
    if (!expiresAt || expiresAt <= startsAt) {
      return toast("到期时间必须晚于开始时间", "error");
    }
    if (target.role === "pending") {
      const confirmed = await confirmAction({
        title: "激活用户订阅",
        message: `确认激活 ${target.name || target.email || "该用户"} 吗？订阅将在北京时间 ${subscriptionDateTime(expiresAt)} 到期。`,
        confirmLabel: "确认分配并激活",
      });
      if (!confirmed) return;
    }
    button.disabled = true;
    const original = button.textContent;
    button.textContent = target.role === "pending" ? "正在激活…" : "正在保存…";
    try {
      const nextResourceGroup = String(values.get("resource_group_id") || "");
      if (nextResourceGroup && nextResourceGroup !== (target.policy?.resource_group?.id || target.policy?.group?.id)) {
        await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(target.id)}/resource-group`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ group_id: nextResourceGroup }),
        });
      }
      for (const family of ["gpt", "claude"]) {
        const nextModelGroup = String(values.get(`model_group_${family}`) || "");
        if (nextModelGroup && nextModelGroup !== target.policy?.provider_groups?.[family]?.id) {
          await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(target.id)}/model-groups/${encodeURIComponent(family)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ group_id: nextModelGroup }),
          });
        }
      }
      const concurrencyValue = String(values.get("user_concurrency") || "").trim();
      const nextConcurrency = concurrencyValue === "" ? null : Number(concurrencyValue);
      if (nextConcurrency !== (target.concurrency?.user_override ?? null)) {
        await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(target.id)}/concurrency`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_concurrency: nextConcurrency }),
        });
      }
      await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(target.id)}/subscription`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          starts_at: startsAt,
          expires_at: expiresAt,
          duration_days: 30,
        }),
      });
      if (target.role === "pending") {
        await requestJson(`${ADMIN_API}/users/${encodeURIComponent(target.id)}/role`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: "user" }),
        });
      }
      clearCache("overview", "chat-admin", "storage-users");
      closeDrawer();
      await renderSubscriptions();
      updateGlobalStatus(await loadOverview());
      toast(target.role === "pending" ? "用户已激活，订阅与功能分配已生效" : "订阅设置已保存");
    } catch (error) {
      toast(error?.message || "订阅保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const applySubscriptionPreset = (button) => {
    const form = document.querySelector("#subscription-control-form");
    if (!form) return;
    const days = Number(button.dataset.days) || 30;
    const startsAt = Math.floor(Date.now() / 1000);
    const expiresAt = defaultSubscriptionExpiry(days, startsAt);
    form.elements.starts_at.value = String(startsAt);
    form.elements.expires_at.value = beijingDateTimeInput(expiresAt);
    const label = form.closest("#drawer-body")?.querySelector("[data-subscription-start-label]");
    if (label) label.textContent = subscriptionDateTime(startsAt);
    toast(`已填入从今天起 ${days} 天的到期时间`, "info");
  };

  const extendSubscription = async (button) => {
    const form = document.querySelector("#subscription-control-form");
    if (!form) return;
    const days = Number(button.dataset.days) || 30;
    const confirmed = await confirmAction({
      title: `续订 ${days} 天`,
      message: "续订会从当前到期日顺延；如果已经过期，则从现在重新计算。",
      confirmLabel: "确认续订",
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(form.dataset.userId)}/subscription/extend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days }),
      });
      clearCache("overview", "chat-admin");
      await buildUsersBundle();
      openSubscriptionDrawer(form.dataset.userId);
      toast(`已续订 ${days} 天`);
    } catch (error) {
      toast(error?.message || "续订失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const cancelSubscription = async (button) => {
    const form = document.querySelector("#subscription-control-form");
    if (!form) return;
    const confirmed = await confirmAction({
      title: "停止订阅",
      message: "停止后用户仍可登录和查看页面，但会立即失去发送消息的权限。历史数据不会删除。",
      confirmLabel: "确认停止",
      danger: true,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(form.dataset.userId)}/subscription/cancel`, {
        method: "POST",
      });
      clearCache("overview", "chat-admin");
      closeDrawer();
      await renderSubscriptions();
      toast("订阅已停止");
    } catch (error) {
      toast(error?.message || "停止订阅失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const resetUserQuota = async (button) => {
    const form = document.querySelector("#subscription-control-form");
    if (!form) return;
    const confirmed = await confirmAction({
      title: "重置模型时间窗",
      message: "确定重置该用户全部模型的当前时间窗吗？此操作不会删除审计历史。",
      confirmLabel: "确认重置",
      danger: true,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await requestJson(`${CHAT_API}/admin/users/${encodeURIComponent(form.dataset.userId)}/quota/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selection_key: null }),
      });
      clearCache("overview", "chat-admin");
      await buildUsersBundle();
      openSubscriptionDrawer(form.dataset.userId);
      toast("模型时间窗已重置");
    } catch (error) {
      toast(error?.message || "时间窗重置失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const groupRuleMap = (group) => new Map((group.rules || []).map((rule) => [rule.selection_key, rule]));

  const planPresetsFor = (data, provider) =>
    data?.presets_by_provider?.[provider]
    || (provider === "gpt" ? data?.presets : [])
    || [];

  const planPresetNote = (preset, provider) => {
    if (!preset) {
      return provider === "claude"
        ? "Claude 官方不公布逐模型固定消息数；模板把五小时会话、周额度和站内公平预算明确分开。"
        : "GPT 官方明确数量、计划倍率和本站保守建议会分开标注；保存前不会生效。";
    }
    const sources = (preset.sources || [])
      .map((source) => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.label)}</a>`)
      .join(" · ");
    return `<strong>${escapeHtml(preset.official_note || "")}</strong><br/>${escapeHtml(preset.recommendation_note || "")}${sources ? `<br/><small>官方来源：${sources}</small>` : ""}`;
  };

  const ruleRow = (selection, rule, selections) => {
    const sameFamily = selections.filter((item) => item.family === selection.family && item.key !== selection.key);
    const limit = rule?.limit_count == null ? "" : Number(rule.limit_count);
    const hours = rule?.limit_count == null ? "" : Math.round((Number(rule.window_seconds) / 3600) * 100) / 100;
    return `<div class="quota-lane" data-rule-key="${escapeHtml(selection.key)}" data-family="${escapeHtml(selection.family)}" data-enabled="${String(Boolean(rule?.enabled))}">
      <label class="lane-toggle"><input type="checkbox" data-rule-enabled ${rule?.enabled ? "checked" : ""}/><span><strong>${escapeHtml(selection.version_label)}</strong><small>${escapeHtml(selection.level_label)}</small></span></label>
      <label class="compact-field"><span>次数</span><input type="number" min="1" step="1" data-rule-limit placeholder="不限" value="${limit}"/></label>
      <label class="compact-field"><span>窗口（小时）</span><input type="number" min="0.02" max="8784" step="0.5" data-rule-window placeholder="—" value="${hours}"/></label>
      <label class="compact-field"><span>到限后</span><select data-rule-fallback><option value="">停止使用</option>${sameFamily.map((item) => `<option value="${escapeHtml(item.key)}" ${rule?.fallback_key === item.key ? "selected" : ""}>${escapeHtml(item.version_label)} · ${escapeHtml(item.level_label)}</option>`).join("")}</select></label>
    </div>`;
  };

  const syncRuleRow = (row) => {
    const enabled = row.querySelector("[data-rule-enabled]").checked;
    const limited = row.querySelector("[data-rule-limit]").value.trim() !== "";
    row.dataset.enabled = String(enabled);
    row.querySelector("[data-rule-limit]").disabled = !enabled;
    row.querySelector("[data-rule-window]").disabled = !enabled || !limited;
    row.querySelector("[data-rule-fallback]").disabled = !enabled || !limited;
  };

  const groupUsersButton = (group, kind) => (
    Number(group?.member_count || 0) > 0
      ? `<button type="button" class="button-secondary" data-action="filter-group-users" data-group-kind="${escapeHtml(kind)}" data-group-id="${escapeHtml(group.id)}">查看并迁移 ${numberText(group.member_count)} 位用户</button>`
      : ""
  );

  const openResourceGroupDrawer = (groupId = "new") => {
    const data = state.chatAdmin;
    if (!data) return;
    const groups = data.resource_groups || data.groups || [];
    const template = groups.find((group) => group.id === "basic") || groups[0] || {};
    const source = groupId === "new"
      ? {
          id: "new",
          name: "",
          description: "",
          is_system: false,
          storage_quota_bytes: template.storage_quota_bytes || 2 * 1024 ** 3,
          max_concurrency: template.max_concurrency || 2,
          default_user_concurrency: template.default_user_concurrency || 1,
        }
      : groups.find((group) => group.id === groupId);
    if (!source) return toast("资源分组数据已变化，请刷新", "error");
    state.activeGroup = clone(source);
    const resourceUsersButton = groupUsersButton(source, "resource");
    const resourceDeleteButton = (
      groupId !== "new"
      && !source.is_system
      && Number(source.member_count || 0) === 0
    )
      ? '<button type="button" class="button-danger" data-action="delete-resource-group">删除资源组</button>'
      : "";
    openDrawer({
      eyebrow: groupId === "new" ? "NEW RESOURCE GROUP" : "RESOURCE GROUP",
      title: groupId === "new" ? "新建资源组" : source.name,
      body: `<form id="resource-group-control-form" data-group-id="${escapeHtml(groupId)}">
        <div class="drawer-section">
          <div class="drawer-section-heading"><div><h3>空间与并发</h3><p>资源组不决定 GPT 或 Claude 的模型权限</p></div><span class="count-pill">${numberText(source.member_count)} 位用户</span></div>
          <div class="field-grid">
            <label class="field"><span>资源组名称</span><input name="name" maxlength="40" required value="${escapeHtml(source.name || "")}" placeholder="例如：基础资源组"/></label>
            <label class="field"><span>说明</span><input name="description" maxlength="200" value="${escapeHtml(source.description || "")}" placeholder="空间与并发分配原则"/></label>
            <label class="field"><span>每位用户媒体空间（GB）</span><input name="storage_quota_gb" type="number" min="0" max="20480" step="0.5" required value="${escapeHtml(toGigabytes(source.storage_quota_bytes))}"/><small>降低额度不会删除已有文件。</small></label>
            <label class="field"><span>资源组最大并发</span><input name="max_concurrency" type="number" min="1" max="100" step="1" required value="${numberText(source.max_concurrency || 1)}"/><small>该资源组全部成员共享的请求上限。</small></label>
            <label class="field"><span>单用户默认并发</span><input name="default_user_concurrency" type="number" min="1" max="100" step="1" required value="${numberText(source.default_user_concurrency || 1)}"/><small>不能超过资源组最大并发。</small></label>
          </div>
        </div>
      </form>`,
      footer: `${resourceUsersButton}${resourceDeleteButton}<button type="button" class="button-secondary" data-close-drawer>取消</button><button type="button" class="button" data-action="save-resource-group">${groupId === "new" ? "创建资源组" : "保存资源组"}</button>`,
    });
  };

  const openGroupDrawer = (groupId = "new", requestedProvider = "gpt", copySourceId = "") => {
    const data = state.chatAdmin;
    if (!data) return;
    const modelGroups = data.model_groups || [];
    const existing = modelGroups.find((group) => group.id === groupId);
    const copySource = modelGroups.find((group) => group.id === copySourceId);
    const provider = existing?.provider_family || copySource?.provider_family || requestedProvider;
    if (!["gpt", "claude"].includes(provider)) return toast("未知 Provider", "error");
    const template = modelGroups.find((group) => group.id === `${provider}-basic`)
      || modelGroups.find((group) => group.provider_family === provider)
      || { rules: [] };
    const source = groupId === "new"
      ? {
          id: "new",
          provider_family: provider,
          name: copySource ? `${copySource.name} 副本` : "",
          description: copySource?.description || "",
          is_system: false,
          is_plan_template: false,
          account_pool_id: copySource?.account_pool_id || template.account_pool_id || `${provider}-default`,
          rules: clone(copySource?.rules || template.rules || []),
        }
      : existing;
    if (!source) return toast("模型分组数据已变化，请刷新", "error");
    const isPlanTemplate = Boolean(source.is_plan_template && groupId !== "new");
    state.activeGroup = clone(source);
    const modelUsersButton = groupUsersButton(source, provider);
    const modelDeleteButton = (
      groupId !== "new"
      && !source.is_system
      && Number(source.member_count || 0) === 0
    )
      ? '<button type="button" class="button-danger" data-action="delete-group">删除模型组</button>'
      : "";
    const selections = (data.selections || []).filter((selection) => selection.family === provider);
    const rules = groupRuleMap(source);
    const providerPresets = planPresetsFor(data, provider);
    const accountPools = accountPoolData().pools || [];
    const currentPoolId = source.account_pool_id || `${provider}-default`;
    const poolOptions = accountPools
      .filter((pool) => pool.provider === provider && (pool.enabled || pool.id === currentPoolId))
      .map((pool) => `<option value="${escapeHtml(pool.id)}" ${pool.id === currentPoolId ? "selected" : ""}>${escapeHtml(pool.name)}${pool.enabled ? "" : "（已停用）"}</option>`)
      .join("");

    openDrawer({
      eyebrow: isPlanTemplate ? "OFFICIAL PLAN TEMPLATE" : groupId === "new" ? `NEW ${provider.toUpperCase()} GROUP` : `${provider.toUpperCase()} POLICY GROUP`,
      title: groupId === "new" ? (copySource ? `复制 ${copySource.name}` : `新建 ${familyText(provider)} 额度组`) : source.name,
      body: `<form id="group-control-form" data-group-id="${escapeHtml(groupId)}" data-provider="${escapeHtml(provider)}">
        ${isPlanTemplate ? '<div class="info-banner"><i>模</i><div><strong>官方套餐基线模板</strong><br/>模板保持不变，便于随时对照。请点击“复制为自定义组”后再修改并分配。</div></div>' : ""}
        <div class="drawer-section"><div class="field-grid">
          <label class="field"><span>分组名称</span><input name="name" maxlength="40" required value="${escapeHtml(source.name || "")}" placeholder="例如：${escapeHtml(familyText(provider))} Pro 组"/></label>
          <label class="field"><span>说明</span><input name="description" maxlength="200" value="${escapeHtml(source.description || "")}" placeholder="该 Provider 的权限和额度原则"/></label>
          <label class="field"><span>${escapeHtml(familyText(provider))} 账号池</span><select name="account_pool_id" required>${poolOptions || `<option value="${escapeHtml(currentPoolId)}">${escapeHtml(currentPoolId)}</option>`}</select><small>决定该额度组的请求使用哪一个同 Provider 账号池。</small></label>
        </div></div>
        <div class="info-banner"><i>池</i><div><strong>账号池与额度组职责不同</strong><br/>账号池管理登录身份和独立 Worker；本页只把用户策略绑定到池，并配置档位、次数窗口与同 Provider 降级。</div></div>
        <div class="drawer-section">
          <div class="drawer-section-heading"><div><h3>${escapeHtml(familyText(provider))} 档位</h3><p>次数留空表示不限；自动降级只能留在当前 Provider</p></div>${familyBadge(provider)}</div>
          ${providerPresets.length ? `<div class="preset-bar"><span>套用 ${escapeHtml(familyText(provider))} 推荐配置</span>${providerPresets.map((preset) => `<button type="button" data-action="apply-group-preset" data-preset-id="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</button>`).join("")}</div>` : ""}
          ${providerPresets.length ? `<div class="info-banner"><i>额</i><div data-plan-preset-note>${planPresetNote(null, provider)}</div></div>` : ""}
          ${provider === "gpt" ? '<div class="info-banner"><i>Mini</i><div><strong>Mini 是官方透明兜底，不是可手选模型</strong><br/>GPT-5.5 Instant/Auto 到限后可能切到 GPT-5.5 Instant mini；GPT-5.6 推理到限后可能切到 GPT-5.4 Thinking mini。官方明确 Mini 不出现在模型选择器，本站因此只展示说明，不伪造独立档位。</div></div>' : ""}
          <div class="quota-lane-list">${selections.map((selection) => ruleRow(selection, rules.get(selection.key) || {}, selections)).join("")}</div>
        </div>
      </form>`,
      footer: isPlanTemplate
        ? `${modelUsersButton}<button type="button" class="button-secondary" data-close-drawer>关闭</button><button type="button" class="button" data-action="copy-group" data-group-id="${escapeHtml(source.id)}" data-provider="${escapeHtml(provider)}">复制为自定义组</button>`
        : `${modelUsersButton}${modelDeleteButton}<button type="button" class="button-secondary" data-close-drawer>取消</button><button type="button" class="button" data-action="save-group">${groupId === "new" ? "创建模型组" : "保存模型策略"}</button>`,
    });
    drawerBody.querySelectorAll("[data-rule-key]").forEach(syncRuleRow);
    if (isPlanTemplate) {
      drawerBody.querySelectorAll("input, select, textarea, button").forEach((control) => {
        control.disabled = true;
      });
    }
  };

  const applyGroupPreset = (presetId) => {
    const form = document.querySelector("#group-control-form");
    const provider = form?.dataset.provider || "gpt";
    const preset = planPresetsFor(state.chatAdmin, provider).find((item) => item.id === presetId);
    if (!preset) return;
    const byKey = new Map((preset.rules || []).map((rule) => [rule.selection_key, rule]));
    drawerBody.querySelectorAll(`[data-rule-key][data-family="${provider}"]`).forEach((row) => {
      const rule = byKey.get(row.dataset.ruleKey);
      if (!rule) return;
      row.querySelector("[data-rule-enabled]").checked = Boolean(rule.enabled);
      row.querySelector("[data-rule-limit]").value = rule.limit_count == null ? "" : String(rule.limit_count);
      row.querySelector("[data-rule-window]").value = rule.limit_count == null ? "" : String(Math.round((Number(rule.window_seconds) / 3600) * 100) / 100);
      row.querySelector("[data-rule-fallback]").value = rule.fallback_key || "";
      syncRuleRow(row);
    });
    const note = drawerBody.querySelector("[data-plan-preset-note]");
    if (note) note.innerHTML = planPresetNote(preset, provider);
    toast(`${preset.label} 建议已填入 ${familyText(provider)} 区域，保存后才会生效`);
  };

  const saveGroup = async (button) => {
    const form = document.querySelector("#group-control-form");
    if (!form) return;
    const values = new FormData(form);
    const rules = [...form.querySelectorAll("[data-rule-key]")].map((row) => {
      const enabled = row.querySelector("[data-rule-enabled]").checked;
      const limitValue = row.querySelector("[data-rule-limit]").value.trim();
      const limit = enabled && limitValue !== "" ? Number(limitValue) : null;
      const hours = Number(row.querySelector("[data-rule-window]").value);
      return {
        selection_key: row.dataset.ruleKey,
        enabled,
        limit_count: limit,
        window_seconds: limit == null ? 0 : Math.max(60, Math.round(hours * 3600)),
        fallback_key: enabled && limit != null ? row.querySelector("[data-rule-fallback]").value || null : null,
      };
    });
    const payload = {
      provider_family: form.dataset.provider,
      name: String(values.get("name") || "").trim(),
      description: String(values.get("description") || "").trim(),
      account_pool_id: String(values.get("account_pool_id") || `${form.dataset.provider}-default`),
      rules,
    };
    if (!payload.name) return toast("请填写分组名称", "error");
    button.disabled = true;
    try {
      const isNew = form.dataset.groupId === "new";
      await requestJson(isNew ? `${CHAT_API}/admin/model-groups` : `${CHAT_API}/admin/model-groups/${encodeURIComponent(form.dataset.groupId)}`, {
        method: isNew ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      clearCache("overview", "chat-admin");
      closeDrawer();
      await renderAccess();
      toast(isNew ? `${familyText(form.dataset.provider)} 额度组已创建` : "模型分组策略已保存");
    } catch (error) {
      toast(error?.message || "分组保存失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const deleteGroup = async (button) => {
    const form = document.querySelector("#group-control-form");
    if (!form || form.dataset.groupId === "new") return;
    if (!await confirmAction({
      title: "删除模型额度组",
      message: "确定删除这个分组吗？仍有成员时系统会拒绝删除。",
      confirmLabel: "确认删除",
      danger: true,
    })) return;
    button.disabled = true;
    try {
      await requestJson(`${CHAT_API}/admin/model-groups/${encodeURIComponent(form.dataset.groupId)}`, { method: "DELETE" });
      clearCache("overview", "chat-admin");
      closeDrawer();
      await renderAccess();
      toast("模型分组已删除");
    } catch (error) {
      toast(error?.message || "分组删除失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const saveResourceGroup = async (button) => {
    const form = document.querySelector("#resource-group-control-form");
    if (!form) return;
    const values = new FormData(form);
    const payload = {
      name: String(values.get("name") || "").trim(),
      description: String(values.get("description") || "").trim(),
      storage_quota_bytes: toBytes(values.get("storage_quota_gb")),
      max_concurrency: Number(values.get("max_concurrency")),
      default_user_concurrency: Number(values.get("default_user_concurrency")),
    };
    if (!payload.name) return toast("请填写资源组名称", "error");
    if (!Number.isInteger(payload.max_concurrency) || !Number.isInteger(payload.default_user_concurrency) || payload.default_user_concurrency > payload.max_concurrency) {
      return toast("单用户默认并发必须是正整数，且不能超过资源组最大并发", "error");
    }
    button.disabled = true;
    try {
      const isNew = form.dataset.groupId === "new";
      await requestJson(isNew ? `${CHAT_API}/admin/resource-groups` : `${CHAT_API}/admin/resource-groups/${encodeURIComponent(form.dataset.groupId)}`, {
        method: isNew ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      clearCache("overview", "chat-admin", "storage-users");
      closeDrawer();
      await renderAccess();
      toast(isNew ? "资源组已创建" : "资源组已保存");
    } catch (error) {
      toast(error?.message || "资源组保存失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const deleteResourceGroup = async (button) => {
    const form = document.querySelector("#resource-group-control-form");
    if (!form || form.dataset.groupId === "new") return;
    if (!await confirmAction({
      title: "删除资源组",
      message: "确定删除这个资源组吗？仍有成员时系统会拒绝删除。",
      confirmLabel: "确认删除",
      danger: true,
    })) return;
    button.disabled = true;
    try {
      await requestJson(`${CHAT_API}/admin/resource-groups/${encodeURIComponent(form.dataset.groupId)}`, { method: "DELETE" });
      clearCache("overview", "chat-admin", "storage-users");
      closeDrawer();
      await renderAccess();
      toast("资源组已删除");
    } catch (error) {
      toast(error?.message || "资源组删除失败", "error");
    } finally {
      button.disabled = false;
    }
  };

  const renderAccess = async (renderId = state.renderId) => {
    const [data, pools] = await Promise.all([
      loadChatAdmin(),
      loadAccountPools().catch(() => ({ pools: [], accounts: [], backend: "unavailable" })),
    ]);
    if (renderId !== state.renderId || state.route !== "access") return;
    state.chatAdmin = data;
    state.accountPools = pools;
    const poolNames = new Map((pools.pools || []).map((pool) => [pool.id, pool.name]));
    const resourceGroups = data.resource_groups || data.groups || [];
    const resourceCards = resourceGroups.map((group) => {
      return `<article class="group-card">
        <header><div><h3>${escapeHtml(group.name)}</h3><p>${escapeHtml(group.description || "未填写说明")}</p></div>${group.is_retired ? statusBadge("degraded", "旧版待迁移") : group.default_role ? statusBadge("ready", group.default_role === "admin" ? "默认管理员组" : "默认用户组") : `<span class="count-pill">${numberText(group.member_count)} 位用户</span>`}</header>
        <div class="group-summary" style="grid-template-columns:repeat(3,1fr)"><span><small>媒体空间</small><strong>${escapeHtml(bytes(group.storage_quota_bytes))}</strong></span><span><small>资源组并发</small><strong>${numberText(group.max_concurrency)}</strong></span><span><small>用户默认</small><strong>${numberText(group.default_user_concurrency)}</strong></span></div>
        <footer><span class="muted-copy">不控制模型权限</span><div class="row-actions">${groupUsersButton(group, "resource")}<button type="button" class="button-secondary" data-action="open-resource-group" data-group-id="${escapeHtml(group.id)}">编辑资源</button></div></footer>
      </article>`;
    }).join("");
    const providerSections = ["gpt", "claude"].map((family) => {
      const familyGroups = (data.model_groups || []).filter((group) => group.provider_family === family);
      const cards = familyGroups.map((group) => {
        const rules = group.rules || [];
        const enabled = rules.filter((rule) => rule.enabled).length;
        const fallbackCount = rules.filter((rule) => rule.fallback_key).length;
        return `<article class="group-card" data-plan-template="${String(Boolean(group.is_plan_template))}">
          <header><div><h3>${escapeHtml(group.name)}</h3><p>${escapeHtml(group.description || "未填写说明")}</p></div>${group.is_retired ? statusBadge("degraded", "旧版待迁移") : group.is_plan_template ? statusBadge("ready", "官方套餐模板") : group.default_role ? statusBadge("ready", group.default_role === "admin" ? "默认管理员组" : "默认用户组") : `<span class="count-pill">${numberText(group.member_count)} 位用户</span>`}</header>
          <div class="group-summary" style="grid-template-columns:repeat(3,1fr)"><span><small>开放档位</small><strong>${enabled} / ${rules.length}</strong></span><span><small>降级链</small><strong>${fallbackCount} 条</strong></span><span><small>${escapeHtml(familyText(family))} 账号池</small><strong>${escapeHtml(poolNames.get(group.account_pool_id) || group.account_pool_id || `${family}-default`)}</strong></span></div>
          <footer><div class="lane-preview" aria-label="档位启用预览" title="${escapeHtml(modelGroupDetail(group, data.selections || [], data))}">${rules.map((rule) => `<i data-enabled="${String(Boolean(rule.enabled))}"></i>`).join("")}</div><div class="row-actions">${groupUsersButton(group, family)}${group.is_plan_template ? `<button type="button" class="text-button" data-action="open-group" data-group-id="${escapeHtml(group.id)}" data-provider="${escapeHtml(family)}">查看数值</button><button type="button" class="button-secondary" data-action="copy-group" data-group-id="${escapeHtml(group.id)}" data-provider="${escapeHtml(family)}">复制后编辑</button>` : `<button type="button" class="button-secondary" data-action="open-group" data-group-id="${escapeHtml(group.id)}" data-provider="${escapeHtml(family)}">编辑模型策略</button>`}</div></footer>
        </article>`;
      }).join("");
      const groupScope = `每位用户最多选择一个组；该组必须关联一个 ${familyText(family)} 账号池`;
      return `<section class="access-policy-section">
        <header class="panel-heading"><div><h3>${escapeHtml(familyText(family))} 额度组</h3><p>${escapeHtml(groupScope)}</p></div><button type="button" class="button-secondary" data-action="open-group" data-group-id="new" data-provider="${escapeHtml(family)}">新建 ${escapeHtml(familyText(family))} 组</button></header>
        <div class="group-grid">${cards || '<div class="empty-state"><div><p>暂无模型分组</p></div></div>'}</div>
      </section>`;
    }).join("");
    content.innerHTML = `
      ${pageIntro("资源、账号池与模型策略分开组合", "资源组管理空间和并发；Provider 账号池管理登录；GPT/Claude 额度组分别绑定同 Provider 的账号池。", '<button type="button" class="button" data-action="open-resource-group" data-group-id="new">新建资源组</button>')}
      <div class="warning-banner"><i>额</i><div><strong>模型次数、自动降级与并发是 Turtle 站内调度规则</strong><br/>它们不是 OpenAI 或 Anthropic 的官方剩余额度；Provider 总并发仍会作为最外层保护。</div></div>
      <section class="access-policy-section"><header class="panel-heading"><div><h3>资源组</h3><p>每位用户唯一继承一份媒体空间和并发策略</p></div></header><div class="group-grid">${resourceCards || '<div class="empty-state"><div><p>暂无资源组</p></div></div>'}</div></section>
      ${providerSections}`;
  };

  const renderStorage = async (renderId = state.renderId) => {
    const [config, users, overview] = await Promise.all([loadStorageConfig(), loadStorageUsers(), loadOverview()]);
    if (renderId !== state.renderId || state.route !== "storage") return;
    const cos = config.cos || {};
    const cdn = config.cdn || {};
    const media = config.media || {};
    const isolation = config.media_isolation || {};
    content.innerHTML = `
      ${pageIntro("存储状态先于配置表单", "日常先确认 Provider、直传和 Media Pump；只有需要变更时再编辑连接与媒体参数。", '<button type="button" class="button-secondary" data-action="test-storage">测试当前存储</button>')}
      <section class="stat-grid">
        <article class="stat-card"><header><span>当前 Provider</span><i>STO</i></header><strong style="font-size:21px">${config.provider === "cos" ? "腾讯云 COS" : "本地磁盘"}</strong><footer>${cos.configured ? "COS 凭据已加密保存" : "COS 尚未配置"}</footer></article>
        <article class="stat-card"><header><span>已用媒体空间</span><i>USE</i></header><strong>${escapeHtml(bytes(overview.storage?.used_bytes))}</strong><footer>${users.items?.length || 0} 位用户已分配额度</footer></article>
        <article class="stat-card"><header><span>浏览器直传</span><i>PUT</i></header><strong style="font-size:21px">${overview.storage?.direct_upload ? "已启用" : "未启用"}</strong><footer>${cos.direct_upload_enabled ? "配置开关已打开" : "新媒体可能经应用接口"}</footer></article>
        <article class="stat-card"><header><span>媒体 CDN</span><i>CDN</i></header><strong style="font-size:21px">${cdn.enabled && cdn.files_ready && cdn.images_ready ? "双域名已启用" : "安全回退"}</strong><footer>${cdn.enabled ? "文件与缩略图分别鉴权" : "当前使用 COS 预签名 URL"}</footer></article>
        <article class="stat-card"><header><span>Media Pump</span><i>PMP</i></header><strong style="font-size:21px">${isolation.pump_configured ? "已连接" : "未配置"}</strong><footer>${isolation.strict ? "严格主机隔离已开启" : "严格隔离未开启"}</footer></article>
      </section>
      ${isolation.strict && !isolation.pump_configured ? '<div class="danger-banner"><i>!</i><div><strong>严格媒体隔离会失败关闭</strong><br/>在 Pump 配置完成前，模型图片输入和生成媒体持久化不会回退到应用主机搬运正文。</div></div>' : ""}

      <form id="storage-control-form">
        <section class="settings-grid">
          <article class="settings-card">
            <header><h3>文件保存位置</h3><p>切换只影响新文件，已有对象仍按原 Provider 读取</p></header>
            <div>
              <div class="choice-grid">
                <label class="choice-card"><input type="radio" name="provider" value="local" ${config.provider === "local" ? "checked" : ""}/><span><strong>本地存储</strong><small>适合本机和小规模使用</small></span></label>
                <label class="choice-card"><input type="radio" name="provider" value="cos" ${config.provider === "cos" ? "checked" : ""}/><span><strong>腾讯云 COS</strong><small>私有 Bucket 与浏览器直传</small></span></label>
              </div>
              <div class="field-grid" style="margin-top:14px">
                <label class="field"><span>地域</span><input name="region" placeholder="ap-tokyo" value="${escapeHtml(cos.region || "")}"/></label>
                <label class="field"><span>Bucket</span><input name="bucket" placeholder="turtle-gpt-1250000000" value="${escapeHtml(cos.bucket || "")}"/></label>
                <label class="field span-2"><span>Endpoint</span><input name="endpoint" placeholder="https://cos.ap-tokyo.myqcloud.com" value="${escapeHtml(cos.endpoint_url || "")}"/></label>
                <label class="field"><span>SecretId ${cos.secret_id_configured ? "· 已保存" : ""}</span><input name="secret_id" type="password" autocomplete="new-password" placeholder="${cos.secret_id_configured ? "留空保持不变" : "输入专用 SecretId"}"/></label>
                <label class="field"><span>SecretKey ${cos.secret_key_configured ? "· 已保存" : ""}</span><input name="secret_key" type="password" autocomplete="new-password" placeholder="${cos.secret_key_configured ? "留空保持不变" : "输入专用 SecretKey"}"/></label>
                <label class="field"><span>对象前缀</span><input name="prefix" value="${escapeHtml(cos.prefix || "")}"/></label>
                <label class="field"><span>寻址方式</span><select name="addressing"><option value="virtual" ${cos.addressing_style === "virtual" ? "selected" : ""}>Virtual-hosted</option><option value="path" ${cos.addressing_style === "path" ? "selected" : ""}>Path</option></select></label>
              </div>
              <label class="switch-row"><span><strong>浏览器直传 COS</strong><small>需要 Bucket CORS 允许 PUT、GET、HEAD 与 Content-Type</small></span><input name="direct" type="checkbox" ${cos.direct_upload_enabled ? "checked" : ""}/></label>
            </div>
          </article>

          <article class="settings-card">
            <header><h3>媒体 CDN</h3><p>原文件与静态缩略图使用独立路径和域名</p></header>
            <div class="field-grid">
              <label class="field span-2"><span>原文件 CDN</span><input name="files_cdn_url" placeholder="https://files.chat.totools.cn" value="${escapeHtml(cdn.files_base_url || "https://files.chat.totools.cn")}"/></label>
              <label class="field span-2"><span>图片缩略图 CDN</span><input name="images_cdn_url" placeholder="https://img.chat.totools.cn" value="${escapeHtml(cdn.images_base_url || "https://img.chat.totools.cn")}"/></label>
              <label class="field"><span>文件 Type A 密钥 ${cdn.files_auth_key_configured ? "· 已保存" : ""}</span><input name="files_cdn_key" type="password" autocomplete="new-password" placeholder="${cdn.files_auth_key_configured ? "留空保持不变" : "控制台鉴权主密钥"}"/></label>
              <label class="field"><span>图片 Type A 密钥 ${cdn.images_auth_key_configured ? "· 已保存" : ""}</span><input name="images_cdn_key" type="password" autocomplete="new-password" placeholder="${cdn.images_auth_key_configured ? "留空保持不变" : "控制台鉴权主密钥"}"/></label>
              <label class="field"><span>鉴权有效期（秒）</span><input name="cdn_auth_ttl" type="number" min="60" max="86400" value="${Number(cdn.auth_ttl_seconds) || 900}"/></label>
              <div class="system-row"><span class="count-pill">Type A</span><div><strong>签名参数固定为 sign，鉴权后缀填 *</strong><small>* 表示全部文件；有效期必须与腾讯 CDN 控制台一致。密钥加密保存且 API 永不回显。</small></div></div>
            </div>
            <label class="switch-row"><span><strong>启用双 CDN 安全访问</strong><small>只有两个域名、两套密钥都完整时才能开启；下载附件仍使用 COS 签名以保留文件名。</small></span><input name="cdn_enabled" type="checkbox" ${cdn.enabled ? "checked" : ""}/></label>
          </article>

          <article class="settings-card">
            <header><h3>媒体空间额度</h3><p>容量策略已从存储连接配置中拆出</p></header>
            <div class="system-list">
              <div class="system-row"><span class="count-pill">分组</span><div><strong>在“分组策略”中统一配置</strong><small>用户按所属分组继承空间大小；这里不再维护会员等级或个人覆盖，避免两套规则互相覆盖。</small></div></div>
            </div>
            <div class="form-actions"><span>调整空间不会删除已有文件</span><button type="button" class="button-secondary" data-route="access">打开分组策略</button></div>
          </article>

          <article class="settings-card">
            <header><h3>媒体处理与单文件限制</h3><p>浏览器压缩只在结果更小时替换原图</p></header>
            <div class="field-grid">
              <label class="field"><span>图片最长边（px）</span><input name="max_dimension" type="number" min="512" max="8192" value="${Number(media.max_image_dimension) || 2048}"/></label>
              <label class="field"><span>WebP 质量</span><input name="image_quality" type="number" min="0.4" max="0.98" step="0.01" value="${Number(media.image_quality) || 0.82}"/></label>
              <label class="field"><span>图片上限（MB）</span><input name="max_image_mb" type="number" min="1" max="200" value="${Math.round((Number(media.max_image_bytes) || 0) / 1024 ** 2)}"/></label>
              <label class="field"><span>视频上限（MB）</span><input name="max_video_mb" type="number" min="1" max="5120" value="${Math.round((Number(media.max_video_bytes) || 0) / 1024 ** 2)}"/></label>
              <label class="field"><span>ZIP 文件上限（MB）</span><input name="max_file_mb" type="number" min="1" max="1024" value="${Math.round((Number(media.max_file_bytes) || 0) / 1024 ** 2) || 200}"/></label>
            </div>
          </article>

          <article class="settings-card">
            <header><h3>静态缩略图</h3><p>最长边 480px，生成一次后直接从图片 CDN/COS 读取</p></header>
            <div class="system-list">
              <div class="system-row"><span class="count-pill">免费</span><div><strong>不调用腾讯云图片处理</strong><small>新上传和新生成图片由浏览器立即生成 WebP 缩略图；原图保持不变。</small></div></div>
              <div class="system-row"><span class="count-pill">迁移</span><div><strong>历史图片需要一次性回填</strong><small>回填在当前管理员浏览器中顺序处理，不经过应用主机，也不会在用户打开列表时集中处理。</small></div></div>
            </div>
            <div class="form-actions"><span>运行期间请保持本页打开</span><button type="button" class="button-secondary" data-action="backfill-thumbnails">回填历史缩略图</button></div>
          </article>

          <article class="settings-card">
            <header><h3>媒体链路边界</h3><p>应用主机只处理签名控制信息，不搬运媒体正文</p></header>
            <div class="system-list">
              <div class="system-row">${statusBadge(isolation.strict ? "ready" : "degraded", isolation.strict ? "已开启" : "未开启")}<div><strong>严格外部媒体模式</strong><small>缺少 Pump 时失败关闭，不回退到主机上传</small></div></div>
              <div class="system-row">${statusBadge(isolation.pump_configured ? "ready" : "planned", isolation.pump_configured ? "已连接" : "待配置")}<div><strong>Cloudflare Media Pump</strong><small>短期能力、签名 URL 与私有 COS 之间搬运字节</small></div></div>
              <div class="system-row">${statusBadge(cos.configured ? "ready" : "planned", cos.configured ? "已保存" : "待配置")}<div><strong>COS 专用凭据</strong><small>Secret 只允许覆盖输入，API 永不回显</small></div></div>
            </div>
          </article>
        </section>
        <div class="form-actions"><span>保存前请确认 Bucket CORS 和稳定加密主密钥</span><button type="button" class="button-secondary" data-action="test-storage">测试已保存配置</button><button type="button" class="button" data-action="save-storage">保存存储设置</button></div>
      </form>`;
  };

  const saveStorage = async (button) => {
    const form = document.querySelector("#storage-control-form");
    if (!form) return;
    const values = new FormData(form);
    const region = String(values.get("region") || "").trim();
    const endpoint = String(values.get("endpoint") || "").trim() || (region ? `https://cos.${region}.myqcloud.com` : "");
    const payload = {
      provider: values.get("provider"),
      cos: {
        region,
        bucket: String(values.get("bucket") || "").trim(),
        endpoint_url: endpoint,
        prefix: String(values.get("prefix") || "").trim(),
        addressing_style: values.get("addressing"),
        direct_upload_enabled: values.get("direct") === "on",
        secret_id: values.get("secret_id"),
        secret_key: values.get("secret_key"),
      },
      cdn: {
        enabled: values.get("cdn_enabled") === "on",
        files_base_url: String(values.get("files_cdn_url") || "").trim(),
        images_base_url: String(values.get("images_cdn_url") || "").trim(),
        files_auth_key: values.get("files_cdn_key"),
        images_auth_key: values.get("images_cdn_key"),
        auth_ttl_seconds: Number(values.get("cdn_auth_ttl")),
      },
      media: {
        max_image_dimension: Number(values.get("max_dimension")),
        image_quality: Number(values.get("image_quality")),
        max_image_bytes: Math.round(Number(values.get("max_image_mb")) * 1024 ** 2),
        max_video_bytes: Math.round(Number(values.get("max_video_mb")) * 1024 ** 2),
        max_file_bytes: Math.round(Number(values.get("max_file_mb")) * 1024 ** 2),
      },
    };
    button.disabled = true;
    button.textContent = "正在保存…";
    try {
      await requestJson(`${STORAGE_API}/admin/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      clearCache("overview", "storage-config", "storage-users");
      await renderStorage();
      toast("存储设置已保存");
    } catch (error) {
      toast(error?.message || "存储设置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存存储设置";
    }
  };

  const testStorage = async (button) => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "正在测试…";
    try {
      const result = await requestJson(`${STORAGE_API}/admin/test`, { method: "POST" });
      toast(result?.message || "存储连接正常");
    } catch (error) {
      toast(error?.message || "存储连接测试失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  const backfillThumbnailItem = async (file) => {
    const source = await requestJson(
      `${STORAGE_API}/files/${encodeURIComponent(file.id)}/url?variant=original`,
    );
    const response = await originalFetch(source.url, source.direct
      ? { credentials: "omit", mode: "cors", cache: "no-store" }
      : { headers: authHeaders(), credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`原图读取失败（HTTP ${response.status}）`);
    const thumbnail = await createStaticThumbnail(await response.blob());
    const ticket = await requestJson(`${STORAGE_API}/thumbnails/presign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: file.id,
        content_type: STATIC_THUMBNAIL.content_type,
        size: thumbnail.blob.size,
        width: thumbnail.width,
        height: thumbnail.height,
      }),
    });
    if (ticket.ready) return;
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
      await requestJson(`${STORAGE_API}/thumbnails/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: file.id }),
      });
    } catch (error) {
      await requestJson(`${STORAGE_API}/thumbnails/${encodeURIComponent(file.id)}`, {
        method: "DELETE",
      }).catch(() => {});
      throw error;
    }
  };

  const backfillThumbnails = async (button) => {
    if (state.thumbnailBackfillRunning) return;
    if (!await confirmAction({
      title: "回填历史缩略图",
      message: "将由当前浏览器顺序读取历史原图、生成缩略图并直传 COS。期间请保持页面打开。",
      confirmLabel: "开始回填",
    })) return;
    state.thumbnailBackfillRunning = true;
    button.disabled = true;
    const originalLabel = button.textContent;
    let cursor = "";
    let completed = 0;
    let failed = 0;
    try {
      do {
        const query = new URLSearchParams({ limit: "24" });
        if (cursor) query.set("cursor", cursor);
        const batch = await requestJson(`${STORAGE_API}/admin/thumbnails/missing?${query.toString()}`);
        for (const file of batch.items || []) {
          button.textContent = `正在回填 ${completed + failed + 1}…`;
          try {
            await backfillThumbnailItem(file);
            completed += 1;
          } catch (_error) {
            failed += 1;
          }
        }
        cursor = batch.has_more ? String(batch.next_cursor || "") : "";
      } while (cursor);
      clearCache("overview", "storage-users");
      toast(
        failed ? `已生成 ${completed} 张缩略图，${failed} 张失败，可稍后重试` : `已生成 ${completed} 张历史缩略图`,
        failed ? "error" : "success",
      );
    } catch (error) {
      toast(error?.message || "历史缩略图回填失败", "error");
    } finally {
      state.thumbnailBackfillRunning = false;
      button.disabled = false;
      button.textContent = originalLabel;
    }
  };

  const renderAnnouncements = async (renderId = state.renderId) => {
    const bundle = await loadAnnouncementAdmin();
    if (renderId !== state.renderId || state.route !== "announcements") return;
    const announcements = Array.isArray(bundle.announcements) ? bundle.announcements : [];
    let announcement = null;
    if (!state.announcementCreateMode) {
      announcement = announcements.find(
        (item) => item.id === state.announcementSelectedId,
      ) || announcements[0] || null;
      state.announcementSelectedId = String(announcement?.id || "");
    }
    const isNew = state.announcementCreateMode || !announcement;
    if (isNew) announcement = {};
    const revision = Number(announcement.revision || 0);
    const enabled = Boolean(announcement.enabled);
    const selectedIndex = announcements.findIndex(
      (item) => item.id === announcement.id,
    );
    content.innerHTML = `
      ${pageIntro(
        "公告管理",
        "创建多条 Markdown 公告；每条公告独立启停、迭代版本和记录已读，用户会依次看到所有未读公告。",
      )}
      <section class="announcement-management-grid">
        <article class="settings-card announcement-list-card">
          <header>
            <div><h3>公告列表</h3><p>${numberText(announcements.length)} 条，启用 ${numberText(announcements.filter((item) => item.enabled).length)} 条</p></div>
            <button type="button" class="button" data-action="new-announcement">新建公告</button>
          </header>
          <div class="announcement-list">
            ${announcements.length ? announcements.map((item) => `
              <button
                type="button"
                class="announcement-list-item"
                data-action="select-announcement"
                data-announcement-id="${escapeHtml(item.id)}"
                data-selected="${!isNew && item.id === announcement.id ? "true" : "false"}"
                aria-pressed="${!isNew && item.id === announcement.id ? "true" : "false"}"
              >
                <span data-state="${item.enabled ? "ready" : "planned"}">${item.enabled ? "展示中" : "已停用"}</span>
                <strong>${escapeHtml(item.title || "未命名公告")}</strong>
                <small>版本 ${numberText(item.revision)} · ${escapeHtml(dateTime(item.updated_at))}</small>
              </button>`).join("") : `
              <div class="announcement-list-empty"><i>告</i><strong>还没有公告</strong><span>从订阅说明或使用规则开始</span></div>`}
          </div>
        </article>
        <div class="announcement-workspace">
          <section class="announcement-admin-grid">
            <article class="settings-card announcement-editor-card">
              <header><h3>${isNew ? "新建公告" : "编辑公告"}</h3><p>用于说明如何订阅、使用规则、维护安排或重要更新</p></header>
              <form id="announcement-form" data-announcement-id="${escapeHtml(announcement.id || "")}">
                <div class="auth-security-status">
                  ${statusBadge(enabled ? "ready" : "planned", enabled ? "正在展示" : isNew ? "新草稿" : "已停用")}
                  <span class="count-pill">${revision ? `版本 ${numberText(revision)}` : "尚未发布"}</span>
                  <span class="count-pill">${announcement.updated_at ? `更新于 ${escapeHtml(dateTime(announcement.updated_at))}` : "等待首次保存"}</span>
                </div>
                <label class="switch-row">
                  <span><strong>启用公告</strong><small>每条公告独立启停；启用或修改会生成该公告的新版本</small></span>
                  <input name="enabled" type="checkbox" ${enabled ? "checked" : ""}/>
                </label>
                <label class="field"><span>公告标题</span><input name="title" maxlength="120" required placeholder="例如：如何订阅 Turtle’s Chat" value="${escapeHtml(announcement.title || "")}"/></label>
                <label class="field"><span>Markdown 正文</span><textarea name="body_markdown" maxlength="20000" rows="18" spellcheck="true" placeholder="# 如何订阅&#10;&#10;1. 联系管理员选择套餐&#10;2. 完成开通后刷新页面&#10;&#10;[查看订阅说明](https://example.com)">${escapeHtml(announcement.body_markdown || "")}</textarea><small>支持标题、粗体、列表、表格、引用、代码和链接；原始 HTML、脚本及远程图片不会执行。</small></label>
                <div class="form-actions">
                  <span>用户关闭后只记录这一条的当前版本；修改后会再次提醒</span>
                  <div class="announcement-form-buttons">
                    ${!isNew ? `<button type="button" class="button-danger" data-action="delete-announcement" data-announcement-id="${escapeHtml(announcement.id)}">删除</button>` : ""}
                    <button type="button" class="button" data-action="save-announcement">${isNew ? enabled ? "创建并发布" : "保存草稿" : enabled ? "保存并发布新版本" : "保存公告设置"}</button>
                  </div>
                </div>
              </form>
            </article>
            <article class="settings-card announcement-preview-card">
              <header><h3>用户侧预览</h3><p>与聊天页公告弹窗使用同一份服务端安全渲染结果</p></header>
              <div class="announcement-preview-shell">
                <div class="announcement-preview-label">ANNOUNCEMENT · ${revision ? `V${numberText(revision)}` : "DRAFT"}</div>
                <h4 data-announcement-preview-title>${escapeHtml(announcement.title || "公告标题预览")}</h4>
                <div class="announcement-markdown-preview" data-announcement-preview-body>${announcement.html || '<p class="muted-copy">输入 Markdown 后将在这里预览。</p>'}</div>
                <footer><span>${selectedIndex >= 0 ? `列表第 ${numberText(selectedIndex + 1)} 条` : "新公告预览"}</span><button type="button" disabled>我知道了</button></footer>
              </div>
              <div class="info-banner"><i>i</i><div><strong>每条公告独立版本化已读</strong><br/>已读记录保存在 PostgreSQL；同一用户可以读过订阅说明，同时仍收到新的维护公告。</div></div>
            </article>
          </section>
        </div>
      </section>`;
    updateGlobalStatus(await loadOverview());
  };

  const refreshAnnouncementPreview = async () => {
    const form = document.querySelector("#announcement-form");
    if (!form || state.route !== "announcements") return;
    const title = String(new FormData(form).get("title") || "").trim();
    const body = String(new FormData(form).get("body_markdown") || "");
    const titleElement = document.querySelector("[data-announcement-preview-title]");
    const bodyElement = document.querySelector("[data-announcement-preview-body]");
    if (titleElement) titleElement.textContent = title || "公告标题预览";
    if (!bodyElement) return;
    const generation = ++state.announcementPreviewGeneration;
    try {
      const payload = await requestJson(`${CHAT_API}/admin/announcements/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body_markdown: body }),
      });
      if (
        generation !== state.announcementPreviewGeneration
        || state.route !== "announcements"
        || !bodyElement.isConnected
      ) return;
      bodyElement.innerHTML = payload.html || '<p class="muted-copy">输入 Markdown 后将在这里预览。</p>';
    } catch (error) {
      if (generation !== state.announcementPreviewGeneration || !bodyElement.isConnected) return;
      bodyElement.innerHTML = `<p class="error-copy">${escapeHtml(error?.message || "预览暂时无法生成")}</p>`;
    }
  };

  const scheduleAnnouncementPreview = () => {
    window.clearTimeout(state.announcementPreviewTimer);
    state.announcementPreviewTimer = window.setTimeout(
      () => void refreshAnnouncementPreview(),
      260,
    );
  };

  const saveAnnouncement = async (button) => {
    const form = document.querySelector("#announcement-form");
    if (!form) return;
    const announcementId = String(form.dataset.announcementId || "");
    const isNew = !announcementId;
    const values = new FormData(form);
    const payload = {
      title: String(values.get("title") || "").trim(),
      body_markdown: String(values.get("body_markdown") || "").trim(),
      enabled: values.get("enabled") === "on",
    };
    if (!payload.title) return toast("请填写公告标题", "error");
    if (payload.enabled && !payload.body_markdown) return toast("启用公告前请填写 Markdown 正文", "error");
    const confirmed = await confirmAction({
      title: isNew
        ? payload.enabled ? "创建并发布公告" : "保存公告草稿"
        : payload.enabled ? "发布公告新版本" : "保存并停用公告",
      message: payload.enabled
        ? "保存后，尚未读过这一条新版本的用户会在下次进入聊天页时看到它；其他公告不受影响。"
        : "这条公告不会在用户侧显示，其他已启用公告仍会正常展示。",
      confirmLabel: payload.enabled ? "确认发布" : "确认保存",
      danger: !isNew && !payload.enabled,
    });
    if (!confirmed) return;
    button.disabled = true;
    button.textContent = payload.enabled ? "正在发布…" : "正在保存…";
    try {
      const result = await requestJson(
        isNew
          ? `${CHAT_API}/admin/announcements`
          : `${CHAT_API}/admin/announcements/${encodeURIComponent(announcementId)}`,
        {
        method: isNew ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        },
      );
      clearCache("announcement-admin");
      state.announcementCreateMode = false;
      state.announcementSelectedId = String(result.announcement?.id || "");
      await renderAnnouncements();
      const changed = result.announcement?.changed !== false;
      toast(
        changed
          ? payload.enabled
            ? `${isNew ? "公告已创建，" : ""}版本 ${numberText(result.announcement?.revision)} 已发布`
            : isNew ? "公告草稿已保存" : "公告已停用"
          : "内容没有变化，无需生成新版本",
        changed ? "success" : "info",
      );
    } catch (error) {
      toast(error?.message || "公告保存失败", "error");
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = isNew ? "保存公告" : "保存公告设置";
      }
    }
  };

  const newAnnouncement = async () => {
    state.announcementCreateMode = true;
    state.announcementSelectedId = "";
    await renderAnnouncements();
    document.querySelector("#announcement-form input[name='title']")?.focus();
  };

  const selectAnnouncement = async (button) => {
    const announcementId = String(button.dataset.announcementId || "");
    if (!announcementId) return;
    state.announcementCreateMode = false;
    state.announcementSelectedId = announcementId;
    await renderAnnouncements();
  };

  const deleteAnnouncement = async (button) => {
    const announcementId = String(button.dataset.announcementId || "");
    if (!announcementId) return;
    const selected = (await loadAnnouncementAdmin()).announcements?.find(
      (item) => item.id === announcementId,
    );
    if (!selected) return toast("公告不存在或已删除", "error");
    if (!await confirmAction({
      title: "删除公告",
      message: `确定删除“${selected.title || "未命名公告"}”吗？删除后用户侧立即隐藏，历史已读记录保留用于审计。`,
      confirmLabel: "确认删除",
      danger: true,
    })) return;
    button.disabled = true;
    try {
      await requestJson(
        `${CHAT_API}/admin/announcements/${encodeURIComponent(announcementId)}`,
        { method: "DELETE" },
      );
      clearCache("announcement-admin");
      state.announcementCreateMode = false;
      state.announcementSelectedId = "";
      await renderAnnouncements();
      toast("公告已删除", "success");
    } catch (error) {
      toast(error?.message || "公告删除失败", "error");
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  };

  const renderSystem = async (renderId = state.renderId) => {
    const [overview, version, ready, authSecurity, upstreamCleanup] = await Promise.all([
      loadOverview(),
      loadVersion().catch(() => ({ version: "未知" })),
      requestJson("/ready").catch(() => ({ status: false })),
      loadAuthSecurityConfig(),
      loadUpstreamCleanup().catch(() => ({
        enabled: false,
        execute: false,
        policy: {
          retention_seconds: 30 * 24 * 60 * 60,
          conversation_action: "delete",
        },
      })),
    ]);
    if (renderId !== state.renderId || state.route !== "system") return;
    const configuration = overview.configuration || {};
    const secretConfigured = Boolean(authSecurity.turnstile_secret_key_configured);
    const cleanupPolicy = upstreamCleanup.policy || {};
    const cleanupHours = Math.round((Number(cleanupPolicy.retention_seconds || 0) / 3600) * 100) / 100;
    content.innerHTML = `
      ${pageIntro("系统边界与高级设置", "注册、维护模式和上游对象保留策略在这里持久管理；日常用户、额度和存储设置仍在各自模块。")}
      <section class="settings-grid">
        <article class="settings-card auth-security-card">
          <header><h3>登录、注册与维护模式</h3><p>关闭注册只隐藏并拒绝新账号，已注册用户仍可正常登录</p></header>
          <form id="auth-security-form">
            <div class="auth-security-status">
              ${statusBadge(authSecurity.registration_enabled ? "ready" : "planned", authSecurity.registration_enabled ? "注册已开放" : "注册已关闭")}
              ${statusBadge(authSecurity.maintenance_enabled ? "degraded" : "ready", authSecurity.maintenance_enabled ? "维护中" : "正常开放")}
              ${statusBadge(authSecurity.turnstile_enabled ? (secretConfigured ? "ready" : "degraded") : "planned", authSecurity.turnstile_enabled ? (secretConfigured ? "Turnstile 已启用" : "Turnstile 配置不完整") : "Turnstile 未启用")}
              <span class="count-pill">${escapeHtml(authSecurity.storage_backend === "postgresql" ? "PostgreSQL 持久化" : "文件持久化")}</span>
            </div>
            <label class="switch-row">
              <span><strong>允许新用户注册</strong><small>关闭后服务端会直接拒绝新账号；首个管理员注册完成时会自动关闭</small></span>
              <input name="registration_enabled" type="checkbox" ${authSecurity.registration_enabled ? "checked" : ""}/>
            </label>
            <label class="switch-row">
              <span><strong>开启全站维护模式</strong><small>管理员仍可进入；其他已登录用户显示下面的维护信息，服务端同时拒绝操作请求。</small></span>
              <input name="maintenance_enabled" type="checkbox" ${authSecurity.maintenance_enabled ? "checked" : ""}/>
            </label>
            <label class="field"><span>维护信息</span><textarea name="maintenance_message" maxlength="800" rows="3" required placeholder="告诉用户维护原因和预计恢复时间">${escapeHtml(authSecurity.maintenance_message || "系统正在维护，请稍后再试。")}</textarea></label>
            <label class="switch-row">
              <span><strong>启用 Cloudflare Turnstile</strong><small>启用后注册按钮必须先取得一次性验证 Token，后端再向 Cloudflare 复核</small></span>
              <input name="turnstile_enabled" type="checkbox" ${authSecurity.turnstile_enabled ? "checked" : ""}/>
            </label>
            <div class="field-grid">
              <label class="field"><span>Site Key</span><input name="turnstile_site_key" autocomplete="off" spellcheck="false" placeholder="填写 Cloudflare Turnstile Site Key" value="${escapeHtml(authSecurity.turnstile_site_key || "")}"/></label>
              <label class="field"><span>Secret Key ${secretConfigured ? "· 已加密保存" : ""}</span><input name="turnstile_secret_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="${secretConfigured ? "留空保持不变" : "填写 Cloudflare Turnstile Secret Key"}"/></label>
            </div>
            <div class="form-actions"><span>Secret 只在本次 HTTPS 保存请求中提交，接口和页面都不会回显</span><button type="button" class="button" data-action="save-auth-security">保存访问设置</button></div>
          </form>
        </article>

        <article class="settings-card">
          <header><h3>ChatGPT 上游对话保留策略</h3><p>只处理 Turtle 精确记录的对象 ID，不会扫描账号中的其他对话</p></header>
          <form id="upstream-cleanup-form">
            <div class="auth-security-status">
              ${statusBadge(upstreamCleanup.enabled ? "ready" : "degraded", upstreamCleanup.enabled ? "清理服务已启用" : "清理服务未启用")}
              ${statusBadge(upstreamCleanup.execute ? "ready" : "planned", upstreamCleanup.execute ? "执行模式" : "仅演练")}
              <span class="count-pill">当前 ${escapeHtml(windowText(cleanupPolicy.retention_seconds))}</span>
            </div>
            <div class="field-grid">
              <label class="field"><span>保留时长（小时）</span><input name="retention_hours" type="number" min="0.1" max="8760" step="0.1" required value="${escapeHtml(cleanupHours || 720)}"/><small>最短约 6 分钟；修改后会重算尚未处理对象的到期时间。</small></label>
              <label class="field"><span>对话到期后</span><select name="conversation_action"><option value="archive" ${cleanupPolicy.conversation_action === "archive" ? "selected" : ""}>归档（仍保留在上游）</option><option value="delete" ${cleanupPolicy.conversation_action === "delete" ? "selected" : ""}>直接删除</option></select><small>输入文件和生成资源仍按精确 ID 删除，不受此选项影响。</small></label>
            </div>
            <div class="form-actions"><span>本地删除聊天时仍会立即进入清理队列</span><button type="button" class="button" data-action="save-upstream-cleanup" ${upstreamCleanup.enabled ? "" : "disabled"}>保存清理策略</button></div>
          </form>
        </article>

        <article class="settings-card"><header><h3>运行组件</h3><p>当前管理后台依赖的核心服务</p></header><div class="system-list">
          <div class="system-row">${statusBadge(ready.status ? "ready" : "degraded", ready.status ? "就绪" : "异常")}<div><strong>Open WebUI ${escapeHtml(version.version || "")}</strong><small>认证、用户、聊天记录与统一入口</small></div></div>
          <div class="system-row">${statusBadge(configuration.database === "postgresql" ? "ready" : "degraded", configuration.database === "postgresql" ? "PostgreSQL" : "嵌入式")}<div><strong>结构化数据</strong><small>用户、对话、配额和 Turtle 账本的持久化来源</small></div></div>
          <div class="system-row">${statusBadge(configuration.redis_configured ? "ready" : "planned", configuration.redis_configured ? "已配置" : "未配置")}<div><strong>Redis 协调层</strong><small>WebSocket 与短期协调，不作为用户数据备份</small></div></div>
        </div></article>

        <article class="settings-card"><header><h3>凭据与配置来源</h3><p>控制台不扩大 Secret 的暴露面</p></header><div class="system-list">
          <div class="system-row"><span class="count-pill">Secret</span><div><strong>Provider API 与网页登录认证</strong><small>只存在部署环境或隔离认证目录；控制台仅显示健康状态</small></div></div>
          <div class="system-row"><span class="count-pill">加密</span><div><strong>COS Secret</strong><small>只允许覆盖，不回显；加密主材料必须在重建后保持稳定</small></div></div>
          <div class="system-row"><span class="count-pill">只读</span><div><strong>监控数据</strong><small>仅聚合路由、状态和时间，不保存提示词或回答</small></div></div>
        </div></article>

        <article class="settings-card"><header><h3>高级 Open WebUI 设置</h3><p>低频功能继续保留在原生页面，避免污染日常控制台</p></header><div>
          <div class="warning-banner" style="margin-top:0"><i>!</i><div><strong>部署托管项不要在网页里临时改</strong><br/>ENABLE_PERSISTENT_CONFIG=false；Provider 路由和关键开关应以 Compose / Secret 为准。</div></div>
          <div class="form-actions"><span>用于模型展示、原生功能等低频配置</span><a class="button-secondary" href="/admin/settings">打开原生高级设置</a></div>
        </div></article>

        <article class="settings-card"><header><h3>发布门禁</h3><p>当前完成不等于允许公网发布</p></header><div class="system-list">
          <div class="system-row"><span class="count-pill">本地</span><div><strong>本机与少量受信用户</strong><small>继续按真实路由、权限、存储和重启门禁验收</small></div></div>
          <div class="system-row"><span class="count-pill">公网</span><div><strong>上游许可与合规仍是阻断项</strong><small>技术隔离、认证和额度不能替代上游授权</small></div></div>
          <div class="system-row"><span class="count-pill">远程</span><div><strong>日本共享服务器需独立盘点</strong><small>不得修改、重启或复用任何 Sub2 资源</small></div></div>
        </div></article>
      </section>`;
    updateGlobalStatus(overview);
  };

  const saveAuthSecurity = async (button) => {
    const form = document.querySelector("#auth-security-form");
    if (!form) return;
    const values = new FormData(form);
    const payload = {
      registration_enabled: values.get("registration_enabled") === "on",
      maintenance_enabled: values.get("maintenance_enabled") === "on",
      maintenance_message: String(values.get("maintenance_message") || "").trim(),
      turnstile_enabled: values.get("turnstile_enabled") === "on",
      turnstile_site_key: String(values.get("turnstile_site_key") || "").trim(),
      turnstile_secret_key: String(values.get("turnstile_secret_key") || "").trim(),
    };
    button.disabled = true;
    button.textContent = "正在验证并保存…";
    try {
      await requestJson(`${AUTH_API}/admin/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      clearCache("auth-security-config");
      await renderSystem();
      toast("登录、注册与维护设置已保存");
    } catch (error) {
      toast(error?.message || "访问设置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存访问设置";
    }
  };

  const saveUpstreamCleanup = async (button) => {
    const form = document.querySelector("#upstream-cleanup-form");
    if (!form) return;
    const values = new FormData(form);
    const hours = Number(values.get("retention_hours"));
    const retentionSeconds = Math.round(hours * 3600);
    if (!Number.isFinite(hours) || retentionSeconds < 300 || retentionSeconds > 365 * 24 * 60 * 60) {
      return toast("保留时长必须在 5 分钟至 365 天之间", "error");
    }
    button.disabled = true;
    button.textContent = "正在保存…";
    try {
      await requestJson(`${ADMIN_API}/upstream-cleanup`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          retention_seconds: retentionSeconds,
          conversation_action: String(values.get("conversation_action") || "delete"),
        }),
      });
      clearCache("upstream-cleanup");
      await renderSystem();
      toast("上游对话保留策略已保存");
    } catch (error) {
      toast(error?.message || "上游清理策略保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存清理策略";
    }
  };

  const render = async () => {
    const route = routeFromLocation();
    const renderId = ++state.renderId;
    state.route = route;
    updateRouteChrome(route);
    app.dataset.sidebarOpen = "false";
    closeDrawer();
    content.setAttribute("aria-busy", "true");
    content.innerHTML = loadingView(route);
    schedulePrefetch();
    try {
      if (route === "overview") await renderOverview(renderId);
      else if (route === "announcements") await renderAnnouncements(renderId);
      else if (route === "operations") await renderOperations(renderId);
      else if (route === "projectApi") await renderProjectApi(renderId);
      else if (route === "providers") await renderProviders(false, renderId);
      else if (route === "users") await renderUsers(renderId);
      else if (route === "subscriptions") await renderSubscriptions(renderId);
      else if (route === "access") await renderAccess(renderId);
      else if (route === "storage") await renderStorage(renderId);
      else if (route === "system") await renderSystem(renderId);
      if (renderId !== state.renderId) return;
      app.dataset.ready = "true";
      if (route !== "overview") {
        void loadOverview().then((overview) => {
          if (renderId !== state.renderId) return;
          updateGlobalStatus(overview);
          return loadProviders().then((payload) => {
            if (renderId === state.renderId) updateGlobalStatus({ ...overview, providers: payload.items || [] });
          });
        }).catch(() => {});
      }
    } catch (error) {
      if (renderId === state.renderId) errorView(error);
    } finally {
      if (renderId === state.renderId) {
        content.setAttribute("aria-busy", "false");
        state.forceRefresh = false;
      }
    }
  };

  const navigate = (route) => {
    if (!ROUTES[route]) return;
    if (routeFromLocation() === route) {
      void render();
      return;
    }
    window.location.hash = `/${route}`;
  };

  document.addEventListener("click", (event) => {
    const routeButton = event.target.closest("[data-route]");
    if (routeButton) {
      event.preventDefault();
      navigate(routeButton.dataset.route);
      return;
    }
    const close = event.target.closest("[data-close-drawer]");
    if (close) return closeDrawer();
    if (event.target.closest("[data-close-modal]")) return closeModal(false);
    if (event.target.closest("[data-toggle-sidebar]")) {
      app.dataset.sidebarOpen = String(app.dataset.sidebarOpen !== "true");
      return;
    }
    if (event.target.closest("[data-close-sidebar]")) {
      app.dataset.sidebarOpen = "false";
      return;
    }
    const filter = event.target.closest("[data-user-filter]");
    if (filter) {
      state.userRole = filter.dataset.userFilter;
      document.querySelectorAll("[data-user-filter]").forEach((button) => button.setAttribute("aria-pressed", String(button === filter)));
      renderUserTableRegion();
      return;
    }
    const subscriptionFilter = event.target.closest("[data-subscription-filter]");
    if (subscriptionFilter) {
      state.subscriptionStatus = subscriptionFilter.dataset.subscriptionFilter;
      state.subscriptionSelectedUserIds.clear();
      document.querySelectorAll("[data-subscription-filter]").forEach((button) => button.setAttribute("aria-pressed", String(button === subscriptionFilter)));
      renderSubscriptionTableRegion();
      return;
    }
    const range = event.target.closest("[data-operations-hours]");
    if (range) {
      state.operationsHours = Number(range.dataset.operationsHours) || 1;
      clearCache(`operations-${state.operationsHours}`);
      void renderOperations();
      return;
    }
    const action = event.target.closest("[data-action]");
    if (!action) return;
    const name = action.dataset.action;
    if (name === "refresh") {
      state.forceRefresh = true;
      clearCache();
      void render();
    } else if (name === "refresh-providers") {
      void refreshProviders(action);
    } else if (name === "confirm-modal") {
      closeModal(true);
    } else if (name === "open-project-api-user-picker") {
      openProjectApiUserPicker();
    } else if (name === "add-project-api-user") {
      void addProjectApiUser(action);
    } else if (name === "edit-project-api-user") {
      openProjectApiUserEditor(action);
    } else if (name === "save-project-api-user") {
      void saveProjectApiUser(action);
    } else if (name === "grant-project-api-credit") {
      openProjectApiCreditGrant(action);
    } else if (name === "submit-project-api-credit") {
      void submitProjectApiCredit(action);
    } else if (name === "delete-project-api-user") {
      void deleteProjectApiUser(action);
    } else if (name === "project-api-hours") {
      state.projectApiHours = Number(action.dataset.hours) || 24;
      state.projectApiOffset = 0;
      void renderProjectApi();
    } else if (name === "reset-project-api-filters") {
      resetProjectApiFilters();
    } else if (name === "project-api-page") {
      void changeProjectApiPage(action);
    } else if (name === "save-provider-display") {
      void saveProviderDisplay(action);
    } else if (name === "open-account-pool") {
      openAccountPoolDrawer(
        action.dataset.poolId || "new",
        action.dataset.provider || "gpt",
      );
    } else if (name === "save-account-pool") {
      void saveAccountPool(action);
    } else if (name === "delete-account-pool") {
      void deleteAccountPool(action);
    } else if (name === "open-account") {
      openAccountDrawer(action.dataset.accountId || "new", action.dataset.poolId || "gpt-default");
    } else if (name === "save-account") {
      void saveAccount(action);
    } else if (name === "prepare-account-runtime") {
      void prepareAccountRuntime(action);
    } else if (name === "probe-account") {
      void probeAccount(action);
    } else if (name === "start-account-reauth") {
      void accountReauth(action, "start");
    } else if (name === "verify-account-reauth") {
      void accountReauth(action, "verify");
    } else if (name === "cancel-account-reauth") {
      void accountReauth(action, "cancel");
    } else if (name === "probe-account-pool") {
      void probeAccountPool(action);
    } else if (name === "open-user") {
      openUserDrawer(action.dataset.userId);
    } else if (name === "save-user") {
      void saveUser(action);
    } else if (name === "open-subscription") {
      openSubscriptionDrawer(action.dataset.userId);
    } else if (name === "save-subscription") {
      void saveSubscription(action);
    } else if (name === "select-visible-subscriptions") {
      filteredSubscriptions()
        .filter((item) => item.role !== "admin")
        .forEach((item) => state.subscriptionSelectedUserIds.add(item.id));
      renderSubscriptionTableRegion();
    } else if (name === "clear-subscription-selection") {
      state.subscriptionSelectedUserIds.clear();
      renderSubscriptionTableRegion();
    } else if (name === "open-bulk-groups") {
      openBulkGroupModal();
    } else if (name === "apply-bulk-groups") {
      void applyBulkGroups(action);
    } else if (name === "reset-subscription-group-filters") {
      state.subscriptionResourceGroup = "";
      state.subscriptionGptGroup = "";
      state.subscriptionClaudeGroup = "";
      state.subscriptionSelectedUserIds.clear();
      void renderSubscriptions();
    } else if (name === "filter-group-users") {
      filterUsersByGroup(action);
    } else if (name === "apply-subscription-preset") {
      applySubscriptionPreset(action);
    } else if (name === "extend-subscription") {
      void extendSubscription(action);
    } else if (name === "cancel-subscription") {
      void cancelSubscription(action);
    } else if (name === "reset-user-quota") {
      void resetUserQuota(action);
    } else if (name === "open-resource-group") {
      openResourceGroupDrawer(action.dataset.groupId);
    } else if (name === "save-resource-group") {
      void saveResourceGroup(action);
    } else if (name === "delete-resource-group") {
      void deleteResourceGroup(action);
    } else if (name === "open-group") {
      openGroupDrawer(action.dataset.groupId, action.dataset.provider || "gpt");
    } else if (name === "copy-group") {
      openGroupDrawer("new", action.dataset.provider || "gpt", action.dataset.groupId);
    } else if (name === "apply-group-preset") {
      applyGroupPreset(action.dataset.presetId);
    } else if (name === "save-group") {
      void saveGroup(action);
    } else if (name === "delete-group") {
      void deleteGroup(action);
    } else if (name === "save-storage") {
      void saveStorage(action);
    } else if (name === "test-storage") {
      void testStorage(action);
    } else if (name === "backfill-thumbnails") {
      void backfillThumbnails(action);
    } else if (name === "save-auth-security") {
      void saveAuthSecurity(action);
    } else if (name === "save-upstream-cleanup") {
      void saveUpstreamCleanup(action);
    } else if (name === "new-announcement") {
      void newAnnouncement();
    } else if (name === "select-announcement") {
      void selectAnnouncement(action);
    } else if (name === "delete-announcement") {
      void deleteAnnouncement(action);
    } else if (name === "save-announcement") {
      void saveAnnouncement(action);
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-user-search]")) {
      state.userQuery = event.target.value;
      renderUserTableRegion();
    }
    if (event.target.matches("[data-subscription-search]")) {
      state.subscriptionQuery = event.target.value;
      state.subscriptionSelectedUserIds.clear();
      renderSubscriptionTableRegion();
    }
    if (event.target.closest("#announcement-form")) {
      scheduleAnnouncementPreview();
    }
    if (event.target.matches("[data-project-api-user-search]")) {
      window.clearTimeout(state.projectApiUserSearchTimer);
      state.projectApiUserSearchGeneration += 1;
      state.projectApiUserSearchTimer = window.setTimeout(
        () => void renderProjectApiUserSearch(event.target.value),
        1000,
      );
    }
    const row = event.target.closest("[data-rule-key]");
    if (row) syncRuleRow(row);
  });

  document.addEventListener("change", (event) => {
    const subscriptionGroupFilter = event.target.closest("[data-subscription-group-filter]");
    if (subscriptionGroupFilter) {
      const kind = subscriptionGroupFilter.dataset.subscriptionGroupFilter;
      if (kind === "resource") state.subscriptionResourceGroup = subscriptionGroupFilter.value;
      else if (kind === "gpt") state.subscriptionGptGroup = subscriptionGroupFilter.value;
      else if (kind === "claude") state.subscriptionClaudeGroup = subscriptionGroupFilter.value;
      state.subscriptionSelectedUserIds.clear();
      renderSubscriptionTableRegion();
      return;
    }
    const subscriptionSelectAll = event.target.closest("[data-subscription-select-all]");
    if (subscriptionSelectAll) {
      filteredSubscriptions()
        .filter((item) => item.role !== "admin")
        .forEach((item) => {
          if (subscriptionSelectAll.checked) state.subscriptionSelectedUserIds.add(item.id);
          else state.subscriptionSelectedUserIds.delete(item.id);
        });
      renderSubscriptionTableRegion();
      return;
    }
    const subscriptionSelect = event.target.closest("[data-subscription-select]");
    if (subscriptionSelect) {
      if (subscriptionSelect.checked) state.subscriptionSelectedUserIds.add(subscriptionSelect.value);
      else state.subscriptionSelectedUserIds.delete(subscriptionSelect.value);
      renderSubscriptionTableRegion();
      return;
    }
    const modelGroupChoice = event.target.closest("[data-model-group-choice]");
    if (modelGroupChoice) {
      const picker = modelGroupChoice.closest("[data-model-group-picker]");
      picker?.querySelectorAll(".model-group-choice").forEach((choice) => {
        const input = choice.querySelector("[data-model-group-choice]");
        choice.dataset.selected = String(Boolean(input?.checked));
      });
      const family = modelGroupChoice.dataset.family;
      const group = state.usersBundle?.modelGroups?.find(
        (item) => item.id === modelGroupChoice.value && item.provider_family === family,
      );
      const detail = picker?.querySelector(`[data-model-group-detail="${family}"]`);
      if (detail) detail.innerHTML = modelGroupVisual(group, state.usersBundle);
      return;
    }
    const groupSelect = event.target.closest("[data-group-detail-select]");
    if (groupSelect) {
      const detail = groupSelect.closest(".field")?.querySelector("[data-group-detail]");
      if (detail) detail.textContent = groupSelect.selectedOptions[0]?.getAttribute("title") || "未找到分组详情";
      if (
        groupSelect.name === "resource_group_id"
        && groupSelect.closest("#subscription-control-form")
      ) {
        const group = state.usersBundle?.resourceGroups?.find(
          (item) => item.id === groupSelect.value,
        );
        const concurrency = groupSelect
          .closest("#subscription-control-form")
          ?.querySelector('input[name="user_concurrency"]');
        if (group && concurrency) {
          concurrency.max = String(Number(group.max_concurrency) || 1);
          concurrency.placeholder = `继承 ${numberText(group.default_user_concurrency || 1)}`;
          if (Number(concurrency.value || 0) > Number(group.max_concurrency || 1)) {
            concurrency.value = "";
          }
        }
      }
      return;
    }
    const permissionSwitch = event.target.closest("[data-project-api-permission-switch]");
    if (permissionSwitch) {
      void toggleProjectApiPermission(permissionSwitch);
      return;
    }
    const filter = event.target.closest("[data-project-api-filter]");
    if (!filter) return;
    const name = filter.dataset.projectApiFilter;
    if (name === "owner") {
      state.projectApiOwner = filter.value;
      state.projectApiKey = "";
    } else if (name === "key") state.projectApiKey = filter.value;
    else if (name === "model") state.projectApiModel = filter.value;
    else if (name === "outcome") state.projectApiOutcome = filter.value;
    state.projectApiOffset = 0;
    void renderProjectApi();
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-project-cost-config]");
    if (!form) return;
    event.preventDefault();
    void saveProjectApiConfig(form);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!modal.hidden) closeModal(false);
    else if (!drawer.hidden) closeDrawer();
    else app.dataset.sidebarOpen = "false";
  });

  window.addEventListener("hashchange", () => void render());
  window.addEventListener("pageshow", () => {
    if (!storedToken()) accessGate(new RequestError("请先登录后再进入管理后台", "unauthorized", 401));
  });

  if (!window.location.hash) window.history.replaceState(null, "", `${window.location.pathname}#/overview`);
  window.setInterval(updateLoginCountdowns, 1000);
  window.setInterval(() => {
    if (document.visibilityState !== "visible" || !drawer.hidden) return;
    if (state.route === "overview") {
      if (Date.now() - state.lastOverviewAutoAt < 60_000) return;
      state.lastOverviewAutoAt = Date.now();
      clearCache("overview", "chat-admin");
      void renderOverview().catch(() => {});
    } else if (state.route === "operations") {
      clearCache(`operations-${state.operationsHours}`);
      void renderOperations().catch(() => {});
    }
  }, 10_000);
  void render();
})();
