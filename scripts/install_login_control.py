"""Install the local-only login control for the deployment-managed GPT account.

This script creates only an opaque shared secret, a sanitized runtime manifest,
and a user LaunchAgent. It never opens or reads ChatGPT authentication files.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any


LABEL = "com.turtleligpt.login-control"


def _atomic_write(
    path: Path,
    data: bytes,
    mode: int,
    *,
    private_parent: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private_parent:
        os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _ensure_secret(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Login-control secret must be a regular file")
        os.chmod(path, 0o600)
        value = path.read_text(encoding="utf-8").strip()
        if not 32 <= len(value) <= 512 or any(character.isspace() for character in value):
            raise SystemExit("Existing login-control secret is invalid; it was not replaced")
        return
    _atomic_write(path, f"{secrets.token_urlsafe(48)}\n".encode(), 0o600)


def _ensure_manifest(path: Path) -> None:
    default_account = {
        "provider": "gpt",
        "cdp_port": 9223,
        "auth_dir": ".runtime/gpt4free-auth",
        "profile_dir": ".runtime/gpt4free-chrome-profile",
        "pid_file": ".runtime/gpt4free-chrome.pid",
        "login_url": "https://chatgpt.com/",
        "worker_service_label": "com.turtleligpt.gpt4free",
        "worker_port": 8320,
    }
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Account runtime manifest must be a regular file")
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit("Existing account runtime manifest is invalid") from exc
        if payload.get("version") not in {1, 2} or not isinstance(payload.get("accounts"), dict):
            raise SystemExit("Existing account runtime manifest has an unsupported shape")
        payload["version"] = 2
        payload["accounts"].setdefault("legacy-primary", default_account)
    else:
        payload = {"version": 2, "accounts": {"legacy-primary": default_account}}
    _atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        0o600,
    )


def _launch_agent(root: Path, python: Path) -> bytes:
    logs = root / ".runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs, 0o700)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "chatgpt_web_gateway.login_control",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "login-control-launchd.stdout.log"),
        "StandardErrorPath": str(logs / "login-control-launchd.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _load_agent(path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(path)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit("Unable to bootstrap the login-control LaunchAgent")
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit("Unable to start the login-control LaunchAgent")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Turtle's local login control")
    parser.add_argument("--load", action="store_true", help="bootstrap and start the LaunchAgent")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("Project .venv is missing; install dependencies before login control")
    runtime = root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    _ensure_secret(runtime / "secrets" / "turtle_login_control_secret")
    _ensure_manifest(runtime / "account-runtimes.json")

    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LABEL}.plist"
    _atomic_write(
        plist_path,
        _launch_agent(root, python),
        0o644,
        private_parent=False,
    )
    if args.load:
        _load_agent(plist_path)
    print("Login control installed without reading or exporting browser credentials.")


if __name__ == "__main__":
    main()
