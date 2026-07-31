from __future__ import annotations

import unittest

from chatgpt_web_gateway.account_pool import UpstreamAccount
from chatgpt_web_gateway.upstream import UpstreamResourceMetadata
from chatgpt_web_gateway.upstream_cleanup import (
    CleanupResource,
    UpstreamCleanupManager,
)


class FakeCleanupStore:
    def __init__(self) -> None:
        self.claimed = False
        self.completed: list[int] = []
        self.retried: list[tuple[int, str]] = []
        self.scheduled: list[tuple[str | None, str | None, str]] = []
        self.recorded: list[dict] = []

    def mark_due(self):
        return {"ttl": 1, "orphan": 0}

    def claim_due(self, _limit: int):
        self.claimed = True
        return [
            CleanupResource(
                id=7,
                account_id="account-a",
                pool_id="gpt-default",
                resource_type="conversation",
                resource_id="conversation_12345678",
                attempts=1,
            )
        ]

    def complete(self, resource_id: int):
        self.completed.append(resource_id)

    def retry(
        self,
        resource_id: int,
        *,
        error_class: str,
        attempts: int,
        delay_seconds: int | None = None,
    ):
        del attempts, delay_seconds
        self.retried.append((resource_id, error_class))

    def schedule(
        self,
        *,
        chat_id: str | None,
        user_id: str | None,
        reason: str,
    ):
        self.scheduled.append((chat_id, user_id, reason))
        return 1

    def record(self, **kwargs):
        self.recorded.append(kwargs)
        return len(kwargs["metadata"].input_file_ids)

    def status(self):
        return {"counts": [], "next_due_at": None}

    def policy(self):
        return {
            "retention_seconds": 60,
            "conversation_action": "archive",
            "updated_by": "test",
            "updated_at": 1,
        }

    def update_policy(
        self,
        *,
        retention_seconds: int,
        conversation_action: str,
        updated_by: str,
    ):
        return {
            "retention_seconds": retention_seconds,
            "conversation_action": conversation_action,
            "updated_by": updated_by,
            "updated_at": 2,
            "active_resources_rescheduled": 1,
        }


class FakeCleanupClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def cleanup_resource(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "dry_run": False, "http_status": 204}


class FakeAccountPool:
    def __init__(self, *, active: int = 0) -> None:
        self.active = active
        self.client = FakeCleanupClient()
        self.upstream_account = UpstreamAccount(
            id="account-a",
            pool_id="gpt-default",
            provider="gpt",
            name="A",
            worker_endpoint="http://worker.test/v1",
            health_path=None,
            max_concurrency=1,
        )

    async def snapshot(self):
        return {
            "accounts": [
                {
                    "id": "account-a",
                    "provider": "gpt",
                    "active": self.active,
                    "status": "ready",
                    "session_state": "valid",
                }
            ]
        }

    async def account(self, account_id: str):
        return self.upstream_account if account_id == "account-a" else None

    async def client_for(self, _account):
        return self.client


def manager(
    *,
    execute: bool,
    account_pool: FakeAccountPool | None = None,
) -> tuple[UpstreamCleanupManager, FakeCleanupStore, FakeAccountPool]:
    pool = account_pool or FakeAccountPool()
    value = UpstreamCleanupManager(
        database_url=None,
        account_pool=pool,
        enabled=False,
        execute=False,
        ttl_seconds=60,
        conversation_action="delete",
        interval_seconds=60,
        batch_size=10,
    )
    store = FakeCleanupStore()
    value.enabled = True
    value.execute = execute
    value.store = store
    return value, store, pool


class UpstreamCleanupManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_marks_candidates_without_contacting_worker(self) -> None:
        value, store, pool = manager(execute=False)

        result = await value.run_once()

        self.assertTrue(result["dry_run"])
        self.assertFalse(store.claimed)
        self.assertEqual(pool.client.calls, [])

    async def test_execute_deletes_one_exact_tracked_resource(self) -> None:
        value, store, pool = manager(execute=True)

        result = await value.run_once()

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(store.completed, [7])
        self.assertEqual(store.retried, [])
        self.assertEqual(
            pool.client.calls,
            [
                {
                    "resource_type": "conversation",
                    "resource_id": "conversation_12345678",
                    "dry_run": False,
                    "conversation_action": "archive",
                }
            ],
        )

    async def test_busy_account_is_requeued_without_cleanup_call(self) -> None:
        pool = FakeAccountPool(active=1)
        value, store, _ = manager(execute=True, account_pool=pool)

        result = await value.run_once()

        self.assertEqual(result["retried"], 1)
        self.assertEqual(
            store.retried,
            [(7, "account_not_idle")],
        )
        self.assertEqual(pool.client.calls, [])

    async def test_local_delete_schedule_is_durable(self) -> None:
        value, store, _ = manager(execute=False)

        scheduled = await value.schedule(
            chat_id="chat-a",
            user_id="user-a",
            reason="local_chat_deleted",
        )

        self.assertEqual(scheduled, 1)
        self.assertEqual(
            store.scheduled,
            [("chat-a", "user-a", "local_chat_deleted")],
        )

    async def test_policy_update_is_exposed_and_persisted_by_store(self) -> None:
        value, _store, _ = manager(execute=False)

        result = await value.update_policy(
            retention_seconds=86_400,
            conversation_action="delete",
            updated_by="admin-a",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["policy"]["retention_seconds"], 86_400)
        self.assertEqual(result["policy"]["conversation_action"], "delete")

    async def test_record_forwards_a_shorter_stage_cleanup_ttl(self) -> None:
        value, store, _ = manager(execute=False)

        recorded = await value.record(
            account_id="account-a",
            pool_id="gpt-default",
            user_id="user-a",
            chat_id="image-stage:session-a",
            metadata=UpstreamResourceMetadata(
                input_file_ids=("file_12345678",),
            ),
            ttl_seconds=3_600,
        )

        self.assertEqual(recorded, 1)
        self.assertEqual(store.recorded[0]["ttl_seconds"], 3_600)
