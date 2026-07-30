from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from chatgpt_web_gateway.app import (
    _claude_request_payload,
    _derive_upstream_conversation_key,
    _has_unsealed_image_url,
    _image_url_count,
    _message_shape,
    _payload_has_explicit_web_search,
    _payload_has_search_intent,
    _request_payload,
    _rewrite_nonstream,
    _sse_data_has_answer_text,
    create_app,
)
from chatgpt_web_gateway.config import Settings
from chatgpt_web_gateway.models import ChatCompletionRequest
from chatgpt_web_gateway.security import RedactingFilter, redact
from chatgpt_web_gateway.project_usage import (
    MemoryProjectUsageStore,
    ProjectRequestConflict,
)
from chatgpt_web_gateway.upstream import SearchPresentationBuffer, normalize_sse_events


def test_answer_text_detection_ignores_control_and_whitespace() -> None:
    assert not _sse_data_has_answer_text("[DONE]")
    assert not _sse_data_has_answer_text(
        json.dumps(
            {
                "choices": [
                    {"delta": {"role": "assistant", "content": " \n"}}
                ]
            }
        )
    )
    assert not _sse_data_has_answer_text(
        json.dumps(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"id": "tool-1"}]}}
                ]
            }
        )
    )
    assert _sse_data_has_answer_text(
        json.dumps(
            {
                "choices": [
                    {"delta": {"content": "你好"}}
                ]
            }
        )
    )


def settings(**overrides) -> Settings:
    values = {
        "gateway_api_key": "gateway-test-key",
        "backend": "mock",
        "public_model_name": "gpt-5-web",
        "upstream_model": "auto",
        "upstream_base_url": "http://upstream.test/v1",
        "upstream_health_path": None,
        "upstream_api_key": "upstream-test-key",
        "upstream_timeout_seconds": 30.0,
    }
    values.update(overrides)
    return Settings(**values)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer gateway-test-key"}


def grant_project_credit(
    client: TestClient,
    owner: str,
    *,
    amount_microusd: int = 100_000_000,
) -> None:
    response = client.post(
        f"/internal/project-api/permissions/{owner}/credits",
        headers=headers(),
        json={
            "amount_microusd": amount_microusd,
            "reason": "测试额度",
            "idempotency_key": f"test-grant-{owner}",
            "updated_by": "admin-1",
        },
    )
    assert response.status_code == 200


def add_memory_backup_account(
    client: TestClient,
    *,
    account_id: str = "backup-account",
    endpoint: str = "http://backup.test/v1",
) -> None:
    store = client.app.state.account_pool.store
    source = dict(store.accounts["legacy-primary"])
    source.update(
        {
            "id": account_id,
            "name": "备用账号",
            "worker_endpoint": endpoint,
            "priority": 0,
            "last_used_at": None,
            "last_success_at": None,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )
    store.accounts[account_id] = source


def test_requires_bearer_key() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.get("/v1/models")
    assert response.status_code == 401


def test_project_api_key_is_owner_scoped_and_secret_is_returned_once() -> None:
    with TestClient(create_app(settings())) as client:
        denied = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "小说项目"},
        )
        assert denied.status_code == 403

        permission = client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        assert permission.status_code == 200
        grant_project_credit(client, "user-a")

        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "小说项目"},
        )
        assert created.status_code == 200
        secret = created.json()["api_key"]
        assert secret.startswith("turtle_proj_")

        listed = client.get(
            "/internal/project-api/keys?owner_user_id=user-a",
            headers=headers(),
        )
        assert listed.status_code == 200
        assert listed.json()["items"][0]["owner_user_id"] == "user-a"
        assert "api_key" not in listed.json()["items"][0]

        project_headers = {"Authorization": f"Bearer {secret}"}
        context = client.get("/v1/project/context", headers=project_headers)
        assert context.status_code == 200
        assert context.json() == {
            "object": "project.context",
            "project_key_id": created.json()["id"],
            "owner_user_id": "user-a",
            "is_master": False,
        }
        master_context = client.get("/v1/project/context", headers=headers())
        assert master_context.status_code == 200
        assert master_context.json()["is_master"] is True
        assert master_context.json()["project_key_id"] is None
        assert master_context.json()["owner_user_id"] is None

        completion = client.post(
            "/v1/chat/completions",
            headers=project_headers,
            json={
                "model": "gpt-5-web",
                "turtle_request_id": "owner-scope-request-0001",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert completion.status_code == 200

        usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        )
        assert usage.status_code == 200
        payload = usage.json()
        assert payload["totals"]["requests"] == 1
        assert payload["totals"]["locally_estimated_requests"] == 1
        assert payload["totals"]["official_cost_microusd"] > 0
        assert (
            payload["totals"]["actual_cost_microusd"]
            == payload["totals"]["official_cost_microusd"]
        )
        assert payload["pricing_config"]["cost_multiplier"] == 1
        assert payload["recent"][0]["project_name"] == "小说项目"
        assert payload["recent"][0]["usage_source"] == "locally_estimated"
        assert payload["recent"][0]["pricing_profile"] == "gpt-5.6-sol"

        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": False, "updated_by": "admin-1"},
        )
        assert client.get("/v1/models", headers=project_headers).status_code == 401
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        assert client.get("/v1/models", headers=project_headers).status_code == 200

        revoked = client.delete(
            f"/internal/project-api/keys/{created.json()['id']}?owner_user_id=user-a",
            headers=headers(),
        )
        assert revoked.status_code == 200
        assert client.get("/v1/models", headers=project_headers).status_code == 401


def test_project_api_credit_grant_and_request_deduplication() -> None:
    with TestClient(create_app(settings())) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "额度项目"},
        ).json()
        grant = {
            "amount_microusd": 10_000_000,
            "reason": "初始测试额度",
            "idempotency_key": "grant-user-a-0001",
            "updated_by": "admin-1",
        }
        first_grant = client.post(
            "/internal/project-api/permissions/user-a/credits",
            headers=headers(),
            json=grant,
        )
        repeated_grant = client.post(
            "/internal/project-api/permissions/user-a/credits",
            headers=headers(),
            json=grant,
        )
        assert first_grant.status_code == 200
        assert repeated_grant.status_code == 200
        assert repeated_grant.json()["id"] == first_grant.json()["id"]

        project_headers = {"Authorization": f"Bearer {created['api_key']}"}
        body = {
            "model": "gpt-5-web",
            "turtle_request_id": "credit-dedupe-request-0001",
            "max_completion_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert client.post(
            "/v1/chat/completions",
            headers=project_headers,
            json=body,
        ).status_code == 200
        duplicate = client.post(
            "/v1/chat/completions",
            headers=project_headers,
            json=body,
        )
        assert duplicate.status_code == 409

        usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()
        credits = client.get(
            "/internal/project-api/permissions/user-a/credits",
            headers=headers(),
        ).json()
        permission = client.get(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
        ).json()
        assert usage["totals"]["requests"] == 1
        assert len(credits["items"]) == 2
        assert {item["entry_type"] for item in credits["items"]} == {
            "grant",
            "usage",
        }
        assert permission["reserved_microusd"] == 0
        assert permission["balance_microusd"] == (
            10_000_000 - usage["totals"]["actual_cost_microusd"]
        )


def test_project_api_ledger_is_concurrency_safe() -> None:
    store = MemoryProjectUsageStore("concurrency-test-master")
    store.set_permission(
        "user-a",
        enabled=True,
        updated_by="admin-1",
    )
    key = store.create_key("user-a", "并发项目")

    def grant_once():
        return store.grant_credit(
            "user-a",
            1_000_000,
            reason="并发发放",
            idempotency_key="concurrent-grant-0001",
            updated_by="admin-1",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        grants = list(executor.map(lambda _index: grant_once(), range(40)))
    assert len({item["id"] for item in grants}) == 1
    assert store.permission("user-a")["balance_microusd"] == 1_000_000

    def settle(index: int):
        request_id = f"concurrent-request-{index:04d}"
        store.begin_request(
            key["id"],
            request_id,
            authorization_microusd=5_000,
        )
        return store.record(
            {
                "request_id": request_id,
                "key_id": key["id"],
                "provider": "gpt",
                "model": "gpt-5-web",
                "route": "latest:medium",
                "stream": False,
                "outcome": "success",
                "status_code": 200,
                "prompt_tokens": 10,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "completion_tokens": 10,
                "total_tokens": 20,
                "usage_source": "upstream_reported",
                "points": 0,
                "pricing_profile": "gpt-5.6-sol",
                "price_card_version": "test",
                "input_rate_nano_usd": 5_000,
                "cached_input_rate_nano_usd": 500,
                "cache_write_rate_nano_usd": 6_250,
                "output_rate_nano_usd": 30_000,
                "official_cost_microusd": 1_000,
                "latency_ms": 10,
            }
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        settled = list(executor.map(settle, range(100)))
    assert all(item["recorded"] for item in settled)
    assert store.permission("user-a")["balance_microusd"] == 900_000
    assert store.permission("user-a")["reserved_microusd"] == 0
    assert len(store.usage) == 100
    assert store.list_keys("user-a")[0]["request_count"] == 100
    assert len(store.list_credit_ledger("user-a", limit=200)) == 101

    duplicate_entry = dict(store.usage[0])
    with ThreadPoolExecutor(max_workers=16) as executor:
        duplicates = list(
            executor.map(lambda _index: store.record(duplicate_entry), range(40))
        )
    assert all(not item["recorded"] for item in duplicates)
    assert store.permission("user-a")["balance_microusd"] == 900_000
    assert store.list_keys("user-a")[0]["request_count"] == 100

    try:
        store.begin_request(
            key["id"],
            "concurrent-request-0000",
            authorization_microusd=5_000,
        )
    except ProjectRequestConflict:
        pass
    else:
        raise AssertionError("duplicate request id must be rejected")


def test_project_api_prepaid_credit_fails_closed() -> None:
    with TestClient(create_app(settings())) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "零额度项目"},
        ).json()
        project_headers = {"Authorization": f"Bearer {created['api_key']}"}

        missing_idempotency = client.post(
            "/v1/chat/completions",
            headers=project_headers,
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert missing_idempotency.status_code == 400
        assert missing_idempotency.json()["error"]["type"] == (
            "idempotency_key_required"
        )

        insufficient = client.post(
            "/v1/chat/completions",
            headers={
                **project_headers,
                "Idempotency-Key": "insufficient-credit-request-0001",
            },
            json={
                "model": "gpt-5-web",
                "max_completion_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert insufficient.status_code == 402
        usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()
        assert usage["totals"]["requests"] == 0
        assert client.get(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
        ).json()["balance_microusd"] == 0


def test_project_usage_filters_do_not_cross_user_ownership() -> None:
    with TestClient(create_app(settings())) as client:
        secrets = {}
        for owner in ("user-a", "user-b"):
            client.put(
                f"/internal/project-api/permissions/{owner}",
                headers=headers(),
                json={"enabled": True, "updated_by": "admin-1"},
            )
            grant_project_credit(client, owner)
            created = client.post(
                "/internal/project-api/keys",
                headers=headers(),
                json={"owner_user_id": owner, "name": f"{owner}-project"},
            )
            secrets[owner] = created.json()["api_key"]
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {secrets[owner]}"},
                json={
                    "model": "gpt-5-web",
                    "turtle_request_id": f"ownership-{owner}-request",
                    "messages": [{"role": "user", "content": owner}],
                },
            )

        scoped = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()
        assert scoped["totals"]["requests"] == 1
        assert {item["owner_user_id"] for item in scoped["projects"]} == {"user-a"}


def test_project_api_permission_enforces_key_limit_and_delete_revokes_keys() -> None:
    with TestClient(create_app(settings())) as client:
        permission = client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1", "max_keys": 1},
        )
        assert permission.status_code == 200
        assert permission.json()["max_keys"] == 1

        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "第一个项目"},
        )
        assert created.status_code == 200
        secret = created.json()["api_key"]

        limited = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "第二个项目"},
        )
        assert limited.status_code == 409
        assert "最多可创建 1 个" in limited.json()["detail"]

        deleted = client.delete(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
        )
        assert deleted.status_code == 200
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        ).status_code == 401
        assert client.get(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
        ).json()["enabled"] is False


def test_project_api_keys_only_expose_gpt_model() -> None:
    with TestClient(create_app(settings(claude_pool_enabled=True))) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        grant_project_credit(client, "user-a")
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "GPT 项目"},
        ).json()
        project_headers = {"Authorization": f"Bearer {created['api_key']}"}

        models = client.get("/v1/models", headers=project_headers)
        assert models.status_code == 200
        assert [item["id"] for item in models.json()["data"]] == ["gpt-5-web"]

        denied = client.post(
            "/v1/chat/completions",
            headers=project_headers,
                json={
                    "model": "claude-web",
                    "turtle_request_id": "claude-project-denied-0001",
                    "messages": [{"role": "user", "content": "hello"}],
                },
        )
        assert denied.status_code == 404


def test_project_usage_prefers_upstream_reported_token_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5-6-thinking",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 350,
                    "total_tokens": 1550,
                    "prompt_tokens_details": {
                        "cached_tokens": 200,
                        "cache_write_tokens": 100,
                    },
                },
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        grant_project_credit(client, "user-a")
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "token-project"},
        ).json()
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {created['api_key']}"},
            json={
                "model": "gpt-5-web",
                "turtle_request_id": "reported-usage-request-0001",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200
        usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()

    assert usage["totals"]["reported_tokens"] == 1550
    assert usage["totals"]["official_cost_microusd"] == 15600
    assert usage["totals"]["actual_cost_microusd"] == 15600
    assert usage["recent"][0]["usage_source"] == "upstream_reported"
    assert usage["recent"][0]["cached_tokens"] == 200
    assert usage["recent"][0]["cache_write_tokens"] == 100
    assert usage["recent"][0]["pricing_profile"] == "gpt-5.5"
    assert usage["price_profiles"]["gpt-5.6-sol"] == {
        "model": "gpt-5.6-sol",
        "input_usd_per_million": 5.0,
        "cached_input_usd_per_million": 0.5,
        "cache_write_usd_per_million": 6.25,
        "output_usd_per_million": 30.0,
    }


def test_project_usage_uses_route_specific_api_price_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-priced",
                "object": "chat.completion",
                "created": 1,
                "model": "o3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                },
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        grant_project_credit(client, "user-a")
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "priced-project"},
        ).json()
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {created['api_key']}"},
            json={
                "model": "gpt-5-web",
                "turtle_request_id": "route-price-request-0001",
                "turtle_model_version": "o3",
                "turtle_thinking_level": "standard",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200
        first_usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()
        updated = client.put(
            "/internal/project-api/config",
            headers=headers(),
            json={"cost_multiplier": 1.5, "updated_by": "admin-1"},
        )
        assert updated.status_code == 200
        assert updated.json()["cost_multiplier"] == 1.5
        second = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {created['api_key']}"},
            json={
                "model": "gpt-5-web",
                "turtle_request_id": "route-price-request-0002",
                "turtle_model_version": "o3",
                "turtle_thinking_level": "standard",
                "messages": [{"role": "user", "content": "hello again"}],
            },
        )
        assert second.status_code == 200
        usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()
        client.put(
            "/internal/project-api/config",
            headers=headers(),
            json={"cost_multiplier": 0, "updated_by": "admin-1"},
        )
        assert client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {created['api_key']}"},
            json={
                "model": "gpt-5-web",
                "turtle_request_id": "route-price-request-0003",
                "turtle_model_version": "o3",
                "turtle_thinking_level": "standard",
                "messages": [{"role": "user", "content": "zero multiplier"}],
            },
        ).status_code == 200
        zero_usage = client.get(
            "/internal/project-api/usage?hours=24&owner_user_id=user-a",
            headers=headers(),
        ).json()

    original = first_usage["recent"][0]
    assert original["route"] == "o3:standard"
    assert original["pricing_profile"] == "o3"
    assert original["official_cost_microusd"] == 280
    assert original["cost_multiplier"] == 1
    assert original["actual_cost_microusd"] == 280
    newest = next(
        item for item in usage["recent"] if item["cost_multiplier"] == 1.5
    )
    historical = next(
        item for item in usage["recent"] if item["cost_multiplier"] == 1
    )
    assert newest["official_cost_microusd"] == 280
    assert newest["cost_multiplier"] == 1.5
    assert newest["actual_cost_microusd"] == 420
    assert historical["cost_multiplier"] == 1
    assert historical["actual_cost_microusd"] == 280
    assert usage["totals"]["official_cost_microusd"] == 560
    assert usage["totals"]["actual_cost_microusd"] == 700
    free = next(
        item for item in zero_usage["recent"] if item["cost_multiplier"] == 0
    )
    assert free["official_cost_microusd"] == 280
    assert free["actual_cost_microusd"] == 0
    assert zero_usage["totals"]["official_cost_microusd"] == 840
    assert zero_usage["totals"]["actual_cost_microusd"] == 700


def test_nonstream_openai_shape() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={"model": "gpt-5-web", "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gpt-5-web"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "hello"}


def test_historical_empty_assistant_message_is_discarded() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": ""},
                    {"role": "user", "content": "second"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "mock: second"


def test_latest_empty_assistant_message_is_still_rejected() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": ""},
                ],
            },
        )

    assert response.status_code == 422


def test_image_url_count_reports_only_structured_media() -> None:
    assert _image_url_count(
        {
            "messages": [
                {"role": "user", "content": "plain text"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                    ],
                },
            ]
        }
    ) == 1
    assert _message_shape(
        {
            "messages": [
                {"role": "assistant", "content": "prior"},
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
                },
            ]
        }
    ) == (2, "user", True)


def test_unsealed_image_url_detection_requires_a_managed_source_token() -> None:
    source_token = f"{'a' * 20}.{'b' * 43}"
    assert _has_unsealed_image_url(
        {
            "messages": [
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/a.png"},
                        }
                    ]
                }
            ]
        }
    )
    assert not _has_unsealed_image_url(
        {
            "messages": [
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.test/a.png",
                                "turtle_source": source_token,
                            },
                        }
                    ]
                }
            ]
        }
    )


def test_models_expose_family_version_and_thinking_controls() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.get("/v1/models", headers=headers())

    assert response.status_code == 200
    model = response.json()["data"][0]
    assert model["id"] == "gpt-5-web"
    assert model["name"] == "GPT"
    assert model["turtle"]["family"] == "gpt"
    assert model["turtle"]["default_version"] == "gpt-5-5"
    versions = model["turtle"]["versions"]
    assert [item["id"] for item in versions] == [
        "latest",
        "gpt-5-5",
        "gpt-5-3",
        "o3",
    ]
    latest = next(item for item in versions if item["id"] == "latest")
    assert latest["default_thinking_level"] == "medium"
    assert [item["id"] for item in latest["thinking_levels"]] == [
        "medium",
        "high",
        "xhigh",
        "pro",
    ]
    gpt_55 = next(item for item in versions if item["id"] == "gpt-5-5")
    assert gpt_55["default_thinking_level"] == "instant"
    assert gpt_55["thinking_levels"] == [{"id": "instant", "label": "极速"}]
    gpt_53 = next(item for item in versions if item["id"] == "gpt-5-3")
    assert gpt_53["default_thinking_level"] == "standard"
    assert [item["id"] for item in gpt_53["thinking_levels"]] == ["standard"]
    o3 = next(item for item in versions if item["id"] == "o3")
    assert o3["default_thinking_level"] == "standard"
    assert [item["id"] for item in o3["thinking_levels"]] == ["standard"]
    assert model["turtle"]["picker"]["model_order"] == [
        "latest",
        "gpt-5-5",
        "gpt-5-3",
        "o3",
    ]


def test_stream_uses_openai_sse_and_done_marker() -> None:
    with TestClient(create_app(settings())) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        ) as response:
            lines = [line for line in response.iter_lines() if line]
    assert response.status_code == 200
    assert lines[-1] == "data: [DONE]"
    events = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_oversized_stream_delta_is_split_without_changing_the_answer() -> None:
    source = {
        "id": "chatcmpl-upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5-6-thinking",
        "choices": [
            {
                "index": 0,
                "delta": {"content": "海龟正在逐步游过屏幕"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 10},
    }

    encoded = normalize_sse_events(json.dumps(source, ensure_ascii=False), "gpt-5-web", 3)
    events = [json.loads(item) for item in encoded]

    assert len(events) > 1
    assert "".join(item["choices"][0]["delta"]["content"] for item in events) == source["choices"][0]["delta"]["content"]
    assert all(item["model"] == "gpt-5-web" for item in events)
    assert all(item["choices"][0]["finish_reason"] is None for item in events[:-1])
    assert all("usage" not in item for item in events[:-1])
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"] == source["usage"]


def test_reasoning_delta_is_preserved_without_being_synthesized() -> None:
    source = {
        "id": "chatcmpl-upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5-6-thinking",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": None,
                    "reasoning": "正在比较两个可行方案",
                },
                "finish_reason": None,
            }
        ],
    }

    encoded = normalize_sse_events(json.dumps(source, ensure_ascii=False), "gpt-5-web", 3)
    event = json.loads(encoded[0])

    assert len(encoded) == 1
    assert event["model"] == "gpt-5-web"
    assert event["choices"][0]["delta"] == source["choices"][0]["delta"]


def test_streamed_markdown_heading_is_separated_from_previous_sentence() -> None:
    source = {
        "model": "gpt-5-6-thinking",
        "choices": [
            {
                "index": 0,
                "delta": {"content": "会先核对官方页面。## 结论\n\n已确认。"},
                "finish_reason": None,
            }
        ],
    }

    encoded = normalize_sse_events(
        json.dumps(source, ensure_ascii=False),
        "gpt-5-web",
        256,
    )
    event = json.loads(encoded[0])

    assert event["choices"][0]["delta"]["content"] == (
        "会先核对官方页面。\n\n## 结论\n\n已确认。"
    )


def test_streamed_markdown_heading_is_separated_across_delta_boundary() -> None:
    source = {
        "model": "gpt-5-6-thinking",
        "choices": [
            {
                "index": 0,
                "delta": {"content": "## 结论\n\n已确认。"},
                "finish_reason": None,
            }
        ],
    }

    encoded = normalize_sse_events(
        json.dumps(source, ensure_ascii=False),
        "gpt-5-web",
        256,
        "会先核对官方页面。",
    )
    event = json.loads(encoded[0])

    assert event["choices"][0]["delta"]["content"] == "\n\n## 结论\n\n已确认。"


def test_nonstream_markdown_heading_is_separated_without_mutating_source() -> None:
    source = {
        "model": "gpt-5-6-thinking",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "会先核对官方页面。## 结论\n\n已确认。",
                }
            }
        ],
    }

    normalized = _rewrite_nonstream(source, "gpt-5-web")

    assert normalized["choices"][0]["message"]["content"] == (
        "会先核对官方页面。\n\n## 结论\n\n已确认。"
    )
    assert source["choices"][0]["message"]["content"] == (
        "会先核对官方页面。## 结论\n\n已确认。"
    )


def _presentation_event(
    *,
    content: str | None = None,
    reasoning: str | None = None,
) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-presentation",
            "model": "gpt-5-web",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": content,
                        **({"reasoning": reasoning} if reasoning is not None else {}),
                    },
                    "finish_reason": None,
                }
            ],
        },
        ensure_ascii=False,
    )


def test_search_presentation_discards_orphaned_prefix_before_heading() -> None:
    presentation = SearchPresentationBuffer(enabled=True)
    progress_event = _presentation_event(reasoning="正在搜索网页\n")
    output = presentation.feed(progress_event)

    for piece in (".0 Release 页面，", "确认发布日期。", "## 结论\n\n", "已确认。"):
        output.extend(presentation.feed(_presentation_event(content=piece)))

    reasoning = ""
    content = ""
    for event in output:
        delta = json.loads(event)["choices"][0]["delta"]
        reasoning += str(delta.get("reasoning") or "")
        content += str(delta.get("content") or "")

    assert "正在搜索网页" in reasoning
    assert content == "## 结论\n\n已确认。"
    assert ".0 Release" not in content


def test_search_presentation_moves_short_work_note_into_reasoning() -> None:
    presentation = SearchPresentationBuffer(enabled=True)
    output: list[str] = []

    for piece in ("会先核对", "官方页面并确认发布日期。", "## 结论\n\n", "已确认。"):
        output.extend(presentation.feed(_presentation_event(content=piece)))

    reasoning = ""
    content = ""
    for event in output:
        delta = json.loads(event)["choices"][0]["delta"]
        reasoning += str(delta.get("reasoning") or "")
        content += str(delta.get("content") or "")

    assert reasoning.strip() == "会先核对官方页面并确认发布日期。"
    assert content == "## 结论\n\n已确认。"


def test_search_presentation_moves_neutral_intro_before_heading_into_reasoning() -> None:
    presentation = SearchPresentationBuffer(enabled=True)
    output: list[str] = []

    for piece in (
        "发布日期和两项最关键的更新，并只引用官方来源。",
        "## 结论\n\n",
        "已确认。",
    ):
        output.extend(presentation.feed(_presentation_event(content=piece)))

    reasoning = ""
    content = ""
    for event in output:
        delta = json.loads(event)["choices"][0]["delta"]
        reasoning += str(delta.get("reasoning") or "")
        content += str(delta.get("content") or "")

    assert reasoning.strip() == "发布日期和两项最关键的更新，并只引用官方来源。"
    assert content == "## 结论\n\n已确认。"


def test_search_presentation_flushes_plain_answer_after_second_paragraph() -> None:
    presentation = SearchPresentationBuffer(
        enabled=True,
        detection_chars=12,
    )
    first = _presentation_event(content="这是普通回答第一段。\n\n")
    second = _presentation_event(content="这是普通回答第二段。")

    assert presentation.feed(first) == []
    assert presentation.feed(second) == [first, second]


def test_payload_search_intent_accepts_explicit_feature_and_user_text() -> None:
    assert _payload_has_search_intent(
        {
            "features": {"web_search": True},
            "messages": [{"role": "user", "content": "告诉我答案"}],
        }
    )
    assert _payload_has_search_intent(
        {
            "messages": [
                {"role": "assistant", "content": "之前的回答"},
                {"role": "user", "content": "请联网核对官方页面"},
            ]
        }
    )
    assert not _payload_has_search_intent(
        {
            "messages": [
                {"role": "user", "content": "解释这段本地代码"},
            ]
        }
    )
    assert not _payload_has_explicit_web_search(
        {
            "web_search": False,
            "features": {"web_search": True},
        }
    )


def test_claude_search_toggle_controls_tool_without_prompt_keywords() -> None:
    enabled = ChatCompletionRequest(
        model="claude-web",
        messages=[{"role": "user", "content": "这个消息需要最新信息吗？"}],
        web_search=True,
        turtle_claude_model="claude-sonnet-5",
        turtle_claude_thinking="standard",
    )
    disabled = ChatCompletionRequest(
        model="claude-web",
        messages=[{"role": "user", "content": "请联网搜索官方信息"}],
        web_search=False,
        turtle_claude_model="claude-sonnet-5",
        turtle_claude_thinking="standard",
    )

    enabled_payload, _ = _claude_request_payload(enabled, settings())
    disabled_payload, _ = _claude_request_payload(disabled, settings())

    assert enabled_payload["web_search"] is True
    assert disabled_payload["web_search"] is False


def test_request_payload_enables_native_web_search_for_search_intent() -> None:
    request = ChatCompletionRequest(
        model="gpt-5-web",
        messages=[{"role": "user", "content": "请联网查看官方页面并回答"}],
        stream=True,
    )

    payload = _request_payload(request, settings())

    assert payload["web_search"] is True
    assert payload["model"] == "auto"


def test_upstream_stream_smoothing_preserves_finish_event_and_done_marker() -> None:
    observed = {}
    content_event = {
        "id": "chatcmpl-upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5-6-thinking",
        "choices": [{"index": 0, "delta": {"content": "abcdefghijkl"}, "finish_reason": None}],
    }
    finish_event = {
        **content_event,
        "provider": "OpenaiAccount",
        "conversation": {
            "conversation_id": "private-upstream-conversation",
            "user_id": "private-upstream-device",
        },
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        body = "".join(
            [
                f"data: {json.dumps(content_event)}\n\n",
                f"data: {json.dumps(finish_event)}\n\n",
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    smooth_settings = settings(
        backend="upstream",
        stream_chunk_chars=4,
        stream_chunk_delay_ms=0,
    )
    with TestClient(
        create_app(smooth_settings, upstream_transport=httpx.MockTransport(handler))
    ) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "turtle_chat_id": "chat-stream-opaque",
            },
        ) as response:
            lines = [line for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[-1] == "data: [DONE]"
    events = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    content_events = [item for item in events if item["choices"][0]["delta"].get("content")]
    assert [item["choices"][0]["delta"]["content"] for item in content_events] == ["abcd", "efgh", "ijkl"]
    assert "".join(item["choices"][0]["delta"]["content"] for item in content_events) == "abcdefghijkl"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert observed["conversation_id"] == _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-default", "chat-stream-opaque"
    )
    assert all("conversation" not in item for item in events)
    assert all("conversation_id" not in item for item in events)
    assert all("provider" not in item for item in events)


def test_upstream_done_releases_lease_and_tracking_before_trailing_wait() -> None:
    tracked: list[dict[str, object]] = []
    stream_state = {"closed": False}
    finish_event = {
        "id": "chatcmpl-upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5-5-instant",
        "provider": "OpenaiAccount",
        "conversation": {
            "conversation_id": "private-upstream-conversation",
        },
        "choices": [
            {
                "index": 0,
                "delta": {"content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }

    class DoneThenDelayedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                f"data: {json.dumps(finish_event)}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            await asyncio.sleep(1)
            yield b'data: {"should_not":"escape"}\n\n'

        async def aclose(self) -> None:
            stream_state["closed"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=DoneThenDelayedStream(),
        )

    upstream_settings = settings(
        backend="upstream",
        stream_chunk_delay_ms=0,
    )
    with TestClient(
        create_app(
            upstream_settings,
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:

        async def record(**kwargs):
            tracked.append(kwargs)
            return 1

        client.app.state.upstream_cleanup.record = record
        started = time.monotonic()
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "turtle_chat_id": "chat-done-release",
                "turtle_request_id": "streamdoneearly1",
            },
        ) as response:
            lines = [line for line in response.iter_lines() if line]
        elapsed = time.monotonic() - started
        lease = client.app.state.account_pool.store.leases["streamdoneearly1"]

    assert response.status_code == 200
    assert lines[-1] == "data: [DONE]"
    assert all("should_not" not in line for line in lines)
    assert elapsed < 0.7
    assert stream_state["closed"] is True
    assert lease["state"] == "completed"
    assert lease["outcome"] == "success"
    assert tracked[-1]["metadata"].conversation_id == "private-upstream-conversation"


def test_full_message_history_supports_multi_turn() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {"role": "user", "content": "请记住我的名字是海棠"},
                    {"role": "assistant", "content": "好的"},
                    {"role": "user", "content": "我的名字是什么？"},
                ],
            },
        )
    assert response.json()["choices"][0]["message"]["content"] == "海棠"


def test_unknown_model_is_rejected() -> None:
    with TestClient(create_app(settings())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={"model": "not-real", "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 404


def test_upstream_conversation_key_is_opaque_and_stable_across_account_migration() -> None:
    first_account = _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-default", "chat-opaque"
    )
    migrated_account = _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-default", "chat-opaque"
    )
    another_chat = _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-default", "chat-other"
    )
    another_pool = _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-secondary", "chat-opaque"
    )

    assert first_account == migrated_account
    assert first_account != another_chat
    assert first_account != another_pool
    assert first_account.startswith("turtle-v1-")
    assert len(first_account) == len("turtle-v1-") + 64
    assert "chat-opaque" not in first_account
    assert "gpt-default" not in first_account


def test_upstream_request_maps_model_and_response_maps_back() -> None:
    observed = {}
    tracked: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "provider": "OpenaiAccount",
                "conversation": {
                    "conversation_id": "private-upstream-conversation",
                    "user_id": "private-upstream-device",
                    "turtle_input_file_ids": ["file_12345678"],
                    "turtle_generated_asset_ids": ["asset_12345678"],
                },
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    upstream_settings = settings(backend="upstream")
    with TestClient(create_app(upstream_settings, upstream_transport=transport)) as client:
        async def record(**kwargs):
            tracked.append(kwargs)
            return 3

        client.app.state.upstream_cleanup.record = record
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_account_pool_id": "gpt-default",
                "turtle_user_id": "user-opaque",
                "turtle_chat_id": "chat-opaque",
                "turtle_request_id": "request-opaque",
                "conversation_id": "caller-controlled-collision",
            },
        )
    assert observed["model"] == "auto"
    assert not any(key.startswith("turtle_") for key in observed)
    assert observed["conversation_id"] == _derive_upstream_conversation_key(
        "gateway-test-key", "gpt-default", "chat-opaque"
    )
    response_payload = response.json()
    assert response_payload["model"] == "gpt-5-web"
    assert "conversation" not in response_payload
    assert "conversation_id" not in response_payload
    assert "provider" not in response_payload
    assert len(tracked) == 2
    assert tracked[0]["metadata"].conversation_cache_key == observed[
        "conversation_id"
    ]
    assert tracked[1]["account_id"] == "legacy-primary"
    assert tracked[1]["user_id"] == "user-opaque"
    assert tracked[1]["chat_id"] == "chat-opaque"
    metadata = tracked[1]["metadata"]
    assert metadata.conversation_id == "private-upstream-conversation"
    assert metadata.input_file_ids == ("file_12345678",)
    assert metadata.generated_asset_ids == ("asset_12345678",)


def test_caller_conversation_id_is_dropped_without_a_turtle_chat() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "choices": [],
            },
        )

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "background task"}],
                "conversation_id": "caller-controlled-collision",
            },
        )

    assert response.status_code == 200
    assert "conversation_id" not in observed


def test_account_pool_does_not_retry_a_rate_limited_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-rate-limit",
            },
        )
        snapshot = client.get("/internal/account-pools", headers=headers()).json()

    assert response.status_code == 429
    assert calls == 1
    account = next(item for item in snapshot["accounts"] if item["id"] == "legacy-primary")
    assert account["active"] == 0
    assert account["status"] == "ready"
    lane = next(
        item
        for item in account["quota"]["lanes"]
        if item["selection_key"] == "gpt-5-5:instant"
    )
    assert lane["state"] == "cooldown"
    assert lane["blocked_until"] is not None


def test_account_pool_retries_rate_limit_on_a_distinct_account() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "upstream.test":
            return httpx.Response(
                429,
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-backup",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "backup answer",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-rate-failover",
                "turtle_user_id": "user-a",
                "turtle_chat_id": "chat-rate-failover",
            },
        )
        store = client.app.state.account_pool.store
        affinity = store.affinity[("gpt-default", "chat-rate-failover")]

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "backup answer"
    assert hosts == ["upstream.test", "backup.test"]
    assert affinity["preferred_account_id"] == "backup-account"
    assert affinity["migration_count"] == 1
    assert affinity["last_migration_reason"] == "failover_rate_limit"


def test_stream_empty_result_fails_over_before_any_client_content() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "upstream.test":
            role_only = {
                "id": "chatcmpl-empty",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "auto",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(
                    f"data: {json.dumps(role_only)}\n\n"
                    "data: [DONE]\n\n"
                ),
            )
        answer = {
            "id": "chatcmpl-backup",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "auto",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "backup stream answer"},
                    "finish_reason": "stop",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=f"data: {json.dumps(answer)}\n\ndata: [DONE]\n\n",
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "turtle_request_id": "request-empty-stream-failover",
                "turtle_user_id": "user-a",
                "turtle_chat_id": "chat-empty-stream-failover",
            },
        ) as response:
            lines = [line for line in response.iter_lines() if line]
        store = client.app.state.account_pool.store
        affinity = store.affinity[
            ("gpt-default", "chat-empty-stream-failover")
        ]
        primary_lease = store.leases[
            "request-empty-stream-failover"
        ]

    assert response.status_code == 200
    assert hosts == ["upstream.test", "backup.test"]
    assert lines[-1] == "data: [DONE]"
    events = [
        json.loads(line.removeprefix("data: "))
        for line in lines[:-1]
    ]
    assert "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in events
    ) == "backup stream answer"
    assert primary_lease["outcome"] == "error"
    assert primary_lease["error_class"] == "upstream_empty_stream"
    assert affinity["preferred_account_id"] == "backup-account"
    assert affinity["last_migration_reason"] == "failover_empty_stream"


def test_account_pool_retries_explicit_limit_wrapped_as_upstream_500() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "upstream.test":
            return httpx.Response(
                500,
                json={
                    "error": {
                        "message": (
                            "RuntimeError: You've hit your limit. "
                            "Please try again later."
                        )
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-limit-backup",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "backup after explicit limit",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-wrapped-limit-failover",
                "turtle_user_id": "user-a",
                "turtle_chat_id": "chat-wrapped-limit-failover",
            },
        )
        snapshot = client.get("/internal/account-pools", headers=headers()).json()

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "backup after explicit limit"
    )
    assert hosts == ["upstream.test", "backup.test"]
    primary = next(
        item for item in snapshot["accounts"] if item["id"] == "legacy-primary"
    )
    lane = next(
        item
        for item in primary["quota"]["lanes"]
        if item["selection_key"] == "gpt-5-5:instant"
    )
    assert primary["status"] == "ready"
    assert lane["state"] == "cooldown"


def test_account_pool_does_not_retry_an_ambiguous_upstream_500() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        return httpx.Response(
            500,
            json={"error": {"message": "ambiguous internal failure"}},
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-ambiguous-upstream-500",
            },
        )

    assert response.status_code == 502
    assert hosts == ["upstream.test"]


def test_account_pool_does_not_replay_an_ambiguous_transport_failure() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "upstream.test":
            raise httpx.ConnectError("connection lost", request=request)
        return httpx.Response(200, json={"choices": []})

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-transport-failure",
            },
        )

    assert response.status_code == 502
    assert hosts == ["upstream.test"]


def test_auth_failure_excludes_account_without_automatic_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "login required"}})

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-auth-failure",
            },
        )
        snapshot = client.get("/internal/account-pools", headers=headers()).json()

    assert response.status_code == 401
    assert calls == 1
    account = next(item for item in snapshot["accounts"] if item["id"] == "legacy-primary")
    assert account["active"] == 0
    assert account["status"] == "reauth_required"
    assert account["session_state"] == "expired"


def test_account_pool_retries_auth_failure_on_a_distinct_account() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "upstream.test":
            return httpx.Response(
                401,
                json={"error": {"message": "login required"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-backup",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "choices": [],
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-auth-failover",
            },
        )
        snapshot = client.get(
            "/internal/account-pools",
            headers=headers(),
        ).json()

    assert response.status_code == 200
    assert hosts == ["upstream.test", "backup.test"]
    primary = next(
        item
        for item in snapshot["accounts"]
        if item["id"] == "legacy-primary"
    )
    assert primary["status"] == "reauth_required"
    assert primary["session_state"] == "expired"


def test_client_creation_failure_releases_account_lease() -> None:
    async def broken_client(_account):
        raise RuntimeError("unavailable")

    with TestClient(create_app(settings(backend="upstream"))) as client:
        client.app.state.account_pool.client_for = broken_client
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-client-fail",
            },
        )
        snapshot = client.get("/internal/account-pools", headers=headers()).json()

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "无法建立上游连接"
    account = next(item for item in snapshot["accounts"] if item["id"] == "legacy-primary")
    assert account["active"] == 0


def test_client_creation_failure_fails_over_before_sending_request() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-backup",
                "object": "chat.completion",
                "created": 1,
                "model": "auto",
                "choices": [],
            },
        )

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        add_memory_backup_account(client)
        original_client_for = client.app.state.account_pool.client_for

        async def selective_client(account):
            if account.id == "legacy-primary":
                raise RuntimeError("worker unavailable")
            return await original_client_for(account)

        client.app.state.account_pool.client_for = selective_client
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_request_id": "request-client-failover",
            },
        )

    assert response.status_code == 200
    assert hosts == ["backup.test"]


def test_account_pool_admin_flow_keeps_new_account_disabled_until_enabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"object": "list", "data": []})

    pool_settings = settings(
        backend="upstream",
        account_allowed_hosts=("127.0.0.1", "localhost"),
    )
    with TestClient(
        create_app(pool_settings, upstream_transport=httpx.MockTransport(handler))
    ) as client:
        pool_response = client.post(
            "/internal/account-pools",
            headers=headers(),
            json={"name": "备用池", "description": "隔离测试账号"},
        )
        assert pool_response.status_code == 200
        pool_id = pool_response.json()["id"]

        account_response = client.post(
            f"/internal/account-pools/{pool_id}/accounts",
            headers=headers(),
            json={
                "name": "备用 A",
                "worker_endpoint": "http://127.0.0.1:8321/v1",
                "health_path": "/healthz",
                "max_concurrency": 1,
                "priority": 0,
            },
        )
        assert account_response.status_code == 200
        account = account_response.json()
        assert account["enabled"] is False

        probe = client.post(
            f"/internal/accounts/{account['id']}/probe",
            headers=headers(),
        )
        assert probe.status_code == 200
        assert probe.json()["ok"] is True

        probed_snapshot = client.get("/internal/account-pools", headers=headers()).json()
        probed = next(item for item in probed_snapshot["accounts"] if item["id"] == account["id"])
        assert probed["enabled"] is False
        assert probed["status"] == "disabled"
        assert probed["session_state"] == "valid"

        enabled = client.put(
            f"/internal/accounts/{account['id']}",
            headers=headers(),
            json={
                "name": account["name"],
                "worker_endpoint": account["worker_endpoint"],
                "health_path": account["health_path"],
                "max_concurrency": account["max_concurrency"],
                "priority": account["priority"],
                "quota_profile": "free",
                "enabled": True,
            },
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "ready"
        assert enabled.json()["quota_profile"] == "free"

        tuned = client.put(
            f"/internal/accounts/{account['id']}/settings",
            headers=headers(),
            json={
                "name": account["name"],
                "enabled": True,
                "quota_profile": "free",
                "max_concurrency": 3,
            },
        )
        assert tuned.status_code == 200
        assert tuned.json()["max_concurrency"] == 3

        final_snapshot = client.get("/internal/account-pools", headers=headers()).json()
        ready = next(item for item in final_snapshot["accounts"] if item["id"] == account["id"])
        assert ready["available"] is True
        assert ready["quota_profile"] == "free"
        assert ready["max_concurrency"] == 3
        free_instant = next(
            item
            for item in ready["quota"]["lanes"]
            if item["selection_key"] == "gpt-5-5:instant"
        )
        assert free_instant["dispatch_budget_count"] is None
        assert free_instant["published_window_seconds"] == 5 * 60 * 60
        assert free_instant["state"] == "dynamic"

        capacity = client.get(
            f"/internal/account-pools/{pool_id}/capacity",
            headers=headers(),
        )
        assert capacity.status_code == 200
    assert capacity.json()["admission_capacity"] == 3


def test_account_reauth_opens_isolated_login_and_recovers_only_after_restart_probe() -> None:
    upstream_actions: list[str] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/OpenaiAccount/auth/capture":
            upstream_actions.append("capture")
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/healthz":
            upstream_actions.append("probe")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "private-upstream-id",
                    "name": "ChatGPT 主账号",
                },
            )
        return httpx.Response(200, json={"object": "list", "data": []})

    def control_handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "account_id": "legacy-primary",
                    "configured": True,
                    "browser_state": "ready",
                },
            )
        if action == "open":
            return httpx.Response(
                200,
                json={
                    "account_id": "legacy-primary",
                    "configured": True,
                    "browser_state": "ready",
                    "last_action": "login_opened",
                },
            )
        if action == "restart":
            return httpx.Response(
                200,
                json={
                    "account_id": "legacy-primary",
                    "configured": True,
                    "browser_state": "stopped",
                    "last_action": "worker_restarted",
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    with tempfile.TemporaryDirectory() as temp:
        secret = Path(temp) / "login-control-secret"
        secret.write_text("b" * 64, encoding="utf-8")
        os.chmod(secret, 0o600)
        reauth_settings = settings(
            backend="upstream",
            upstream_health_path="/healthz",
            login_control_url="http://127.0.0.1:8340",
            login_control_secret_file=str(secret),
        )
        with TestClient(
            create_app(
                reauth_settings,
                upstream_transport=httpx.MockTransport(upstream_handler),
                login_control_transport=httpx.MockTransport(control_handler),
            )
        ) as client:
            started = client.post(
                "/internal/accounts/legacy-primary/reauth/start",
                headers=headers(),
            )
            assert started.status_code == 200
            assert started.json()["state"] == "waiting_for_login"

            waiting = client.get("/internal/account-pools", headers=headers()).json()
            account = waiting["accounts"][0]
            assert account["status"] == "reauth_required"
            assert account["available"] is False
            assert account["login_runtime"]["configured"] is True

            ordinary_probe = client.post(
                "/internal/accounts/legacy-primary/probe",
                headers=headers(),
            )
            assert ordinary_probe.status_code == 200
            still_waiting = client.get("/internal/account-pools", headers=headers()).json()
            assert still_waiting["accounts"][0]["status"] == "reauth_required"

            upstream_actions.clear()
            verified = client.post(
                "/internal/accounts/legacy-primary/reauth/verify",
                headers=headers(),
            )
            assert verified.status_code == 200
            assert upstream_actions == ["capture", "probe", "probe"]
            assert verified.json()["state"] == "ready"
            assert verified.json()["upstream_display_name"] == "ChatGPT 主账号"
            assert "private-upstream-id" not in verified.text
            ready = client.get("/internal/account-pools", headers=headers()).json()
            assert ready["accounts"][0]["available"] is True
            assert ready["accounts"][0]["upstream_display_name"] == "ChatGPT 主账号"


def test_claude_reauth_switches_manual_browser_to_cdp_only_when_verifying() -> None:
    upstream_actions: list[str] = []
    control_actions: list[str] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ClaudeWeb/auth/capture":
            upstream_actions.append("capture")
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/healthz":
            upstream_actions.append("probe")
            return httpx.Response(
                200,
                json={"ok": True, "name": "Claude 主账号"},
            )
        return httpx.Response(404)

    def control_handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        control_actions.append(action)
        base = {
            "account_id": "legacy-claude-primary",
            "configured": True,
            "provider": "claude",
            "login_mode": "remote_browser",
        }
        if action == "status":
            return httpx.Response(200, json={**base, "browser_state": "manual"})
        if action == "open":
            return httpx.Response(
                200,
                json={
                    **base,
                    "browser_state": "manual",
                    "login_session_url": (
                        "https://chat.example.test/__turtle_login/connect#token="
                        + "t" * 64
                    ),
                    "login_session_expires_at": int(time.time()) + 600,
                },
            )
        if action == "capture":
            return httpx.Response(200, json={**base, "browser_state": "ready"})
        if action == "restart":
            return httpx.Response(200, json={**base, "browser_state": "stopped"})
        return httpx.Response(404, json={"detail": "missing"})

    with tempfile.TemporaryDirectory() as temp:
        secret = Path(temp) / "login-control-secret"
        secret.write_text("b" * 64, encoding="utf-8")
        os.chmod(secret, 0o600)
        reauth_settings = settings(
            backend="upstream",
            claude_pool_enabled=True,
            login_control_url="http://127.0.0.1:8340",
            login_control_secret_file=str(secret),
        )
        with TestClient(
            create_app(
                reauth_settings,
                upstream_transport=httpx.MockTransport(upstream_handler),
                login_control_transport=httpx.MockTransport(control_handler),
            )
        ) as client:
            started = client.post(
                "/internal/accounts/legacy-claude-primary/reauth/start",
                headers=headers(),
            )
            assert started.status_code == 200
            assert started.json()["runtime"]["browser_state"] == "manual"

            upstream_actions.clear()
            control_actions.clear()
            verified = client.post(
                "/internal/accounts/legacy-claude-primary/reauth/verify",
                headers=headers(),
            )

    assert verified.status_code == 200
    assert control_actions == ["status", "capture", "restart"]
    assert upstream_actions == ["capture", "probe", "probe"]
    assert verified.json()["state"] == "ready"
    assert verified.json()["upstream_display_name"] == "Claude 主账号"


def test_account_reauth_stops_before_probe_when_browser_auth_capture_fails() -> None:
    upstream_actions: list[str] = []
    control_actions: list[str] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/OpenaiAccount/auth/capture":
            upstream_actions.append("capture")
            return httpx.Response(401, json={"error": {"message": "capture incomplete"}})
        if request.url.path == "/healthz":
            upstream_actions.append("probe")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def control_handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        control_actions.append(action)
        if action in {"status", "open"}:
            return httpx.Response(
                200,
                json={
                    "account_id": "legacy-primary",
                    "configured": True,
                    "browser_state": "ready",
                },
            )
        if action == "restart":
            return httpx.Response(200, json={"browser_state": "stopped"})
        return httpx.Response(404, json={"detail": "missing"})

    with tempfile.TemporaryDirectory() as temp:
        secret = Path(temp) / "login-control-secret"
        secret.write_text("b" * 64, encoding="utf-8")
        os.chmod(secret, 0o600)
        reauth_settings = settings(
            backend="upstream",
            upstream_health_path="/healthz",
            login_control_url="http://127.0.0.1:8340",
            login_control_secret_file=str(secret),
        )
        with TestClient(
            create_app(
                reauth_settings,
                upstream_transport=httpx.MockTransport(upstream_handler),
                login_control_transport=httpx.MockTransport(control_handler),
            )
        ) as client:
            started = client.post(
                "/internal/accounts/legacy-primary/reauth/start",
                headers=headers(),
            )
            assert started.status_code == 200
            upstream_actions.clear()
            control_actions.clear()

            verified = client.post(
                "/internal/accounts/legacy-primary/reauth/verify",
                headers=headers(),
            )

            assert verified.status_code == 409
            assert (
                "未能从安全登录页捕获登录状态"
                in verified.json()["error"]["message"]
            )
            assert upstream_actions == ["capture"]
            assert control_actions == ["status"]
            waiting = client.get("/internal/account-pools", headers=headers()).json()
            assert waiting["accounts"][0]["status"] == "reauth_required"


def test_account_onboarding_needs_only_a_label_and_opens_isolated_login() -> None:
    control_actions: list[tuple[str, str]] = []

    def control_handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        account_id, action = parts[-2:]
        control_actions.append((account_id, action))
        base = {
            "account_id": account_id,
            "configured": True,
            "worker_port": 18361,
            "worker_state": "ready",
            "credential_state": "empty",
        }
        if action == "provision":
            return httpx.Response(200, json={**base, "browser_state": "stopped"})
        if action == "open":
            return httpx.Response(
                200,
                json={
                    **base,
                    "browser_state": "ready",
                    "last_action": "login_opened",
                },
            )
        if action == "status":
            return httpx.Response(200, json={**base, "browser_state": "ready"})
        return httpx.Response(404, json={"detail": "missing"})

    with tempfile.TemporaryDirectory() as temp:
        secret = Path(temp) / "login-control-secret"
        secret.write_text("c" * 64, encoding="utf-8")
        os.chmod(secret, 0o600)
        onboarding_settings = settings(
            backend="upstream",
            login_control_url="http://127.0.0.1:8340",
            login_control_secret_file=str(secret),
        )
        with TestClient(
            create_app(
                onboarding_settings,
                login_control_transport=httpx.MockTransport(control_handler),
            )
        ) as client:
            response = client.post(
                "/internal/account-pools/gpt-default/accounts/onboard",
                headers=headers(),
                json={"name": "备用账号 1"},
            )
            assert response.status_code == 200
            result = response.json()
            account_id = result["account_id"]
            assert account_id.startswith("acct-")
            assert result["name"] == "备用账号 1"
            assert result["state"] == "waiting_for_login"
            assert result["account_status"] == "reauth_required"

            snapshot = client.get("/internal/account-pools", headers=headers()).json()
            account = next(
                item for item in snapshot["accounts"] if item["id"] == account_id
            )
            assert account["name"] == "备用账号 1"
            assert account["enabled"] is False
            assert account["max_concurrency"] == 1
            assert account["worker_endpoint"] == (
                "http://host.docker.internal:18361/v1"
            )
            assert account["login_runtime"]["credential_state"] == "empty"

    assert control_actions[:2] == [
        (account_id, "provision"),
        (account_id, "open"),
    ]


def test_account_pool_delete_is_limited_to_empty_custom_pools() -> None:
    with TestClient(create_app(settings())) as client:
        created = client.post(
            "/internal/account-pools",
            headers=headers(),
            json={
                "provider": "gpt",
                "name": "可删除空池",
                "description": "",
                "enabled": True,
            },
        )
        assert created.status_code == 200
        pool_id = created.json()["id"]

        removed = client.delete(
            f"/internal/account-pools/{pool_id}",
            headers=headers(),
        )
        assert removed.status_code == 200
        assert removed.json()["deleted"] is True

        protected = client.delete(
            "/internal/account-pools/gpt-default",
            headers=headers(),
        )
        assert protected.status_code == 409
        assert "默认账号池" in protected.json()["error"]["message"]


def test_runtime_controls_map_to_allowlisted_upstream_route() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": "o3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_model_version": "o3",
                "turtle_thinking_level": "standard",
            },
        )

    assert response.status_code == 200
    assert observed["model"] == "o3"
    assert "reasoning_effort" not in observed
    assert "turtle_model_version" not in observed
    assert "turtle_thinking_level" not in observed


def test_current_gpt_generations_map_to_authenticated_web_capabilities() -> None:
    observed_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": observed_requests[-1]["model"],
                "choices": [],
            },
        )

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        selections = [
            ("latest", "medium"),
            ("latest", "high"),
            ("latest", "xhigh"),
            ("latest", "pro"),
            ("gpt-5-5", "instant"),
            ("gpt-5-3", "standard"),
            ("o3", "standard"),
        ]
        for version, level in selections:
            response = client.post(
                "/v1/chat/completions",
                headers=headers(),
                json={
                    "model": "gpt-5-web",
                    "messages": [{"role": "user", "content": "hello"}],
                    "turtle_model_version": version,
                    "turtle_thinking_level": level,
                },
            )
            assert response.status_code == 200

    assert [(item["model"], item.get("reasoning_effort")) for item in observed_requests] == [
        ("gpt-5-6-thinking", "medium"),
        ("gpt-5-6-thinking", "high"),
        ("gpt-5-6-thinking", "x-high"),
        ("gpt-5-6-pro", None),
        ("gpt-5-5-instant", None),
        ("gpt-5-3", None),
        ("o3", None),
    ]


def test_invalid_runtime_selection_is_rejected_before_upstream() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [{"role": "user", "content": "hello"}],
                "turtle_model_version": "gpt-4o",
                "turtle_thinking_level": "deep",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert called is False


def test_gateway_health_can_use_generic_upstream_health_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": False, "state": "login_required"})
        return httpx.Response(200, json={"object": "list", "data": []})

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_explicit_upstream_health_path_checks_auth_without_models_fallback() -> None:
    observed_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "login required"}})

    with TestClient(
        create_app(
            settings(
                backend="upstream",
                upstream_health_path="/api/OpenaiAccount/quota",
            ),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert observed_paths == ["/api/OpenaiAccount/quota"]


def test_expired_upstream_session_returns_redacted_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "access_token=do-not-leak-this expired"}},
        )

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={"model": "gpt-5-web", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 401
    serialized = response.text
    assert "do-not-leak-this" not in serialized
    assert "[REDACTED]" in serialized


def test_image_content_is_forwarded_without_reading_it() -> None:
    observed = {}
    source_token = f"{'a' * 20}.{'b' * 43}"

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "x", "object": "chat.completion", "model": "upstream", "choices": []},
        )

    with TestClient(
        create_app(settings(backend="upstream"), upstream_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://cos.example/image.png?signature=test",
                                    "turtle_source": source_token,
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert observed["messages"][0]["content"][1]["type"] == "image_url"
    assert (
        observed["messages"][0]["content"][1]["image_url"]["turtle_source"]
        == source_token
    )


def test_inline_image_is_rejected_in_strict_media_mode() -> None:
    with TestClient(create_app(settings(backend="upstream"))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 400
    assert "managed HTTPS URL" in response.json()["error"]["message"]


def test_malformed_managed_source_token_is_rejected_before_upstream() -> None:
    with TestClient(create_app(settings(backend="upstream"))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "gpt-5-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://files.chat.totools.cn/turtle-gpt/files/users/u/a.png",
                                    "turtle_source": "tampered",
                                },
                            }
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 400
    assert "source token is invalid" in response.json()["error"]["message"]


def test_project_key_rejects_an_unmanaged_image_before_upstream() -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(500)

    with TestClient(
        create_app(
            settings(backend="upstream"),
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        client.put(
            "/internal/project-api/permissions/user-a",
            headers=headers(),
            json={"enabled": True, "updated_by": "admin-1"},
        )
        grant_project_credit(client, "user-a")
        created = client.post(
            "/internal/project-api/keys",
            headers=headers(),
            json={"owner_user_id": "user-a", "name": "图片项目"},
        ).json()
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {created['api_key']}",
                "Idempotency-Key": "project-unmanaged-image-0001",
            },
            json={
                "model": "gpt-5-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://example.test/image.png"
                                },
                            },
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "托管媒体来源" in response.json()["error"]["message"]
    assert upstream_calls == 0


def test_secret_redaction() -> None:
    value = redact(
        "Authorization: Bearer abcdefghijklmnop access_token=secret-value cookie=session=secret"
    )
    assert "abcdefghijklmnop" not in value
    assert "secret-value" not in value
    assert "session=secret" not in value


def test_redaction_filter_preserves_numeric_logging_arguments() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "port=%d", (8000,), None)
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "port=8000"


def test_claude_requests_are_routed_through_the_claude_account_pool() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            observed["host"] = request.url.host
            observed["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "claude-completion",
                    "object": "chat.completion",
                    "model": "claude-web",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Claude pool ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True, "status": "ready"})
        return httpx.Response(404)

    claude_settings = settings(
        backend="upstream",
        claude_pool_enabled=True,
        claude_upstream_base_url="http://claude.test/v1",
        claude_upstream_health_path="/healthz",
    )
    with TestClient(
        create_app(
            claude_settings,
            upstream_transport=httpx.MockTransport(handler),
        )
    ) as client:
        models = client.get("/v1/models", headers=headers())
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "claude-web",
                "messages": [{"role": "user", "content": "请联网搜索官方信息"}],
                "web_search": True,
                "turtle_claude_model": "claude-sonnet-5",
                "turtle_claude_thinking": "standard",
                "turtle_account_pool_id": "claude-default",
                "turtle_user_id": "user-1",
                "turtle_chat_id": "chat-1",
                "turtle_request_id": "request-claude-1",
            },
        )

    assert models.status_code == 200
    assert {item["id"] for item in models.json()["data"]} == {
        "gpt-5-web",
        "claude-web",
    }
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Claude pool ok"
    assert observed["host"] == "claude.test"
    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["turtle_claude_model"] == "claude-sonnet-5"
    assert payload["web_search"] is True
    assert "turtle_account_pool_id" not in payload
    assert "turtle_user_id" not in payload
