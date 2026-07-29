from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse


def _find_chrome(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise SystemExit(f"Chrome executable does not exist: {candidate}")

    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("Google Chrome or Chromium was not found")


def _cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
            payload = json.load(response)
        return isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _open_cdp_page(port: int, url: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}",
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not payload.get("webSocketDebuggerUrl"):
            raise ValueError("missing page target")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SystemExit("Dedicated Chrome started but the login page could not be opened") from exc


def _process_command(pid: int) -> str:
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _reap_child_process(pid: int, timeout_seconds: float = 2.0) -> bool:
    """Reap a Chrome process when the launcher is running in a long-lived parent.

    The login-control service invokes this module in-process, so the dedicated
    Chrome remains its child after launch.  A normal stop must therefore call
    waitpid after Chrome exits or each login cycle leaves a zombie behind.  The
    standalone CLI may not own the process; ChildProcessError is expected there.
    """

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        if waited_pid == pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _dedicated_chrome_state(port: int, profile_dir: Path, pid_file: Path) -> str:
    cdp_ready = _cdp_ready(port)
    if not pid_file.is_file() or pid_file.is_symlink():
        return "occupied" if cdp_ready else "stopped"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "occupied"
    command = _process_command(pid)
    profile_marker = f"--user-data-dir={profile_dir}"
    if not command:
        return "occupied" if cdp_ready else "stopped"
    if profile_marker not in command:
        return "occupied"
    if f"--remote-debugging-port={port}" in command:
        return "ready" if cdp_ready else "starting"
    return "occupied" if cdp_ready else "manual"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the dedicated Chrome used to refresh Turtle's Chat gpt4free login"
    )
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--profile-dir", default=".runtime/gpt4free-chrome-profile")
    parser.add_argument("--pid-file", default=".runtime/gpt4free-chrome.pid")
    parser.add_argument("--url", default="https://chatgpt.com/")
    parser.add_argument("--service-label", default="gpt4free")
    parser.add_argument("--chrome-path")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Open a normal visible login window without CDP/debugging",
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    parsed_url = urlparse(args.url)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise SystemExit("--url must be an HTTPS URL")
    service_label = str(args.service_label or "dedicated").strip()
    if not service_label or len(service_label) > 80:
        raise SystemExit("--service-label must contain 1-80 characters")
    if args.manual and args.headless:
        raise SystemExit("--manual cannot be combined with --headless")
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    runtime_dir = profile_dir.parent
    pid_file = Path(args.pid_file).expanduser().resolve()
    pid_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(pid_file.parent, 0o700)
    if args.status:
        print(_dedicated_chrome_state(args.port, profile_dir, pid_file))
        return
    if args.stop:
        if not pid_file.is_file():
            print("Dedicated Chrome is already stopped")
            return
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise SystemExit("Refusing to use an invalid dedicated Chrome PID file") from exc
        command = _process_command(pid)
        if command and f"--user-data-dir={profile_dir}" not in command:
            raise SystemExit(
                f"Refusing to stop a process that is not the dedicated {service_label} Chrome"
            )
        reaped = False
        if command:
            os.kill(pid, signal.SIGTERM)
            reaped = _reap_child_process(pid, timeout_seconds=10.0)
        if not reaped:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _process_command(pid):
                time.sleep(0.2)
            if _process_command(pid):
                raise SystemExit("Dedicated Chrome did not stop within 10 seconds")
        pid_file.unlink(missing_ok=True)
        print(f"Dedicated {service_label} Chrome stopped")
        return
    current_state = _dedicated_chrome_state(args.port, profile_dir, pid_file)
    requested_state = "manual" if args.manual else "ready"
    if current_state == requested_state:
        if args.manual:
            print(f"Dedicated {service_label} Chrome is already open for manual login")
            return
        _open_cdp_page(args.port, args.url)
        print(f"Dedicated {service_label} Chrome is already ready on 127.0.0.1:{args.port}")
        return
    if current_state in {"manual", "ready", "starting", "occupied"}:
        raise SystemExit(
            f"Dedicated {service_label} Chrome is already running in a different mode"
        )

    chrome = _find_chrome(args.chrome_path)
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile_dir, 0o700)
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    command = [str(chrome), f"--user-data-dir={profile_dir}"]
    if args.manual:
        command.extend(
            [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--password-store=basic",
                "--window-size=1400,860",
                "--new-window",
                args.url,
            ]
        )
    else:
        command.extend(
            [
                f"--remote-debugging-port={args.port}",
                "--remote-debugging-address=127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-startup-window",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-dev-shm-usage",
                "--disable-sync",
                "--password-store=basic",
                "--window-size=1400,860",
            ]
        )
    if args.headless:
        command.extend(["--headless=new", "--disable-gpu"])
    with open(os.devnull, "wb") as sink:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
        )
    pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
    os.chmod(pid_file, 0o600)

    deadline = time.monotonic() + (15 if args.manual else 45)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("Dedicated Chrome exited before the login window became ready")
        if args.manual and _dedicated_chrome_state(args.port, profile_dir, pid_file) == "manual":
            print(f"Dedicated {service_label} Chrome is open for manual login")
            print(
                f"Sign in to {parsed_url.hostname} in that dedicated window; "
                "do not export browser data."
            )
            return
        if _cdp_ready(args.port):
            _open_cdp_page(args.port, args.url)
            mode = "headless" if args.headless else "visible"
            print(
                f"Dedicated {service_label} Chrome is ready in {mode} mode "
                f"on 127.0.0.1:{args.port}"
            )
            if not args.headless:
                print(
                    f"Sign in to {parsed_url.hostname} in that dedicated window; "
                    "do not export browser data."
                )
            return
        time.sleep(0.2)
    if args.manual:
        raise SystemExit("Timed out waiting for the dedicated Chrome login window")
    raise SystemExit("Timed out waiting for the dedicated Chrome debug endpoint")


if __name__ == "__main__":
    main()
