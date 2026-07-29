from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding" / "open-webui"


def test_chat_points_write_routes_are_removed() -> None:
    router = (BRANDING / "turtle_chat" / "router.py").read_text(encoding="utf-8")
    assert "/points-policy" not in router
    assert "/balance" not in router
    assert "BalanceAdjustmentForm" not in router
    assert '"costs":' not in router


def test_chat_points_runtime_gate_is_removed_but_legacy_schema_remains() -> None:
    store = (BRANDING / "turtle_chat" / "store.py").read_text(encoding="utf-8")
    metering = (BRANDING / "turtle_chat" / "metering.py").read_text(encoding="utf-8")
    assert "class ChatQuotaError" not in store
    assert "def adjust_balance" not in store
    assert "def set_points_metered" not in store
    assert "points_exceeded" not in metering
    assert "CREATE TABLE IF NOT EXISTS chat_ledger" in store
    assert "metered       INTEGER NOT NULL" in store


def test_chat_points_controls_and_labels_are_removed_from_active_frontends() -> None:
    for name in ("admin-console.js", "storage-controls.js", "model-controls.js"):
        source = (BRANDING / name).read_text(encoding="utf-8")
        assert "站内积分" not in source
        assert "points-policy" not in source
        assert "remaining_points" not in source
        assert "granted_points" not in source
