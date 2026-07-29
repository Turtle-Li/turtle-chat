from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from chatgpt_web_gateway.login_control import (
    AccountRuntime,
    ControlSettings,
    LoginControlClient,
    LoginControlError,
    LoginControlService,
    _systemd_path,
    _worker_systemd_unit,
    create_login_control_app,
    load_runtime_manifest,
)


class LoginControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / ".runtime"
        self.secrets = self.runtime / "secrets"
        self.secrets.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        os.chmod(self.secrets, 0o700)
        self.secret = self.secrets / "turtle_login_control_secret"
        self.secret.write_text("a" * 64, encoding="utf-8")
        os.chmod(self.secret, 0o600)
        self.manifest = self.runtime / "account-runtimes.json"
        self._write_manifest()
        self.settings = ControlSettings(
            project_root=self.root,
            runtime_root=self.runtime,
            manifest_path=self.manifest,
            secret_path=self.secret,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_manifest(self, *, worker_port: int = 8320) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": {
                        "legacy-primary": {
                            "cdp_port": 9223,
                            "profile_dir": ".runtime/gpt4free-chrome-profile",
                            "pid_file": ".runtime/gpt4free-chrome.pid",
                            "login_url": "https://chatgpt.com/",
                            "worker_service_label": "com.turtleligpt.gpt4free",
                            "worker_port": worker_port,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.manifest, 0o600)

    def test_manifest_contains_only_validated_isolated_runtime_metadata(self) -> None:
        runtimes = load_runtime_manifest(self.settings)
        runtime = runtimes["legacy-primary"]
        self.assertEqual(runtime.cdp_port, 9223)
        self.assertEqual(runtime.worker_port, 8320)
        self.assertTrue(runtime.profile_dir.is_relative_to(self.runtime.resolve()))

        self._write_manifest(worker_port=9223)
        with self.assertRaises(LoginControlError):
            load_runtime_manifest(self.settings)

    async def test_authenticated_client_reads_status_and_opens_only_mapped_account(self) -> None:
        with (
            patch(
                "chatgpt_web_gateway.login_control._dedicated_chrome_state",
                return_value="ready",
            ),
            patch("chatgpt_web_gateway.login_control._run_chrome") as launch,
        ):
            app = create_login_control_app(self.settings)
            client = LoginControlClient(
                base_url="http://127.0.0.1:8340",
                secret_path=self.secret,
                transport=httpx.ASGITransport(app=app),
            )
            try:
                status = await client.status("legacy-primary")
                self.assertTrue(status["configured"])
                self.assertEqual(status["login_mode"], "local_window")
                self.assertEqual(status["browser_state"], "ready")
                opened = await client.open("legacy-primary")
                self.assertEqual(opened["last_action"], "login_opened")
                missing = await client.status("unknown-account")
                self.assertFalse(missing["configured"])
                self.assertEqual(missing["control_state"], "not_configured")
            finally:
                await client.close()
        launch.assert_called_once()

    async def test_client_accepts_only_short_lived_https_remote_login_links(self) -> None:
        expires_at = int(time.time()) + 600

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "account_id": "legacy-primary",
                    "configured": True,
                    "login_mode": "remote_browser",
                    "browser_state": "ready",
                    "login_session_url": (
                        "https://chat.example.test/admin/login/connect#token=" + "t" * 64
                    ),
                    "login_session_expires_at": expires_at,
                },
            )

        client = LoginControlClient(
            base_url="http://127.0.0.1:8340",
            secret_path=self.secret,
            transport=httpx.MockTransport(handler),
        )
        try:
            status = await client.status("legacy-primary")
            opened = await client.open("legacy-primary")
        finally:
            await client.close()
        self.assertNotIn("login_session_url", status)
        self.assertNotIn("login_session_expires_at", status)
        self.assertEqual(opened["login_mode"], "remote_browser")
        self.assertEqual(opened["login_session_expires_at"], expires_at)
        self.assertTrue(opened["login_session_url"].startswith("https://"))

    async def test_remote_service_issues_fragment_capability_only_from_open(self) -> None:
        store = self.runtime / "remote-browser-sessions.json"
        settings = ControlSettings(
            project_root=self.root,
            runtime_root=self.runtime,
            manifest_path=self.manifest,
            secret_path=self.secret,
            login_mode="remote_browser",
            remote_browser_public_url="https://chat.example.test/__turtle_login/connect",
            remote_browser_session_store=store,
            remote_browser_ttl_seconds=600,
        )
        with (
            patch(
                "chatgpt_web_gateway.login_control._dedicated_chrome_state",
                return_value="ready",
            ),
            patch("chatgpt_web_gateway.login_control._run_chrome"),
        ):
            service = LoginControlService(settings)
            status = service.status("legacy-primary")
            opened = service.open("legacy-primary")
        self.assertEqual(status["login_mode"], "remote_browser")
        self.assertNotIn("login_session_url", status)
        self.assertRegex(opened["login_session_url"], r"#token=[A-Za-z0-9_-]{32,256}$")
        self.assertLessEqual(opened["login_session_expires_at"], int(time.time()) + 600)
        self.assertNotIn(
            opened["login_session_url"].split("#token=", 1)[1],
            store.read_text(encoding="utf-8"),
        )

    async def test_claude_login_uses_manual_browser_then_same_profile_cdp_capture(
        self,
    ) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "accounts": {
                        "legacy-claude-primary": {
                            "provider": "claude",
                            "cdp_port": 9225,
                            "auth_dir": ".runtime/claude-auth",
                            "profile_dir": ".runtime/claude-chrome-profile",
                            "pid_file": ".runtime/claude-chrome.pid",
                            "login_url": "https://claude.ai/login",
                            "worker_service_label": "com.turtleligpt.claude",
                            "worker_port": 8330,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.manifest, 0o600)
        browser = {"state": "stopped"}
        actions: list[tuple[str | None, bool]] = []

        def run_chrome(_runtime, action=None, *, manual=False):
            actions.append((action, manual))
            if action == "--stop":
                browser["state"] = "stopped"
            else:
                browser["state"] = "manual" if manual else "ready"

        with (
            patch(
                "chatgpt_web_gateway.login_control._dedicated_chrome_state",
                side_effect=lambda *_args: browser["state"],
            ),
            patch(
                "chatgpt_web_gateway.login_control._run_chrome",
                side_effect=run_chrome,
            ),
        ):
            app = create_login_control_app(self.settings)
            client = LoginControlClient(
                base_url="http://127.0.0.1:8340",
                secret_path=self.secret,
                transport=httpx.ASGITransport(app=app),
            )
            try:
                opened = await client.open("legacy-claude-primary")
                self.assertEqual(opened["browser_state"], "manual")
                capture = await client.prepare_capture("legacy-claude-primary")
                self.assertEqual(capture["browser_state"], "ready")
            finally:
                await client.close()

        self.assertEqual(
            actions,
            [
                (None, True),
                ("--stop", False),
                (None, False),
            ],
        )

    def test_service_refuses_non_private_manifest(self) -> None:
        os.chmod(self.manifest, 0o644)
        with self.assertRaises(LoginControlError):
            LoginControlService(self.settings)

    def test_systemd_worker_unit_uses_scalar_paths_without_literal_quotes(self) -> None:
        project = self.root / "project with space"
        python = project / ".venv" / "bin" / "python"
        launcher = project / "scripts" / "run_gpt4free.py"
        python.parent.mkdir(parents=True)
        launcher.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        launcher.write_text("# launcher", encoding="utf-8")
        runtime = AccountRuntime(
            account_id="acct-systemd",
            provider="gpt",
            cdp_port=19260,
            auth_dir=project / ".runtime" / "accounts" / "acct-systemd" / "auth",
            profile_dir=project
            / ".runtime"
            / "accounts"
            / "acct-systemd"
            / "chrome-profile",
            pid_file=project
            / ".runtime"
            / "accounts"
            / "acct-systemd"
            / "chrome.pid",
            login_url="https://chatgpt.com/",
            worker_service_label="com.turtleligpt.gpt4free.acct-systemd",
            worker_port=18360,
        )
        settings = ControlSettings(
            project_root=project,
            runtime_root=project / ".runtime",
            manifest_path=project / ".runtime" / "account-runtimes.json",
            secret_path=project
            / ".runtime"
            / "secrets"
            / "turtle_login_control_secret",
            service_manager="systemd-user",
        )
        unit = _worker_systemd_unit(settings, runtime).decode()
        self.assertIn(f"WorkingDirectory={_systemd_path(project)}\n", unit)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertIn(r"\x20", _systemd_path(project))
        self.assertIn(
            f"StandardOutput=append:{_systemd_path(runtime.auth_dir.parent / 'logs' / 'worker.stdout.log')}",
            unit,
        )

    async def test_provision_creates_private_isolated_runtime_and_supports_rollback(self) -> None:
        python = self.root / ".venv" / "bin" / "python"
        launcher = self.root / "scripts" / "run_gpt4free.py"
        python.parent.mkdir(parents=True)
        launcher.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        launcher.write_text("# launcher", encoding="utf-8")
        launch_agents = self.root / "LaunchAgents"
        settings = ControlSettings(
            project_root=self.root,
            runtime_root=self.runtime,
            manifest_path=self.manifest,
            secret_path=self.secret,
            launch_agents_dir=launch_agents,
            worker_port_start=18360,
            cdp_port_start=19260,
        )
        with (
            patch("chatgpt_web_gateway.login_control._bootstrap_worker_agent") as bootstrap,
            patch("chatgpt_web_gateway.login_control._bootout_worker_agent") as bootout,
            patch("chatgpt_web_gateway.login_control._worker_ready", return_value=True),
            patch(
                "chatgpt_web_gateway.login_control._worker_reachable",
                return_value=True,
            ),
            patch(
                "chatgpt_web_gateway.login_control._dedicated_chrome_state",
                return_value="stopped",
            ),
        ):
            app = create_login_control_app(settings)
            client = LoginControlClient(
                base_url="http://127.0.0.1:8340",
                secret_path=self.secret,
                transport=httpx.ASGITransport(app=app),
            )
            try:
                provisioned = await client.provision("acct-new")
                self.assertTrue(provisioned["configured"])
                self.assertEqual(provisioned["credential_state"], "empty")
                self.assertEqual(provisioned["worker_port"], 18360)
                status = await client.status("acct-new")
                self.assertEqual(status["worker_state"], "ready")
                rolled_back = await client.rollback_provision("acct-new")
                self.assertFalse(rolled_back["configured"])
            finally:
                await client.close()

        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("acct-new", payload["accounts"])
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        auth_dir = self.runtime / "accounts" / "acct-new" / "auth"
        self.assertTrue(auth_dir.is_dir())
        self.assertEqual(auth_dir.stat().st_mode & 0o777, 0o700)
        bootstrap.assert_called_once()
        bootout.assert_called_once()

    async def test_provision_builds_a_claude_runtime_with_claude_login_and_worker(self) -> None:
        python = self.root / ".venv" / "bin" / "python"
        launcher = self.root / "scripts" / "run_claude_worker.py"
        python.parent.mkdir(parents=True)
        launcher.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        launcher.write_text("# launcher", encoding="utf-8")
        settings = ControlSettings(
            project_root=self.root,
            runtime_root=self.runtime,
            manifest_path=self.manifest,
            secret_path=self.secret,
            launch_agents_dir=self.root / "LaunchAgents",
            worker_port_start=18360,
            cdp_port_start=19260,
        )
        with (
            patch("chatgpt_web_gateway.login_control._bootstrap_worker_agent"),
            patch("chatgpt_web_gateway.login_control._worker_ready", return_value=True),
            patch(
                "chatgpt_web_gateway.login_control._worker_reachable",
                return_value=True,
            ),
            patch(
                "chatgpt_web_gateway.login_control._dedicated_chrome_state",
                return_value="stopped",
            ),
        ):
            app = create_login_control_app(settings)
            client = LoginControlClient(
                base_url="http://127.0.0.1:8340",
                secret_path=self.secret,
                transport=httpx.ASGITransport(app=app),
            )
            try:
                provisioned = await client.provision(
                    "acct-claude",
                    provider="claude",
                )
                self.assertEqual(provisioned["provider"], "claude")
            finally:
                await client.close()

        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        entry = payload["accounts"]["acct-claude"]
        self.assertEqual(payload["version"], 2)
        self.assertEqual(entry["provider"], "claude")
        self.assertEqual(entry["login_url"], "https://claude.ai/login")
        plist = next((self.root / "LaunchAgents").glob("*.plist")).read_bytes()
        self.assertIn(b"run_claude_worker.py", plist)
        self.assertIn(b"CLAUDE_BROWSER_PORT", plist)

    def test_provision_sanitizes_filesystem_failures(self) -> None:
        python = self.root / ".venv" / "bin" / "python"
        launcher = self.root / "scripts" / "run_gpt4free.py"
        python.parent.mkdir(parents=True)
        launcher.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        launcher.write_text("# launcher", encoding="utf-8")
        settings = ControlSettings(
            project_root=self.root,
            runtime_root=self.runtime,
            manifest_path=self.manifest,
            secret_path=self.secret,
            launch_agents_dir=self.root / "LaunchAgents",
            worker_port_start=18360,
            cdp_port_start=19260,
        )
        service = LoginControlService(settings)
        private_detail = "/private/credential/location"
        with patch(
            "chatgpt_web_gateway.login_control._worker_launch_agent",
            side_effect=OSError(private_detail),
        ):
            with self.assertRaises(LoginControlError) as raised:
                service.provision("acct-failure")

        self.assertEqual(str(raised.exception), "账号隔离运行环境创建失败")
        self.assertNotIn(private_detail, str(raised.exception))
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("acct-failure", payload["accounts"])


if __name__ == "__main__":
    unittest.main()
