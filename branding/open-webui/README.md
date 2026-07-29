# Turtle’s Chat Open WebUI brand layer

This directory builds a small derivative image on top of a pinned Open WebUI image.
It changes only:

- the `Turtle’s Chat` name and title;
- favicon, app icon, splash image, sidebar logo, and local ChatGPT/Claude Provider marks;
- the color, glass, radius, shadow, login, sidebar, and chat-input theme;
- system-driven light/dark appearance (manual stale theme choices are reset on load);
- form and chat inputs without the duplicated cyan focus rectangle;
- a top-left provider-family selector plus one official-style composer menu for version/thinking controls;
- per-device, per-family runtime preferences injected into compatible chat requests;
- server-enforced per-user version/thinking permissions with Pro disabled for ordinary users by default;
- an internal fixed-point chat ledger with reservation, first-content commit, zero-content release,
  administrator grants/corrections, and no prompt/response storage;
- browser-side image compression plus optional presigned Tencent COS upload;
- a dynamic local/COS provider, encrypted runtime configuration, resource-group-inherited per-user byte quota,
  and immutable user-ID object prefixes;
- a redesigned user media-space dialog with quota overview, category filters, preview cards,
  pagination, download/delete actions, and intentional empty/error/loading states;
- a standalone `/admin` console with overview, operations monitoring, Provider, user, resource/model-policy,
  storage, and system sections; Open WebUI Settings contains only one entry into that console;
- Provider-managed GPT/Claude chat display names without changing the stable model IDs, with Open WebUI's
  evaluation Arena disabled and any legacy `arena-model` removed from the visible model response;
- account-card reauthentication for deployment-mapped ChatGPT workers: pause scheduling, open only the
  mapped Chrome profile, require manual login, restart the worker, and recover only after a real probe;
- a redesigned storage page with provider status, local/COS configuration and media-processing limits;
  storage size, resource-group concurrency and per-user default concurrency live in the resource policy editor;
- user/resource-group/Provider/global server-side concurrency limits and Redis FIFO queueing without a composer queue banner,
  sanitized request latency/error metrics and container resource monitoring;
- external-pump response-image/video persistence and presigned model/download URLs;
- deployment-managed native `gpt-image` generation that forces URL output and deterministic Pump persistence;
- stream-safe generated media rendering plus a compact main-image/thumbnail gallery with edit,
  single-image download, and client-side multi-image ZIP download;
- strict rejection of server-side multipart and inline/non-HTTPS model media;
- Open WebUI's automatic custom-name suffix.

The current families are `GPT` and `Claude`. GPT defaults to `latest` / Medium, displayed as
“Latest · GPT-5.6”; its reviewed lanes are Instant (5.5), GPT-5.6 Thinking Medium/High/Extra High,
GPT-5.6 Pro, and the GPT-5.4 Thinking/Pro compatibility lanes. The 5.5 label is intentional because
ChatGPT's current Latest slider combines that Instant lane with the 5.6 Thinking/Pro lanes. Claude
publishes the same `turtle` metadata shape from its isolated worker; its five text lanes are production
verified, while media and terms review remain separate release gates. Options come from Provider metadata,
with the same safe built-in GPT fallback for initial page load. The server remains authoritative and
rejects unsupported selections even if browser data is modified.

The authenticated Open WebUI route adds a second, user-specific authority. Each user composes one resource
group, one GPT model group, and one Claude model group; the database permits at most one group within the
same Provider. Ordinary users initially receive Instant, Medium, High, and GPT-5.4 Thinking; Pro and Extra
High require an explicit administrator assignment. Its GPT group editor offers editable Go, Plus, 5× Pro,
and 20× Pro presets; its Claude editor offers Free, Pro, Max 5×, and Max 20× presets plus five-lane
success/activity statistics. Official plan facts stay separate from Turtle recommendations. Applying a
preset only fills the form until save, and changing one Provider never changes the resource or other
Provider group. GPT Mini is shown only as an upstream, non-selectable fallback note. The retired Turtle
points ledger remains in PostgreSQL only for audit and rollback; active requests use model windows,
automatic fallback, concurrency, and real upstream limits without a local points or simulated USD balance.

`STORAGE_PROVIDER=turtle` is deployment-managed. Turtle's runtime storage configuration uses the
dedicated PostgreSQL database: shared settings and encrypted COS credentials live in JSONB, while
active byte quotas come from Turtle resource groups. Legacy per-user quota rows are rollback inputs only.
Redis is ephemeral coordination only and never the
user-data source of truth. The real Tencent COS path must not be marked working until a private
Bucket, least-privilege credentials, CORS, direct PUT, signed GET, deletion, quota, and cross-user
tests pass. The active Media Pump is a Cloudflare Worker, never a service on the Japan application host.
Its authenticated generated-image → COS path, native Open WebUI page, and compatible image input have
passed locally; manual browser upload, video, and the Japan-host repeat remain gates. See
`../../docs/design/object_storage.md` and `../../docs/operations/turtle_media_pump.md`.

The patch script checks exact upstream markers and fails the image build if Open WebUI
changes those files. To upgrade, change `OPEN_WEBUI_BASE_IMAGE`, rebuild, and visually
verify login, signup, chat, sidebar, light mode, dark mode, desktop, and mobile widths.

The built-in rotating image intro remains hidden so authentication opens directly on a restrained
workspace entry. The current local UI candidate adds a wide-screen product introduction beside the
login/signup card while keeping tablet and mobile layouts focused on the form. It also adds a Provider-aware
new-chat welcome panel with GPT/Claude-specific starter tasks that fill the native composer without sending
a request. A slow cyan/teal atmospheric field, fine star-grid geometry, shell-like elliptical tracks,
layered glass and reduced-motion fallback preserve depth without restoring the old campaign page. First-user
and returning-user variants retain accurate, action-specific subtitles. The new
public Origin also passes real presigned PUT/GET/HEAD preflight and object-transfer checks; manual
main-object plus thumbnail browser upload and the remaining media gates are still separate.
Desktop dark mode was visually accepted for the single 53px thinking trigger, unified
model/thinking/advanced panel, GPT-5.6/5.5/5.4 secondary menus, synchronous media-space launcher,
collapsed/expanded sidebar, user-only media dialog, and native-style administrator Settings entries.
The subscription-preset editor also passed a 390×844 responsive-browser check without horizontal
overflow. The launcher renders before its capability request, keeps a stable placeholder, and no longer
overlaps Open WebUI's negative-margin user footer. Its quota result now seeds an in-memory first-page
prefetch; opening the media dialog renders cached quota/list data immediately and revalidates stale data
without persistent cross-account browser storage. Real-device mobile and the full light-mode regression
remain explicit follow-up gates rather than assumed passes. The standalone administrator console has
passed image build, API, permission-boundary, and responsive-code checks; its real-browser visual and
interaction pass remains explicit rather than inferred from those automated checks.
The administrator console now renders core overview data independently from slow Provider probes,
prefetches the other read-only modules during idle time, deduplicates in-flight reads, and uses labeled
shimmer/module loading states instead of unexplained empty blocks. Request-trend values use an explicit
light/dark-safe tooltip element and remain keyboard-readable.
The 2026-07-23 Provider/display/relogin changes passed image, API, PostgreSQL and container tests; their
browser-only visual interaction remains pending because no controllable browser instance was available.

The authentication landing page passed a public desktop browser check plus a local 390×844 responsive
check. The removed promotional intro and obsolete local-only privacy sentence are absent from the visible
and accessibility snapshots. See `../../docs/reports/auth_landing_acceptance_20260723.md`.

ChatGPT and Claude use separate vendored SVG marks in the workspace trigger, welcome state, model menu,
and model/assistant avatar surfaces. A desktop and 390×844 browser pass confirmed both Provider marks and
the GPT ↔ Claude switch without sending a chat request; the Turtle mark remains limited to product branding.

Brand and Provider marks must remain local image assets rather than network URLs; keeping them in the image
avoids external tracking and broken branding during startup.

Open WebUI remains third-party software governed by its own license. Re-check its current
branding/license terms before increasing the deployment beyond the small private user group.
