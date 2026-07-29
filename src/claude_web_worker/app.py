from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import AuthError, AuthSession, load_auth_session
from .client import (
    ClaudeClient,
    ClaudeWebError,
    collect_stream_text,
    completion_identity,
    iter_stream_deltas,
    openai_chunk,
)
from .config import Settings
from .login import _capture_and_validate
from .models import (
    CLAUDE_ROUTES,
    ChatCompletionRequest,
    UnsupportedContent,
    load_verified_routes,
    model_metadata,
    resolve_route,
    serialize_history,
)
from .verify import _verify


logger = logging.getLogger("uvicorn.error")
ClientFactory = Callable[[Settings, AuthSession], ClaudeClient]


def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class ClaudeRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory or (lambda resolved, auth: ClaudeClient(resolved, auth))
        self.client: ClaudeClient | None = None
        self.status = "login_required"
        self.verified_routes: tuple[str, ...] = ()
        self.last_checked_at = 0.0
        self._auth_stamp: tuple[int, int] | None = None
        self._verified_stamp: tuple[int, int] | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await client.close()

    async def _reload_auth(self) -> None:
        current_stamp = _file_stamp(self.settings.auth_path)
        if current_stamp == self._auth_stamp and self.client is not None:
            return
        await self.close()
        self._auth_stamp = current_stamp
        self.last_checked_at = 0.0
        if current_stamp is None:
            self.status = "login_required"
            return
        try:
            auth = load_auth_session(self.settings.auth_path)
            self.client = self.client_factory(self.settings, auth)
            self.status = "checking"
        except (AuthError, RuntimeError, ValueError):
            self.status = "login_required"

    def _reload_verified_routes(self) -> None:
        current_stamp = _file_stamp(self.settings.verified_models_path)
        if current_stamp == self._verified_stamp:
            return
        self._verified_stamp = current_stamp
        self.verified_routes = load_verified_routes(self.settings.verified_models_path)

    async def ensure_ready(self, *, force: bool = False) -> str:
        async with self._lock:
            await self._reload_auth()
            self._reload_verified_routes()
            if self.client is None:
                self.status = "login_required"
                return self.status
            now = time.monotonic()
            if force or now - self.last_checked_at >= self.settings.health_cache_seconds:
                try:
                    await self.client.validate()
                    self.status = "ready" if self.verified_routes else "model_verification_required"
                except ClaudeWebError as exc:
                    self.status = (
                        "reauthentication_required"
                        if exc.reauthentication_required
                        else "upstream_unreachable"
                    )
                except Exception:
                    self.status = "upstream_unreachable"
                self.last_checked_at = now
            elif self.status == "ready" and not self.verified_routes:
                self.status = "model_verification_required"
            elif self.status == "model_verification_required" and self.verified_routes:
                self.status = "ready"
            return self.status

    def mark_failure(self, error: ClaudeWebError) -> None:
        if error.reauthentication_required:
            self.status = "reauthentication_required"
        elif self.status == "ready":
            self.status = "upstream_unreachable"
        self.last_checked_at = time.monotonic()


def _error(status_code: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "param": None, "code": None}},
    )


def _runtime_error(status: str) -> JSONResponse:
    if status == "login_required":
        return _error(503, "Claude login is required", "authentication_error")
    if status == "reauthentication_required":
        return _error(503, "Claude login has expired; sign in again", "authentication_error")
    if status == "model_verification_required":
        return _error(503, "Claude routes have not completed real verification", "configuration_error")
    return _error(502, "Claude Web is temporarily unreachable", "upstream_error")


def create_app(
    settings: Settings | None = None,
    *,
    runtime: ClaudeRuntime | None = None,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()
    resolved_runtime = runtime or ClaudeRuntime(resolved, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.claude_runtime = resolved_runtime
        await resolved_runtime.ensure_ready(force=True)
        try:
            yield
        finally:
            await resolved_runtime.close()

    application = FastAPI(title="Turtle Claude Web Worker", version="0.1.0", lifespan=lifespan)

    async def require_key(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
            supplied, resolved.worker_api_key
        ):
            raise HTTPException(
                status_code=401,
                detail={"message": "invalid API key", "type": "authentication_error"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.get("/")
    async def root() -> dict[str, str]:
        return {"service": "turtle-claude-web-worker", "status": "ok"}

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, Any]:
        active: ClaudeRuntime = request.app.state.claude_runtime
        status = await active.ensure_ready()
        return {
            "ok": status == "ready",
            "status": status,
            "public_model": resolved.public_model_name,
            "verified_route_count": len(active.verified_routes),
            "text_only": True,
            "auth_isolated": True,
        }

    @application.post(
        "/api/ClaudeWeb/auth/capture",
        dependencies=[Depends(require_key)],
    )
    async def capture_auth(request: Request) -> dict[str, Any]:
        """Capture the isolated browser session and verify every published lane."""

        active: ClaudeRuntime = request.app.state.claude_runtime
        try:
            await _capture_and_validate(
                resolved.browser_port,
                resolved.auth_path,
            )
            verified = await _verify(
                resolved.auth_path,
                resolved.verified_models_path,
                tuple(route.key for route in CLAUDE_ROUTES),
            )
            if set(verified) != {route.key for route in CLAUDE_ROUTES}:
                raise AuthError(
                    "Not every published Claude route passed real verification"
                )
            runtime_status = await active.ensure_ready(force=True)
        except (AuthError, ClaudeWebError, OSError, RuntimeError, ValueError):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Claude login or real-route verification did not complete",
                    "type": "authentication_error",
                },
            )
        return {
            "ok": runtime_status == "ready",
            "status": runtime_status,
            "verified_route_count": len(verified),
        }

    @application.get("/v1/models", dependencies=[Depends(require_key)])
    async def models(request: Request) -> dict[str, Any]:
        active: ClaudeRuntime = request.app.state.claude_runtime
        status = await active.ensure_ready()
        data = []
        if status == "ready" and active.verified_routes:
            data.append(model_metadata(resolved.public_model_name, active.verified_routes))
        return {"object": "list", "data": data}

    @application.post("/v1/chat/completions", dependencies=[Depends(require_key)])
    async def completions(body: ChatCompletionRequest, request: Request):
        if body.model != resolved.public_model_name:
            return _error(404, f"unknown model: {body.model}", "invalid_request_error")
        active: ClaudeRuntime = request.app.state.claude_runtime
        status = await active.ensure_ready()
        if status != "ready" or active.client is None:
            return _runtime_error(status)
        client = active.client
        try:
            route = resolve_route(
                body.turtle_claude_model,
                body.turtle_claude_thinking,
                active.verified_routes,
            )
            prompt = serialize_history(body.messages)
        except (ValueError, UnsupportedContent) as exc:
            return _error(400, str(exc), "invalid_request_error")

        started = time.monotonic()
        logger.info(
            "claude_request_started stream=%s route=%s web_search=%s",
            body.stream,
            route.key,
            body.web_search,
        )
        try:
            handle = await client.start_completion(
                prompt,
                route,
                web_search=body.web_search,
            )
        except ClaudeWebError as exc:
            active.mark_failure(exc)
            logger.warning(
                "claude_request_failed status=%s operation=%s",
                exc.status_code,
                exc.operation,
            )
            return _runtime_error(active.status) if exc.reauthentication_required else _error(
                exc.status_code,
                "Claude Web request failed",
                "upstream_error",
            )
        except Exception:
            logger.warning("claude_request_failed status=502 operation=start")
            return _error(502, "Claude Web request failed", "upstream_error")

        if not body.stream:
            try:
                content = await collect_stream_text(handle.response)
                if not content.strip():
                    return _error(502, "Claude Web returned no effective content", "upstream_error")
                completion_id, created = completion_identity()
                logger.info(
                    "claude_request_completed route=%s total_ms=%d",
                    route.key,
                    int((time.monotonic() - started) * 1000),
                )
                return {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": resolved.public_model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            except ClaudeWebError as exc:
                active.mark_failure(exc)
                return _error(502, "Claude Web stream failed", "upstream_error")
            finally:
                try:
                    await asyncio.shield(client.delete_conversation(handle.conversation_id))
                except Exception:
                    logger.warning("claude_conversation_cleanup_failed")

        completion_id, created = completion_identity()

        async def stream() -> AsyncIterator[bytes]:
            sent_content = False
            try:
                yield openai_chunk(
                    completion_id=completion_id,
                    public_model=resolved.public_model_name,
                    created=created,
                    delta={"role": "assistant", "content": ""},
                )
                async for delta in iter_stream_deltas(handle.response):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        sent_content = True
                    yield openai_chunk(
                        completion_id=completion_id,
                        public_model=resolved.public_model_name,
                        created=created,
                        delta=delta,
                    )
                if sent_content:
                    yield openai_chunk(
                        completion_id=completion_id,
                        public_model=resolved.public_model_name,
                        created=created,
                        delta={},
                        finish_reason="stop",
                    )
                else:
                    yield b'data: {"error":{"message":"Claude Web returned no effective content","type":"upstream_error"}}\n\n'
                yield b"data: [DONE]\n\n"
                logger.info(
                    "claude_request_completed route=%s total_ms=%d",
                    route.key,
                    int((time.monotonic() - started) * 1000),
                )
            except ClaudeWebError as exc:
                active.mark_failure(exc)
                yield b'data: {"error":{"message":"Claude Web stream failed","type":"upstream_error"}}\n\n'
                yield b"data: [DONE]\n\n"
            except Exception:
                yield b'data: {"error":{"message":"Claude Web stream failed","type":"upstream_error"}}\n\n'
                yield b"data: [DONE]\n\n"
            finally:
                try:
                    await asyncio.shield(client.delete_conversation(handle.conversation_id))
                except Exception:
                    logger.warning("claude_conversation_cleanup_failed")

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return application
