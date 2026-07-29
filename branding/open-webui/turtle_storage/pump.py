"""Signed control-plane client for the external Turtle Media Pump."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout


TIMESTAMP_HEADER = "X-Turtle-Pump-Timestamp"
NONCE_HEADER = "X-Turtle-Pump-Nonce"
SIGNATURE_HEADER = "X-Turtle-Pump-Signature"
MODEL_SOURCE_CONTEXT = b"turtle-model-source-v1\0"
MODEL_SOURCE_ID_CONTEXT = b"turtle-model-source-id-v1\0"
MODEL_SOURCE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class MediaPumpError(RuntimeError):
    pass


def strict_media_mode() -> bool:
    return os.getenv("TURTLE_MEDIA_STRICT", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class RejectServerFileUploadMiddleware:
    """Reject multipart file bodies before FastAPI parses or stores them."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = str(scope.get("path") or "").rstrip("/")
        method = str(scope.get("method") or "").upper()
        if scope.get("type") == "http" and strict_media_mode() and method == "POST" and path == "/api/v1/files":
            body = json.dumps(
                {
                    "detail": {
                        "code": "server_file_upload_disabled",
                        "message": "为保护主服务器带宽，文件只能直传对象存储",
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 409,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class MediaPumpClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("TURTLE_MEDIA_PUMP_URL", "").strip().rstrip("/")
        self.secret = os.getenv("TURTLE_MEDIA_PUMP_SECRET", "").strip()
        self.timeout_seconds = int(os.getenv("TURTLE_MEDIA_PUMP_TIMEOUT_SECONDS", "1800"))

    def configured(self) -> bool:
        if len(self.secret) < 32 or not self.base_url:
            return False
        try:
            parsed = urlparse(self.base_url)
        except ValueError:
            return False
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password

    @staticmethod
    def _validated_https_url(value: str, label: str) -> str:
        try:
            parsed = urlparse(str(value or ""))
        except ValueError as exc:
            raise MediaPumpError(f"{label} is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise MediaPumpError(f"{label} must be an HTTPS URL")
        return str(value)

    @staticmethod
    def _b64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def seal_model_source(
        self,
        *,
        primary_url: str,
        fallback_url: str | None,
        source_key: str,
        ttl_seconds: int,
    ) -> str:
        """Seal CDN/COS sources and a stable, opaque upstream reuse key."""
        if not self.configured():
            raise MediaPumpError("external media pump is not configured")
        primary = self._validated_https_url(primary_url, "primary model source")
        fallback = (
            self._validated_https_url(fallback_url, "fallback model source")
            if fallback_url
            else None
        )
        if fallback == primary:
            fallback = None
        ttl = int(ttl_seconds)
        if ttl < 60 or ttl > 900:
            raise MediaPumpError("model source TTL must be between 60 and 900 seconds")
        source_id = hmac.new(
            self.secret.encode("utf-8"),
            MODEL_SOURCE_ID_CONTEXT + str(source_key).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not MODEL_SOURCE_ID_RE.fullmatch(source_id):
            raise MediaPumpError("model source ID generation failed")
        payload = json.dumps(
            {
                "v": 1,
                "exp": int(time.time()) + ttl,
                "id": source_id,
                "primary_url": primary,
                **({"fallback_url": fallback} if fallback else {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = self._b64url(payload)
        signature = hmac.new(
            self.secret.encode("utf-8"),
            MODEL_SOURCE_CONTEXT + encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded}.{self._b64url(signature)}"

    def _headers(self, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        digest = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(["POST", path, timestamp, nonce, digest]).encode("utf-8")
        signature = hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "turtle-media-pump-control/1.0",
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature,
        }

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.configured():
            raise MediaPumpError("external media pump is not configured")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timeout = ClientTimeout(total=self.timeout_seconds, connect=30)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}{path}",
                    data=body,
                    headers=self._headers(path, body),
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raise MediaPumpError(f"media pump returned HTTP {response.status}")
                    result = await response.json(content_type=None)
        except MediaPumpError:
            raise
        except Exception as exc:
            raise MediaPumpError("media pump request failed") from exc
        if not isinstance(result, dict):
            raise MediaPumpError("media pump returned an invalid response")
        return result

    async def probe(self, source_url: str, max_bytes: int) -> dict:
        result = await self._post(
            "/v1/probe",
            {"source_url": source_url, "max_bytes": int(max_bytes)},
        )
        size = int(result.get("size") or 0)
        content_type = str(result.get("content_type") or "").split(";", 1)[0].lower()
        if size <= 0 or not content_type:
            raise MediaPumpError("media pump probe response is incomplete")
        response = {"size": size, "content_type": content_type}
        for field in ("width", "height"):
            try:
                value = int(result.get(field) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                response[field] = value
        return response

    async def transfer(
        self,
        *,
        source_url: str,
        destination_url: str,
        destination_headers: dict[str, str],
        expected_size: int,
        expected_content_type: str,
        max_bytes: int,
    ) -> dict:
        result = await self._post(
            "/v1/transfers",
            {
                "source_url": source_url,
                "destination_url": destination_url,
                "destination_headers": destination_headers,
                "expected_size": int(expected_size),
                "expected_content_type": expected_content_type,
                "max_bytes": int(max_bytes),
            },
        )
        if result.get("ok") is not True or int(result.get("size") or 0) != int(expected_size):
            raise MediaPumpError("media pump transfer verification failed")
        return result


MEDIA_PUMP = MediaPumpClient()
