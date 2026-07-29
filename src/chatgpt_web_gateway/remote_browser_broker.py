"""Minimal self-hosted noVNC broker for one-time ChatGPT login sessions."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .remote_browser_sessions import (
    RemoteBrowserSessionError,
    consume_connection,
    exchange_pending_session,
)


COOKIE_NAME = "turtle_remote_login"
PUBLIC_PREFIX = "/__turtle_login"


@dataclass(frozen=True, slots=True)
class RemoteBrowserBrokerSettings:
    bind_host: str
    port: int
    session_store: Path
    novnc_root: Path
    vnc_host: str
    vnc_port: int

    @classmethod
    def from_env(cls) -> "RemoteBrowserBrokerSettings":
        bind_host = os.getenv("TURTLE_REMOTE_BROWSER_BIND_HOST", "127.0.0.1").strip()
        try:
            bind_address = ipaddress.ip_address(bind_host)
        except ValueError as exc:
            raise ValueError("TURTLE_REMOTE_BROWSER_BIND_HOST must be an IP address") from exc
        if bind_address.is_unspecified or not (
            bind_address.is_loopback or bind_address.is_private
        ):
            raise ValueError("TURTLE_REMOTE_BROWSER_BIND_HOST must remain private")
        port = int(os.getenv("TURTLE_REMOTE_BROWSER_PORT", "36080"))
        vnc_host = os.getenv("TURTLE_REMOTE_BROWSER_VNC_HOST", "127.0.0.1").strip()
        vnc_port = int(os.getenv("TURTLE_REMOTE_BROWSER_VNC_PORT", "35900"))
        if vnc_host not in {"127.0.0.1", "::1"}:
            raise ValueError("TURTLE_REMOTE_BROWSER_VNC_HOST must remain loopback-only")
        if not 1 <= port <= 65535 or not 1 <= vnc_port <= 65535:
            raise ValueError("Remote-browser ports must be between 1 and 65535")
        default_root = Path(__file__).resolve().parents[2]
        project_root = Path(
            os.getenv("TURTLE_LOGIN_PROJECT_ROOT", str(default_root))
        ).expanduser().resolve()
        runtime_root = project_root / ".runtime"
        session_store = Path(
            os.getenv(
                "TURTLE_REMOTE_BROWSER_SESSION_STORE",
                str(runtime_root / "remote-browser-sessions.json"),
            )
        ).expanduser().absolute()
        resolved_store = session_store.resolve()
        try:
            resolved_store.relative_to(runtime_root.resolve())
        except ValueError as exc:
            raise ValueError("Remote-browser session store must remain under .runtime") from exc
        if session_store.is_symlink():
            raise ValueError("Remote-browser session store must not be a symlink")
        novnc_root = Path(
            os.getenv("TURTLE_NOVNC_ROOT", "/usr/share/novnc")
        ).expanduser().resolve()
        if not novnc_root.is_dir() or not (novnc_root / "vnc.html").is_file():
            raise ValueError("A verified noVNC static root is required")
        return cls(
            bind_host=bind_host,
            port=port,
            session_store=resolved_store,
            novnc_root=novnc_root,
            vnc_host=vnc_host,
            vnc_port=vnc_port,
        )


class SessionExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=256)


def _connect_html() -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Turtle Chat 安全登录</title>
  <style nonce="{nonce}">
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#07111f; color:#e5eef8; font:16px system-ui,sans-serif; }}
    main {{ max-width:34rem; padding:2rem; text-align:center; }}
    p {{ color:#9fb3c8; line-height:1.7; }}
  </style>
</head>
<body><main><h1>正在建立隔离登录窗口</h1><p id="state">请稍候。此链接只可使用一次。</p></main>
<script nonce="{nonce}">
(async () => {{
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get('token') || '';
  history.replaceState(null, '', location.pathname);
  const state = document.getElementById('state');
  try {{
    const response = await fetch('{PUBLIC_PREFIX}/session', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'content-type': 'application/json'}},
      body: JSON.stringify({{token}})
    }});
    if (!response.ok) throw new Error('expired');
    location.replace('{PUBLIC_PREFIX}/vnc.html?autoconnect=true&resize=scale&path=__turtle_login%2Fwebsockify');
  }} catch (_) {{
    state.textContent = '链接已过期或已使用，请回到 Provider 页面重新开始登录。';
  }}
}})();
</script></body></html>"""
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; connect-src 'self'; script-src 'nonce-"
        + nonce
        + "'; style-src 'nonce-"
        + nonce
        + "'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return response


async def _relay_websocket(websocket: WebSocket, reader, writer) -> None:
    async def browser_to_vnc() -> None:
        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                if text is None:
                    continue
                data = text.encode("utf-8")
            writer.write(data)
            await writer.drain()

    async def vnc_to_browser() -> None:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                return
            await websocket.send_bytes(data)

    tasks = {
        asyncio.create_task(browser_to_vnc()),
        asyncio.create_task(vnc_to_browser()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except (WebSocketDisconnect, ConnectionError, OSError):
            pass
    await asyncio.gather(*pending, return_exceptions=True)
    writer.close()
    await writer.wait_closed()


def create_remote_browser_broker_app(
    settings: RemoteBrowserBrokerSettings,
) -> FastAPI:
    application = FastAPI(
        title="Turtle Remote Login Broker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @application.get(f"{PUBLIC_PREFIX}/healthz")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @application.get(f"{PUBLIC_PREFIX}/connect", response_class=HTMLResponse)
    async def connect() -> HTMLResponse:
        return _connect_html()

    @application.post(f"{PUBLIC_PREFIX}/session")
    async def exchange(payload: SessionExchange):
        try:
            connection, _account_id, expires_at = exchange_pending_session(
                settings.session_store,
                raw_token=payload.token,
            )
        except RemoteBrowserSessionError:
            return JSONResponse({"ok": False}, status_code=410)
        max_age = max(1, expires_at - int(time.time()))
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            connection,
            max_age=max_age,
            expires=max_age,
            path=PUBLIC_PREFIX,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @application.websocket(f"{PUBLIC_PREFIX}/websockify")
    async def websockify(websocket: WebSocket):
        raw_cookie = websocket.cookies.get(COOKIE_NAME, "")
        try:
            consume_connection(settings.session_store, raw_cookie=raw_cookie)
        except RemoteBrowserSessionError:
            await websocket.close(code=4403)
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(settings.vnc_host, settings.vnc_port),
                timeout=3.0,
            )
        except (TimeoutError, OSError):
            await websocket.close(code=1013)
            return
        protocols = {
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        }
        await websocket.accept(subprotocol="binary" if "binary" in protocols else None)
        try:
            await _relay_websocket(websocket, reader, writer)
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass

    application.mount(
        f"{PUBLIC_PREFIX}/",
        StaticFiles(directory=settings.novnc_root, html=False, check_dir=True),
        name="novnc-static",
    )
    return application


def main() -> None:
    settings = RemoteBrowserBrokerSettings.from_env()
    uvicorn.run(
        create_remote_browser_broker_app(settings),
        host=settings.bind_host,
        port=settings.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
