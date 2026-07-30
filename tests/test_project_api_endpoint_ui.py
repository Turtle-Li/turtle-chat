from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"
DIRECT_BASE = "https://api.chat.turtleligpt.com/v1"


def test_standalone_project_console_recommends_direct_edge() -> None:
    html = (BRANDING / "project-api.html").read_text(encoding="utf-8")
    script = (BRANDING / "project-api.js").read_text(encoding="utf-8")

    assert 'id="api-base"' in html
    assert 'id="api-base-compat"' in html
    assert 'id="copy-api-base"' in html
    assert 'id="copy-quickstart"' in html
    assert DIRECT_BASE in script
    assert "PROJECT_API_COMPAT_BASE = `${window.location.origin}/api/project/v1`" in script
    assert "PROJECT_API_BASE}/chat/completions" in script
    assert "Idempotency-Key" in script
    assert "Promise.allSettled" in script
    assert (
        'document.querySelector("#api-base").textContent='
        "`${window.location.origin}/api/project/v1`"
    ) not in script


def test_in_app_project_console_recommends_and_copies_direct_edge() -> None:
    script = (BRANDING / "storage-controls.js").read_text(encoding="utf-8")

    assert f'const PROJECT_API_PUBLIC_BASE = "{DIRECT_BASE}";' in script
    assert "推荐 Base URL" in script
    assert "兼容旧地址" in script
    assert "data-project-copy-endpoint" in script
    assert "navigator.clipboard.writeText(endpointCopy.dataset.projectCopyEndpoint)" in script
    assert "`${window.location.origin}/api/project/v1`" in script
    assert "Promise.allSettled" in script
