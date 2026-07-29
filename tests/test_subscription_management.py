from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"


def test_upstream_update_checks_do_not_surface_in_the_chat_workspace() -> None:
    local_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    production_compose = (
        ROOT / "deploy" / "turtle-gpt" / "compose.blue-green.yml"
    ).read_text(encoding="utf-8")
    image_recipe = (BRANDING / "Dockerfile").read_text(encoding="utf-8")

    assert 'ENABLE_VERSION_UPDATE_CHECK: "false"' in local_compose
    assert 'ENABLE_VERSION_UPDATE_CHECK: "false"' in production_compose
    assert "ENV ENABLE_VERSION_UPDATE_CHECK=false" in image_recipe


def test_subscription_schema_and_server_gate_are_present() -> None:
    store = (BRANDING / "turtle_chat" / "store.py").read_text(encoding="utf-8")
    metering = (BRANDING / "turtle_chat" / "metering.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS chat_subscription" in store
    assert "CREATE TABLE IF NOT EXISTS chat_subscription_event" in store
    assert "require_active_subscription" in store
    assert "chat_subscription_" in metering
    assert "SUBSCRIPTION_CACHE.require_active" in metering


def test_subscription_cache_is_bounded_and_single_flight() -> None:
    cache = (BRANDING / "turtle_chat" / "subscription_cache.py").read_text(
        encoding="utf-8"
    )
    assert "TURTLE_SUBSCRIPTION_CACHE_TTL_SECONDS" in cache
    assert "TURTLE_SUBSCRIPTION_NEGATIVE_TTL_SECONDS" in cache
    assert "nx=True" in cache
    assert "px=self.lock_ttl_ms" in cache
    assert "redis.eval" in cache
    assert "min(ttl, max(1, boundary))" in cache
    assert "asyncio.Semaphore" in cache


def test_admin_ui_separates_users_from_subscription_control() -> None:
    html = (BRANDING / "admin-console.html").read_text(encoding="utf-8")
    script = (BRANDING / "admin-console.js").read_text(encoding="utf-8")
    assert 'data-route="subscriptions"' in html
    assert "const renderSubscriptions" in script
    assert "const openSubscriptionDrawer" in script
    assert "const saveSubscription" in script
    assert "默认规则：激活日起第 30 天" in script
    assert "功能与额度请到“订阅管理”调整" in script
    assert "动态额度 · 以上游为准" in script
    assert "套餐倍率 · 以上游为准" in script
    assert "本站不设硬上限" in script
    assert "turtle-admin.css?v=20260725.10" in html
    assert "turtle-admin.js?v=20260725.11" in html


def test_admin_ui_supports_safe_group_migration_and_hides_duplicate_quota_panel() -> None:
    router = (BRANDING / "turtle_chat" / "router.py").read_text(encoding="utf-8")
    store = (BRANDING / "turtle_chat" / "store.py").read_text(encoding="utf-8")
    script = (BRANDING / "admin-console.js").read_text(encoding="utf-8")
    assert '/admin/users/bulk-groups' in router
    assert "bulk_assign_groups" in store
    assert "RETIRED_LEGACY_GROUP_IDS" in store
    assert "data-subscription-group-filter" in script
    assert "data-subscription-select" in script
    assert "filter-group-users" in script
    assert "当前额度" not in script


def test_chat_ui_blocks_pending_and_expired_without_hiding_pages() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "const chatAccessBlock" in script
    assert "订阅已到期" in script
    assert "turtleAccessDisabled" in script
    assert "chatSubscription?.active" in script
    assert "动态额度 · 以上游为准" in script
    assert "chatPolicyIsAdmin = payload.is_admin === true" in script
    assert "管理员不限额" in script
    assert "不受站内次数限制" in script
    assert "turtle-model-controls.js?v=20260728.10" in patcher
    assert "custom.css?v=20260729.1" in patcher
    assert "turtle-storage-controls.js?v=20260729.1" in patcher


def test_startup_splash_uses_ios_material_and_visible_progress_until_the_app_is_ready() -> None:
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    styles = (BRANDING / "custom.css").read_text(encoding="utf-8")

    assert 'class="turtle-loading-card"' in patcher
    assert 'role="status"' in patcher
    assert 'aria-label="Turtle’s Chat 正在打开工作区"' in patcher
    assert 'class="turtle-ios-spinner"' in patcher
    assert 'class="turtle-loading-label"' in patcher
    assert "dataset.turtleSplashView" in patcher
    assert "? 'conversation'" in patcher
    assert "? 'new'" in patcher
    assert ": 'other'" in patcher
    assert "dataset.turtleSidebarOpen" in patcher
    assert "localStorage.sidebar === 'true'" in patcher
    assert "const startupResponses = new Map();" in patcher
    assert "const startupCacheTtlMs = 8000;" in patcher
    assert 'glob("2.*.js")' in patcher
    assert "appShellPreload.rel = 'modulepreload'" in patcher
    assert "appShellPreload.setAttribute('fetchpriority', 'high')" in patcher
    assert "const authenticatedStartupPaths = new Map([" in patcher
    assert "url.pathname === '/api/config'" in patcher
    assert "['/api/v1/auths/', 'session']" in patcher
    assert "['/api/v1/users/user/settings', 'settings']" in patcher
    assert "['/api/models', 'models']" in patcher
    assert "['/api/v1/configs/banners', 'banners']" in patcher
    assert "['/api/v1/tools/', 'tools']" in patcher
    assert "['/api/v1/chats/?page=1', 'chat-list']" in patcher
    assert "['/api/v1/chats/pinned', 'pinned-chats']" in patcher
    assert "['/api/v1/folders/', 'folders']" in patcher
    assert "['/api/v1/folders/shared', 'shared-folders']" in patcher
    assert "method !== 'GET'" in patcher
    assert "url.origin !== window.location.origin" in patcher
    assert "authenticatedStartupPaths.get(`${url.pathname}${url.search}`)" in patcher
    assert "start('session', '/api/v1/auths/'" in patcher
    assert "startupResponses.get(key) || start(key, input, init)" in patcher
    assert "for (const [path, key] of authenticatedStartupPaths)" not in patcher
    assert "return (await pending).clone();" in patcher
    assert "window.fetch = startupFetch;" in patcher
    assert "const nativeRemove = Element.prototype.remove;" in patcher
    assert "const minimumVisibleUntil = performance.now() + 650;" in patcher
    assert "minimumVisibleUntil - performance.now()" in patcher
    assert "splash.remove = () => {" in patcher
    assert "'#message-input-container, #auth-page'" in patcher
    assert "attempt < (isChatView ? 60 : 12)" in patcher
    assert "window.requestAnimationFrame" in patcher
    assert "splash.classList.add('turtle-splash-leaving')" in patcher
    assert "splash.addEventListener('transitionend', removeNow" in patcher
    assert "window.setTimeout(removeNow, 180)" in patcher
    assert "#splash-screen.turtle-splash-leaving" in styles
    assert "#splash-screen > .turtle-splash-shell" in styles
    assert "#splash-screen > div.turtle-loading-card" in styles
    assert "backdrop-filter: blur(42px) saturate(185%)" in styles
    assert "backdrop-filter: blur(34px) saturate(190%)" in styles
    assert "-apple-system" in styles
    assert "#splash-screen .turtle-ios-spinner" in styles
    assert "#splash-screen .turtle-loading-label" in styles
    assert "#splash-screen #progress-background" in styles
    assert "#splash-screen #progress-background::after" in styles
    assert "#splash-screen #progress-bar" in styles
    assert '#progress-bar[style*="width: 0%"]' in styles
    assert "min-width: 0;" in styles
    assert "min-width: 0.3rem" not in styles
    assert "@keyframes turtle-ios-spin" in styles
    assert "@keyframes turtle-ios-progress" in styles
    assert "transition: width 160ms cubic-bezier" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_sidebar_and_conversation_session_caches_are_isolated() -> None:
    cache = (BRANDING / "client-read-cache.js").read_text(encoding="utf-8")
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    image = (BRANDING / "Dockerfile").read_text(encoding="utf-8")

    assert "turtle-sidebar-read-cache-v1:" in cache
    assert 'digest("SHA-256"' in cache
    assert "SIDEBAR_MAX_STALE_MS" in cache
    assert "SIDEBAR_REVALIDATE_AFTER_MS" in cache
    assert "sidebarInflight" in cache
    assert "sidebarSingleFlightJoins" in cache
    assert "CONVERSATION_CACHE_LIMIT = 36" in cache
    assert "CONVERSATION_SESSION_LIMIT = 12" in cache
    assert "turtle-conversation-read-cache-v1:" in cache
    assert "CONVERSATION_CACHE_TTL_MS" in cache
    assert "TASK_CACHE_TTL_MS" in cache
    assert "conversationCache = new Map()" in cache
    assert "writeConversationStorageEntry" in cache
    assert "conversationSessionHits" in cache
    assert "sidebarPreview" not in cache
    assert "`/api/v1/chats/${encodedId}`" in cache
    assert "`/api/tasks/chat/${encodedId}`" in cache
    assert "sessionStorage.setItem(key, JSON.stringify(entry))" in cache
    assert "localStorage.setItem" not in cache
    assert "private-message-body" not in cache
    assert "clearAllReadCaches" in cache
    assert "turtle:client-read-cache-updated" in cache
    assert "turtle:client-read-cache-updated" in script
    assert "clientReadCacheRefreshTimer" in script
    assert 'Path("/tmp/turtle-client-read-cache.js")' in patcher
    assert "__TURTLE_CLIENT_READ_CACHE__" in patcher
    assert "COPY client-read-cache.js /tmp/turtle-client-read-cache.js" in image


def test_open_webui_v011_frontend_compatibility_and_release_ui_suppression() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    styles = (BRANDING / "custom.css").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "#model-selector-0-button, #model-selector-model-button" in script
    assert 'button[aria-controls="sidebar-chats-content"]' in script
    assert "data-turtle-chat-heading" in styles
    assert "#model-selector-model-button" in styles
    assert 'document.querySelector("#turtle-chat-home")?.remove()' in script
    assert "HOME_PROMPTS" not in script
    assert "backdrop-filter: blur(8px)" in styles
    assert "background: transparent !important;" in styles
    assert "ui_settings['showChangelog'] = False" in patcher
    assert "ui_settings['showUpdateToast'] = False" in patcher
    assert "refreshWorkspaceChatList" in script
    assert "CHAT_LIST_REFRESH_EXPORT" in script
    assert "export const refreshChatList = async" in patcher


def test_single_model_permissions_render_as_static_composer_labels() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    styles = (BRANDING / "custom.css").read_text(encoding="utf-8")
    assert "allowedLanesForCapability" in script
    assert "allowedProviderFamilies" in script
    assert "hasSingleStableLane" in script
    assert "providerModelsCaptured" in script
    assert "publishedProviderFamilies.has(family)" in script
    assert "allowed.length === 1 && allowed[0].lane.available" in script
    assert 'selector.dataset.turtleSingleModel = "true"' in script
    assert "controls.dataset.singleSelection = String(singleSelection)" in script
    assert '[data-turtle-single-model="true"]' in styles
    assert '[data-single-selection="true"]' in styles


def test_new_account_model_recovery_and_picker_quota_details_are_wired() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "ensureInitialWorkspaceModelQuery" in script
    assert "payload.model = modelId" in script
    assert "quotaIndicator" in script
    assert "剩余 / 总额 · 刷新时间" in script
    assert "laneRefreshText" in script
    assert "await get_all_models(request, refresh=True, user=user)" in patcher
    assert "model_id in {'gpt-5-web', 'claude-web'}" in patcher
    assert patcher.count("model_id not in {'gpt-5-web', 'claude-web'}") >= 4
    assert 'chat_utils_path = Path("/app/backend/open_webui/utils/chat.py")' in patcher
    assert 'openai_path = Path("/app/backend/open_webui/routers/openai.py")' in patcher
    assert "and not bypass_filter" in patcher
    assert "deployment-managed and are authorized by prepare_chat_request" in patcher
    assert "turtle-runtime-option-check" not in script
    assert "checkmark" not in script


def test_quota_countdown_floors_partial_units_and_generated_outputs_are_decorated() -> None:
    model_script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    storage_script = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")
    stylesheet = (BRANDING / "custom.css").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    media = (BRANDING / "turtle_storage" / "media.py").read_text(encoding="utf-8")
    assert "Math.floor((seconds % 3600) / 60)" in model_script
    assert "Math.ceil((seconds % 3600) / 60)" not in model_script
    assert "def requested_image_count" in media
    assert "'n': requested_image_count(user_message)" in patcher
    assert "decorateManagedOutputs" in storage_script
    assert "turtle-generated-gallery" in storage_script
    assert "prepareGeneratedImageEdit" in storage_script
    assert "chatForm?.parentElement?.querySelector" in storage_script
    assert "downloadGeneratedGallery" in storage_script
    assert "storedZip" in storage_script
    assert "data-gallery-download-menu" in storage_script
    assert "下载当前图片" in storage_script
    assert "下载全部图片" in storage_script
    assert "div.my-1.w-full.flex.flex-wrap" in storage_script
    assert "turtle-generated-file-card" in storage_script
    assert "generatedAttachmentName" in storage_script
    assert "generatedAttachmentType" in storage_script
    assert "downloadManagedAttachmentOnPage" in storage_script
    assert "`下载附件 ${name}`" in storage_script
    assert 'action.textContent = "下载"' in storage_script
    assert 'link.removeAttribute("target")' in storage_script
    assert "openManagedAttachmentPreview" not in storage_script
    assert "turtle-attachment-preview" not in storage_script
    assert "附件已安全保存到" not in storage_script
    assert "turtle-download-frame" in storage_script
    assert "window.location.assign" not in storage_script
    assert "syncNativeReasoningDisclosure" in storage_script
    assert "turtleNativeReasoningOpen" in storage_script
    assert 'active.getAttribute("aria-expanded") !== "true"' in storage_script
    assert 'completed.getAttribute("aria-expanded") === "true"' in storage_script
    assert 'progressLabel || "正在思考"' not in storage_script
    assert "GPT 正在思考" not in storage_script
    assert "请求已发送，正在等待首段内容" not in storage_script
    assert "仍在等待 GPT 返回首段内容" not in storage_script
    assert "已等待" not in storage_script
    assert "thinkingWaitTimer" not in storage_script
    assert "thinkingWaitStartedAt" not in storage_script
    assert 'a[href^="sandbox:" i]' in storage_script
    assert "turtle-unsupported-sandbox-link" in storage_script
    assert "data-turtle-sandbox-warning" in storage_script
    assert "GPT 只返回了临时运行路径，没有生成本站可下载的文件" in storage_script
    assert ".turtle-generated-gallery-stage" in stylesheet
    assert ".turtle-generated-gallery-rail" in stylesheet
    assert ".turtle-generated-file-card" in stylesheet
    assert "#turtle-attachment-preview" not in stylesheet
    assert ".turtle-thinking-wait" not in stylesheet
    assert ".turtle-native-thinking-dot" not in stylesheet
    assert ".turtle-thinking-wait-copy" not in stylesheet
    assert ".turtle-thinking-wait time" not in stylesheet
    assert ".turtle-unsupported-sandbox-link" in stylesheet
    assert ".turtle-sandbox-warning" in stylesheet
    stage_start = stylesheet.index(".turtle-generated-gallery-stage {")
    overlay_start = stylesheet.index(".turtle-generated-gallery-overlay {")
    download_pointer_start = stylesheet.index(".turtle-generated-gallery-download {")
    download_start = stylesheet.index(
        ".turtle-generated-gallery-download {", download_pointer_start + 1
    )
    stage_rules = stylesheet[stage_start : stylesheet.index("}", stage_start)]
    overlay_rules = stylesheet[overlay_start : stylesheet.index("}", overlay_start)]
    download_rules = stylesheet[download_start : stylesheet.index("}", download_start)]
    assert "padding: 0;" in stage_rules
    assert "border: 0;" in stage_rules
    assert "background: transparent;" in stage_rules
    assert "box-shadow: none;" in stage_rules
    assert "bottom: 0.42rem;" in overlay_rules
    assert "display: flex;" in download_rules
    assert "height: 2.12rem;" in download_rules
    assert "line-height: 1;" in download_rules
    assert "visible_stream_output" in media
    assert "return visible_stream_output(current_output)" in patcher
    assert "{'done': True, 'output': output, 'usage': usage}" in patcher
    assert "{'done': True, 'output': output}" in patcher


def test_new_chat_keeps_open_webui_client_side_navigation() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    start = script.index(
        'const newChatButton = event.target.closest("#sidebar-new-chat-button");'
    )
    end = script.index('const chatLink = event.target.closest("a[href]");', start)
    new_chat_handler = script[start:end]
    assert 'newChatButton.setAttribute("href", workspaceUrl(provider));' in new_chat_handler
    assert "window.location.assign" not in new_chat_handler
    assert "event.preventDefault()" not in new_chat_handler
    assert "event.stopImmediatePropagation()" not in new_chat_handler


def test_provider_switch_reuses_spa_navigation_and_search_progress_is_streamed() -> None:
    model_script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    storage_script = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")
    stylesheet = (BRANDING / "custom.css").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    open_workspace_start = model_script.index("const openWorkspace = (provider) => {")
    open_workspace_end = model_script.index("const chatIdFromLink", open_workspace_start)
    open_workspace = model_script[open_workspace_start:open_workspace_end]

    assert "await loadSpaNavigation()" in open_workspace
    assert "await navigate(target)" in open_workspace
    assert "workspaceNavigationRequest" in open_workspace
    assert "workspaceNavigationTarget" in open_workspace
    assert "syncProviderWorkspaceLinks" in model_script
    assert 'data-sveltekit-preload-data", "hover"' in model_script
    assert "turtle-provider-workspace-link" in stylesheet
    assert "one gesture cannot invoke both routers" in model_script
    assert "event.stopPropagation()" in model_script
    assert "__TURTLE_SVELTEKIT_NAVIGATION_MODULE__" in model_script
    assert "export function goto(url, opts = {})" in patcher
    assert '" as g" not in candidate_source' in patcher
    assert "response.clone().body" not in model_script
    assert "chatSearchIntent" not in model_script
    assert "rememberComposerSearchIntent" not in model_script
    assert "pendingComposerSearchIntent" not in model_script
    assert '"turtle-chat-progress"' not in model_script
    assert 'window.addEventListener("turtle-chat-progress", receiveChatProgress)' not in storage_script
    assert "liveChatProgress" not in storage_script
    assert "nativeReasoningLabel" in storage_script
    assert "button?.innerText || button?.textContent" in storage_script
    assert "syncNativeReasoningDisclosure();" in storage_script
    assert "decorateResponsePresentation" in storage_script
    assert "turtle-response-markdown" in storage_script
    assert "turtle-reasoning-trace" in storage_script
    assert "#response-content-container .turtle-response-markdown > h2" in stylesheet
    assert "#response-content-container .turtle-reasoning-content" in stylesheet
    assert "turtle_web_search_progress" not in patcher
    assert "turtle_search_progress = {'last': None, 'done': False}" not in patcher
    assert "turtle_search_progress_done" not in patcher
    assert "if (!storedToken() && !capturedAuthorization)" in storage_script
    assert "projectAccess = false;" in storage_script
    assert '.turtle-thinking-wait[data-mode="search"]' not in stylesheet


def test_claude_search_toggle_and_workspace_picker_close_follow_official_flow() -> None:
    model_script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    stylesheet = (BRANDING / "custom.css").read_text(encoding="utf-8")

    assert 'const CLAUDE_WEB_SEARCH_STORAGE_KEY = "turtle-claude-web-search-v1";' in model_script
    assert "return saved === null ? true : saved !== \"false\";" in model_script
    assert "const syncClaudeWebSearchToggle = () => {" in model_script
    assert "网页搜索已开启，由 Claude 自主判断何时搜索" in model_script
    search_toggle_start = model_script.index("const syncClaudeWebSearchToggle = () => {")
    search_toggle_end = model_script.index("const loadSpaNavigation", search_toggle_start)
    assert 'button[aria-label="扩展功能"][aria-expanded="true"]' not in model_script[
        search_toggle_start:search_toggle_end
    ]
    assert 'payload.web_search = claudeWebSearchEnabled();' in model_script
    assert "syncClaudeWebSearchToggle();" in model_script
    assert 'attributeFilter: ["src", "disabled", "aria-expanded"]' in model_script
    assert "const dismissNativeWorkspacePicker = () => {" in model_script
    assert "openWorkspace(targetProvider).finally(dismissNativeWorkspacePicker)" in model_script
    assert ".turtle-claude-web-search-switch[data-state=\"checked\"]" in stylesheet


def test_dark_chat_palette_matches_the_restrained_auth_blue_teal() -> None:
    stylesheet = (BRANDING / "custom.css").read_text(encoding="utf-8")

    assert "--turtle-bg: #0f2330;" in stylesheet
    assert "--turtle-bg-soft: #152d3b;" in stylesheet
    assert "--color-gray-950: #0f2330 !important;" in stylesheet
    assert "linear-gradient(142deg, #0b1823 0%" in stylesheet
    assert "rgba(29, 146, 167, 0.14)" in stylesheet
    assert "rgba(10, 27, 37, 0.82)" in stylesheet
    assert "rgba(12, 31, 42, 0.97)" not in stylesheet
    assert "rgba(12, 28, 40, 0.99)" in stylesheet
    assert "rgba(43, 75, 108, 0.96)" not in stylesheet


def test_frontend_startup_avoids_two_serial_render_gates() -> None:
    script = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    patcher = (BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    static_cache = (BRANDING / "turtle_static.py").read_text(encoding="utf-8")
    assert "hasEmbeddedProviders" in script
    assert "void loadConversationIndex()" in script
    assert "provider_for_chat(None, chat[5])" in patcher
    assert "provider: str | None = None" in patcher
    assert "never waits for deferred scripts or DOMContentLoaded" in patcher
    assert 'id="logo"' in patcher
    assert "public, max-age=60, stale-while-revalidate=300" in static_cache
