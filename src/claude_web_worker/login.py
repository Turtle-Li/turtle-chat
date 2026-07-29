from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from chatgpt_web_gateway.chrome_launcher import _dedicated_chrome_state, main as chrome_main

from .auth import AuthError, load_auth_session, save_auth_session, session_from_cdp_cookies
from .client import ClaudeClient
from .config import Settings


DEFAULT_PORT = 9225
DEFAULT_PROFILE = ".runtime/claude-chrome-profile"
DEFAULT_PID = ".runtime/claude-chrome.pid"
DEFAULT_AUTH = ".runtime/claude-auth/session.json"
LOGIN_URL = "https://claude.ai/login"


def _read_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise AuthError("The dedicated Claude login browser is not ready") from exc


async def _cdp_command(websocket_url: str, method: str) -> dict[str, Any]:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise AuthError("Claude login capture requires the optional browser dependencies") from exc

    async with connect(websocket_url, open_timeout=5, close_timeout=2, max_size=16 << 20) as socket:
        await socket.send(json.dumps({"id": 1, "method": method}, separators=(",", ":")))
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=10)
            payload = json.loads(raw)
            if payload.get("id") != 1:
                continue
            if payload.get("error"):
                raise AuthError("The dedicated browser could not provide the Claude login session")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AuthError("The dedicated browser returned an invalid login state")
            return result


async def _capture_cookies(port: int) -> list[dict[str, Any]]:
    version = _read_json(f"http://127.0.0.1:{port}/json/version")
    browser_ws = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if isinstance(browser_ws, str) and browser_ws:
        try:
            result = await _cdp_command(browser_ws, "Storage.getCookies")
            cookies = result.get("cookies")
            if isinstance(cookies, list):
                return cookies
        except AuthError:
            pass

    targets = _read_json(f"http://127.0.0.1:{port}/json/list")
    if not isinstance(targets, list):
        raise AuthError("The dedicated browser has no readable Claude login page")
    pages = [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and isinstance(target.get("webSocketDebuggerUrl"), str)
    ]
    pages.sort(key=lambda item: "claude.ai" not in str(item.get("url") or ""))
    if not pages:
        raise AuthError("The dedicated browser has no readable Claude login page")
    result = await _cdp_command(str(pages[0]["webSocketDebuggerUrl"]), "Network.getAllCookies")
    cookies = result.get("cookies")
    if not isinstance(cookies, list):
        raise AuthError("The dedicated browser returned an invalid Claude login state")
    return cookies


def _runtime_settings(auth_path: Path) -> Settings:
    proxy_url = os.getenv("CLAUDE_PROXY_URL", "").strip() or None
    return Settings(
        worker_api_key="login-capture-local-only",
        auth_path=auth_path,
        verified_models_path=auth_path.parent / "verified-models.json",
        proxy_url=proxy_url,
    )


async def _capture_and_validate(port: int, auth_path: Path) -> None:
    cookies = await _capture_cookies(port)
    session = session_from_cdp_cookies(cookies)
    client = ClaudeClient(_runtime_settings(auth_path), session)
    try:
        organization = await client.validate()
    finally:
        await client.close()
    save_auth_session(auth_path, session.with_organization(organization))


def _chrome_args(args: argparse.Namespace, action: str | None = None) -> list[str]:
    values = [
        "--port",
        str(args.port),
        "--profile-dir",
        str(args.profile_dir),
        "--pid-file",
        str(args.pid_file),
        "--url",
        LOGIN_URL,
        "--service-label",
        "Claude",
    ]
    if args.chrome_path:
        values.extend(["--chrome-path", args.chrome_path])
    if action:
        values.append(action)
    return values


def _browser_state(args: argparse.Namespace) -> str:
    return _dedicated_chrome_state(
        args.port,
        Path(args.profile_dir).expanduser().resolve(),
        Path(args.pid_file).expanduser().resolve(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open and capture Turtle's dedicated Claude Web login session"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    parser.add_argument("--pid-file", default=DEFAULT_PID)
    parser.add_argument("--auth-path", default=DEFAULT_AUTH)
    parser.add_argument("--chrome-path")
    parser.add_argument("--keep-open", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--capture", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    auth_path = Path(args.auth_path).expanduser().absolute()

    if args.status:
        browser = _browser_state(args)
        try:
            load_auth_session(auth_path)
            auth = "configured"
        except AuthError:
            auth = "login_required"
        print(f"browser={browser} auth={auth}")
        return
    if args.stop:
        chrome_main(_chrome_args(args, "--stop"))
        return
    if args.capture:
        if _browser_state(args) != "ready":
            raise SystemExit("Claude login page is not open; run turtle-claude-login first")
        try:
            asyncio.run(_capture_and_validate(args.port, auth_path))
        except AuthError as exc:
            raise SystemExit(str(exc)) from exc
        except Exception as exc:
            raise SystemExit(
                "Claude login could not be validated; keep the login page open and try again"
            ) from exc
        print("Claude login was validated and saved securely; no credential value was displayed.")
        if not args.keep_open:
            chrome_main(_chrome_args(args, "--stop"))
        return

    chrome_main(_chrome_args(args))
    print("Complete the Claude sign-in in that window, then run turtle-claude-login --capture.")


if __name__ == "__main__":
    main()
