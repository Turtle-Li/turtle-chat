from __future__ import annotations

import importlib.util
import os
import signal
from pathlib import Path

import pytest

from chatgpt_web_gateway import chrome_launcher


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gpt4free.py"
LAUNCHER_SPEC = importlib.util.spec_from_file_location("turtle_run_gpt4free", LAUNCHER_PATH)
assert LAUNCHER_SPEC is not None and LAUNCHER_SPEC.loader is not None
LAUNCHER_MODULE = importlib.util.module_from_spec(LAUNCHER_SPEC)
LAUNCHER_SPEC.loader.exec_module(LAUNCHER_MODULE)
secure_auth_permissions = LAUNCHER_MODULE.secure_auth_permissions

CLAUDE_LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_claude_worker.py"
CLAUDE_LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "turtle_run_claude_worker", CLAUDE_LAUNCHER_PATH
)
assert CLAUDE_LAUNCHER_SPEC is not None and CLAUDE_LAUNCHER_SPEC.loader is not None
CLAUDE_LAUNCHER_MODULE = importlib.util.module_from_spec(CLAUDE_LAUNCHER_SPEC)
CLAUDE_LAUNCHER_SPEC.loader.exec_module(CLAUDE_LAUNCHER_MODULE)


def mode(path) -> int:
    return path.stat().st_mode & 0o777


def test_secure_auth_permissions_covers_all_files_and_nested_directories(tmp_path) -> None:
    auth_dir = tmp_path / "auth"
    nested_dir = auth_dir / "nested"
    nested_dir.mkdir(parents=True)
    root_file = auth_dir / "runtime-cache.json"
    nested_file = nested_dir / "session-cache.json"
    root_file.write_text("test", encoding="utf-8")
    nested_file.write_text("test", encoding="utf-8")
    auth_dir.chmod(0o755)
    nested_dir.chmod(0o755)
    root_file.chmod(0o644)
    nested_file.chmod(0o666)

    secure_auth_permissions(auth_dir)

    assert mode(auth_dir) == 0o700
    assert mode(nested_dir) == 0o700
    assert mode(root_file) == 0o600
    assert mode(nested_file) == 0o600


def test_secure_auth_permissions_rejects_symlinks(tmp_path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("test", encoding="utf-8")
    os.symlink(outside, auth_dir / "linked-cache")

    with pytest.raises(SystemExit, match="must not contain symlinks"):
        secure_auth_permissions(auth_dir)


def test_dedicated_chrome_launcher_accepts_isolated_service_parameters(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        chrome_launcher,
        "_dedicated_chrome_state",
        lambda port, profile, pid_file: "ready" if port == 9224 else "stopped",
    )

    chrome_launcher.main(
        [
            "--port",
            "9224",
            "--profile-dir",
            str(tmp_path / "claude-profile"),
            "--pid-file",
            str(tmp_path / "claude.pid"),
            "--url",
            "https://claude.ai/login",
            "--service-label",
            "Claude",
            "--status",
        ]
    )

    assert capsys.readouterr().out.strip() == "ready"


def test_dedicated_chrome_launcher_rejects_non_https_login_page(tmp_path) -> None:
    with pytest.raises(SystemExit, match="HTTPS URL"):
        chrome_launcher.main(
            [
                "--profile-dir",
                str(tmp_path / "profile"),
                "--pid-file",
                str(tmp_path / "browser.pid"),
                "--url",
                "http://example.test/login",
                "--status",
            ]
        )


def test_manual_login_launcher_opens_url_without_cdp_or_automation_flags(
    tmp_path, monkeypatch, capsys
) -> None:
    profile_dir = (tmp_path / "profile").resolve()
    pid_file = tmp_path / "browser.pid"
    launched: dict[str, list[str]] = {}

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(chrome_launcher, "_find_chrome", lambda _path: Path("/bin/true"))
    monkeypatch.setattr(
        chrome_launcher.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.setdefault("command", command) and Process(),
    )
    monkeypatch.setattr(
        chrome_launcher,
        "_dedicated_chrome_state",
        lambda _port, _profile, pid: "manual" if pid.is_file() else "stopped",
    )

    chrome_launcher.main(
        [
            "--port",
            "9225",
            "--profile-dir",
            str(profile_dir),
            "--pid-file",
            str(pid_file),
            "--url",
            "https://claude.ai/login",
            "--service-label",
            "Claude",
            "--manual",
        ]
    )

    command = launched["command"]
    assert "https://claude.ai/login" in command
    assert not any(value.startswith("--remote-debugging-") for value in command)
    assert "--enable-automation" not in command
    assert "--headless=new" not in command
    assert "manual login" in capsys.readouterr().out


def test_dedicated_chrome_stop_reaps_child_process(tmp_path, monkeypatch, capsys) -> None:
    profile_dir = (tmp_path / "profile").resolve()
    pid_file = tmp_path / "browser.pid"
    pid_file.write_text("4242\n", encoding="utf-8")
    killed: list[tuple[int, signal.Signals]] = []
    waited: list[tuple[int, int]] = []

    monkeypatch.setattr(
        chrome_launcher,
        "_process_command",
        lambda _pid: (
            "google-chrome --remote-debugging-port=9223 "
            f"--user-data-dir={profile_dir}"
        ),
    )
    monkeypatch.setattr(chrome_launcher, "_cdp_ready", lambda _port: False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        waited.append((pid, options))
        return pid, 0

    monkeypatch.setattr(os, "waitpid", waitpid)

    chrome_launcher.main(
        [
            "--port",
            "9223",
            "--profile-dir",
            str(profile_dir),
            "--pid-file",
            str(pid_file),
            "--stop",
        ]
    )

    assert killed == [(4242, signal.SIGTERM)]
    assert waited == [(4242, os.WNOHANG)]
    assert not pid_file.exists()
    assert "stopped" in capsys.readouterr().out


def test_claude_launcher_loads_only_allowlisted_environment(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "GATEWAY_API_KEY=test-key\n"
        "UPSTREAM_API_KEY=worker-key\n"
        "CLAUDE_PORT=8330\n"
        "WEBUI_SECRET_KEY=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_WORKER_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_PORT", raising=False)
    monkeypatch.delenv("WEBUI_SECRET_KEY", raising=False)

    CLAUDE_LAUNCHER_MODULE.load_known_env(env_path)

    assert os.environ["GATEWAY_API_KEY"] == "test-key"
    assert os.environ["UPSTREAM_API_KEY"] == "worker-key"
    assert os.environ["CLAUDE_WORKER_API_KEY"] == "worker-key"
    assert os.environ["CLAUDE_PORT"] == "8330"
    assert "WEBUI_SECRET_KEY" not in os.environ


def test_gpt4free_overlay_tracks_image_tasks_before_completion() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert (
        '-                    if m.get("status") == "finished_successfully" '
        'and m.get("metadata", {}).get("image_gen_task_id"):'
    ) in overlay
    assert (
        '+                    if m.get("metadata", {}).get("image_gen_task_id"):'
    ) in overlay
    assert (
        '+                    elif m.get("p") == "/message/metadata/image_gen_task_id" '
        'and m.get("v"):'
    ) in overlay
    assert "+            conversation.task = None" in overlay
    assert "+            conversation.generated_images = None" in overlay


def test_gpt4free_overlay_preserves_upstream_thought_summary() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert '-                    yield Reasoning(token="", status=fields.thoughts_summary)' in overlay
    assert "+def _visible_reasoning_recap(value: Any, *, limit: int = 12_000)" in overlay
    assert '+        if fields.p == "/message/metadata/turn_summary":' in overlay
    assert '+                            yield Reasoning(token=f"{recap}\\n\\n")' in overlay
    assert '+    if content_type == "reasoning_recap":' in overlay
    assert '+    if content_type != "thoughts":' in overlay
    assert "+            conversation.turtle_reasoning_recaps = set()" in overlay


def test_gpt4free_overlay_emits_sources_as_native_url_citations() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert "diff --git a/g4f/client/stubs.py b/g4f/client/stubs.py" in overlay
    assert "+from ..providers.response import Reasoning, Sources, ToolCalls, AudioResponse" in overlay
    assert "+    annotations: Optional[List[dict]] = None" in overlay
    assert "+        elif isinstance(content, Sources):" in overlay
    assert '+                    "type": "url_citation",' in overlay
    assert "+class OpenAISources(Sources):" in overlay


def test_gpt4free_overlay_streams_official_style_search_progress() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert "+def _search_progress_labels(entries: Any, *, limit: int = 8)" in overlay
    assert "+def _new_search_progress_labels(entries: Any, seen: set[str])" in overlay
    assert "+def _search_progress_events(entries: Any, fields: Any)" in overlay
    assert '+                            yield Reasoning(status="正在搜索网页")' in overlay
    assert "+                            for progress in _search_progress_events(entries, fields):" in overlay
    assert '+                            for source_key in ("sources", "items", "fallback_items"):' in overlay
    assert "+                                yield Reasoning(status=progress)" in overlay
    assert "+                        yield Reasoning(status=initial_text)" in overlay
    assert '+        ])) + "\\n\\n"' in overlay


def test_gpt4free_overlay_accepts_data_handoff_frames_and_ignores_controls() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert (
        "-from ...requests.curl_cffi import AsyncSession\n"
        "+from ...requests.curl_cffi import AsyncSession, CurlWsFlag"
    ) in overlay
    assert (
        "+                            payload, frame_flags = "
        "await websocket.recv(timeout=remaining)"
    ) in overlay
    assert "+                            if frame_flags & CurlWsFlag.CLOSE:" in overlay
    assert (
        "+                            if not frame_flags & "
        "(CurlWsFlag.TEXT | CurlWsFlag.BINARY):"
    ) in overlay
    assert "+                            if not payload:" in overlay
    assert "+                            frames = json.loads(payload)" in overlay
    assert "+                    receive_deadline = loop.time() + timeout" in overlay
    assert "+                            receive_deadline = loop.time() + timeout" in overlay
    assert "websocket.recv_json(timeout=timeout)" not in overlay
    assert "websocket.recv_str(timeout=timeout)" not in overlay


def test_gpt4free_overlay_seals_only_exact_sandbox_zip_downloads() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert "+SANDBOX_FILE_URL_RE = re.compile(" in overlay
    assert "+async def resolve_sandbox_file_source(" in overlay
    assert "+async def rewrite_sandbox_file_stream(" in overlay
    assert '+        f"/interpreter/download?{query}"' in overlay
    assert '+        candidate = metadata.get("download_url")' in overlay
    assert "+            download_url = candidate.strip()" in overlay
    assert "+        if not isinstance(metadata, dict):" in overlay
    assert "+    # Interpreter metadata is intentionally sparse" in overlay
    assert "+    # Media Pump authoritatively probes size/type before transferring" in overlay
    assert "+    for attempt in range(15):" in overlay
    assert '+            raise MediaPumpError("ChatGPT ZIP download is not ready")' in overlay
    assert "+        await asyncio.sleep(min(0.5 * (2 ** attempt), 5.0))" in overlay
    assert "diff --git a/g4f/client/__init__.py b/g4f/client/__init__.py" in overlay
    assert "g4f/client/__init__.py" in LAUNCHER_MODULE.OVERLAY_FILES
    assert "g4f/client/stubs.py" in LAUNCHER_MODULE.OVERLAY_FILES
    assert '+        requested_outputs = kwargs.pop("n", 1)' in overlay
    assert (
        '+        repeat_count = requested_outputs if provider_name == "OpenaiAccount" else 1'
        in overlay
    )
    assert "+        for _ in range(repeat_count):" in overlay
    assert '+    r"/interpreter/download$"' in overlay
    assert '+    if set(query) != {"message_id", "sandbox_path"}:' in overlay
    assert '+    if not sandbox_path.startswith("/mnt/data/"):' in overlay
    assert '+        and filename.lower().endswith(".zip")' in overlay
    assert (
        '+                                output, sandbox_file_buffer = await '
        'rewrite_sandbox_file_stream('
    ) in overlay


def test_gpt4free_overlay_routes_health_probe_through_configured_proxy() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert '+            or os.environ.get("G4F_PROXY")' in overlay
    assert "+            proxy=proxy," in overlay


def test_gpt4free_overlay_retries_only_pre_message_403_requests() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert (
        '+        """Retry pre-message 403s without replaying a submitted chat message."""'
        in overlay
    )
    assert "+                if response.status == 403 and attempt < 4:" in overlay
    assert overlay.count("_safe_request_json(") == 4
    assert "+                    proof_token = auth_result.proof_token = get_config(user_agent)" in overlay
    assert "+                    json_data={\"p\": get_requirements_token(proof_token)}," in overlay


def test_gpt4free_overlay_singleflights_short_homepage_warm_cache() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert "+    _turtle_home_warm_ttl_seconds: float = 30.0" in overlay
    assert '+        """Single-flight a short-lived authenticated homepage warm-up."""' in overlay
    assert "+        async with cls._turtle_home_warm_lock:" in overlay
    assert "+                    cls._turtle_home_warmed_at = 0.0" in overlay
    assert "+                await cls._warm_home(session, auth_result)" in overlay


def test_gpt4free_overlay_reuses_verified_model_files_and_tracks_cdn_delivery() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert '+MODEL_SOURCE_CONTEXT = b"turtle-model-source-v1\\0"' in overlay
    assert "+MODEL_INPUT_MAX_BYTES = 20 * 1024**2" in overlay
    assert "+class ModelMediaSource:" in overlay
    assert "+class MediaPumpRequestError(MediaPumpError):" in overlay
    assert "+def open_model_source(image_url: Any) -> ModelMediaSource:" in overlay
    assert '+        attempts = int(os.getenv("TURTLE_MEDIA_PUMP_RETRY_ATTEMPTS", "2"))' in overlay
    assert '+        path == "/v1/transfers"' in overlay
    assert '+        "retry_count": max(0, int(result.get("_control_attempts") or 1) - 1),' in overlay
    assert '+PRIVATE_INPUT_FILE_CACHE_ATTR = "__turtle_input_file_cache"' in overlay
    assert '+                        f"{cls.url}/backend-api/files/{cached[\'file_id\']}/download",' in overlay
    assert '+                                metrics["file_cache_hit"] += 1' in overlay
    assert '+                transfer = await transfer_media(' in overlay
    assert '+        semaphore = asyncio.Semaphore(configured_parallel)' in overlay
    assert "+            results = await asyncio.gather(" in overlay
    assert '+        "configured_parallel": 0,' in overlay
    assert '+        "max_parallel": 0,' in overlay
    assert '+            conversation.turtle_media_metrics = _new_media_metrics()' in overlay
    assert (
        "+            conversation.turtle_upstream_stage_metrics = "
        "_new_upstream_stage_metrics()"
    ) in overlay
    assert "+        self.turtle_media_metrics = _new_media_metrics()" in overlay
    assert (
        "+        self.turtle_upstream_stage_metrics = "
        "_new_upstream_stage_metrics()"
    ) in overlay
    assert "+                remember_conversation(first_chunk)" in overlay
    assert "+                    if not isinstance(first_chunk, BaseConversation):" in overlay
    assert "+        self.turtle_input_file_caches: dict[str, dict[str, dict]] = {}" in overlay
    assert '+                    and re.fullmatch(r"turtle-v1-[0-9a-f]{64}", config.conversation_id)' in overlay
    assert '+                                "turtle_input_file_cache": turtle_input_file_cache,' in overlay
    assert "+                        self.turtle_input_file_caches.pop(resource_id, None)" in overlay
    assert "TURTLE_MEDIA_UPLOAD_CONCURRENCY" in LAUNCHER_MODULE.ALLOWED_ENV_NAMES


def test_gpt4free_overlay_captures_login_without_sending_a_chat() -> None:
    overlay = (LAUNCHER_PATH.parents[1] / "patches/gpt4free-openaiaccount-gpt56.patch").read_text(
        encoding="utf-8"
    )

    assert '+        @self.app.post("/api/OpenaiAccount/auth/capture")' in overlay
    assert '+                configured_auth_dir = os.environ.get("GPT4FREE_AUTH_DIR")' in overlay
    assert "+                set_cookies_dir(configured_auth_dir)" in overlay
    assert '+                    "auth_OpenaiChat.json",' in overlay
    assert "+                cache_file.chmod(0o600)" in overlay
    assert "+                    os.fsync(directory_fd)" in overlay
    assert '+                await provider.login()' in overlay
    assert '+                request_url = getattr(request, "url", None)' in overlay
    assert (
        '+                if not isinstance(request_url, str) '
        "or not isinstance(request_headers, dict):"
    ) in overlay
    assert '-                        await textarea.send_keys("Hello")' in overlay
    assert '-                await button.click()' in overlay
    assert '+                raise MissingAuthError("ChatGPT login was not detected")' in overlay
    assert "+            page = await browser.get(cls.url, new_tab=True)" in overlay
    assert '+                        const response = await fetch("/api/auth/session", {' in overlay
    assert '+                        return typeof session.accessToken === "string"' in overlay
    assert '+            """, await_promise=True, return_by_value=True)' in overlay
    assert (
        '+            debug.log(f"OpenaiChat: Access token: '
        "{'No' if cls._api_key is None else 'Yes'}\")"
    ) in overlay
    assert (
        '+            debug.log(f"OpenaiChat: Access token: '
        "{'False' if cls._api_key is None else cls._api_key[:12]"
    ) not in overlay
    assert "+            elif BrowserConfig.port is not None:" in overlay
    assert "+                        await target.aclose()" in overlay
    assert "+                    await browser.connection.aclose()" in overlay
    assert "+                util.get_registered_instances().discard(browser)" in overlay
    assert "g4f/requests/__init__.py" in LAUNCHER_MODULE.OVERLAY_FILES
