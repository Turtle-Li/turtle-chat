"""Apply the small version-checked Turtle's Chat Open WebUI patch."""

import re
from pathlib import Path


def replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text(encoding="utf-8")
    occurrences = source.count(before)
    if occurrences != 1:
        raise RuntimeError(
            f"Expected exactly one branding marker in {path}, found {occurrences}. "
            "The Open WebUI base image likely changed and the patch needs review."
        )
    path.write_text(source.replace(before, after, 1), encoding="utf-8")


env_path = Path("/app/backend/open_webui/env.py")
replace_once(
    env_path,
    """WEBUI_NAME = os.getenv('WEBUI_NAME', 'Open WebUI')
if WEBUI_NAME != 'Open WebUI':
    WEBUI_NAME += ' (Open WebUI)'
""",
    """# Turtle's Chat is a private, small-user deployment with its own brand.
WEBUI_NAME = os.getenv('WEBUI_NAME', 'Turtle’s Chat')
""",
)

# Docker secrets keep database credentials out of Compose files and container
# labels. Open WebUI normally accepts only a plaintext DATABASE_PASSWORD env;
# add a file-based equivalent before it assembles DATABASE_URL.
replace_once(
    env_path,
    """DATABASE_PASSWORD = os.getenv('DATABASE_PASSWORD')
""",
    """DATABASE_PASSWORD = os.getenv('DATABASE_PASSWORD')
DATABASE_PASSWORD_FILE = os.getenv('DATABASE_PASSWORD_FILE')
if not DATABASE_PASSWORD and DATABASE_PASSWORD_FILE:
    try:
        DATABASE_PASSWORD = Path(DATABASE_PASSWORD_FILE).read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise RuntimeError('Unable to read the configured database password file') from exc
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', DATABASE_PASSWORD):
        raise RuntimeError('The database password file has an invalid value')
""",
)

replace_once(
    env_path,
    """REDIS_URL = os.getenv('REDIS_URL', '')
REDIS_CLUSTER = os.getenv('REDIS_CLUSTER', 'False').lower() == 'true'
""",
    """REDIS_URL = os.getenv('REDIS_URL', '')
REDIS_PASSWORD_FILE = os.getenv('REDIS_PASSWORD_FILE')
REDIS_HOST = os.getenv('REDIS_HOST', '')
if not REDIS_URL and REDIS_HOST:
    redis_password = ''
    if REDIS_PASSWORD_FILE:
        try:
            redis_password = Path(REDIS_PASSWORD_FILE).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise RuntimeError('Unable to read the configured Redis password file') from exc
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', redis_password):
            raise RuntimeError('The Redis password file has an invalid value')
    redis_auth = f':{redis_password}@' if redis_password else ''
    redis_port = os.getenv('REDIS_PORT', '6379')
    redis_database = os.getenv('REDIS_DATABASE', '0')
    REDIS_URL = f'redis://{redis_auth}{REDIS_HOST}:{redis_port}/{redis_database}'
REDIS_CLUSTER = os.getenv('REDIS_CLUSTER', 'False').lower() == 'true'
""",
)

index_path = Path("/app/build/index.html")
app_shell_modules = sorted(
    Path("/app/build/_app/immutable/nodes").glob("2.*.js")
)
if len(app_shell_modules) != 1:
    raise RuntimeError(
        "Expected exactly one authenticated app-shell route module, "
        f"found {len(app_shell_modules)}"
    )
app_shell_module_href = f"/_app/immutable/nodes/{app_shell_modules[0].name}"
replace_once(index_path, "<title>Open WebUI</title>", "<title>Turtle’s Chat</title>")
replace_once(
    index_path,
    """\t\t<link rel="stylesheet" href="/static/custom.css" crossorigin="use-credentials" />
""",
    """\t\t<link rel="stylesheet" href="/static/custom.css?v=20260728.16" crossorigin="use-credentials" />
\t\t<link rel="preload" href="/static/turtle-gpt-logo.webp" as="image" type="image/webp" fetchpriority="high" />
\t\t<link rel="preload" href="/static/turtle-provider-chatgpt.svg" as="image" type="image/svg+xml" />
\t\t<link rel="preload" href="/static/turtle-provider-claude.svg" as="image" type="image/svg+xml" />
\t\t<script defer src="/static/turtle-model-controls.js?v=20260728.10" crossorigin="use-credentials"></script>
\t\t<script defer src="/static/turtle-storage-controls.js?v=20260728.2" crossorigin="use-credentials"></script>
""",
)
replace_once(
    index_path,
    """\t\t\t\tif (!localStorage?.theme) {
\t\t\t\t\tlocalStorage.theme = 'system';
\t\t\t\t}
""",
    """\t\t\t\t// Turtle's Chat follows the operating-system appearance on every load.
\t\t\t\tlocalStorage.theme = 'system';
\t\t\t\t// Apply the remembered Provider palette before first paint. Deferred
\t\t\t\t// controls will revalidate it against the route and server models.
\t\t\t\ttry {
\t\t\t\t\tconst model = new URLSearchParams(window.location.search).get('model');
\t\t\t\t\tconst routedProvider =
\t\t\t\t\t\tmodel === 'claude-web' ? 'claude' : model === 'gpt-5-web' ? 'gpt' : '';
\t\t\t\t\tconst storedProvider = localStorage.getItem('turtle-provider-workspace-v1');
\t\t\t\t\tconst provider =
\t\t\t\t\t\troutedProvider || (storedProvider === 'claude' ? 'claude' : 'gpt');
\t\t\t\t\tconst startupToken = localStorage.getItem('token') || '';
\t\t\t\t\tdocument.documentElement.dataset.turtleProvider = provider;
\t\t\t\t\tdocument.documentElement.dataset.turtleSession =
\t\t\t\t\t\tstartupToken ? 'returning' : 'guest';
\t\t\t\t\tconst splashPath = window.location.pathname;
\t\t\t\t\tdocument.documentElement.dataset.turtleSplashView =
\t\t\t\t\t\t/^\\/(?:c|chat)\\//.test(splashPath)
\t\t\t\t\t\t\t? 'conversation'
\t\t\t\t\t\t\t: splashPath === '/'
\t\t\t\t\t\t\t\t? 'new'
\t\t\t\t\t\t\t\t: 'other';
\t\t\t\t\tconst sidebarWidth = Number(localStorage.getItem('sidebarWidth'));
\t\t\t\t\tif (Number.isFinite(sidebarWidth) && sidebarWidth >= 220 && sidebarWidth <= 480) {
\t\t\t\t\t\tdocument.documentElement.style.setProperty(
\t\t\t\t\t\t\t'--turtle-splash-sidebar-width',
\t\t\t\t\t\t\t`${sidebarWidth}px`
\t\t\t\t\t\t);
\t\t\t\t\t}
\t\t\t\t\tdocument.documentElement.dataset.turtleSidebarOpen =
\t\t\t\t\t\twindow.innerWidth >= 768 && localStorage.sidebar === 'true' ? 'true' : 'false';
\t\t\t\t\tif (
\t\t\t\t\t\tstartupToken &&
\t\t\t\t\t\t(splashPath === '/' || /^\\/(?:c|chat)\\//.test(splashPath))
\t\t\t\t\t) {
\t\t\t\t\t\tconst appShellPreload = document.createElement('link');
\t\t\t\t\t\tappShellPreload.rel = 'modulepreload';
\t\t\t\t\t\tappShellPreload.href = '__TURTLE_APP_SHELL_MODULE__';
\t\t\t\t\t\tappShellPreload.setAttribute('fetchpriority', 'high');
\t\t\t\t\t\tdocument.head.append(appShellPreload);
\t\t\t\t\t}
\t\t\t\t} catch (_) {
\t\t\t\t\tdocument.documentElement.dataset.turtleProvider = 'gpt';
\t\t\t\t\tdocument.documentElement.dataset.turtleSession = 'guest';
\t\t\t\t\tdocument.documentElement.dataset.turtleSplashView = 'new';
\t\t\t\t\tdocument.documentElement.dataset.turtleSidebarOpen = 'false';
\t\t\t\t}

\t\t\t\t// Prewarm only the two render-gating reads while the application
\t\t\t\t// module downloads. All remaining exact-path GETs join a lazy
\t\t\t\t// single-flight when Open WebUI actually asks for them, so a warm
\t\t\t\t// sidebar/session snapshot never competes with the app module.
\t\t\t\t(() => {
\t\t\t\t\tconst nativeFetch = window.fetch.bind(window);
\t\t\t\t\tconst startupResponses = new Map();
\t\t\t\t\tconst startupCacheTtlMs = 8000;
\t\t\t\t\tconst expiresAt = Date.now() + startupCacheTtlMs;
\t\t\t\t\tconst token = localStorage.getItem('token') || '';
\t\t\t\t\tconst authorization = token ? `Bearer ${token}` : '';
\t\t\t\t\tconst authenticatedStartupPaths = new Map([
\t\t\t\t\t\t['/api/v1/auths/', 'session'],
\t\t\t\t\t\t['/api/v1/users/user/settings', 'settings'],
\t\t\t\t\t\t['/api/models', 'models'],
\t\t\t\t\t\t['/api/v1/configs/banners', 'banners'],
\t\t\t\t\t\t['/api/v1/tools/', 'tools'],
\t\t\t\t\t\t['/api/v1/chats/?page=1', 'chat-list'],
\t\t\t\t\t\t['/api/v1/chats/pinned', 'pinned-chats'],
\t\t\t\t\t\t['/api/v1/chats/all/tags', 'chat-tags'],
\t\t\t\t\t\t['/api/v1/folders/', 'folders'],
\t\t\t\t\t\t['/api/v1/folders/shared', 'shared-folders'],
\t\t\t\t\t\t['/api/v1/notes/pinned', 'pinned-notes']
\t\t\t\t\t]);

\t\t\t\t\tconst requestKey = (input, init = {}) => {
\t\t\t\t\t\tconst request =
\t\t\t\t\t\t\ttypeof Request !== 'undefined' && input instanceof Request ? input : null;
\t\t\t\t\t\tconst method = String(init?.method || request?.method || 'GET').toUpperCase();
\t\t\t\t\t\tif (method !== 'GET') return '';

\t\t\t\t\t\tlet url;
\t\t\t\t\t\ttry {
\t\t\t\t\t\t\turl = new URL(request?.url || input, window.location.href);
\t\t\t\t\t\t} catch (_) {
\t\t\t\t\t\t\treturn '';
\t\t\t\t\t\t}
\t\t\t\t\t\tif (url.origin !== window.location.origin) return '';
\t\t\t\t\t\tif (url.pathname === '/api/config') return 'config';
\t\t\t\t\t\tconst key = authenticatedStartupPaths.get(`${url.pathname}${url.search}`);
\t\t\t\t\t\tif (!key || !token) return '';

\t\t\t\t\t\tconst headers = new Headers(init?.headers || request?.headers || {});
\t\t\t\t\t\treturn headers.get('Authorization') === authorization ? key : '';
\t\t\t\t\t};

\t\t\t\t\tconst start = (key, input, init) => {
\t\t\t\t\t\tconst existing = startupResponses.get(key);
\t\t\t\t\t\tif (existing) return existing;
\t\t\t\t\t\tconst response = nativeFetch(input, init)
\t\t\t\t\t\t\t.then((result) => {
\t\t\t\t\t\t\t\tif (result.status >= 500) startupResponses.delete(key);
\t\t\t\t\t\t\t\treturn result;
\t\t\t\t\t\t\t})
\t\t\t\t\t\t\t.catch((error) => {
\t\t\t\t\t\t\t\tstartupResponses.delete(key);
\t\t\t\t\t\t\t\tthrow error;
\t\t\t\t\t\t\t});
\t\t\t\t\t\tstartupResponses.set(key, response);
\t\t\t\t\t\treturn response;
\t\t\t\t\t};

\t\t\t\t\tstart('config', '/api/config', {
\t\t\t\t\t\tmethod: 'GET',
\t\t\t\t\t\tcredentials: 'include',
\t\t\t\t\t\theaders: { 'Content-Type': 'application/json' }
\t\t\t\t\t});
\t\t\t\t\tif (token) {
\t\t\t\t\t\tstart('session', '/api/v1/auths/', {
\t\t\t\t\t\t\tmethod: 'GET',
\t\t\t\t\t\t\tcredentials: 'include',
\t\t\t\t\t\t\theaders: {
\t\t\t\t\t\t\t\tAccept: 'application/json',
\t\t\t\t\t\t\t\t'Content-Type': 'application/json',
\t\t\t\t\t\t\t\tAuthorization: authorization
\t\t\t\t\t\t\t}
\t\t\t\t\t\t});
\t\t\t\t\t}

\t\t\t\t\tconst startupFetch = async (input, init) => {
\t\t\t\t\t\tconst key = requestKey(input, init);
\t\t\t\t\t\tif (!key || Date.now() >= expiresAt) return nativeFetch(input, init);
\t\t\t\t\t\tconst pending =
\t\t\t\t\t\t\tstartupResponses.get(key) || start(key, input, init);
\t\t\t\t\t\tif (pending) {
\t\t\t\t\t\t\ttry {
\t\t\t\t\t\t\t\treturn (await pending).clone();
\t\t\t\t\t\t\t} catch (_) {
\t\t\t\t\t\t\t\tstartupResponses.delete(key);
\t\t\t\t\t\t\t}
\t\t\t\t\t\t}
\t\t\t\t\t\treturn nativeFetch(input, init);
\t\t\t\t\t};

\t\t\t\t\twindow.fetch = startupFetch;
\t\t\t\t\twindow.setTimeout(() => {
\t\t\t\t\t\tstartupResponses.clear();
\t\t\t\t\t\tif (window.fetch === startupFetch) window.fetch = nativeFetch;
\t\t\t\t\t}, startupCacheTtlMs);
\t\t\t\t})();
\t\t\t\t/* __TURTLE_CLIENT_READ_CACHE__ */
""",
)
replace_once(
    index_path,
    "__TURTLE_APP_SHELL_MODULE__",
    app_shell_module_href,
)

client_read_cache_path = Path("/tmp/turtle-client-read-cache.js")
client_read_cache_source = client_read_cache_path.read_text(encoding="utf-8")
if "</script" in client_read_cache_source.lower():
    raise RuntimeError("The client read cache cannot be safely embedded in index.html")
replace_once(
    index_path,
    "\t\t\t\t/* __TURTLE_CLIENT_READ_CACHE__ */",
    "\n".join(
        f"\t\t\t\t{line}" if line else ""
        for line in client_read_cache_source.splitlines()
    ),
)
replace_once(
    index_path,
    """\t\t<link
\t\t\trel=\"icon\"
\t\t\ttype=\"image/svg+xml\"
\t\t\thref=\"/static/favicon.svg\"
\t\t\tcrossorigin=\"use-credentials\"
\t\t/>
""",
    "",
)
replace_once(
    index_path,
    """\t\t\t\tconst isDarkMode = document.documentElement.classList.contains('dark');

\t\t\t\tconst logo = document.createElement('img');
\t\t\t\tlogo.id = 'logo';
\t\t\t\tlogo.style =
\t\t\t\t\t'position: absolute; width: auto; height: 6rem; top: 44%; left: 50%; transform: translateX(-50%); display:block;';
\t\t\t\tlogo.loading = 'eager';
\t\t\t\tlogo.fetchPriority = 'high';
\t\t\t\tlogo.src = isDarkMode ? '/static/splash-dark.png' : '/static/splash.png';

\t\t\t\tdocument.addEventListener('DOMContentLoaded', function () {
\t\t\t\t\tconst splash = document.getElementById('splash-screen');
\t\t\t\t\tif (document.documentElement.classList.contains('her')) {
\t\t\t\t\t\treturn;
\t\t\t\t\t}

\t\t\t\t\tif (splash) splash.prepend(logo);
\t\t\t\t});
""",
    """\t\t\t\t// The splash shell and image are parser-visible below, so their
\t\t\t\t// first paint never waits for deferred scripts or DOMContentLoaded.
""",
)
replace_once(
    index_path,
    """\t\t<div
\t\t\tid="splash-screen"
\t\t\tstyle="position: fixed; z-index: 100; top: 0; left: 0; width: 100%; height: 100%"
\t\t>
\t\t\t<style type="text/css" nonce="">
""",
    """\t\t<div
\t\t\tid="splash-screen"
\t\t\tstyle="position: fixed; z-index: 100; top: 0; left: 0; width: 100%; height: 100%"
\t\t>
\t\t\t<div
\t\t\t\tclass="turtle-splash-shell"
\t\t\t\trole="status"
\t\t\t\taria-live="polite"
\t\t\t\taria-atomic="true"
\t\t\t\taria-label="Turtle’s Chat 正在准备你的工作区"
\t\t\t>
\t\t\t\t<aside class="turtle-splash-sidebar" aria-hidden="true">
\t\t\t\t\t<header class="turtle-splash-brand">
\t\t\t\t\t\t<img
\t\t\t\t\t\t\tid="logo"
\t\t\t\t\t\t\tclass="turtle-splash-logo"
\t\t\t\t\t\t\talt=""
\t\t\t\t\t\t\taria-hidden="true"
\t\t\t\t\t\t\tsrc="/static/turtle-gpt-logo.webp"
\t\t\t\t\t\t\twidth="128"
\t\t\t\t\t\t\theight="128"
\t\t\t\t\t\t\tloading="eager"
\t\t\t\t\t\t\tfetchpriority="high"
\t\t\t\t\t\t\tdecoding="sync"
\t\t\t\t\t\t/>
\t\t\t\t\t\t<strong>Turtle’s Chat</strong>
\t\t\t\t\t\t<i class="turtle-splash-sidebar-toggle"></i>
\t\t\t\t\t</header>
\t\t\t\t\t<div class="turtle-splash-new-chat"><i></i><span></span></div>
\t\t\t\t\t<nav class="turtle-splash-nav">
\t\t\t\t\t\t<div><i></i><span></span></div>
\t\t\t\t\t\t<div><i></i><span></span></div>
\t\t\t\t\t\t<small>最近对话</small>
\t\t\t\t\t\t<div><i></i><span></span></div>
\t\t\t\t\t\t<div><i></i><span></span></div>
\t\t\t\t\t\t<div><i></i><span></span></div>
\t\t\t\t\t</nav>
\t\t\t\t\t<div class="turtle-splash-profile"><i></i><span></span><b></b></div>
\t\t\t\t</aside>

\t\t\t\t<main class="turtle-splash-workspace" aria-hidden="true">
\t\t\t\t\t<header class="turtle-splash-topbar">
\t\t\t\t\t\t<i class="turtle-splash-menu"></i>
\t\t\t\t\t\t<span class="turtle-splash-provider-pill">
\t\t\t\t\t\t\t<i></i>
\t\t\t\t\t\t\t<b class="turtle-splash-provider-gpt">GPT</b>
\t\t\t\t\t\t\t<b class="turtle-splash-provider-claude">Claude</b>
\t\t\t\t\t\t</span>
\t\t\t\t\t\t<span class="turtle-splash-top-actions"><i></i><i></i><i></i></span>
\t\t\t\t\t</header>

\t\t\t\t\t<section class="turtle-splash-chat-frame">
\t\t\t\t\t\t<div class="turtle-splash-home">
\t\t\t\t\t\t\t<div class="turtle-splash-home-heading">
\t\t\t\t\t\t\t\t<span class="turtle-splash-provider-mark">
\t\t\t\t\t\t\t\t\t<img
\t\t\t\t\t\t\t\t\t\tclass="turtle-splash-provider-gpt"
\t\t\t\t\t\t\t\t\t\tsrc="/static/turtle-provider-chatgpt.svg"
\t\t\t\t\t\t\t\t\t\talt=""
\t\t\t\t\t\t\t\t\t/>
\t\t\t\t\t\t\t\t\t<img
\t\t\t\t\t\t\t\t\t\tclass="turtle-splash-provider-claude"
\t\t\t\t\t\t\t\t\t\tsrc="/static/turtle-provider-claude.svg"
\t\t\t\t\t\t\t\t\t\talt=""
\t\t\t\t\t\t\t\t\t/>
\t\t\t\t\t\t\t\t</span>
\t\t\t\t\t\t\t\t<strong>
\t\t\t\t\t\t\t\t\t<b class="turtle-splash-provider-gpt">GPT</b>
\t\t\t\t\t\t\t\t\t<b class="turtle-splash-provider-claude">Claude</b>
\t\t\t\t\t\t\t\t</strong>
\t\t\t\t\t\t\t</div>

\t\t\t\t\t\t\t<div class="turtle-splash-composer">
\t\t\t\t\t\t\t\t<span class="turtle-splash-composer-status">
\t\t\t\t\t\t\t\t\t<i></i><span></span>
\t\t\t\t\t\t\t\t</span>
\t\t\t\t\t\t\t\t<span class="turtle-splash-progress"><i></i></span>
\t\t\t\t\t\t\t\t<span class="turtle-splash-composer-actions"><i></i><i></i><b></b></span>
\t\t\t\t\t\t\t</div>

\t\t\t\t\t\t\t<div class="turtle-splash-prompt-grid">
\t\t\t\t\t\t\t\t<i></i><i></i><i></i>
\t\t\t\t\t\t\t</div>
\t\t\t\t\t\t</div>
\t\t\t\t\t</section>
\t\t\t\t</main>
\t\t\t</div>
\t\t\t<script>
\t\t\t\t(() => {
\t\t\t\t\tconst splash = document.getElementById('splash-screen');
\t\t\t\t\tif (!splash) return;

\t\t\t\t\tconst nativeRemove = Element.prototype.remove;
\t\t\t\t\tconst minimumVisibleUntil = performance.now() + 650;
\t\t\t\t\tlet exitStarted = false;
\t\t\t\t\tconst removeNow = () => {
\t\t\t\t\t\tif (splash.isConnected) nativeRemove.call(splash);
\t\t\t\t\t};
\t\t\t\t\tconst revealApp = (attempt = 0) => {
\t\t\t\t\t\twindow.requestAnimationFrame(() => {
\t\t\t\t\t\t\tconst splashView =
\t\t\t\t\t\t\t\tdocument.documentElement.dataset.turtleSplashView || 'new';
\t\t\t\t\t\t\tconst isChatView = splashView === 'new' || splashView === 'conversation';
\t\t\t\t\t\t\tconst readySelector = isChatView
\t\t\t\t\t\t\t\t? '#message-input-container, #auth-page'
\t\t\t\t\t\t\t\t: '#chat-container, #message-input-container, #auth-page';
\t\t\t\t\t\t\tconst appReady = document.querySelector(readySelector);
\t\t\t\t\t\t\tif (!appReady && attempt < (isChatView ? 60 : 12)) {
\t\t\t\t\t\t\t\trevealApp(attempt + 1);
\t\t\t\t\t\t\t\treturn;
\t\t\t\t\t\t\t}
\t\t\t\t\t\t\tconst startExit = () => window.requestAnimationFrame(() => {
\t\t\t\t\t\t\t\tif (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
\t\t\t\t\t\t\t\t\tremoveNow();
\t\t\t\t\t\t\t\t\treturn;
\t\t\t\t\t\t\t\t}
\t\t\t\t\t\t\t\tsplash.classList.add('turtle-splash-leaving');
\t\t\t\t\t\t\t\tsplash.addEventListener('transitionend', removeNow, { once: true });
\t\t\t\t\t\t\t\twindow.setTimeout(removeNow, 180);
\t\t\t\t\t\t\t});
\t\t\t\t\t\t\tconst remainingVisibilityMs = Math.max(
\t\t\t\t\t\t\t\t0,
\t\t\t\t\t\t\t\tminimumVisibleUntil - performance.now()
\t\t\t\t\t\t\t);
\t\t\t\t\t\t\tif (remainingVisibilityMs > 0) {
\t\t\t\t\t\t\t\twindow.setTimeout(startExit, remainingVisibilityMs);
\t\t\t\t\t\t\t\treturn;
\t\t\t\t\t\t\t}
\t\t\t\t\t\t\tstartExit();
\t\t\t\t\t\t});
\t\t\t\t\t};

\t\t\t\t\tsplash.remove = () => {
\t\t\t\t\t\tif (exitStarted) return;
\t\t\t\t\t\texitStarted = true;
\t\t\t\t\t\trevealApp();
\t\t\t\t\t};
\t\t\t\t})();
\t\t\t</script>
\t\t\t<style type="text/css" nonce="">
""",
)
replace_once(
    index_path,
    """\t\t\t<div
\t\t\t\tstyle="
\t\t\t\t\tposition: absolute;
\t\t\t\t\ttop: 33%;
""",
    """\t\t\t<div
\t\t\t\tclass="turtle-loading-card"
\t\t\t\trole="status"
\t\t\t\taria-live="polite"
\t\t\t\taria-atomic="true"
\t\t\t\taria-label="Turtle’s Chat 正在打开工作区"
\t\t\t\tstyle="
\t\t\t\t\tposition: absolute;
\t\t\t\t\ttop: 33%;
    """,
)
replace_once(
    index_path,
    """\t\t\t\t<img
\t\t\t\t\tid="logo-her"
""",
    """\t\t\t\t<span class="turtle-ios-spinner" aria-hidden="true"></span>
\t\t\t\t<span class="turtle-loading-label">正在打开工作区</span>
\t\t\t\t<img
\t\t\t\t\tid="logo-her"
""",
)

index_source = index_path.read_text(encoding="utf-8")
dark_theme_occurrences = index_source.count("#171717")
if dark_theme_occurrences < 2:
    raise RuntimeError(
        "Expected Open WebUI dark theme markers in index.html. "
        "The base image likely changed and the patch needs review."
    )
index_source = index_source.replace("#171717", "#213b59")
index_source = index_source.replace(
    "/static/splash-dark.png",
    "/static/turtle-gpt-logo.webp",
).replace(
    "/static/splash.png",
    "/static/turtle-gpt-logo.webp",
)
index_path.write_text(index_source, encoding="utf-8")

model_controls_path = Path("/app/build/static/turtle-model-controls.js")
navigation_module = None
for source_map in Path("/app/build/_app/immutable/chunks").glob("*.js.map"):
    if "export function goto(url, opts = {})" in source_map.read_text(
        encoding="utf-8"
    ):
        candidate = source_map.with_suffix("")
        candidate_source = candidate.read_text(encoding="utf-8")
        if " as g" not in candidate_source:
            raise RuntimeError(
                "The Open WebUI navigation module no longer exports goto as g. "
                "The pinned client-side navigation integration needs review."
            )
        navigation_module = f"/_app/immutable/chunks/{candidate.name}"
        break
if navigation_module is None:
    raise RuntimeError(
        "Unable to locate Open WebUI's client-side navigation module. "
        "The base image likely changed and the patch needs review."
    )
replace_once(
    model_controls_path,
    "__TURTLE_SVELTEKIT_NAVIGATION_MODULE__",
    navigation_module,
)

chat_list_module = None
chat_list_refresh_export = None
for source_map in Path("/app/build/_app/immutable/chunks").glob("*.js.map"):
    source_map_text = source_map.read_text(encoding="utf-8")
    if "export const refreshChatList = async" not in source_map_text:
        continue
    candidate = source_map.with_suffix("")
    candidate_source = candidate.read_text(encoding="utf-8")
    local_match = re.search(
        r"const ([A-Za-z_$][A-Za-z0-9_$]*)=async\([^)]*\)=>\{.{0,700}?\.refreshPinned",
        candidate_source,
    )
    if local_match is None:
        raise RuntimeError(
            "Unable to locate Open WebUI's compiled refreshChatList function. "
            "The pinned client-side chat list integration needs review."
        )
    export_match = re.search(
        rf"\b{re.escape(local_match.group(1))} as ([A-Za-z_$][A-Za-z0-9_$]*)\b",
        candidate_source,
    )
    if export_match is None:
        raise RuntimeError(
            "Open WebUI's refreshChatList function is no longer exported. "
            "The pinned client-side chat list integration needs review."
        )
    chat_list_module = f"/_app/immutable/chunks/{candidate.name}"
    chat_list_refresh_export = export_match.group(1)
    break
if chat_list_module is None or chat_list_refresh_export is None:
    raise RuntimeError(
        "Unable to locate Open WebUI's chat list store module. "
        "The base image likely changed and the patch needs review."
    )
replace_once(
    model_controls_path,
    "__TURTLE_CHAT_LIST_MODULE__",
    chat_list_module,
)
replace_once(
    model_controls_path,
    "__TURTLE_CHAT_LIST_REFRESH_EXPORT__",
    chat_list_refresh_export,
)

# Open WebUI's message component normally increments a local render count while
# the browser already holds the entire chat JSON. Replace that one pinned
# function so the top sentinel first obtains the preceding indexed depth range,
# then renders it while preserving the reader's scroll anchor.
messages_module = None
for source_map in Path("/app/build/_app/immutable/chunks").glob("*.js.map"):
    if "const loadMoreMessages = async () =>" not in source_map.read_text(
        encoding="utf-8"
    ):
        continue
    candidate = source_map.with_suffix("")
    candidate_source = candidate.read_text(encoding="utf-8")
    before = (
        "const y=async()=>{const h=E();h&&(h.scrollTop=h.scrollTop+100),"
        "x(u,!0),b(b()+8),W(),await dt(),x(u,!1)};let N=null,D=null;"
    )
    after = (
        "const y=async()=>{const h=E(),O=h?h.scrollHeight:0;"
        "h&&(h.scrollTop=h.scrollTop+100),x(u,!0);try{let F=!0;"
        "c().turtlePage?.hasMore&&window.__turtleHistoryPager&&"
        "(F=await window.__turtleHistoryPager.loadOlder(v(),c())),"
        "F&&(b(b()+8),c(c(),!0),W(),await dt(),h&&"
        "(h.scrollTop=h.scrollTop+Math.max(0,h.scrollHeight-O)))}finally{x(u,!1)}};"
        "let N=null,D=null;"
    )
    if candidate_source.count(before) != 1:
        raise RuntimeError(
            "Unable to locate Open WebUI's compiled loadMoreMessages function. "
            "The pinned indexed-history integration needs review."
        )
    candidate.write_text(
        candidate_source.replace(before, after, 1),
        encoding="utf-8",
    )
    messages_module = candidate
    break
if messages_module is None:
    raise RuntimeError(
        "Unable to locate Open WebUI's Messages.svelte chunk. "
        "The base image likely changed and the history patch needs review."
    )

# Keep the original managed URL bound to ImagePreview, but make the inline
# <img> use only Turtle's persisted static-thumbnail redirect. This prevents a
# long chat from downloading every original merely to paint message previews;
# opening ImagePreview remains the explicit original-object request.
image_module = None
for source_map in Path("/app/build/_app/immutable/chunks").glob("*.js.map"):
    source_map_text = source_map.read_text(encoding="utf-8")
    if (
        "export let imageClassName = 'rounded-lg';" not in source_map_text
        or "<ImagePreview bind:show={showImagePreview}" not in source_map_text
    ):
        continue
    candidate = source_map.with_suffix("")
    candidate_source = candidate.read_text(encoding="utf-8")
    before = (
        'Ye(N,"aria-label",V),Ye(B,"src",n(g)),Ye(B,"alt",c()),'
        'nt(B,1,Pt(_()))'
    )
    after = (
        'Ye(N,"aria-label",V),Ye(B,"src",n(g).replace('
        r'/\/api\/v1\/files\/([^/?#]+)\/content(?:\/[^?#]+)?/,'
        '"/api/v1/turtle/storage/files/$1/thumbnail")),'
        'Ye(B,"alt",c()),nt(B,1,Pt(_()))'
    )
    if candidate_source.count(before) != 1:
        raise RuntimeError(
            "Unable to locate Open WebUI's compiled inline Image source. "
            "The pinned thumbnail/original split needs review."
        )
    candidate.write_text(
        candidate_source.replace(before, after, 1),
        encoding="utf-8",
    )
    image_module = candidate
    break
if image_module is None:
    raise RuntimeError(
        "Unable to locate Open WebUI's Image.svelte chunk. "
        "The base image likely changed and the thumbnail patch needs review."
    )


# Register the dynamic Turtle provider while retaining Open WebUI's provider
# interface for every existing upload/generation call site.
provider_path = Path("/app/backend/open_webui/storage/provider.py")
replace_once(
    provider_path,
    """    elif storage_provider == 'azure':
        Storage = AzureStorageProvider()
    else:
        raise RuntimeError(f'Unsupported storage provider: {storage_provider}')
""",
    """    elif storage_provider == 'azure':
        Storage = AzureStorageProvider()
    elif storage_provider == 'turtle':
        from open_webui.turtle_storage.provider import TurtleStorageProvider

        Storage = TurtleStorageProvider()
    else:
        raise RuntimeError(f'Unsupported storage provider: {storage_provider}')
""",
)


# Open WebUI 0.11 makes Redis lock renew/release ownership-safe upstream. Keep
# this as a version gate so a future base image cannot silently regress to the
# old GET+DEL implementation.
socket_utils_path = Path("/app/backend/open_webui/socket/utils.py")
socket_utils_source = socket_utils_path.read_text(encoding="utf-8")
for atomic_lock_marker in (
    "_RENEW_SCRIPT = \"\"\"",
    "_RELEASE_SCRIPT = \"\"\"",
    "return bool(self.redis.eval(self._RENEW_SCRIPT, 1, self.lock_name, self.lock_id, self.timeout_secs))",
    "self.redis.eval(self._RELEASE_SCRIPT, 1, self.lock_name, self.lock_id)",
):
    if socket_utils_source.count(atomic_lock_marker) != 1:
        raise RuntimeError(
            "Open WebUI's ownership-safe Redis lock implementation changed. "
            "The websocket cleanup lock needs review."
        )

socket_main_path = Path("/app/backend/open_webui/socket/main.py")
replace_once(
    socket_main_path,
    """                await asyncio.sleep(SESSION_POOL_TIMEOUT)
        finally:
            session_release_func()
""",
    """                await asyncio.sleep(
                    min(
                        SESSION_POOL_TIMEOUT,
                        max(1, WEBSOCKET_REDIS_LOCK_TIMEOUT // 2),
                    )
                )
        finally:
            session_release_func()
""",
)


# Expose the authenticated storage/user-space/admin API.
main_path = Path("/app/backend/open_webui/main.py")
replace_once(
    main_path,
    """from open_webui.routers.retrieval import (
""",
    """from open_webui.turtle_admin.router import router as turtle_admin_router
from open_webui.turtle_auth.router import router as turtle_auth_router
from open_webui.turtle_auth.service import public_registration_enabled
from open_webui.turtle_chat.router import router as turtle_chat_router
from open_webui.turtle_project_api.router import (
    proxy_router as turtle_project_api_proxy_router,
    router as turtle_project_api_router,
)
from open_webui.turtle_storage.pump import RejectServerFileUploadMiddleware
from open_webui.turtle_storage.router import router as turtle_storage_router
from open_webui.turtle_static import TurtleStaticCacheMiddleware
from open_webui.routers.retrieval import (
""",
)

# Local PostgreSQL history is authoritative. After a user deletes a chat,
# enqueue cleanup of only the exact upstream resources previously recorded by
# Turtle. Notification failure never rolls back the local deletion; the
# Gateway also performs a periodic orphan scan.
chats_router_path = Path("/app/backend/open_webui/routers/chats.py")
replace_once(
    chats_router_path,
    """from open_webui.utils.models import get_all_models
""",
    """from open_webui.utils.models import get_all_models
from open_webui.turtle_chat.history import get_chat_envelope, initial_chat_page
from open_webui.turtle_chat.upstream_cleanup import schedule_upstream_cleanup
""",
)
replace_once(
    chats_router_path,
    """    result = await Chats.delete_chats_by_user_id(user.id, db=db)
    if result:
        await publish_event(
            request,
            EVENTS.CHAT_DELETED_ALL,
            actor=user,
            subject_id=user.id,
            subject_type='user',
        )
    return result
""",
    """    result = await Chats.delete_chats_by_user_id(user.id, db=db)
    if result:
        await publish_event(
            request,
            EVENTS.CHAT_DELETED_ALL,
            actor=user,
            subject_id=user.id,
            subject_type='user',
        )
        schedule_upstream_cleanup(user_id=user.id)
    return result
""",
)
replace_once(
    chats_router_path,
    """    if user.role == 'admin':
        result = await Chats.delete_chat_by_id(id, db=db)
    else:
        result = await Chats.delete_chat_by_id_and_user_id(id, user.id, db=db)

    if result:
        await publish_event(
            request,
            EVENTS.CHAT_DELETED,
            actor=user,
            subject_id=id,
            data={'owner_id': chat.user_id},
        )
    return result
""",
    """    if user.role == 'admin':
        result = await Chats.delete_chat_by_id(id, db=db)
    else:
        result = await Chats.delete_chat_by_id_and_user_id(id, user.id, db=db)

    if result:
        await publish_event(
            request,
            EVENTS.CHAT_DELETED,
            actor=user,
            subject_id=id,
            data={'owner_id': chat.user_id},
        )
        schedule_upstream_cleanup(chat_id=id, user_id=chat.user_id)
    return result
""",
)
replace_once(
    chats_router_path,
    """        await publish_event(
            request,
            EVENTS.CHAT_UPDATED,
            actor=user,
            subject_id=id,
            data={'title': chat.title},
        )
        return ChatResponse.model_validate(chat, from_attributes=True)
""",
    """        await publish_event(
            request,
            EVENTS.CHAT_UPDATED,
            actor=user,
            subject_id=id,
            data={'title': chat.title},
        )
        if request.headers.get('X-Turtle-History-Response') == 'paged':
            envelope = await get_chat_envelope(id, db=db)
            if envelope is not None:
                return await initial_chat_page(envelope, db=db)
        return ChatResponse.model_validate(chat, from_attributes=True)
""",
)
replace_once(
    main_path,
    """app.include_router(files.router, prefix='/api/v1/files', tags=['files'])
app.include_router(functions.router, prefix='/api/v1/functions', tags=['functions'])
""",
    """app.include_router(files.router, prefix='/api/v1/files', tags=['files'])
app.include_router(turtle_admin_router, prefix='/api/v1/turtle/admin', tags=['turtle-admin'])
app.include_router(turtle_auth_router, prefix='/api/v1/turtle/auth', tags=['turtle-auth'])
app.include_router(turtle_storage_router, prefix='/api/v1/turtle/storage', tags=['turtle-storage'])
app.include_router(turtle_chat_router, prefix='/api/v1/turtle/chat', tags=['turtle-chat'])
app.include_router(turtle_project_api_router, prefix='/api/v1/turtle/project-api', tags=['turtle-project-api'])
app.include_router(turtle_project_api_proxy_router, prefix='/api/project/v1', tags=['turtle-project-api-proxy'])
app.include_router(functions.router, prefix='/api/v1/functions', tags=['functions'])
""",
)

replace_once(
    main_path,
    """            'enable_signup': config.get('ui.enable_signup'),
""",
    """            # Turtle owns the durable registration switch because generic
            # Open WebUI persistent configuration is intentionally disabled.
            # The empty installation always keeps the first-admin path visible.
            'enable_signup': onboarding or await public_registration_enabled(),
""",
)

replace_once(
    main_path,
    """# --- static assets & files ---
# Serve build-time static assets (CSS, JS, images, favicon, etc.)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
""",
    """# The administrator console is a separate document, not a chat-page
# modal. Its APIs still enforce the normal Open WebUI administrator role.
@app.get('/admin')
@app.get('/admin/')
async def turtle_admin_console():
    return FileResponse(
        os.path.join(STATIC_DIR, 'turtle-admin.html'),
        media_type='text/html',
        headers={'Cache-Control': 'no-store'},
    )


@app.get('/projects')
@app.get('/projects/')
async def turtle_project_api_console():
    return FileResponse(
        os.path.join(STATIC_DIR, 'turtle-project-api.html'),
        media_type='text/html',
        headers={'Cache-Control': 'no-store'},
    )


# --- static assets & files ---
# Serve build-time static assets (CSS, JS, images, favicon, etc.)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
""",
)


# Enforce per-user runtime permissions, model windows, concurrency, and
# sanitized request tracking inside the authenticated Open WebUI request path.
# The Gateway remains loopback-only and still validates the global route allowlist.
openai_path = Path("/app/backend/open_webui/routers/openai.py")
replace_once(
    openai_path,
    """from open_webui.models.users import UserModel
from open_webui.utils.access_control import check_model_access, has_connection_access, has_permission
""",
    """from open_webui.models.users import UserModel
from open_webui.turtle_chat.metering import (
    fail_chat_request,
    finalize_chat_response,
    mark_chat_connected,
    mark_chat_upstream_started,
    prepare_chat_request,
    release_chat_request,
    tracked_chat_stream,
)
from open_webui.utils.access_control import check_model_access, has_connection_access, has_permission
""",
)

# `main.chat_completion` performs the first native model ACL check before it
# starts the background chat task. `utils.chat.generate_chat_completion`
# performs the same check again inside that task. The first Turtle exemption
# alone therefore lets the HTTP endpoint return 200 before ordinary users
# receive an asynchronous "Model not found". Apply the same exact-alias
# exemption at the second layer; all Turtle subscription, Provider-group,
# quota, concurrency and Gateway allowlist checks remain in force.
chat_utils_path = Path("/app/backend/open_webui/utils/chat.py")
replace_once(
    chat_utils_path,
    """        # Check if user has access to the model
        if not bypass_filter and user.role == 'user':
            try:
                await check_model_access(user, model)
            except Exception as e:
                raise e
""",
    """        # Check if user has access to the model. Turtle aliases are
        # deployment-managed and are authorized by prepare_chat_request.
        if (
            model_id not in {'gpt-5-web', 'claude-web'}
            and not bypass_filter
            and user.role == 'user'
        ):
            try:
                await check_model_access(user, model)
            except Exception as e:
                raise e
    """,
)

# `generate_chat_completion` finally dispatches into
# `routers.openai.generate_openai_chat_completion`, which applies a third
# native custom-model ACL. Turtle aliases intentionally have no Models row, so
# ordinary users otherwise fail here before the request reaches the Gateway.
# Skip only that redundant ACL for the two deployment-managed aliases.
replace_once(
    openai_path,
    """    if model_info:
        if model_info.base_model_id:
            base_model_id = (
                request.base_model_id if hasattr(request, 'base_model_id') else model_info.base_model_id
            )  # Use request's base_model_id if available
            payload['model'] = base_model_id
            model_id = base_model_id

        params = model_info.params.model_dump()

        if params:
            system = params.pop('system', None)

            payload = apply_model_params_to_body_openai(params, payload)
            if not bypass_system_prompt:
                payload = await apply_system_prompt_to_body(system, payload, metadata, user)

        await check_model_access(user, model_info, bypass_filter)
    else:
        await check_model_access(user, None, bypass_filter)
""",
    """    if model_info:
        if model_info.base_model_id:
            base_model_id = (
                request.base_model_id if hasattr(request, 'base_model_id') else model_info.base_model_id
            )  # Use request's base_model_id if available
            payload['model'] = base_model_id
            model_id = base_model_id

        params = model_info.params.model_dump()

        if params:
            system = params.pop('system', None)

            payload = apply_model_params_to_body_openai(params, payload)
            if not bypass_system_prompt:
                payload = await apply_system_prompt_to_body(system, payload, metadata, user)

        if model_id not in {'gpt-5-web', 'claude-web'}:
            await check_model_access(user, model_info, bypass_filter)
    elif model_id not in {'gpt-5-web', 'claude-web'}:
        await check_model_access(user, None, bypass_filter)
""",
)

# A partial/stale shared model cache must not turn a valid Turtle Provider
# alias into a user-visible "Model not found". Refresh once only on this rare
# error path; unknown model identifiers still fail closed.
replace_once(
    main_path,
    """    model_id = form_data.get('model', None)
    model_item = form_data.pop('model_item', {})
    tasks = form_data.pop('background_tasks', None)

    metadata = {}
""",
    """    model_id = form_data.get('model', None)
    model_item = form_data.pop('model_item', {})
    tasks = form_data.pop('background_tasks', None)

    if (
        model_id in {'gpt-5-web', 'claude-web'}
        and model_id not in request.app.state.MODELS
    ):
        await get_all_models(request, refresh=True, user=user)

    metadata = {}
""",
)

# Turtle's two deployment-managed aliases are deliberately not persisted as
# Open WebUI custom-model rows. Native model ACL therefore reports
# "Model not found" for every ordinary user even after the shared model cache
# contains the alias. Skip only that redundant native ACL layer for the two
# exact aliases; Turtle subscription, Provider-group, quota and concurrency
# checks still run in prepare_chat_request, while every other model keeps the
# stock Open WebUI access-control path.
replace_once(
    main_path,
    """            # Check if user has access to the model
            if not BYPASS_MODEL_ACCESS_CONTROL and (user.role != 'admin' or not BYPASS_ADMIN_ACCESS_CONTROL):
                try:
                    await check_model_access(user, model, model_info=model_info)
                except Exception as e:
                    raise e
""",
    """            # Check if user has access to the model. Turtle aliases are
            # deployment-managed and are authorized by prepare_chat_request.
            if (
                model_id not in {'gpt-5-web', 'claude-web'}
                and not BYPASS_MODEL_ACCESS_CONTROL
                and (user.role != 'admin' or not BYPASS_ADMIN_ACCESS_CONTROL)
            ):
                try:
                    await check_model_access(user, model, model_info=model_info)
                except Exception as e:
                    raise e
""",
)

# Turtle's two deployment-managed Provider aliases are authorized by the
# Provider model-group policy below, not by Open WebUI's separate Model
# AccessGrant table. Keep every actually published alias visible so the native
# model selector cannot become empty after an administrator assigns Turtle
# groups. Per-lane authorization remains fail-closed in Turtle metering and the
# Gateway allowlist.
model_utils_path = Path("/app/backend/open_webui/utils/models.py")
replace_once(
    model_utils_path,
    """        filtered_models = []
        for model in models:
            if model.get('arena'):
""",
    """        filtered_models = []
        for model in models:
            if model.get('id') in {'gpt-5-web', 'claude-web'}:
                filtered_models.append(model)
                continue
            if model.get('arena'):
""",
)
replace_once(
    openai_path,
    """    filtered_models = []
    for model in models.get('data', []):
        model_info = model_infos.get(model['id'])
""",
    """    filtered_models = []
    for model in models.get('data', []):
        if model.get('id') in {'gpt-5-web', 'claude-web'}:
            filtered_models.append(model)
            continue
        model_info = model_infos.get(model['id'])
""",
)


# Keep registration durable across Open WebUI restarts and require the
# server-side Turnstile Siteverify decision before any user row is created.
auth_utils_path = Path("/app/backend/open_webui/utils/auth.py")
replace_once(
    auth_utils_path,
    """def get_verified_user(user=Depends(get_current_user)):
    if user.role not in VERIFIED_USER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user
""",
    """async def get_verified_user(
    request: Request,
    user=Depends(get_current_user),
):
    from open_webui.turtle_auth.service import public_auth_security_config

    site_access = await public_auth_security_config()
    if user.role != 'admin' and site_access.get('maintenance_enabled'):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(
                site_access.get('maintenance_message')
                or '系统正在维护，请稍后再试。'
            ),
        )
    if user.role == 'pending' and request.method.upper() in {'GET', 'HEAD', 'OPTIONS'}:
        return user
    if user.role not in {'user', 'admin'}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user
""",
)

auths_path = Path("/app/backend/open_webui/routers/auths.py")
replace_once(
    auths_path,
    """from open_webui.utils.access_control import get_permissions, has_permission
""",
    """from open_webui.turtle_auth.service import (
    disable_registration_after_first_admin,
    enforce_signup_security,
)
from open_webui.utils.access_control import get_permissions, has_permission
""",
)
replace_once(
    auths_path,
    """        await Config.upsert({'ui.enable_signup': False})
""",
    """        await Config.upsert({'ui.enable_signup': False})
        await disable_registration_after_first_admin()
""",
)
replace_once(
    auths_path,
    """        if has_users:
            if not await Config.get('ui.enable_signup') or not await Config.get('ui.enable_login_form'):
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
""",
    """        if has_users:
            if not await Config.get('ui.enable_login_form'):
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
""",
)
replace_once(
    auths_path,
    """    else:
        if has_users:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    if not validate_email_format(form_data.email.lower()):
""",
    """    else:
        if has_users:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    await enforce_signup_security(
        request,
        form_data.turnstile_token,
        has_users=has_users,
    )

    if not validate_email_format(form_data.email.lower()):
""",
)

auth_models_path = Path("/app/backend/open_webui/models/auths.py")
replace_once(
    auth_models_path,
    """from pydantic import BaseModel, field_validator
""",
    """from pydantic import BaseModel, Field, field_validator
""",
)
replace_once(
    auth_models_path,
    """class SignupForm(BaseModel):
    name: str
    email: str
    password: str
    profile_image_url: str | None = '/user.png'
""",
    """class SignupForm(BaseModel):
    name: str
    email: str
    password: str
    profile_image_url: str | None = '/user.png'
    turnstile_token: str | None = Field(
        default=None,
        max_length=2048,
        repr=False,
        exclude=True,
    )
""",
)

# Open WebUI 0.11 introduces a per-user "What's New" modal in addition to its
# version-update toast. Turtle owns release communication through the
# announcement system, so neither upstream surface should appear to end users
# (including administrators). Keep the stored settings otherwise intact.
users_router_path = Path("/app/backend/open_webui/routers/users.py")
replace_once(
    users_router_path,
    """    # user already fetched by get_verified_user — no need to refetch
    return user.settings
""",
    """    # user already fetched by get_verified_user — no need to refetch
    settings = dict(user.settings or {})
    ui_settings = dict(settings.get('ui') or {})
    ui_settings['showChangelog'] = False
    ui_settings['showUpdateToast'] = False
    settings['ui'] = ui_settings
    return settings
""",
)
replace_once(
    users_router_path,
    """    updated_user_settings = form_data.model_dump()
    ui_settings = updated_user_settings.get('ui')
""",
    """    updated_user_settings = form_data.model_dump()
    ui_settings = updated_user_settings.get('ui')
    if isinstance(ui_settings, dict):
        ui_settings['showChangelog'] = False
        ui_settings['showUpdateToast'] = False
""",
)
replace_once(
    openai_path,
    """    payload = {**form_data}
    metadata = payload.pop('metadata', None)

    model_id = form_data.get('model')
""",
    """    payload = {**form_data}
    metadata = payload.pop('metadata', None)
    turtle_chat_reservation = None

    model_id = form_data.get('model')
""",
)
replace_once(
    openai_path,
    """    requested_model = payload.get('model')
    # For Chat Completions, strip image parts from multimodal tool messages
""",
    """    requested_model = payload.get('model')
    turtle_chat_reservation = await prepare_chat_request(
        user,
        payload,
        internal_task=bool(
            isinstance(metadata, dict)
            and metadata.get('task')
            and hasattr(request.state, 'metadata')
            and metadata.get('user_id') == user.id
        ),
        chat_id=(
            str(metadata.get('chat_id'))
            if isinstance(metadata, dict) and metadata.get('chat_id')
            else None
        ),
    )
    # For Chat Completions, strip image parts from multimodal tool messages
""",
)
replace_once(
    openai_path,
    """    try:
        session = await get_session()

        r = await session.request(
""",
    """    try:
        session = await get_session()

        await mark_chat_upstream_started(turtle_chat_reservation)
        r = await session.request(
""",
)
replace_once(
    openai_path,
    """        r = await session.request(
            method='POST',
            url=request_url,
            data=payload,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=is_streaming_request),
        )

        # Check if response is SSE
""",
    """        r = await session.request(
            method='POST',
            url=request_url,
            data=payload,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=is_streaming_request),
        )
        await mark_chat_connected(turtle_chat_reservation, r.status)

        # Check if response is SSE
""",
)
replace_once(
    openai_path,
    """            if r.status >= 400:
                error_body = await r.text()
                log.error(
""",
    """            if r.status >= 400:
                error_body = await r.text()
                await fail_chat_request(
                    turtle_chat_reservation,
                    error_type='upstream_http',
                    error_phase='response_headers',
                    http_status=r.status,
                )
                log.error(
""",
)
replace_once(
    openai_path,
    """            streaming = True
            return StreamingResponse(
                stream_wrapper(r, content_handler=stream_chunks_handler),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
""",
    """            streaming = True
            return StreamingResponse(
                tracked_chat_stream(
                    stream_wrapper(r, content_handler=stream_chunks_handler),
                    turtle_chat_reservation,
                ),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
""",
)
replace_once(
    openai_path,
    """            if r.status >= 400:
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=url,
                    api_key=key,
                    status=r.status,
                    requested_model=requested_model,
                    upstream_error=response,
                )
""",
    """            if r.status >= 400:
                await fail_chat_request(
                    turtle_chat_reservation,
                    error_type='upstream_http',
                    error_phase='response_body',
                    http_status=r.status,
                )
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=url,
                    api_key=key,
                    status=r.status,
                    requested_model=requested_model,
                    upstream_error=response,
                )
""",
)
replace_once(
    openai_path,
    """            # Convert Responses API result to simple format
            if is_responses and isinstance(response, dict):
                response = convert_responses_result(response)

            return response
""",
    """            # Convert Responses API result to simple format
            if is_responses and isinstance(response, dict):
                response = convert_responses_result(response)

            await finalize_chat_response(turtle_chat_reservation, response, r.status)
            return response
""",
)
replace_once(
    openai_path,
    """    except Exception as e:
        log.exception(e)

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
""",
    """    except Exception as e:
        log.exception(e)

        await fail_chat_request(
            turtle_chat_reservation,
            error_type='upstream_connection',
            error_phase='connect' if r is None else 'response_body',
            http_status=r.status if r else None,
        )
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
""",
)
replace_once(
    openai_path,
    """    finally:
        if not streaming:
            await cleanup_response(r)


async def embeddings(request: Request, form_data: dict, user):
""",
    """    finally:
        if not streaming:
            await release_chat_request(turtle_chat_reservation, outcome='application_cleanup')
            await cleanup_response(r)


async def embeddings(request: Request, form_data: dict, user):
""",
)


# Give every conversation one immutable Turtle provider family. The small meta
# value lets the sidebar isolate workspaces without loading full chat JSON, and
# keeps imported or legacy conversations deterministic.
chats_path = Path("/app/backend/open_webui/models/chats.py")
replace_once(
    chats_path,
    """from open_webui.models.tags import Tag, TagModel, Tags
from open_webui.utils.misc import get_output_text, sanitize_data_for_db, sanitize_text_for_db
""",
    """from open_webui.models.tags import Tag, TagModel, Tags
from open_webui.turtle_chat.history import (
    invalidate_chat_history_index,
    sync_chat_history_index,
    sync_indexed_chat_message,
)
from open_webui.turtle_chat.provider import meta_with_provider, provider_for_chat
from open_webui.utils.misc import get_output_text, sanitize_data_for_db, sanitize_text_for_db
""",
)
replace_once(
    chats_path,
    """class ChatTitleIdResponse(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int
    last_read_at: int | None = None
    snippet: str | None = None
""",
    """class ChatTitleIdResponse(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int
    last_read_at: int | None = None
    snippet: str | None = None
    provider: str | None = None
""",
)
replace_once(
    chats_path,
    """    async def get_chat_title_id_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        include_folders: bool = False,
        include_pinned: bool = False,
        sort_by: str = 'updated_at',
        sort_dir: str = 'desc',
        skip: int | None = None,
        limit: int | None = None,
        db: AsyncSession | None = None,
    ) -> list[ChatTitleIdResponse]:
        async with get_async_db_context(db) as session:
            stmt = select(Chat.id, Chat.title, Chat.updated_at, Chat.created_at, Chat.last_read_at).filter_by(
                user_id=user_id
            )
""",
    """    async def get_chat_title_id_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        include_folders: bool = False,
        include_pinned: bool = False,
        sort_by: str = 'updated_at',
        sort_dir: str = 'desc',
        skip: int | None = None,
        limit: int | None = None,
        db: AsyncSession | None = None,
    ) -> list[ChatTitleIdResponse]:
        async with get_async_db_context(db) as session:
            stmt = select(
                Chat.id,
                Chat.title,
                Chat.updated_at,
                Chat.created_at,
                Chat.last_read_at,
                Chat.meta,
            ).filter_by(
                user_id=user_id
            )
""",
)
replace_once(
    chats_path,
    """            result = await session.execute(stmt)
            all_chats = result.all()

            return [
                ChatTitleIdResponse.model_validate(
                    {
                        'id': chat[0],
                        'title': chat[1],
                        'updated_at': chat[2],
                        'created_at': chat[3],
                        'last_read_at': chat[4],
                    }
                )
                for chat in all_chats
            ]
""",
    """            result = await session.execute(stmt)
            all_chats = result.all()

            return [
                ChatTitleIdResponse.model_validate(
                    {
                        'id': chat[0],
                        'title': chat[1],
                        'updated_at': chat[2],
                        'created_at': chat[3],
                        'last_read_at': chat[4],
                        'provider': provider_for_chat(None, chat[5]),
                    }
                )
                for chat in all_chats
            ]
""",
)
replace_once(
    chats_path,
    """                    'chat': self._clean_null_bytes(form_data.chat),
                    'folder_id': form_data.folder_id,
                    'meta': internal_meta or {},
""",
    """                    'chat': self._clean_null_bytes(form_data.chat),
                    'folder_id': form_data.folder_id,
                    'meta': meta_with_provider(internal_meta or {}, form_data.chat),
""",
)
replace_once(
    chats_path,
    """                'chat': self._clean_null_bytes(form_data.chat),
                'meta': form_data.meta,
                'variables': form_data.variables or {},
                'pinned': form_data.pinned,
""",
    """                'chat': self._clean_null_bytes(form_data.chat),
                'meta': meta_with_provider(form_data.meta, form_data.chat),
                'variables': form_data.variables or {},
                'pinned': form_data.pinned,
""",
)
replace_once(
    chats_path,
    """                chat_item.chat = self._clean_null_bytes(chat)
                chat_item.title = self._clean_null_bytes(chat['title']) if 'title' in chat else 'New Chat'
                if any(key in chat for key in ('history', 'messages', 'currentId', 'branchPointMessageId')):
""",
    """                chat_item.chat = self._clean_null_bytes(chat)
                chat_item.title = self._clean_null_bytes(chat['title']) if 'title' in chat else 'New Chat'
                chat_item.meta = meta_with_provider(chat_item.meta, chat)
                if any(key in chat for key in ('history', 'messages', 'currentId', 'branchPointMessageId')):
""",
)
replace_once(
    chats_path,
    """            session.add(chat_item)
            await session.commit()

            # Dual-write initial messages to chat_message table
""",
    """            session.add(chat_item)
            await session.commit()

            try:
                await sync_chat_history_index(
                    id,
                    user_id,
                    form_data.chat,
                    chat.updated_at,
                    db=session,
                )
            except Exception as e:
                log.warning('Failed to index initial chat history for %s: %s', id, type(e).__name__)

            # Dual-write initial messages to chat_message table
""",
)
replace_once(
    chats_path,
    """            session.add_all(chats)
            await session.commit()

            # Dual-write messages to chat_message table
""",
    """            session.add_all(chats)
            await session.commit()

            for form_data, imported_chat in zip(chat_import_forms, chats):
                try:
                    await sync_chat_history_index(
                        imported_chat.id,
                        user_id,
                        form_data.chat,
                        imported_chat.updated_at,
                        db=session,
                    )
                except Exception as e:
                    log.warning(
                        'Failed to index imported chat history for %s: %s',
                        imported_chat.id,
                        type(e).__name__,
                    )

            # Dual-write messages to chat_message table
""",
)
replace_once(
    chats_path,
    """                if touch:
                    chat_item.updated_at = int(time.time())

                await session.commit()

                return ChatModel.model_validate(chat_item)
""",
    """                if touch:
                    chat_item.updated_at = int(time.time())

                await session.commit()

                try:
                    await sync_chat_history_index(
                        id,
                        chat_item.user_id,
                        chat_item.chat,
                        chat_item.updated_at,
                        db=session,
                    )
                except Exception as e:
                    log.warning('Failed to index updated chat history for %s: %s', id, type(e).__name__)
                    try:
                        await invalidate_chat_history_index(id)
                    except Exception:
                        pass

                return ChatModel.model_validate(chat_item)
""",
)
replace_once(
    chats_path,
    """            # Dual-write to chat_message table
            try:
                await ChatMessages.upsert_message(
                    message_id=message_id,
                    chat_id=id,
                    user_id=user_id,
                    data=saved_message,
                )
            except Exception as e:
                log.warning(f'Failed to write to chat_message table: {e}')

            return updated_chat
""",
    """            # Dual-write to chat_message table
            try:
                await ChatMessages.upsert_message(
                    message_id=message_id,
                    chat_id=id,
                    user_id=user_id,
                    data=saved_message,
                )
            except Exception as e:
                log.warning(f'Failed to write to chat_message table: {e}')

            try:
                await sync_indexed_chat_message(
                    id,
                    user_id,
                    message_id,
                    saved_message,
                    updated_chat.updated_at,
                    updated_chat.current_message_id,
                    len(((updated_chat.chat or {}).get('history') or {}).get('messages') or {}),
                )
            except Exception as e:
                log.warning(
                    'Failed to update indexed chat message for %s: %s',
                    id,
                    type(e).__name__,
                )

            return updated_chat
""",
)
replace_once(
    chats_path,
    """            await self.backfill_messages_by_chat_id(id, user_id, messages)
            await ChatMessages.delete_message_ids_by_chat_id(id, deleted_ids)

            return updated_chat
""",
    """            await self.backfill_messages_by_chat_id(id, user_id, messages)
            await ChatMessages.delete_message_ids_by_chat_id(id, deleted_ids)
            try:
                await sync_chat_history_index(
                    id,
                    user_id,
                    updated_chat.chat,
                    updated_chat.updated_at,
                )
            except Exception as e:
                log.warning(
                    'Failed to rebuild indexed chat after deletion for %s: %s',
                    id,
                    type(e).__name__,
                )
                try:
                    await invalidate_chat_history_index(id)
                except Exception:
                    pass

            return updated_chat
""",
)
replace_once(
    chats_path,
    """                flag_modified(chat_item, 'chat')
                await session.commit()

                return ChatModel.model_validate(chat_item)
        except Exception:
            return None

    async def add_message_files_by_id_and_message_id""",
    """                flag_modified(chat_item, 'chat')
                await session.commit()
                updated_chat = ChatModel.model_validate(chat_item)
                saved_message = (
                    ((updated_chat.chat or {}).get('history') or {})
                    .get('messages', {})
                    .get(message_id)
                )
                if isinstance(saved_message, dict):
                    try:
                        await sync_indexed_chat_message(
                            id,
                            updated_chat.user_id,
                            message_id,
                            saved_message,
                            updated_chat.updated_at,
                            updated_chat.current_message_id,
                            len(((updated_chat.chat or {}).get('history') or {}).get('messages') or {}),
                            db=session,
                        )
                    except Exception as e:
                        log.warning(
                            'Failed to update indexed message status for %s: %s',
                            id,
                            type(e).__name__,
                        )

                return updated_chat
        except Exception:
            return None

    async def add_message_files_by_id_and_message_id""",
)

replace_once(
    main_path,
    """app.add_middleware(RedirectMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
""",
    """app.add_middleware(RedirectMiddleware)
app.add_middleware(RejectServerFileUploadMiddleware)
app.add_middleware(TurtleStaticCacheMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
""",
)


# Enforce per-user storage capacity on ordinary server-side uploads and use
# presigned GET redirects for authenticated cloud-file downloads.
files_path = Path("/app/backend/open_webui/routers/files.py")
replace_once(
    files_path,
    """from fastapi.responses import FileResponse, StreamingResponse
""",
    """from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
""",
)
replace_once(
    files_path,
    """from open_webui.storage.provider import Storage
from open_webui.utils.auth import get_admin_user, get_verified_user
""",
    """from open_webui.storage.provider import Storage
from open_webui.turtle_storage.quota import (
    MediaTooLargeError,
    QuotaExceededError,
    ensure_media_size,
    ensure_upload_capacity,
    media_size_http_exception,
    quota_http_exception,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
""",
)
replace_once(
    files_path,
    """        if max_size and len(contents) > int(max_size) * 1024 * 1024:
            await asyncio.to_thread(Storage.delete_file, file_path)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=ERROR_MESSAGES.FILE_TOO_LARGE(size=f'{max_size} MB'),
            )

        # SHA-256 of raw uploaded bytes for incremental sync diffing.
""",
    """        if max_size and len(contents) > int(max_size) * 1024 * 1024:
            await asyncio.to_thread(Storage.delete_file, file_path)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=ERROR_MESSAGES.FILE_TOO_LARGE(size=f'{max_size} MB'),
            )

        try:
            ensure_media_size(file.content_type, len(contents))
        except MediaTooLargeError as media_size_error:
            await asyncio.to_thread(Storage.delete_file, file_path)
            raise media_size_http_exception(media_size_error) from media_size_error

        try:
            await ensure_upload_capacity(user.id, len(contents), db, role=user.role)
        except QuotaExceededError as quota_error:
            await asyncio.to_thread(Storage.delete_file, file_path)
            raise quota_http_exception(quota_error) from quota_error

        # SHA-256 of raw uploaded bytes for incremental sync diffing.
""",
)
replace_once(
    files_path,
    """@router.get('/{id}/content')
async def get_file_content_by_id(
    id: str,
    user=Depends(get_verified_user),
    attachment: bool = Query(False),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if file.user_id == user.id or user.role == 'admin' or await has_access_to_file(id, 'read', user, db=db):
        try:
            file_path = await asyncio.to_thread(Storage.get_file, file.path)
            file_path = Path(file_path)
""",
    """@router.get('/{id}/content')
async def get_file_content_by_id(
    id: str,
    user=Depends(get_verified_user),
    attachment: bool = Query(False),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if file.user_id == user.id or user.role == 'admin' or await has_access_to_file(id, 'read', user, db=db):
        direct_url = (
            Storage.presign_download(
                file.path,
                filename=(file.meta or {}).get('name', file.filename),
                attachment=attachment,
            )
            if hasattr(Storage, 'presign_download')
            else None
        )
        if direct_url:
            return RedirectResponse(
                direct_url,
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={
                    'Cache-Control': 'private, max-age=300',
                    'Vary': 'Authorization, Cookie',
                },
            )
        try:
            file_path = await asyncio.to_thread(Storage.get_file, file.path)
            file_path = Path(file_path)
""",
)
replace_once(
    files_path,
    """        result = await Files.delete_file_by_id(id, db=db)
        if result:
            try:
                await asyncio.to_thread(Storage.delete_file, file.path)
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'file-{id}')
            except Exception as e:
                log.exception(e)
                log.error('Error deleting files')
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT('Error deleting files'),
                )
            await publish_event(
""",
    """        try:
            await asyncio.to_thread(Storage.delete_file, file.path)
        except Exception as e:
            log.warning(f'Object deletion failed before database deletion: {type(e).__name__}')
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=ERROR_MESSAGES.DEFAULT('Error deleting stored object'),
            )

        result = await Files.delete_file_by_id(id, db=db)
        if result:
            try:
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'file-{id}')
            except Exception as e:
                log.warning(f'File vector cleanup failed after deletion: {type(e).__name__}')
            await publish_event(
""",
)
replace_once(
    files_path,
    """    if file.user_id == user.id or user.role == 'admin' or await has_access_to_file(id, 'read', user, db=db):
        file_path = file.path

        # Handle Unicode filenames
""",
    """    if file.user_id == user.id or user.role == 'admin' or await has_access_to_file(id, 'read', user, db=db):
        direct_url = (
            Storage.presign_download(
                file.path,
                filename=(file.meta or {}).get('name', file.filename),
                attachment=True,
            )
            if hasattr(Storage, 'presign_download')
            else None
        )
        if direct_url:
            return RedirectResponse(direct_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        file_path = file.path

        # Handle Unicode filenames
""",
)


# Open WebUI's native image-generation route normally downloads an OpenAI-style
# URL and re-uploads the bytes from the application host. In strict mode, force
# URL output and hand that URL to the external Pump instead.
images_router_path = Path("/app/backend/open_webui/routers/images.py")
replace_once(
    images_router_path,
    """from open_webui.constants import ERROR_MESSAGES
""",
    """from open_webui.constants import ERROR_MESSAGES
from open_webui.turtle_storage.media import persist_generated_media_url
from open_webui.turtle_storage.pump import strict_media_mode
""",
)
replace_once(
    images_router_path,
    """                **({} if not image_config.IMAGES_OPENAI_API_PARAMS else image_config.IMAGES_OPENAI_API_PARAMS),
            }

            session = await get_session()
""",
    """                **({} if not image_config.IMAGES_OPENAI_API_PARAMS else image_config.IMAGES_OPENAI_API_PARAMS),
            }
            if strict_media_mode():
                data['response_format'] = 'url'

            session = await get_session()
""",
)
replace_once(
    images_router_path,
    """            for image in res['data']:
                if image_url := image.get('url', None):
                    image_data, content_type = await get_image_data(
                        image_url,
                        {k: v for k, v in headers.items() if k != 'Content-Type'},
                    )
                else:
                    image_data, content_type = await get_image_data(image['b64_json'])

                _, url = await upload_image(request, image_data, content_type, {**data, **metadata}, user)
                images.append({'url': url})
            return images

        elif image_config.IMAGE_GENERATION_ENGINE == 'gemini':
""",
    """            for image in res['data']:
                if image_url := image.get('url', None):
                    if strict_media_mode():
                        url = await persist_generated_media_url(
                            request,
                            image_url,
                            {**data, **metadata},
                            user,
                        )
                        images.append({'url': url})
                        continue
                    image_data, content_type = await get_image_data(
                        image_url,
                        {k: v for k, v in headers.items() if k != 'Content-Type'},
                    )
                else:
                    if strict_media_mode():
                        raise HTTPException(
                            status_code=502,
                            detail='严格媒体隔离要求生图上游返回短期 URL',
                        )
                    image_data, content_type = await get_image_data(image['b64_json'])

                _, url = await upload_image(request, image_data, content_type, {**data, **metadata}, user)
                images.append({'url': url})
            return images

        elif image_config.IMAGE_GENERATION_ENGINE == 'gemini':
""",
)


# Keep managed COS images as short-lived URLs for model requests, mirror
# structured/Markdown/HTML response media, and persist the final rewritten
# output through the external pump; Open WebUI never reads the media body.
middleware_path = Path("/app/backend/open_webui/utils/middleware.py")
replace_once(
    middleware_path,
    """from open_webui.utils.filter import (
""",
    """from open_webui.turtle_storage.media import (
    bind_message_image_file_ids,
    carry_forward_message_images,
    get_presigned_model_image_source,
    persist_generated_media_url,
    persist_output_media,
    requested_image_count,
    visible_stream_output,
)
from open_webui.turtle_storage.pump import strict_media_mode
from open_webui.utils.filter import (
""",
)
replace_once(
    middleware_path,
    """                # Skip forced image generation when native FC is enabled - model can use generate_image tool
                if metadata.get('params', {}).get('function_calling') == 'legacy':
                    form_data = await chat_image_generation_handler(request, form_data, extra_params, user)
""",
    """                # The Turtle web-account route does not execute Open WebUI's native
                # generate_image tool. In strict mode use the deterministic image
                # endpoint so the external Pump, never the app host, moves bytes.
                if strict_media_mode() or metadata.get('params', {}).get('function_calling') == 'legacy':
                    form_data = await chat_image_generation_handler(request, form_data, extra_params, user)
""",
)
replace_once(
    middleware_path,
    """                form_data=CreateImageForm(**{'prompt': prompt}),
""",
    """                form_data=CreateImageForm(
                    **{
                        'prompt': prompt,
                        'n': requested_image_count(user_message),
                    }
                ),
""",
)
replace_once(
    middleware_path,
    """        if url.startswith('data:image/png;base64'):
            url = await get_image_url_from_base64(request, url, metadata, user)

        image_urls.append(url)
""",
    """        if url.startswith('data:image/') and not strict_media_mode():
            url = await get_image_url_from_base64(request, url, metadata, user)
        elif url.startswith(('http://', 'https://')):
            url = await persist_generated_media_url(request, url, metadata, user)

        image_urls.append(url)
""",
)
replace_once(
    middleware_path,
    """            image_url = item.get('image_url', {}).get('url', '')
            if image_url.startswith('data:image/'):
                new_content.append(item)
                continue

            try:
""",
    """            image_url = item.get('image_url', {}).get('url', '')
            if image_url.startswith('data:image/'):
                new_content.append(item)
                continue

            managed_source = await get_presigned_model_image_source(image_url, user)
            if managed_source:
                new_content.append(
                    {
                        'type': 'image_url',
                        'image_url': managed_source,
                    }
                )
                continue

            try:
""",
)
replace_once(
    middleware_path,
    """                # Mark all in-progress items as completed
                for item in output:
""",
    """                output = await persist_output_media(request, output, metadata, user)

                # Mark all in-progress items as completed
                for item in output:
""",
)


# Keep structured images out of the textual attachment inventory. They are
# forwarded as actual image_url parts below, while ordinary files still retain
# the helpful <attached_files> context.
replace_once(
    middleware_path,
    """        files_with_urls = [
            file
            for file in stored_message.get('files', [])
            if file.get('url') and not file.get('url').startswith('data:')
        ]
""",
    """        files_with_urls = [
            file
            for file in stored_message.get('files', [])
            if file.get('url')
            and not file.get('url').startswith('data:')
            and file.get('type') != 'image'
            and not str(file.get('content_type') or '').startswith('image/')
        ]
""",
)
replace_once(
    middleware_path,
    """            # Inject image files into content as image_url parts (mirrors frontend logic)
            for message in form_data['messages']:
""",
    """            # On the first DB-backed turn, the authoritative file ID is
            # already present even when content is still plain text. Bind it
            # before the upstream compatibility loop consumes file metadata.
            form_data['messages'] = bind_message_image_file_ids(form_data.get('messages', []))

            # Inject image files into content as image_url parts (mirrors frontend logic)
            for message in form_data['messages']:
""",
)
replace_once(
    middleware_path,
    """    if regeneration_prompt:
        form_data['messages'].append({'role': 'user', 'content': regeneration_prompt})

    if is_saved_chat_id(chat_id) and user_message_id:
""",
    """    if regeneration_prompt:
        form_data['messages'].append({'role': 'user', 'content': regeneration_prompt})

    # Bind first-turn preview URLs to immutable file IDs before historical
    # carry-forward and compaction can discard the accompanying file metadata.
    form_data['messages'] = bind_message_image_file_ids(form_data.get('messages', []))

    # Preserve historical images on the newest user turn before context
    # compaction so the active branch cannot discard them as old media.
    form_data['messages'] = carry_forward_message_images(form_data.get('messages', []))

    if is_saved_chat_id(chat_id) and user_message_id:
""",
)
replace_once(
    middleware_path,
    """    form_data = await convert_url_images_to_base64(form_data, user=user)

    event_emitter = await get_event_emitter(metadata)
""",
    """    # OpenaiAccount currently reads media only from the final user turn.
    # Carry images from this branch forward before converting managed file IDs
    # to signed CDN-first source envelopes so later questions can inspect them.
    form_data['messages'] = carry_forward_message_images(form_data.get('messages', []))
    form_data = await convert_url_images_to_base64(form_data, user=user)

    event_emitter = await get_event_emitter(metadata)
""",
)

replace_once(
    middleware_path,
    """            def full_output():
                return prior_output + output if prior_output else output
""",
    """            def full_output():
                current_output = prior_output + output if prior_output else output
                return visible_stream_output(current_output)
""",
)

replace_once(
    middleware_path,
    """                    elif usage:
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {'done': True, 'usage': usage},
                        )
                    else:
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {'done': True},
                        )
""",
    """                    elif usage:
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {'done': True, 'output': output, 'usage': usage},
                        )
                    else:
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {'done': True, 'output': output},
                        )
""",
)


# Foreground generation ends as soon as the answer has been stored and emitted.
# Title, tag, memory and other housekeeping jobs continue independently without
# holding chat:active=true or the composer stop button open.
replace_once(
    main_path,
    """        initial_title_generation = None
        if is_new_chat and tasks and TASKS.TITLE_GENERATION in tasks:
            initial_title_generation = tasks.pop(TASKS.TITLE_GENERATION)
""",
    """        # Keep the first title task on the normal post-response task list.
        # Upstream starts it immediately with the same Request object as the
        # foreground completion. On a one-account web worker that can make the
        # first assistant placeholder consume the title JSON instead of the
        # user's answer. The detached post-response runner below preserves fast
        # composer completion while isolating the two model calls.
        initial_title_generation = None
""",
)
replace_once(
    middleware_path,
    """async def background_tasks_handler(ctx):
""",
    """_turtle_background_jobs = set()


def schedule_background_tasks(ctx):
    async def runner():
        try:
            await background_tasks_handler(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Detached chat background tasks failed: %s', type(exc).__name__)

    task = asyncio.create_task(runner(), name='turtle-chat-background-tasks')
    _turtle_background_jobs.add(task)
    task.add_done_callback(_turtle_background_jobs.discard)


async def background_tasks_handler(ctx):
""",
)
replace_once(
    middleware_path,
    """                    await outlet_filter_handler(ctx)
                    await background_tasks_handler(ctx)

            response = build_response_object(response, merge_events_into_response(response_data, events))
""",
    """                    await outlet_filter_handler(ctx)
                    schedule_background_tasks(ctx)

            response = build_response_object(response, merge_events_into_response(response_data, events))
""",
)
replace_once(
    middleware_path,
    """                await outlet_filter_handler(ctx)
                await background_tasks_handler(ctx)
            except asyncio.CancelledError:
""",
    """                await outlet_filter_handler(ctx)
                schedule_background_tasks(ctx)
            except asyncio.CancelledError:
""",
)


# A newly submitted user turn proves older empty assistant placeholders are no
# longer active. Close those abandoned records so one failed branch cannot keep
# the sidebar or composer in a permanent generating state.
replace_once(
    main_path,
    """                    user_message = metadata.get('user_message') or {}
                    selected_chat_models = user_message.get('models') if isinstance(user_message, dict) else None
""",
    """                    user_message = metadata.get('user_message') or {}
                    current_assistant_ids = {
                        entry.get('message_id') for entry in message_ids if entry.get('message_id')
                    }
                    existing_messages = await Chats.get_messages_map_by_chat_id(chat_id) or {}
                    for stale_message_id, stale_message in existing_messages.items():
                        if stale_message_id in current_assistant_ids or not isinstance(stale_message, dict):
                            continue
                        if (
                            stale_message.get('role') == 'assistant'
                            and stale_message.get('done') is False
                            and not stale_message.get('content')
                            and not stale_message.get('output')
                            and not stale_message.get('error')
                        ):
                            await Chats.upsert_message_to_chat_by_id_and_message_id(
                                chat_id,
                                stale_message_id,
                                {'done': True},
                            )

                    selected_chat_models = user_message.get('models') if isinstance(user_message, dict) else None
""",
)
