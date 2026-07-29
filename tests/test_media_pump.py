from __future__ import annotations

import json
import time
import uuid

import httpx
from fastapi.testclient import TestClient

from turtle_media_pump.app import create_app
from turtle_media_pump.config import PumpSettings
from turtle_media_pump.security import NONCE_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_request


SECRET = "pump-test-secret-that-is-longer-than-thirty-two-characters"


def settings() -> PumpSettings:
    return PumpSettings(
        shared_secret=SECRET,
        source_hosts=("source.example",),
        destination_hosts=("destination.example",),
        max_bytes=1024,
        timeout_seconds=30,
    )


def signed_headers(path: str, body: bytes, *, nonce: str | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()))
    value = nonce or uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: value,
        SIGNATURE_HEADER: sign_request(SECRET, "POST", path, timestamp, value, body),
    }


def post(client: TestClient, path: str, payload: dict, *, nonce: str | None = None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(path, content=body, headers=signed_headers(path, body, nonce=nonce))


def test_probe_uses_range_without_downloading_the_object() -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["range"] = request.headers.get("range", "")
        return httpx.Response(
            206,
            headers={
                "Content-Range": "bytes 0-0/5",
                "Content-Type": "image/png",
                "Content-Length": "1",
            },
            content=b"h",
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(handler))) as client:
        response = post(
            client,
            "/v1/probe",
            {"source_url": "https://source.example/file?sig=secret", "max_bytes": 10},
        )

    assert response.status_code == 200
    assert response.json() == {"size": 5, "content_type": "image/png"}
    assert observed["range"] == "bytes=0-0"


def test_transfer_streams_source_to_destination_and_returns_digest() -> None:
    uploaded: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "source.example":
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png", "Content-Length": "5"},
                content=b"hello",
            )
        uploaded["body"] = await request.aread()
        uploaded["content_type"] = request.headers.get("content-type")
        uploaded["content_length"] = request.headers.get("content-length")
        return httpx.Response(200)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(handler))) as client:
        response = post(
            client,
            "/v1/transfers",
            {
                "source_url": "https://source.example/file?sig=source",
                "destination_url": "https://destination.example/object?sig=destination",
                "destination_headers": {"Content-Type": "image/png"},
                "expected_size": 5,
                "expected_content_type": "image/png",
                "max_bytes": 10,
            },
        )

    assert response.status_code == 200
    assert response.json()["size"] == 5
    assert len(response.json()["sha256"]) == 64
    assert uploaded == {
        "body": b"hello",
        "content_type": "image/png",
        "content_length": "5",
    }


def test_unallowlisted_destination_is_rejected_before_upload() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(handler))) as client:
        response = post(
            client,
            "/v1/transfers",
            {
                "source_url": "https://source.example/file",
                "destination_url": "https://not-allowed.example/object",
                "destination_headers": {"Content-Type": "image/png"},
                "expected_size": 5,
                "expected_content_type": "image/png",
                "max_bytes": 10,
            },
        )

    assert response.status_code == 403
    assert calls == []


def test_signature_replay_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-0/5", "Content-Type": "image/png"},
            content=b"h",
        )

    nonce = uuid.uuid4().hex
    payload = {"source_url": "https://source.example/file", "max_bytes": 10}
    with TestClient(create_app(settings(), transport=httpx.MockTransport(handler))) as client:
        first = post(client, "/v1/probe", payload, nonce=nonce)
        second = post(client, "/v1/probe", payload, nonce=nonce)

    assert first.status_code == 200
    assert second.status_code == 409
