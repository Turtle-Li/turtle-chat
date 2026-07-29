from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .config import PumpSettings
from .security import NONCE_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, NonceGuard, sign_request


logger = logging.getLogger("uvicorn.error")
SAFE_DESTINATION_HEADERS = {
    "content-type",
    "content-length",
    "origin",
    "x-ms-blob-type",
    "x-ms-version",
    "x-cos-storage-class",
}


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=10, max_length=16_384)
    max_bytes: int | None = Field(default=None, gt=0)


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=10, max_length=16_384)
    destination_url: str = Field(min_length=10, max_length=16_384)
    destination_headers: dict[str, str] = Field(default_factory=dict)
    expected_size: int = Field(gt=0)
    expected_content_type: str = Field(min_length=3, max_length=160)
    max_bytes: int = Field(gt=0)


class PumpTransferError(RuntimeError):
    def __init__(self, message: str, status_code: int = status.HTTP_502_BAD_GATEWAY):
        self.status_code = status_code
        super().__init__(message)


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == item or normalized.endswith(f".{item}") for item in allowed)


def _validate_url(value: str, allowed_hosts: tuple[str, ...], label: str) -> str:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise PumpTransferError(f"invalid {label} URL", status.HTTP_400_BAD_REQUEST) from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise PumpTransferError(f"invalid {label} URL", status.HTTP_400_BAD_REQUEST)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PumpTransferError(f"invalid {label} host", status.HTTP_400_BAD_REQUEST)
    if not _host_allowed(host, allowed_hosts):
        raise PumpTransferError(f"{label} host is not allowlisted", status.HTTP_403_FORBIDDEN)
    return value


def _parse_size(headers: httpx.Headers) -> int | None:
    content_range = headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)
    raw = headers.get("content-length", "")
    return int(raw) if raw.isdigit() else None


def _content_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()


async def _source_response(
    client: httpx.AsyncClient,
    settings: PumpSettings,
    source_url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    current = _validate_url(source_url, settings.source_hosts, "source")
    for redirect_count in range(settings.max_redirects + 1):
        request = client.build_request("GET", current, headers=headers)
        response = await client.send(request, stream=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        await response.aclose()
        if not location or redirect_count >= settings.max_redirects:
            raise PumpTransferError("source redirect limit exceeded")
        current = _validate_url(urljoin(current, location), settings.source_hosts, "source redirect")
    raise PumpTransferError("source redirect limit exceeded")


def _bounded_limit(requested: int | None, settings: PumpSettings) -> int:
    if requested is None:
        return settings.max_bytes
    return min(int(requested), settings.max_bytes)


def _clean_destination_headers(values: dict[str, str], expected_size: int, content_type: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values.items():
        normalized = str(name).strip().lower()
        if normalized not in SAFE_DESTINATION_HEADERS:
            raise PumpTransferError("destination header is not allowlisted", status.HTTP_400_BAD_REQUEST)
        if "\r" in str(value) or "\n" in str(value):
            raise PumpTransferError("invalid destination header", status.HTTP_400_BAD_REQUEST)
        result[name] = str(value)
    result["Content-Type"] = content_type
    result["Content-Length"] = str(expected_size)
    return result


def create_app(
    settings: PumpSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved = settings or PumpSettings.from_env()
    nonce_guard = NonceGuard(resolved.timestamp_skew_seconds * 2)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        timeout = httpx.Timeout(resolved.timeout_seconds, connect=30.0)
        application.state.client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await application.state.client.aclose()

    app = FastAPI(title="Turtle Media Pump", version="0.1.0", lifespan=lifespan)

    async def require_signature(request: Request) -> None:
        timestamp_value = request.headers.get(TIMESTAMP_HEADER, "")
        nonce = request.headers.get(NONCE_HEADER, "")
        supplied = request.headers.get(SIGNATURE_HEADER, "")
        try:
            timestamp = int(timestamp_value)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="invalid pump signature") from exc
        now = int(time.time())
        if abs(now - timestamp) > resolved.timestamp_skew_seconds:
            raise HTTPException(status_code=401, detail="expired pump signature")
        if len(nonce) < 16 or len(nonce) > 128 or not supplied:
            raise HTTPException(status_code=401, detail="invalid pump signature")
        body = await request.body()
        expected = sign_request(
            resolved.shared_secret,
            request.method,
            request.url.path,
            timestamp_value,
            nonce,
            body,
        )
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid pump signature")
        if not nonce_guard.accept(nonce, now):
            raise HTTPException(status_code=409, detail="replayed pump request")

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "turtle-media-pump", "max_bytes": resolved.max_bytes}

    @app.post("/v1/probe", dependencies=[Depends(require_signature)])
    async def probe(payload: ProbeRequest, request: Request) -> dict[str, Any]:
        limit = _bounded_limit(payload.max_bytes, resolved)
        response: httpx.Response | None = None
        try:
            response = await _source_response(
                request.app.state.client,
                resolved,
                payload.source_url,
                headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
            )
            if response.status_code not in {200, 206}:
                raise PumpTransferError("source probe failed")
            size = _parse_size(response.headers)
            if size is None or size <= 0:
                raise PumpTransferError("source size is unavailable")
            if size > limit:
                raise PumpTransferError("source exceeds transfer limit", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
            return {"size": size, "content_type": _content_type(response.headers)}
        except PumpTransferError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("Media probe failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="source probe failed") from exc
        finally:
            if response is not None:
                await response.aclose()

    @app.post("/v1/transfers", dependencies=[Depends(require_signature)])
    async def transfer(payload: TransferRequest, request: Request) -> dict[str, Any]:
        source: httpx.Response | None = None
        try:
            limit = _bounded_limit(payload.max_bytes, resolved)
            if payload.expected_size > limit:
                raise PumpTransferError("transfer exceeds limit", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
            destination_url = _validate_url(
                payload.destination_url,
                resolved.destination_hosts,
                "destination",
            )
            source = await _source_response(
                request.app.state.client,
                resolved,
                payload.source_url,
                headers={"Accept-Encoding": "identity"},
            )
            if source.status_code != 200:
                raise PumpTransferError("source download failed")
            source_size = _parse_size(source.headers)
            if source_size is not None and source_size != payload.expected_size:
                raise PumpTransferError("source size changed", status.HTTP_409_CONFLICT)
            source_type = _content_type(source.headers)
            expected_type = payload.expected_content_type.split(";", 1)[0].strip().lower()
            if source_type != "application/octet-stream" and source_type != expected_type:
                raise PumpTransferError("source content type changed", status.HTTP_409_CONFLICT)

            transferred = 0
            digest = hashlib.sha256()

            async def body() -> AsyncIterator[bytes]:
                nonlocal transferred
                async for chunk in source.aiter_bytes(256 * 1024):
                    transferred += len(chunk)
                    if transferred > payload.expected_size or transferred > limit:
                        raise PumpTransferError("source exceeded declared size", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
                    digest.update(chunk)
                    yield chunk

            headers = _clean_destination_headers(
                payload.destination_headers,
                payload.expected_size,
                expected_type,
            )
            destination_request = request.app.state.client.build_request(
                "PUT",
                destination_url,
                headers=headers,
                content=body(),
            )
            destination = await request.app.state.client.send(destination_request)
            try:
                if destination.status_code < 200 or destination.status_code >= 300:
                    raise PumpTransferError("destination upload failed")
            finally:
                await destination.aclose()
            if transferred != payload.expected_size:
                raise PumpTransferError("transferred size mismatch", status.HTTP_409_CONFLICT)
            return {
                "ok": True,
                "size": transferred,
                "content_type": expected_type,
                "sha256": digest.hexdigest(),
            }
        except PumpTransferError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("Media transfer failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="media transfer failed") from exc
        finally:
            if source is not None:
                await source.aclose()

    return app
