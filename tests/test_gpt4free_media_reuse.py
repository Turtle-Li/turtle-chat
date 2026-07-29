from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import importlib.util
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime" / "gpt4free-src"))
if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("gpt4free runtime dependencies are not installed")

from g4f.Provider.needs_auth.OpenaiChat import (  # noqa: E402
    OpenaiChat,
    Conversation,
    PRIVATE_INPUT_FILE_CACHE_ATTR,
    _merge_model_media,
    _new_media_metrics,
    _private_input_file_cache_key,
)
from g4f.Provider.openai.media_pump import (  # noqa: E402
    MODEL_SOURCE_CONTEXT,
    MODEL_INPUT_MAX_BYTES,
    MediaPumpError,
    ModelMediaSource,
    _post_sync,
    open_model_source,
    probe_media,
)


PUMP_SECRET = "unit-test-pump-secret-that-is-at-least-thirty-two-characters"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def sealed_image_url(*, expires_at: int | None = None) -> dict:
    primary = (
        "https://files.chat.totools.cn/"
        "turtle-gpt/files/users/user-a/image.png?sign=test"
    )
    payload = {
        "v": 1,
        "exp": expires_at or int(time.time()) + 600,
        "id": "a" * 64,
        "primary_url": primary,
        "fallback_url": (
            "https://bucket.cos.ap-shanghai.myqcloud.com/"
            "turtle-gpt/files/users/user-a/image.png?q-signature"
        ),
    }
    encoded = _b64url(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        PUMP_SECRET.encode("utf-8"),
        MODEL_SOURCE_CONTEXT + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return {
        "url": primary,
        "turtle_source": f"{encoded}.{_b64url(signature)}",
    }


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, post_payloads: list[dict] | None = None):
        self.cookie_jar = []
        self.post_payloads = list(post_payloads or [])
        self.get_count = 0
        self.post_count = 0

    def get(self, _url, **_kwargs):
        self.get_count += 1
        return FakeResponse({"download_url": "https://oaiusercontent.example/reused"})

    def post(self, _url, **_kwargs):
        self.post_count += 1
        return FakeResponse(self.post_payloads.pop(0))


class ConcurrentFakeSession:
    def __init__(self):
        self.cookie_jar = []
        self.create_count = 0
        self.confirm_count = 0

    def post(self, url, **kwargs):
        if str(url).endswith("/backend-api/files"):
            self.create_count += 1
            name = str((kwargs.get("json") or {}).get("file_name") or "file")
            file_id = f"file_{hashlib.sha256(name.encode()).hexdigest()[:12]}"
            return FakeResponse(
                {
                    "file_id": file_id,
                    "upload_url": f"https://oaiusercontent.example/{file_id}",
                }
            )
        self.confirm_count += 1
        file_id = str(url).split("/files/", 1)[-1].split("/", 1)[0]
        return FakeResponse(
            {"download_url": f"https://oaiusercontent.example/{file_id}/download"}
        )


class FakeControlResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit: int):
        return self.body


def control_http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://pump.example/v1/probe",
        status,
        "test error",
        {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


class ModelSourceEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "TURTLE_MEDIA_PUMP_URL": "https://pump.example",
                "TURTLE_MEDIA_PUMP_SECRET": PUMP_SECRET,
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_signed_source_round_trips_and_tampering_fails_closed(self):
        image_url = sealed_image_url()
        source = open_model_source(image_url)

        self.assertEqual(source.media_id, "a" * 64)
        self.assertEqual(source.primary_url, image_url["url"])
        self.assertIn(".cos.ap-shanghai.myqcloud.com/", source.fallback_url)

        tampered = dict(image_url)
        tampered["url"] = tampered["url"].replace("image.png", "other.png")
        with self.assertRaises(MediaPumpError):
            open_model_source(tampered)

        expired = sealed_image_url(expires_at=int(time.time()) - 1)
        with self.assertRaises(MediaPumpError):
            open_model_source(expired)

    def test_merge_preserves_verified_source_metadata_for_last_user_branch(self):
        image_url = sealed_image_url()
        messages = [
            {"role": "assistant", "content": "prior"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": image_url},
                ],
            },
        ]

        merged = list(_merge_model_media(None, messages))

        self.assertEqual(len(merged), 1)
        self.assertIsInstance(merged[0][0], ModelMediaSource)
        self.assertEqual(merged[0][0].media_id, "a" * 64)

    def test_merge_deduplicates_message_and_compatibility_media_source(self):
        image_url = sealed_image_url()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": image_url},
                ],
            },
        ]

        merged = list(
            _merge_model_media([(image_url["url"], "image.png")], messages)
        )

        self.assertEqual(len(merged), 1)
        self.assertIsInstance(merged[0][0], ModelMediaSource)
        self.assertEqual(merged[0][0].media_id, "a" * 64)


class UpstreamFileReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "TURTLE_MEDIA_PUMP_URL": "https://pump.example",
                "TURTLE_MEDIA_PUMP_SECRET": PUMP_SECRET,
            },
            clear=False,
        )
        self.environment.start()
        OpenaiChat._headers = {}
        self.auth = SimpleNamespace(cookies={}, headers={})

    async def asyncTearDown(self):
        self.environment.stop()

    async def test_valid_cached_file_id_skips_probe_and_transfer(self):
        source = open_model_source(sealed_image_url())
        source_cache_key = _private_input_file_cache_key(source)
        conversation = Conversation(user_id=None)
        conversation.turtle_media_metrics = _new_media_metrics()
        setattr(
            conversation,
            PRIVATE_INPUT_FILE_CACHE_ATTR,
            {
                source_cache_key: {
                    "file_id": "file_12345678",
                    "file_name": "image.png",
                    "file_size": 4,
                    "use_case": "multimodal",
                    "mime_type": "image/png",
                    "extension": ".png",
                    "width": 1,
                    "height": 1,
                }
            },
        )
        session = FakeSession()

        with (
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.probe_media",
                new=AsyncMock(side_effect=AssertionError("probe must not run")),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.transfer_media",
                new=AsyncMock(side_effect=AssertionError("transfer must not run")),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.raise_for_status",
                new=AsyncMock(),
            ),
        ):
            uploaded = await OpenaiChat.upload_files(
                session,
                self.auth,
                [source],
                conversation,
            )

        self.assertEqual(uploaded[0].get("file_id"), "file_12345678")
        self.assertEqual(session.get_count, 1)
        self.assertEqual(session.post_count, 0)
        self.assertEqual(conversation.turtle_media_metrics["file_cache_hit"], 1)
        self.assertEqual(conversation.turtle_media_metrics["transfer_count"], 0)

    async def test_cache_miss_uploads_once_and_stores_only_minimal_metadata(self):
        source = open_model_source(sealed_image_url())
        conversation = Conversation(user_id=None)
        conversation.turtle_media_metrics = _new_media_metrics()
        shared_cache = {}
        setattr(conversation, PRIVATE_INPUT_FILE_CACHE_ATTR, shared_cache)
        session = FakeSession(
            post_payloads=[
                {
                    "file_id": "file_abcdefgh",
                    "upload_url": "https://oaiusercontent.example/upload-target",
                },
                {"download_url": "https://oaiusercontent.example/download"},
            ]
        )
        probe = {
            "size": 4,
            "content_type": "image/png",
            "max_bytes": 1024,
            "width": 1,
            "height": 1,
            "source": "primary",
            "cdn_cache": "miss",
            "retry_count": 1,
        }
        transfer = {
            "size": 4,
            "source": "primary",
            "cdn_cache": "hit",
            "retry_count": 1,
        }

        with (
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.probe_media",
                new=AsyncMock(return_value=probe),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.transfer_media",
                new=AsyncMock(return_value=transfer),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.raise_for_status",
                new=AsyncMock(),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            uploaded = await OpenaiChat.upload_files(
                session,
                self.auth,
                [source],
                conversation,
            )

        cache = getattr(conversation, PRIVATE_INPUT_FILE_CACHE_ATTR)
        self.assertIs(cache, shared_cache)
        cached = cache[_private_input_file_cache_key(source)]
        self.assertEqual(uploaded[0].get("file_id"), "file_abcdefgh")
        self.assertNotIn("upload_url", cached)
        self.assertNotIn("download_url", cached)
        self.assertEqual(conversation.turtle_media_metrics["file_cache_miss"], 1)
        self.assertEqual(conversation.turtle_media_metrics["cdn_miss"], 1)
        self.assertEqual(conversation.turtle_media_metrics["cdn_hit"], 1)
        self.assertEqual(conversation.turtle_media_metrics["retry_count"], 2)
        self.assertEqual(conversation.turtle_media_metrics["transfer_bytes"], 4)
        self.assertNotIn(PRIVATE_INPUT_FILE_CACHE_ATTR, conversation.get_dict())

    async def test_managed_uploads_use_bounded_concurrency_and_preserve_order(self):
        sources = [
            ModelMediaSource(
                "https://files.chat.totools.cn/"
                f"turtle-gpt/files/users/user-a/image-{index}.png",
                media_id=f"{index:064x}",
            )
            for index in range(3)
        ]
        conversation = Conversation(user_id=None)
        session = ConcurrentFakeSession()
        state = {"active": 0, "maximum": 0}
        release = asyncio.Event()
        all_prepared = asyncio.Event()
        original_post = session.post

        def track_prepared(url, **kwargs):
            response = original_post(url, **kwargs)
            if session.create_count == 3:
                all_prepared.set()
            return response

        session.post = track_prepared

        async def transfer(**_kwargs):
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            if state["active"] == 2:
                await asyncio.wait_for(all_prepared.wait(), timeout=1)
                state["prepared_before_release"] = session.create_count
                release.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=1)
                await asyncio.sleep(0)
                return {
                    "size": 4,
                    "source": "primary",
                    "cdn_cache": "hit",
                    "retry_count": 0,
                }
            finally:
                state["active"] -= 1

        probe = {
            "size": 4,
            "content_type": "image/png",
            "max_bytes": 1024,
            "width": 1,
            "height": 1,
            "source": "primary",
            "cdn_cache": "hit",
            "retry_count": 0,
        }
        with (
            patch.dict(
                os.environ,
                {"TURTLE_MEDIA_UPLOAD_CONCURRENCY": "2"},
                clear=False,
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.probe_media",
                new=AsyncMock(return_value=probe),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.transfer_media",
                new=transfer,
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.raise_for_status",
                new=AsyncMock(),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            uploaded = await OpenaiChat.upload_files(
                session,
                self.auth,
                [
                    (source, f"image-{index}.png")
                    for index, source in enumerate(sources)
                ],
                conversation,
            )

        self.assertEqual(
            [item.get("file_name") for item in uploaded],
            ["image-0.png", "image-1.png", "image-2.png"],
        )
        self.assertEqual(state["maximum"], 2)
        self.assertEqual(state["prepared_before_release"], 3)
        self.assertEqual(session.create_count, 3)
        self.assertEqual(session.confirm_count, 3)
        self.assertEqual(
            conversation.turtle_media_metrics["configured_parallel"],
            2,
        )
        self.assertEqual(conversation.turtle_media_metrics["max_parallel"], 2)

    async def test_duplicate_managed_sources_share_one_in_flight_upload(self):
        source = ModelMediaSource(
            "https://files.chat.totools.cn/"
            "turtle-gpt/files/users/user-a/duplicate.png",
            media_id="a" * 64,
        )
        conversation = Conversation(user_id=None)
        session = ConcurrentFakeSession()
        probe = AsyncMock(
            return_value={
                "size": 4,
                "content_type": "image/png",
                "max_bytes": 1024,
                "source": "primary",
                "cdn_cache": "hit",
                "retry_count": 0,
            }
        )
        transfer = AsyncMock(
            return_value={
                "size": 4,
                "source": "primary",
                "cdn_cache": "hit",
                "retry_count": 0,
            }
        )
        with (
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.probe_media",
                new=probe,
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.transfer_media",
                new=transfer,
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.raise_for_status",
                new=AsyncMock(),
            ),
            patch(
                "g4f.Provider.needs_auth.OpenaiChat.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            uploaded = await OpenaiChat.upload_files(
                session,
                self.auth,
                [(source, "duplicate.png"), (source, "duplicate.png")],
                conversation,
            )

        self.assertEqual(len(uploaded), 2)
        self.assertEqual(uploaded[0].get("file_id"), uploaded[1].get("file_id"))
        self.assertEqual(probe.await_count, 1)
        self.assertEqual(transfer.await_count, 1)
        self.assertEqual(session.create_count, 1)

    def test_private_cache_key_ignores_signed_query_and_envelope_identity(self):
        first = ModelMediaSource(
            "https://files.chat.totools.cn/"
            "turtle-gpt/files/users/user-a/image.png?sign=first",
            media_id="a" * 64,
        )
        second = ModelMediaSource(
            "https://files.chat.totools.cn/"
            "turtle-gpt/files/users/user-a/image.png?sign=second",
            media_id="b" * 64,
        )
        different = ModelMediaSource(
            "https://files.chat.totools.cn/"
            "turtle-gpt/files/users/user-a/other.png?sign=second",
            media_id="a" * 64,
        )

        self.assertEqual(
            _private_input_file_cache_key(first),
            _private_input_file_cache_key(second),
        )
        self.assertNotEqual(
            _private_input_file_cache_key(first),
            _private_input_file_cache_key(different),
        )

    async def test_model_probe_limit_is_clamped_below_worker_buffer_boundary(self):
        source = open_model_source(sealed_image_url())
        pump_result = {
            "size": 4,
            "content_type": "image/png",
            "source": "primary",
            "cdn_cache": "hit",
        }
        with (
            patch.dict(
                os.environ,
                {"TURTLE_MEDIA_PUMP_MAX_INPUT_BYTES": str(200 * 1024**2)},
                clear=False,
            ),
            patch(
                "g4f.Provider.openai.media_pump._post_sync",
                return_value=pump_result,
            ) as post,
        ):
            result = await probe_media(source)

        self.assertEqual(result["size"], 4)
        self.assertEqual(
            post.call_args.args[1]["max_bytes"],
            MODEL_INPUT_MAX_BYTES,
        )


class MediaPumpControlRetryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "TURTLE_MEDIA_PUMP_URL": "https://pump.example",
                "TURTLE_MEDIA_PUMP_SECRET": PUMP_SECRET,
                "TURTLE_MEDIA_PUMP_RETRY_ATTEMPTS": "2",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_probe_retries_structured_502_and_uses_a_fresh_control_request(self):
        failure = control_http_error(
            502,
            {
                "detail": "source probe failed",
                "code": "source_probe_failed",
                "phase": "probe",
                "source": "fallback",
                "retryable": True,
            },
        )
        success = FakeControlResponse(
            {
                "size": 4,
                "content_type": "image/png",
                "source": "fallback",
                "cdn_cache": "unknown",
            }
        )
        observed_nonces = []

        def urlopen(request, **_kwargs):
            observed_nonces.append(request.get_header("X-turtle-pump-nonce"))
            if len(observed_nonces) == 1:
                raise failure
            return success

        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=urlopen,
            ),
            patch("g4f.Provider.openai.media_pump.time.sleep") as sleep,
            patch(
                "g4f.Provider.openai.media_pump.random.uniform",
                return_value=0,
            ),
        ):
            result = _post_sync("/v1/probe", {"source_url": "https://source.example/a"})

        self.assertEqual(result["_control_attempts"], 2)
        self.assertEqual(len(observed_nonces), 2)
        self.assertNotEqual(observed_nonces[0], observed_nonces[1])
        sleep.assert_called_once_with(0.35)

    def test_probe_exposes_only_bounded_failure_context_after_retry(self):
        failures = [
            control_http_error(
                502,
                {
                    "detail": "source probe failed",
                    "code": "source_probe_failed",
                    "phase": "probe",
                    "source": "fallback",
                    "retryable": True,
                    "source_url": "https://must-not-appear.example/secret",
                },
            )
            for _ in range(2)
        ]
        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=failures,
            ),
            patch("g4f.Provider.openai.media_pump.time.sleep"),
            patch(
                "g4f.Provider.openai.media_pump.random.uniform",
                return_value=0,
            ),
        ):
            with self.assertRaisesRegex(
                MediaPumpError,
                (
                    "external media pump probe failed after 2 attempts "
                    r"\(HTTP 502, code=source_probe_failed, source=fallback\)"
                ),
            ) as captured:
                _post_sync("/v1/probe", {"source_url": "https://source.example/a"})

        self.assertNotIn("must-not-appear", str(captured.exception))

    def test_probe_retries_an_unstructured_edge_502_but_not_a_403(self):
        edge_failure = urllib.error.HTTPError(
            "https://pump.example/v1/probe",
            502,
            "Bad Gateway",
            {},
            io.BytesIO(b"<html>edge failure</html>"),
        )
        success = FakeControlResponse({"size": 4, "content_type": "image/png"})
        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=[edge_failure, success],
            ) as urlopen,
            patch("g4f.Provider.openai.media_pump.time.sleep"),
            patch(
                "g4f.Provider.openai.media_pump.random.uniform",
                return_value=0,
            ),
        ):
            result = _post_sync("/v1/probe", {"source_url": "https://source.example/a"})

        self.assertEqual(result["_control_attempts"], 2)
        self.assertEqual(urlopen.call_count, 2)

        forbidden = control_http_error(
            403,
            {
                "detail": "source URL is not allowlisted",
                "code": "source_url_is_not_allowlisted",
                "retryable": False,
            },
        )
        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=forbidden,
            ) as urlopen,
            patch("g4f.Provider.openai.media_pump.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(MediaPumpError, "HTTP 403"):
                _post_sync("/v1/probe", {"source_url": "https://source.example/a"})

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_transfer_retries_only_a_structured_pre_destination_source_failure(self):
        source_failure = control_http_error(
            502,
            {
                "detail": "source download failed",
                "code": "source_download_failed",
                "phase": "source",
                "source": "fallback",
                "retryable": True,
            },
        )
        success = FakeControlResponse({"ok": True, "size": 4})
        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=[source_failure, success],
            ) as urlopen,
            patch("g4f.Provider.openai.media_pump.time.sleep") as sleep,
            patch(
                "g4f.Provider.openai.media_pump.random.uniform",
                return_value=0,
            ),
        ):
            result = _post_sync("/v1/transfers", {"source_url": "https://source.example/a"})

        self.assertEqual(result["_control_attempts"], 2)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

        destination_failure = control_http_error(
            502,
            {
                "detail": "destination upload failed",
                "code": "destination_upload_failed",
                "phase": "destination",
                "source": "fallback",
                "retryable": False,
            },
        )
        with (
            patch(
                "g4f.Provider.openai.media_pump.urllib.request.urlopen",
                side_effect=destination_failure,
            ) as urlopen,
            patch("g4f.Provider.openai.media_pump.time.sleep") as sleep,
        ):
            with self.assertRaises(MediaPumpError):
                _post_sync("/v1/transfers", {"source_url": "https://source.example/a"})

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
