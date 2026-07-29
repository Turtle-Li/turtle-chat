from __future__ import annotations

import json
import unittest

import httpx

from chatgpt_web_gateway.upstream import (
    UpstreamClient,
    extract_upstream_media_metrics,
    extract_upstream_resource_metadata,
)


class UpstreamClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_host_alias_is_rewritten_for_loopback_worker(self) -> None:
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["host"] = request.headers["host"]
            return httpx.Response(200, json={"choices": []})

        client = UpstreamClient(
            base_url="http://host.docker.internal:38320/v1",
            health_path=None,
            api_key="worker-key",
            timeout_seconds=10,
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.completion({"model": "test", "messages": []})
        finally:
            await client.close()

        self.assertEqual(observed["host"], "127.0.0.1:38320")

    async def test_regular_upstream_keeps_its_original_host(self) -> None:
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["host"] = request.headers["host"]
            return httpx.Response(200, json={"choices": []})

        client = UpstreamClient(
            base_url="http://worker.test:8320/v1",
            health_path=None,
            api_key="worker-key",
            timeout_seconds=10,
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.completion({"model": "test", "messages": []})
        finally:
            await client.close()

        self.assertEqual(observed["host"], "worker.test:8320")

    async def test_cleanup_resource_sends_only_one_exact_identifier(self) -> None:
        observed: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["path"] = request.url.path
            observed["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "dry_run": False,
                    "http_status": 204,
                },
            )

        client = UpstreamClient(
            base_url="http://worker.test:8320/v1",
            health_path=None,
            api_key="worker-key",
            timeout_seconds=10,
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.cleanup_resource(
                resource_type="input_file",
                resource_id="file_12345678",
                dry_run=False,
                conversation_action="archive",
            )
        finally:
            await client.close()

        self.assertEqual(
            observed,
            {
                "path": "/api/OpenaiAccount/turtle/cleanup",
                "payload": {
                    "resource_type": "input_file",
                    "resource_id": "file_12345678",
                    "dry_run": False,
                    "conversation_action": "archive",
                },
            },
        )
        self.assertEqual(result["http_status"], 204)


class UpstreamResourceMetadataTests(unittest.TestCase):
    def test_extracts_only_valid_opaque_resource_ids(self) -> None:
        metadata = extract_upstream_resource_metadata(
            {
                "conversation": {
                    "conversation_id": "conversation_12345678",
                    "turtle_input_file_ids": [
                        "file_12345678",
                        "../../not-safe",
                        "file_12345678",
                    ],
                    "turtle_generated_asset_ids": ["asset_12345678"],
                    "prompt": "must be ignored",
                }
            }
        )

        self.assertEqual(
            metadata.conversation_id,
            "conversation_12345678",
        )
        self.assertEqual(metadata.input_file_ids, ("file_12345678",))
        self.assertEqual(
            metadata.generated_asset_ids,
            ("asset_12345678",),
        )

    def test_ignores_non_metadata_stream_events(self) -> None:
        metadata = extract_upstream_resource_metadata(
            'data: {"choices":[{"delta":{"content":"hello"}}]}'
        )
        self.assertTrue(metadata.empty)

    def test_extracts_only_bounded_sanitized_media_counters(self) -> None:
        metrics = extract_upstream_media_metrics(
            {
                "conversation": {
                    "turtle_media_metrics": {
                        "v": 1,
                        "probe_count": 2,
                        "transfer_count": 1,
                        "transfer_bytes": 3_151_219,
                        "cdn_hit": 1,
                        "cdn_miss": 1,
                        "fallback_count": 0,
                        "file_cache_hit": 1,
                        "file_cache_miss": 1,
                        "file_cache_stale": 0,
                        "source_url": "must be ignored",
                    }
                }
            }
        )

        self.assertEqual(metrics.transfer_bytes, 3_151_219)
        self.assertEqual(metrics.cdn_hit, 1)
        self.assertEqual(metrics.file_cache_hit, 1)

    def test_rejects_invalid_media_counter_versions_and_values(self) -> None:
        invalid = extract_upstream_media_metrics(
            {
                "conversation": {
                    "turtle_media_metrics": {
                        "v": 2,
                        "transfer_bytes": -1,
                    }
                }
            }
        )
        self.assertTrue(invalid.empty)
