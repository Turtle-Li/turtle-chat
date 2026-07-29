from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"


def test_announcement_schema_and_versioned_receipt_are_present() -> None:
    store = (BRANDING / "turtle_chat" / "store.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS chat_announcement_item (" in store
    assert "CREATE TABLE IF NOT EXISTS chat_announcement_item_receipt (" in store
    assert "PRIMARY KEY (announcement_id, user_id)" in store
    assert "legacy-singleton" in store
    assert "def announcements_admin" in store
    assert "def create_announcement" in store
    assert "def update_announcement" in store
    assert "def delete_announcement" in store
    # Former singleton tables remain only as migration/old-client inputs.
    assert "CREATE TABLE IF NOT EXISTS chat_announcement (" in store
    assert "CREATE TABLE IF NOT EXISTS chat_announcement_receipt (" in store
    assert "ON CONFLICT(announcement_id, user_id) DO UPDATE SET" in store
    assert "revision = excluded.revision" in store


def test_announcement_admin_api_and_preview_are_protected() -> None:
    router = (BRANDING / "turtle_chat" / "router.py").read_text(encoding="utf-8")
    assert '@router.get("/announcements")' in router
    assert '@router.post("/announcements/{announcement_id}/dismiss")' in router
    assert '@router.get("/admin/announcements")' in router
    assert '@router.post("/admin/announcements", status_code=status.HTTP_201_CREATED)' in router
    assert '@router.put("/admin/announcements/{announcement_id}")' in router
    assert '@router.delete("/admin/announcements/{announcement_id}")' in router
    assert '@router.post("/admin/announcements/preview")' in router
    # Singular routes stay available for already-cached v1 pages.
    assert '@router.get("/announcement")' in router
    assert '@router.post("/announcement/dismiss")' in router
    assert '@router.get("/admin/announcement")' in router
    assert '@router.post("/admin/announcement/preview")' in router
    assert '@router.put("/admin/announcement")' in router
    assert router.count("Depends(get_admin_user)") >= 7
    assert "Depends(_get_announcement_user)" in router
    assert 'user.role not in {"pending", "user", "admin"}' in router
    assert 'response.headers["Cache-Control"] = "private, no-store"' in router


def test_markdown_has_server_and_browser_safety_layers() -> None:
    renderer = (BRANDING / "turtle_chat" / "announcement.py").read_text(
        encoding="utf-8"
    )
    browser = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    styles = (BRANDING / "custom.css").read_text(encoding="utf-8")
    assert '"html": False' in renderer
    assert '"linkify": False' in renderer
    assert '.disable("image")' in renderer
    assert "const sanitizeAnnouncementHtml" in browser
    assert '"noopener noreferrer nofollow"' in browser
    assert '["http:", "https:", "mailto:"]' in browser
    assert "html.dark .turtle-announcement-markdown blockquote" in styles
    assert "color: #e2f3f1;" in styles
    assert 'launcher.className = "flex cursor-pointer px-2 py-2 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-850 transition"' in browser
    assert "launcher.className = temporaryChat.className" in browser
    assert "launcher.remove();" in browser
    assert 'launcher.dataset.placement = "floating"' not in browser
    assert "document.body.append(launcher)" not in browser
    assert 'class="size-4.5"' in browser
    assert 'stroke="currentColor" stroke-width="1.5"' in browser
    assert "#turtle-announcement-launcher[data-placement=\"floating\"]" not in styles
    assert "#turtle-announcement-launcher," in styles
    assert "html.dark #turtle-announcement-launcher {" not in styles


def test_admin_editor_and_first_entry_modal_are_wired() -> None:
    html = (BRANDING / "admin-console.html").read_text(encoding="utf-8")
    admin = (BRANDING / "admin-console.js").read_text(encoding="utf-8")
    browser = (BRANDING / "model-controls.js").read_text(encoding="utf-8")
    assert 'data-route="announcements"' in html
    assert "const renderAnnouncements" in admin
    assert "const refreshAnnouncementPreview" in admin
    assert "const saveAnnouncement" in admin
    assert "const newAnnouncement" in admin
    assert "const selectAnnouncement" in admin
    assert "const deleteAnnouncement" in admin
    assert "ANNOUNCEMENTS_ENDPOINT" in browser
    assert "currentAnnouncement?.should_show" in browser
    assert "unreadAnnouncementCount" in browser
    assert "data-announcement-list" in browser
    assert "data-announcement-detail-view" in browser
    assert "showAnnouncementDetail" in browser
    assert "announcementRelativeTime" in browser
    assert "data-announcement-previous" not in browser
    assert "data-announcement-next" not in browser
    assert 'id = "turtle-announcement-launcher"' in browser
    assert "positionAnnouncementLauncher" in browser
    assert 'launcher.dataset.placement = "header"' in browser
    assert "条未读" in browser
    assert "M15 17h5l-1.405-1.405" in browser
    assert "const syncAnnouncement" in browser
    assert "const announcementUiMissing" in browser
    assert "ensureAnnouncementUi();" in browser
    assert "if (announcementLoaded && announcementUiMissing) renderCurrentAnnouncement();" in browser
    assert 'window.location.pathname.startsWith("/auth")' in browser
    assert "(!storedToken() && !sessionRole)" in browser
