from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production.yml"
DEPLOY_SCRIPT = ROOT / "deploy" / "turtle-gpt" / "remote" / "deploy-release"
STOP_INACTIVE_SCRIPT = ROOT / "deploy" / "turtle-gpt" / "remote" / "stop-inactive-slot"


def test_public_workflow_builds_commit_images_without_deployment_secrets() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "packages: write" in workflow
    assert "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "ghcr.io/turtle-li/turtle-chat-gateway" in workflow
    assert "ghcr.io/turtle-li/turtle-chat-open-webui" in workflow
    assert "git-${{ github.sha }}" in workflow
    assert "org.opencontainers.image.revision=${{ github.sha }}" in workflow
    assert "TURTLE_DEPLOY_" not in workflow
    assert "DEPLOY_KEY" not in workflow
    assert "known_hosts" not in workflow
    assert "ssh " not in workflow


def test_remote_deploy_builds_revision_labeled_images_from_release_source() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "building application images from exact source release" in deploy
    assert deploy.count("DOCKER_BUILDKIT=1 docker build") == 2
    assert '"$release_dir"' in deploy
    assert '"$release_dir/branding/open-webui"' in deploy
    assert 'org.opencontainers.image.revision=$SHA' in deploy
    assert "Gateway image revision label mismatch" in deploy
    assert "Open WebUI image revision label mismatch" in deploy


def test_remote_deploy_retains_a_bounded_legacy_bundle_rollback_path() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "source_files=" in deploy
    assert "legacy_files=" in deploy
    assert "loading legacy prebuilt application images" in deploy
    assert "release bundle contains unexpected files" in deploy


def test_inactive_slot_drain_cannot_stop_a_reused_candidate_slot() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    stop_inactive = STOP_INACTIVE_SCRIPT.read_text(encoding="utf-8")

    assert 'active_release=$(<"$STATE_DIR/active-release")' in deploy
    assert '"$active_slot" "$active_release"' in deploy
    assert "readonly LOCK_FILE=/run/lock/turtle-gpt-production-deploy.lock" in stop_inactive
    assert 'flock -w 1800 9' in stop_inactive
    assert "Timers created before release guards were introduced" in stop_inactive
    assert 'revision != "$expected_release"' in stop_inactive
    assert 'org.opencontainers.image.revision' in stop_inactive
