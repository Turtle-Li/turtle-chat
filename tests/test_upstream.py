from __future__ import annotations

import json
import unittest

import httpx

from chatgpt_web_gateway.upstream import (
    UpstreamClient,
    UpstreamFailure,
    extract_upstream_media_metrics,
    extract_upstream_resource_metadata,
    extract_upstream_stage_metrics,
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

    async def test_rate_limit_retry_hint_is_sanitized_and_preserved(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "7200"},
                json={
                    "error": {
                        "message": (
                            "ChatGPT account rate limit reached; "
                            "turtle_retry_after_s=3600"
                        )
                    }
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
            with self.assertRaises(UpstreamFailure) as raised:
                await client.completion({"model": "test", "messages": []})
        finally:
            await client.close()

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after_seconds, 3600)
        self.assertNotIn(
            "turtle_retry_after_s",
            raised.exception.message,
        )

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
                        "retry_count": 1,
                        "file_cache_hit": 1,
                        "file_cache_miss": 1,
                        "file_cache_stale": 0,
                        "upload_wall_ms": 1420,
                        "cache_validation_ms": 12,
                        "probe_ms": 210,
                        "create_ms": 180,
                        "settle_ms": 1000,
                        "transfer_ms": 350,
                        "confirm_ms": 90,
                        "configured_parallel": 3,
                        "max_parallel": 2,
                        "source_url": "must be ignored",
                    }
                }
            }
        )

        self.assertEqual(metrics.transfer_bytes, 3_151_219)
        self.assertEqual(metrics.cdn_hit, 1)
        self.assertEqual(metrics.retry_count, 1)
        self.assertEqual(metrics.file_cache_hit, 1)
        self.assertEqual(metrics.upload_wall_ms, 1420)
        self.assertEqual(metrics.max_parallel, 2)

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

    def test_extracts_only_bounded_upstream_stage_timings(self) -> None:
        metrics = extract_upstream_stage_metrics(
            {
                "conversation": {
                    "turtle_upstream_stage_metrics": {
                        "v": 3,
                        "home_ms": 0,
                        "media_ms": 12_300,
                        "prepare_ms": 240,
                        "requirements_ms": 410,
                        "submit_headers_ms": 90,
                        "submit_namelookup_ms": 4,
                        "submit_connect_ms": 38,
                        "submit_appconnect_ms": 121,
                        "submit_pretransfer_ms": 124,
                        "submit_starttransfer_ms": 1_890,
                        "submit_server_wait_ms": 1_766,
                        "first_event_ms": 2_800,
                        "pre_stream_ms": 15_900,
                        "provider_first_parsed_string_ms": 7_050,
                        "upstream_events_before_first_parsed_string": 3,
                        "provider_first_emitted_string_ms": 7_100,
                        "handoff_used": 1,
                        "handoff_seen_ms": 2_810,
                        "handoff_sse_tail_ms": 18,
                        "handoff_start_ms": 3_100,
                        "handoff_endpoint_ms": 190,
                        "handoff_connect_ms": 170,
                        "handoff_first_frame_ms": 25,
                        "handoff_first_item_ms": 4_200,
                        "handoff_first_item_topic_class": 2,
                        "handoff_items_expected": 0,
                        "handoff_items_conversations": 4,
                        "handoff_items_unscoped": 1,
                        "handoff_done_topic_class": 2,
                        "handoff_total_ms": 4_580,
                        "source_url": "must be ignored",
                    }
                }
            }
        )

        self.assertEqual(metrics.media_ms, 12_300)
        self.assertEqual(metrics.schema_version, 3)
        self.assertEqual(metrics.submit_namelookup_ms, 4)
        self.assertEqual(metrics.submit_connect_ms, 38)
        self.assertEqual(metrics.submit_appconnect_ms, 121)
        self.assertEqual(metrics.submit_pretransfer_ms, 124)
        self.assertEqual(metrics.submit_starttransfer_ms, 1_890)
        self.assertEqual(metrics.submit_server_wait_ms, 1_766)
        self.assertEqual(metrics.first_event_ms, 2_800)
        self.assertEqual(metrics.pre_stream_ms, 15_900)
        self.assertEqual(metrics.provider_first_parsed_string_ms, 7_050)
        self.assertEqual(
            metrics.upstream_events_before_first_parsed_string,
            3,
        )
        self.assertEqual(metrics.provider_first_emitted_string_ms, 7_100)
        self.assertEqual(metrics.handoff_used, 1)
        self.assertEqual(metrics.handoff_start_ms, 3_100)
        self.assertEqual(metrics.handoff_endpoint_ms, 190)
        self.assertEqual(metrics.handoff_connect_ms, 170)
        self.assertEqual(metrics.handoff_first_frame_ms, 25)
        self.assertEqual(metrics.handoff_first_item_ms, 4_200)
        self.assertEqual(metrics.handoff_first_item_topic_class, 2)
        self.assertEqual(metrics.handoff_items_conversations, 4)
        self.assertEqual(metrics.handoff_items_unscoped, 1)
        self.assertEqual(metrics.handoff_done_topic_class, 2)
        self.assertEqual(metrics.handoff_total_ms, 4_580)

        invalid = extract_upstream_stage_metrics(
            {
                "conversation": {
                    "turtle_upstream_stage_metrics": {
                        "v": 2,
                        "pre_stream_ms": 3_600_001,
                        "handoff_used": 2,
                    }
                }
            }
        )
        self.assertTrue(invalid.empty)

        legacy = extract_upstream_stage_metrics(
            {
                "conversation": {
                    "turtle_upstream_stage_metrics": {
                        "v": 1,
                        "pre_stream_ms": 900,
                        "handoff_used": 1,
                        "handoff_total_ms": 500,
                    }
                }
            }
        )
        self.assertEqual(legacy.schema_version, 1)
        self.assertEqual(legacy.pre_stream_ms, 900)
        self.assertEqual(legacy.handoff_used, 0)
        self.assertEqual(legacy.handoff_total_ms, 0)

        handoff_v2 = extract_upstream_stage_metrics(
            {
                "conversation": {
                    "turtle_upstream_stage_metrics": {
                        "v": 2,
                        "submit_headers_ms": 700,
                        "submit_server_wait_ms": 650,
                        "handoff_used": 1,
                    }
                }
            }
        )
        self.assertEqual(handoff_v2.schema_version, 2)
        self.assertEqual(handoff_v2.submit_headers_ms, 700)
        self.assertEqual(handoff_v2.submit_server_wait_ms, 0)
        self.assertEqual(handoff_v2.handoff_used, 1)
