from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production.yml"
DEPLOY_SCRIPT = ROOT / "deploy" / "turtle-gpt" / "remote" / "deploy-release"
PULL_PUBLIC_SCRIPT = ROOT / "deploy" / "turtle-gpt" / "remote" / "pull-public-release"
STOP_INACTIVE_SCRIPT = ROOT / "deploy" / "turtle-gpt" / "remote" / "stop-inactive-slot"
COMPOSE = ROOT / "deploy" / "turtle-gpt" / "compose.blue-green.yml"


def test_public_workflow_builds_commit_images_without_deployment_secrets() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "packages: write" in workflow
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in workflow
    assert "docker/login-action@371161bbe7024a29a25c5e19bfcbc0804fe9ad2c" in workflow
    assert "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a" in workflow
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


def test_remote_deploy_pulls_commit_images_without_building_them() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "registry_files=" in deploy
    assert "ghcr-public-v1" in deploy
    assert "pulling commit-addressed application images" in deploy
    assert 'ghcr.io/turtle-li/turtle-chat-gateway' in deploy
    assert 'ghcr.io/turtle-li/turtle-chat-open-webui' in deploy
    assert 'docker pull "$public_gateway_ref"' in deploy
    assert 'docker pull "$public_webui_ref"' in deploy
    assert "Gateway image architecture mismatch" in deploy
    assert "Open WebUI image architecture mismatch" in deploy
    assert "Gateway image revision label mismatch" in deploy
    assert "Open WebUI image revision label mismatch" in deploy


def test_public_release_poller_fetches_exact_source_after_both_images_exist() -> None:
    poller = PULL_PUBLIC_SCRIPT.read_text(encoding="utf-8")

    assert "refs/heads/main" in poller
    assert 'docker manifest inspect "$gateway_ref"' in poller
    assert 'docker manifest inspect "$webui_ref"' in poller
    assert "images are not both published yet" in poller
    assert "fetch --quiet --depth=1 --no-tags" in poller
    assert 'rev-parse FETCH_HEAD' in poller
    assert "ghcr-public-v1" in poller
    assert '"$DEPLOY_COMMAND" "$SHA"' in poller
    assert "TURTLE_DEPLOY_" not in poller
    assert "ssh " not in poller


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


def test_blue_green_image_generation_uses_its_gateway_credentials() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "IMAGES_OPENAI_API_BASE_URL: http://gateway-blue:8000/v1" in compose
    assert "IMAGES_OPENAI_API_BASE_URL: http://gateway-green:8000/v1" in compose
    assert (
        compose.count(
            "IMAGES_OPENAI_API_KEY: ${GATEWAY_API_KEY:"
            "?set GATEWAY_API_KEY in .env}"
        )
        == 2
    )


def test_remote_deploy_rejects_an_image_route_that_bypasses_gateway() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'expected_base="http://gateway-${slot}:8000/v1"' in deploy
    assert "candidate Open WebUI image route bypasses its Gateway" in deploy
    assert "candidate Open WebUI image Gateway credential is missing" in deploy
    assert 'grep -E \'^IMAGES_OPENAI_API_KEY=.+$\'' in deploy
    assert deploy.index('assert_image_gateway_route "$candidate_slot"') < deploy.index(
        'log "switching new connections to $candidate_slot"'
    )
