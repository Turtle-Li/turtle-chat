"""Focused policy and quota tests runnable inside the Open WebUI image."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.engine import make_url

from .announcement import (
    ANNOUNCEMENT_BODY_MAX,
    render_announcement_markdown,
)
from .concurrency import (
    CHAT_CONCURRENCY,
    ChatConcurrencyCoordinator,
    ChatConcurrencyError,
    ChatQueueTimeout,
)
from .account_pool import AccountPoolAdmission
from .metering import (
    prepare_chat_request,
    response_has_effective_content,
    tracked_chat_stream,
)
from .history import (
    TurtleChatHistoryMessage,
    _chat_shell,
    _normalized_messages,
    get_chat_envelope,
    history_index_contract,
    initial_chat_page,
    older_chat_range,
    sync_indexed_chat_message,
)
from .provider import meta_with_provider, provider_for_chat, provider_for_model
from .subscription_cache import SubscriptionCache
from .store import (
    ChatAnnouncementConflict,
    ChatAnnouncementNotFound,
    ChatModelQuotaError,
    ChatPolicyError,
    ChatSubscriptionError,
    ChatStore,
    SELECTIONS,
    chat_plan_presets,
)
from ..turtle_database import (
    connect_postgres,
    dispose_postgres_engine,
    normalized_postgres_url,
    quote_identifier,
    runtime_database_url,
)


class ProviderIdentityTests(unittest.TestCase):
    def test_published_models_map_to_separate_providers(self):
        self.assertEqual(provider_for_model("gpt-5-web"), "gpt")
        self.assertEqual(provider_for_model("claude-web"), "claude")
        self.assertIsNone(provider_for_model("unknown"))

    def test_legacy_chats_default_to_gpt(self):
        self.assertEqual(provider_for_chat({"title": "legacy"}, {}), "gpt")

    def test_provider_is_inferred_once_and_remains_immutable(self):
        claude_meta = meta_with_provider({}, {"models": ["claude-web"]})
        self.assertEqual(claude_meta["turtle_provider"], "claude")
        unchanged = meta_with_provider(claude_meta, {"models": ["gpt-5-web"]})
        self.assertEqual(unchanged["turtle_provider"], "claude")


class IndexedHistoryContractTests(unittest.TestCase):
    def test_linear_history_gets_stable_depths_and_complete_payloads(self):
        messages = {}
        parent_id = None
        for index in range(20):
            message_id = f"message-{index:02d}"
            messages[message_id] = {
                "id": message_id,
                "parentId": parent_id,
                "childrenIds": ["stale-value"],
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"content-{index}",
                "customPayload": {"index": index},
                "timestamp": 1_700_000_000 + index,
            }
            parent_id = message_id

        normalized, depths, current_id = _normalized_messages(
            {
                "history": {
                    "messages": messages,
                    "currentId": "message-19",
                }
            }
        )
        self.assertEqual(current_id, "message-19")
        self.assertEqual(depths["message-00"], 0)
        self.assertEqual(depths["message-19"], 19)
        self.assertEqual(
            normalized["message-07"]["customPayload"],
            {"index": 7},
        )
        self.assertEqual(
            normalized["message-07"]["childrenIds"],
            ["message-08"],
        )

    def test_branch_siblings_share_depth_and_shell_drops_full_history(self):
        chat = {
            "title": "branch",
            "history": {
                "currentId": "assistant-b",
                "messages": {
                    "root": {
                        "id": "root",
                        "parentId": None,
                        "role": "user",
                        "content": "root",
                    },
                    "assistant-a": {
                        "id": "assistant-a",
                        "parentId": "root",
                        "role": "assistant",
                        "content": "a",
                    },
                    "assistant-b": {
                        "id": "assistant-b",
                        "parentId": "root",
                        "role": "assistant",
                        "content": "b",
                    },
                },
            },
            "messages": [{"id": "legacy-duplicate"}],
        }
        normalized, depths, current_id = _normalized_messages(chat)
        self.assertEqual(current_id, "assistant-b")
        self.assertEqual(depths["assistant-a"], depths["assistant-b"])
        self.assertEqual(
            normalized["root"]["childrenIds"],
            ["assistant-a", "assistant-b"],
        )

        shell = _chat_shell(chat, current_message_id=current_id)
        self.assertNotIn("messages", shell)
        self.assertNotIn("messages", shell["history"])
        self.assertEqual(shell["history"]["currentId"], "assistant-b")

    def test_range_contract_uses_composite_index_without_row_truncation(self):
        contract = history_index_contract()
        self.assertEqual(
            contract["range_index"],
            ["chat_id", "depth", "message_id"],
        )
        self.assertFalse(contract["uses_offset"])
        self.assertFalse(contract["uses_row_limit"])
        self.assertEqual(contract["initial_depth_span"], 16)
        self.assertEqual(contract["page_depth_span"], 8)
        range_index = next(
            index
            for index in TurtleChatHistoryMessage.__table__.indexes
            if index.name == "turtle_chat_history_range_idx"
        )
        self.assertEqual(
            [column.name for column in range_index.columns],
            ["chat_id", "depth", "message_id"],
        )


class IndexedHistoryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_chat_is_read_in_indexed_depth_ranges(self):
        from open_webui.internal.db import Base
        from open_webui.models.chats import Chat
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from . import history as history_module

        messages = {}
        parent_id = None
        for index in range(35):
            message_id = f"message-{index:02d}"
            messages[message_id] = {
                "id": message_id,
                "parentId": parent_id,
                "childrenIds": [],
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"content-{index}",
                "timestamp": 1_700_000_000 + index,
                "arbitraryUiPayload": {"index": index},
            }
            if parent_id:
                messages[parent_id]["childrenIds"] = [message_id]
            parent_id = message_id

        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "history.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda sync_connection: Base.metadata.create_all(
                        sync_connection,
                        tables=[Chat.__table__],
                    )
                )

            history_module._SCHEMA_READY_DATABASES.clear()
            async with session_factory() as db:
                db.add(
                    Chat(
                        id="long-chat",
                        user_id="user-a",
                        title="Long chat",
                        chat={
                            "title": "Long chat",
                            "history": {
                                "currentId": "message-34",
                                "messages": messages,
                            },
                        },
                        created_at=1_700_000_000,
                        updated_at=1_700_000_100,
                        current_message_id="message-34",
                        meta={},
                        variables={},
                    )
                )
                await db.commit()

                envelope = await get_chat_envelope("long-chat", db=db)
                initial = await initial_chat_page(envelope, db=db)
                self.assertEqual(
                    list(initial["chat"]["history"]["messages"]),
                    [f"message-{index:02d}" for index in range(19, 35)],
                )
                self.assertEqual(
                    initial["chat"]["history"]["turtlePage"],
                    {
                        "rangeStart": 19,
                        "rangeEnd": 35,
                        "hasMore": True,
                        "span": 16,
                        "revision": 1_700_000_100,
                        "messageCount": 35,
                    },
                )

                middle = await older_chat_range(
                    envelope,
                    before_depth=19,
                    db=db,
                )
                self.assertEqual(
                    list(middle["messages"]),
                    [f"message-{index:02d}" for index in range(11, 19)],
                )
                oldest = await older_chat_range(
                    envelope,
                    before_depth=11,
                    db=db,
                )
                self.assertEqual(
                    list(oldest["messages"]),
                    [f"message-{index:02d}" for index in range(3, 11)],
                )
                self.assertTrue(oldest["page"]["hasMore"])
                final_page = await older_chat_range(
                    envelope,
                    before_depth=3,
                    db=db,
                )
                self.assertEqual(
                    list(final_page["messages"]),
                    [f"message-{index:02d}" for index in range(3)],
                )
                self.assertFalse(final_page["page"]["hasMore"])
                self.assertEqual(
                    oldest["messages"]["message-03"]["arbitraryUiPayload"],
                    {"index": 3},
                )

                updated_message = {
                    **messages["message-34"],
                    "content": "streamed final content",
                    "arbitraryUiPayload": {"index": 34, "final": True},
                }
                self.assertTrue(
                    await sync_indexed_chat_message(
                        "long-chat",
                        "user-a",
                        "message-34",
                        updated_message,
                        1_700_000_101,
                        "message-34",
                        35,
                        db=db,
                    )
                )
                refreshed_envelope = {
                    **envelope,
                    "updated_at": 1_700_000_101,
                }
                refreshed = await initial_chat_page(refreshed_envelope, db=db)
                self.assertEqual(
                    refreshed["chat"]["history"]["messages"]["message-34"]["content"],
                    "streamed final content",
                )

                new_message = {
                    "id": "message-35",
                    "parentId": "message-34",
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "new incremental row",
                    "timestamp": 1_700_000_035,
                }
                self.assertTrue(
                    await sync_indexed_chat_message(
                        "long-chat",
                        "user-a",
                        "message-35",
                        new_message,
                        1_700_000_102,
                        "message-35",
                        36,
                        db=db,
                    )
                )
                newest = await initial_chat_page(
                    {
                        **envelope,
                        "updated_at": 1_700_000_102,
                        "current_message_id": "message-35",
                    },
                    db=db,
                )
                self.assertEqual(
                    newest["chat"]["history"]["turtlePage"]["rangeStart"],
                    20,
                )
                self.assertIn(
                    "message-35",
                    newest["chat"]["history"]["messages"],
                )

                plan = (
                    await db.execute(
                        text(
                            "EXPLAIN QUERY PLAN "
                            "SELECT message_id FROM turtle_chat_history_message "
                            "WHERE chat_id = :chat_id "
                            "AND depth >= :range_start AND depth < :range_end "
                            "ORDER BY depth, message_id"
                        ),
                        {
                            "chat_id": "long-chat",
                            "range_start": 7,
                            "range_end": 15,
                        },
                    )
                ).all()
                self.assertIn(
                    "turtle_chat_history_range_idx",
                    " ".join(str(column) for row in plan for column in row),
                )
            await engine.dispose()
            history_module._SCHEMA_READY_DATABASES.clear()


@unittest.skipUnless(
    os.getenv("TURTLE_RUN_POSTGRES_TESTS") == "1",
    "PostgreSQL integration test is opt-in",
)
class IndexedHistoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_range_plan_uses_the_composite_index(self):
        from open_webui.internal.db import get_async_db
        from open_webui.models.chats import Chat
        from sqlalchemy import delete, text

        chat_id = f"history-postgres-{uuid.uuid4().hex}"
        messages = {}
        parent_id = None
        for index in range(40):
            message_id = f"message-{index:03d}"
            messages[message_id] = {
                "id": message_id,
                "parentId": parent_id,
                "childrenIds": [],
                "role": "assistant" if index % 2 else "user",
                "content": f"postgres-{index}",
                "timestamp": 1_700_100_000 + index,
            }
            if parent_id:
                messages[parent_id]["childrenIds"] = [message_id]
            parent_id = message_id

        async with get_async_db() as db:
            try:
                connection = await db.connection()
                await connection.run_sync(
                    lambda sync_connection: Chat.__table__.create(
                        sync_connection,
                        checkfirst=True,
                    )
                )
                await db.commit()
                db.add(
                    Chat(
                        id=chat_id,
                        user_id="postgres-history-user",
                        title="Postgres indexed history",
                        chat={
                            "title": "Postgres indexed history",
                            "history": {
                                "currentId": "message-039",
                                "messages": messages,
                            },
                        },
                        created_at=1_700_100_000,
                        updated_at=1_700_100_100,
                        current_message_id="message-039",
                        meta={},
                        variables={},
                    )
                )
                await db.commit()
                envelope = await get_chat_envelope(chat_id, db=db)
                page = await initial_chat_page(envelope, db=db)
                self.assertEqual(
                    len(page["chat"]["history"]["messages"]),
                    16,
                )

                index_definition = (
                    await db.execute(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE schemaname = current_schema() "
                            "AND indexname = 'turtle_chat_history_range_idx'"
                        )
                    )
                ).scalar_one()
                self.assertIn("(chat_id, depth, message_id)", index_definition)

                await db.execute(text("SET LOCAL enable_seqscan = off"))
                plan_rows = (
                    await db.execute(
                        text(
                            "EXPLAIN (COSTS OFF) "
                            "SELECT message_id, payload "
                            "FROM turtle_chat_history_message "
                            "WHERE chat_id = :chat_id "
                            "AND depth >= :range_start AND depth < :range_end "
                            "ORDER BY depth, message_id"
                        ),
                        {
                            "chat_id": chat_id,
                            "range_start": 24,
                            "range_end": 32,
                        },
                    )
                ).all()
                plan = "\n".join(str(row[0]) for row in plan_rows)
                self.assertIn("turtle_chat_history_range_idx", plan)
            finally:
                await db.rollback()
                await db.execute(delete(Chat).where(Chat.id == chat_id))
                await db.commit()


class AccountPoolAdmissionTests(unittest.TestCase):
    def test_capacity_target_is_scoped_by_pool_and_selection_lane(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_BASE_URLS": "http://gateway:8000/v1;http://claude:8330/v1",
                "OPENAI_API_KEYS": "gateway-key;claude-key",
            },
            clear=False,
        ):
            target = AccountPoolAdmission._target("pool-a", "latest:high")
        self.assertIsNotNone(target)
        url, key = target
        parsed = urlsplit(url)
        self.assertEqual(parsed.path, "/internal/account-pools/pool-a/capacity")
        self.assertEqual(parse_qs(parsed.query), {"selection_key": ["latest:high"]})
        self.assertEqual(key, "gateway-key")

    def test_invalidating_a_pool_clears_every_lane_cache(self):
        admission = AccountPoolAdmission()
        admission._cache = {
            "pool-a:latest:medium": (
                0.0,
                {"account_pool": 1, "provider": 2, "global": 3},
            ),
            "pool-a:latest:high": (
                0.0,
                {"account_pool": 1, "provider": 2, "global": 3},
            ),
            "pool-b:latest:medium": (
                0.0,
                {"account_pool": 1, "provider": 2, "global": 3},
            ),
        }
        admission.invalidate("pool-a")
        self.assertEqual(list(admission._cache), ["pool-b:latest:medium"])


class MemoryConcurrencyCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "TURTLE_CONCURRENCY_BACKEND": "memory",
                "TURTLE_CHAT_MAX_CONCURRENCY": "4",
                "TURTLE_GPT_MAX_CONCURRENCY": "4",
                "TURTLE_CLAUDE_MAX_CONCURRENCY": "4",
            },
        )
        self.environment.start()
        self.coordinator = ChatConcurrencyCoordinator()
        self.coordinator.queue_timeout_seconds = 0.5
        self.coordinator.lease_seconds = 60
        self.leases = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for lease in self.leases:
            if not lease._released:
                await lease.release("test_cleanup")
        await asyncio.sleep(0)
        self.environment.stop()

    async def acquire(
        self,
        *,
        user_id="user-a",
        group_id="group-a",
        provider="gpt",
        user_limit=4,
        group_limit=4,
        request_id=None,
        account_pool_id=None,
        account_pool_limit=None,
        provider_limit=None,
        global_limit=None,
        account_pool_limit_resolver=None,
    ):
        lease = await self.coordinator.acquire(
            request_id=request_id or str(uuid.uuid4()),
            user_id=user_id,
            group_id=group_id,
            provider=provider,
            user_limit=user_limit,
            group_limit=group_limit,
            account_pool_id=account_pool_id,
            account_pool_limit=account_pool_limit,
            provider_limit=provider_limit,
            global_limit=global_limit,
            account_pool_limit_resolver=account_pool_limit_resolver,
        )
        self.leases.append(lease)
        return lease

    async def wait_for_state(self, request_id, user_id, expected):
        for _ in range(100):
            status = await self.coordinator.status(request_id, user_id)
            if status["state"] == expected:
                return status
            await asyncio.sleep(0.01)
        self.fail(f"request {request_id} did not reach {expected}")

    def queue_acquire(self, **kwargs):
        task = asyncio.create_task(self.acquire(**kwargs))
        self.tasks.append(task)
        return task

    async def test_user_limit_queues_then_release_admits(self):
        first = await self.acquire(user_limit=1, group_limit=4)
        second_id = str(uuid.uuid4())
        second_task = self.queue_acquire(
            request_id=second_id,
            user_id="user-a",
            group_id="group-b",
            user_limit=1,
            group_limit=4,
        )

        queued = await self.wait_for_state(second_id, "user-a", "queued")
        self.assertEqual(queued["position"], 1)
        self.assertEqual(queued["concurrency"]["user"], {"active": 1, "limit": 1})
        self.assertFalse(second_task.done())

        await first.release("completed")
        second = await asyncio.wait_for(second_task, 0.5)
        self.assertEqual((await self.coordinator.status(second_id, "user-a"))["state"], "admitted")
        await second.release("completed")

    async def test_group_limit_blocks_other_users(self):
        first = await self.acquire(user_id="user-a", group_id="shared", group_limit=1)
        second_id = str(uuid.uuid4())
        second_task = self.queue_acquire(
            request_id=second_id,
            user_id="user-b",
            group_id="shared",
            group_limit=1,
        )

        queued = await self.wait_for_state(second_id, "user-b", "queued")
        self.assertEqual(queued["concurrency"]["group"], {"active": 1, "limit": 1})
        await first.release("completed")
        second = await asyncio.wait_for(second_task, 0.5)
        await second.release("completed")

    async def test_provider_and_global_limits_are_independent(self):
        os.environ["TURTLE_GPT_MAX_CONCURRENCY"] = "1"
        self.coordinator.global_limit = 2
        first = await self.acquire(user_id="gpt-a", group_id="gpt-a")
        blocked_id = str(uuid.uuid4())
        blocked_task = self.queue_acquire(
            request_id=blocked_id,
            user_id="gpt-b",
            group_id="gpt-b",
        )
        await self.wait_for_state(blocked_id, "gpt-b", "queued")

        claude = await self.acquire(
            user_id="claude-a",
            group_id="claude-a",
            provider="claude",
        )
        global_id = str(uuid.uuid4())
        global_task = self.queue_acquire(
            request_id=global_id,
            user_id="claude-b",
            group_id="claude-b",
            provider="claude",
        )
        global_status = await self.wait_for_state(global_id, "claude-b", "queued")
        self.assertEqual(global_status["concurrency"]["global"], {"active": 2, "limit": 2})

        await first.release("completed")
        admitted_gpt = await asyncio.wait_for(blocked_task, 0.5)
        await claude.release("completed")
        admitted_claude = await asyncio.wait_for(global_task, 0.5)
        await admitted_gpt.release("completed")
        await admitted_claude.release("completed")

    async def test_account_pool_limit_queues_without_blocking_another_pool(self):
        first = await self.acquire(
            user_id="pool-a-user-a",
            group_id="pool-a-group-a",
            account_pool_id="pool-a",
            account_pool_limit=1,
        )
        blocked_id = str(uuid.uuid4())
        blocked_task = self.queue_acquire(
            request_id=blocked_id,
            user_id="pool-a-user-b",
            group_id="pool-a-group-b",
            account_pool_id="pool-a",
            account_pool_limit=1,
        )
        queued = await self.wait_for_state(blocked_id, "pool-a-user-b", "queued")
        self.assertEqual(
            queued["concurrency"]["account_pool"],
            {"active": 1, "limit": 1},
        )

        other = await self.acquire(
            user_id="pool-b-user",
            group_id="pool-b-group",
            account_pool_id="pool-b",
            account_pool_limit=1,
        )
        snapshot = await self.coordinator.snapshot(
            account_pools=[
                {"id": "pool-a", "name": "A", "limit": 1},
                {"id": "pool-b", "name": "B", "limit": 1},
            ]
        )
        by_id = {item["pool_id"]: item for item in snapshot["account_pools"]}
        self.assertEqual((by_id["pool-a"]["active"], by_id["pool-a"]["queued"]), (1, 1))
        self.assertEqual((by_id["pool-b"]["active"], by_id["pool-b"]["queued"]), (1, 0))

        await first.release("completed")
        admitted = await asyncio.wait_for(blocked_task, 0.5)
        await admitted.release("completed")
        await other.release("completed")

    async def test_waiter_rechecks_account_pool_capacity_before_admission(self):
        capacity = 1

        async def resolve_capacity():
            return capacity

        first = await self.acquire(
            user_id="capacity-user-a",
            group_id="capacity-group-a",
            account_pool_id="capacity-pool",
            account_pool_limit=1,
            account_pool_limit_resolver=resolve_capacity,
        )
        waiting_id = str(uuid.uuid4())
        waiting_task = self.queue_acquire(
            request_id=waiting_id,
            user_id="capacity-user-b",
            group_id="capacity-group-b",
            account_pool_id="capacity-pool",
            account_pool_limit=1,
            account_pool_limit_resolver=resolve_capacity,
        )
        await self.wait_for_state(waiting_id, "capacity-user-b", "queued")
        capacity = 0
        await first.release("completed")
        await asyncio.sleep(0.05)
        self.assertFalse(waiting_task.done())
        status = await self.coordinator.status(waiting_id, "capacity-user-b")
        self.assertEqual(status["concurrency"]["account_pool"]["limit"], 0)
        capacity = 1
        admitted = await asyncio.wait_for(waiting_task, 0.5)
        await admitted.release("completed")

    async def test_dynamic_capacity_updates_pool_provider_and_global_limits(self):
        async def resolve_capacity():
            return {"account_pool": 3, "provider": 5, "global": 7}

        lease = await self.acquire(
            user_limit=8,
            group_limit=8,
            account_pool_id="dynamic-pool",
            account_pool_limit=1,
            provider_limit=1,
            global_limit=1,
            account_pool_limit_resolver=resolve_capacity,
        )
        self.assertEqual(lease.account_pool_limit, 3)
        self.assertEqual(lease.provider_limit, 4)
        self.assertEqual(lease.global_limit, 4)
        await lease.release("completed")

    async def test_cancelled_and_timed_out_waiters_do_not_leak_slots(self):
        first = await self.acquire(user_limit=1)
        cancelled_id = str(uuid.uuid4())
        cancelled = self.queue_acquire(request_id=cancelled_id, user_limit=1)
        await self.wait_for_state(cancelled_id, "user-a", "queued")
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertEqual(
            (await self.coordinator.status(cancelled_id, "user-a"))["state"],
            "cancelled",
        )

        self.coordinator.queue_timeout_seconds = 0.05
        timeout_id = str(uuid.uuid4())
        with self.assertRaises(ChatQueueTimeout):
            await self.acquire(request_id=timeout_id, user_limit=1)
        self.assertEqual(
            (await self.coordinator.status(timeout_id, "user-a"))["state"],
            "timed_out",
        )
        await first.release("completed")
        snapshot = await self.coordinator.snapshot(["group-a"])
        self.assertEqual(snapshot["global"]["active"], 0)
        self.assertEqual(snapshot["global"]["queued"], 0)

    async def test_duplicate_request_id_is_rejected(self):
        request_id = str(uuid.uuid4())
        first = await self.acquire(request_id=request_id)
        with self.assertRaises(ChatConcurrencyError):
            await self.acquire(request_id=request_id)
        await first.release("completed")


@unittest.skipUnless(
    os.getenv("TURTLE_TEST_REDIS") == "1",
    "set TURTLE_TEST_REDIS=1 inside the Turtle Compose network",
)
class RedisConcurrencyCoordinatorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "TURTLE_CONCURRENCY_BACKEND": "redis",
                "TURTLE_CHAT_MAX_CONCURRENCY": "2",
                "TURTLE_GPT_MAX_CONCURRENCY": "1",
                "TURTLE_CLAUDE_MAX_CONCURRENCY": "2",
                "TURTLE_REDIS_MAX_CONNECTIONS": "8",
                "TURTLE_REDIS_COMMAND_CONCURRENCY": "4",
                "TURTLE_REDIS_MAINTENANCE_CONCURRENCY": "2",
                "TURTLE_REDIS_WAKE_HEADS_PER_POOL": "4",
            },
        )
        self.environment.start()
        self.coordinator = ChatConcurrencyCoordinator()
        self.coordinator.prefix = f"turtle-test:chat-concurrency:{uuid.uuid4()}"
        self.coordinator.queue_timeout_seconds = 5
        self.coordinator.lease_seconds = 60
        self.leases = []
        try:
            await self.coordinator._redis()
        except Exception as exc:
            self.environment.stop()
            self.skipTest(f"Turtle Redis unavailable: {type(exc).__name__}")

    async def asyncTearDown(self):
        for lease in self.leases:
            if not lease._released:
                await lease.release("test_cleanup")
        redis = self.coordinator._redis_client
        if redis is not None:
            keys = []
            async for key in redis.scan_iter(match=f"{self.coordinator.prefix}:*"):
                keys.append(key)
            if keys:
                await redis.unlink(*keys)
            await self.coordinator.close()
        await asyncio.sleep(0)
        self.environment.stop()

    async def acquire(self, **overrides):
        values = {
            "request_id": str(uuid.uuid4()),
            "user_id": "user-a",
            "group_id": "group-a",
            "provider": "gpt",
            "user_limit": 1,
            "group_limit": 2,
        }
        values.update(overrides)
        lease = await self.coordinator.acquire(**values)
        self.leases.append(lease)
        return lease

    async def wait_for_queue(self, request_id, user_id):
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            status = await self.coordinator.status(request_id, user_id)
            if status["state"] == "queued":
                return status
            await asyncio.sleep(0.01)
        self.fail("Redis waiter did not enter the queue")

    async def test_redis_queue_release_timeout_and_duplicate_guard(self):
        self.coordinator.queue_timeout_seconds = 120
        first_id = str(uuid.uuid4())
        first = await self.acquire(request_id=first_id)
        with self.assertRaises(ChatConcurrencyError):
            await self.acquire(request_id=first_id)

        queued_id = str(uuid.uuid4())
        queued_task = asyncio.create_task(
            self.acquire(
                request_id=queued_id,
                user_id="user-b",
                group_id="group-b",
            )
        )
        queued = await self.wait_for_queue(queued_id, "user-b")
        self.assertEqual(queued["concurrency"]["provider"], {"active": 1, "limit": 1})
        await first.release("completed")
        second = await asyncio.wait_for(queued_task, 30)

        self.coordinator.queue_timeout_seconds = 0.05
        timeout_id = str(uuid.uuid4())
        with self.assertRaises(ChatQueueTimeout):
            await self.acquire(
                request_id=timeout_id,
                user_id="user-d",
                group_id="group-d",
            )
        self.assertEqual(
            (await self.coordinator.status(timeout_id, "user-d"))["state"],
            "timed_out",
        )
        await second.release("completed")
        snapshot = await self.coordinator.snapshot(["group-a", "group-b"])
        self.assertEqual(snapshot["global"]["active"], 0)
        self.assertEqual(snapshot["global"]["queued"], 0)

    async def test_redis_account_pool_capacity_is_a_separate_fifo_scope(self):
        os.environ["TURTLE_GPT_MAX_CONCURRENCY"] = "3"
        self.coordinator.global_limit = 3
        first = await self.acquire(
            user_id="pool-a-user-a",
            group_id="pool-a-group-a",
            user_limit=3,
            group_limit=3,
            account_pool_id="pool-a",
            account_pool_limit=1,
        )
        queued_id = str(uuid.uuid4())
        queued_task = asyncio.create_task(
            self.acquire(
                request_id=queued_id,
                user_id="pool-a-user-b",
                group_id="pool-a-group-b",
                user_limit=3,
                group_limit=3,
                account_pool_id="pool-a",
                account_pool_limit=1,
            )
        )
        queued = await self.wait_for_queue(queued_id, "pool-a-user-b")
        self.assertEqual(queued["concurrency"]["account_pool"], {"active": 1, "limit": 1})

        other = await self.acquire(
            user_id="pool-b-user",
            group_id="pool-b-group",
            user_limit=3,
            group_limit=3,
            account_pool_id="pool-b",
            account_pool_limit=1,
        )
        snapshot = await self.coordinator.snapshot(
            account_pools=[
                {"id": "pool-a", "name": "A", "limit": 1},
                {"id": "pool-b", "name": "B", "limit": 1},
            ]
        )
        pools = {item["pool_id"]: item for item in snapshot["account_pools"]}
        self.assertEqual((pools["pool-a"]["active"], pools["pool-a"]["queued"]), (1, 1))
        self.assertEqual((pools["pool-b"]["active"], pools["pool-b"]["queued"]), (1, 0))

        await first.release("completed")
        admitted = await asyncio.wait_for(queued_task, 30)
        await admitted.release("completed")
        await other.release("completed")

    async def test_redis_bounded_commands_absorb_connection_burst(self):
        self.coordinator.queue_timeout_seconds = 30
        first = await self.acquire(
            request_id=str(uuid.uuid4()),
            user_id="burst-holder",
            group_id="burst-holder",
        )

        async def run_waiter(index):
            lease = await self.acquire(
                request_id=str(uuid.uuid4()),
                user_id=f"burst-user-{index}",
                group_id=f"burst-group-{index}",
            )
            await lease.release("completed")

        waiters = [
            asyncio.create_task(run_waiter(index))
            for index in range(40)
        ]
        await asyncio.sleep(0.2)
        await first.release("completed")
        await asyncio.wait_for(asyncio.gather(*waiters), 30)

        pool = self.coordinator._redis_client.connection_pool
        self.assertEqual(type(pool).__name__, "ConnectionPool")
        self.assertEqual(pool.max_connections, 8)
        snapshot = await self.coordinator.snapshot()
        self.assertEqual(snapshot["global"]["active"], 0)
        self.assertEqual(snapshot["global"]["queued"], 0)

    async def test_redis_connection_burst_cancellation_remains_responsive(self):
        self.coordinator.queue_timeout_seconds = 60
        first = await self.acquire(
            request_id=str(uuid.uuid4()),
            user_id="cancel-holder",
            group_id="cancel-holder",
        )

        async def run_waiter(index):
            lease = await self.acquire(
                request_id=request_ids[index],
                user_id=f"cancel-user-{index}",
                group_id=f"cancel-group-{index}",
            )
            await lease.release("completed")

        request_ids = [str(uuid.uuid4()) for _index in range(12)]
        waiters = [
            asyncio.create_task(run_waiter(index))
            for index in range(12)
        ]
        queued = 0
        settle_deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < settle_deadline:
            queued = (await self.coordinator.snapshot())["global"]["queued"]
            if queued == len(waiters):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(queued, len(waiters))
        for task in waiters[:6]:
            task.cancel()
        cancelled = await asyncio.wait_for(
            asyncio.gather(*waiters[:6], return_exceptions=True),
            10,
        )
        self.assertTrue(
            all(isinstance(result, asyncio.CancelledError) for result in cancelled)
        )
        self.assertEqual(
            (
                await self.coordinator.status(
                    request_ids[0],
                    "cancel-user-0",
                )
            )["state"],
            "cancelled",
        )

        await first.release("completed")
        await asyncio.wait_for(asyncio.gather(*waiters[6:]), 10)
        snapshot = await self.coordinator.snapshot()
        self.assertEqual(snapshot["global"]["active"], 0)
        self.assertEqual(snapshot["global"]["queued"], 0)


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "chat.db"
        self.store = ChatStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def _create_group(self, **kwargs):
        return self.store.create_group(
            storage_quota_bytes=2 * 1024**3,
            max_concurrency=2,
            default_user_concurrency=1,
            **kwargs,
        )

    def test_provider_display_names_are_seeded_and_editable_without_changing_ids(self):
        self.assertEqual(
            self.store.provider_display_names(),
            {"gpt": "GPT", "claude": "Claude"},
        )
        updated = self.store.set_provider_display_name(
            "gpt",
            "团队 GPT",
            updated_by="admin",
        )
        self.assertEqual(updated["provider_family"], "gpt")
        self.assertEqual(updated["display_name"], "团队 GPT")
        self.assertEqual(
            self.store.provider_display_names(),
            {"gpt": "团队 GPT", "claude": "Claude"},
        )
        with self.assertRaises(ChatPolicyError):
            self.store.set_provider_display_name("gpt", "  ", updated_by="admin")

    def test_announcements_are_listed_and_dismissed_independently(self):
        self.assertEqual(self.store.announcements_admin(), [])

        first = self.store.create_announcement(
            title="如何订阅",
            body_markdown="## 第一步\n\n请联系管理员。",
            enabled=True,
            updated_by="admin",
        )
        second = self.store.create_announcement(
            title="使用规则",
            body_markdown="请勿共享账号。",
            enabled=True,
            updated_by="admin",
        )
        self.assertEqual(
            [item["id"] for item in self.store.announcements_admin()],
            [second["id"], first["id"]],
        )

        member_items = self.store.announcements_for_user("member-a", "user")
        self.assertEqual(len(member_items), 2)
        self.assertTrue(all(item["should_show"] for item in member_items))
        dismissed = self.store.dismiss_announcement(
            "member-a",
            first["id"],
            first["revision"],
        )
        self.assertTrue(dismissed["dismissed"])
        member_state = {
            item["id"]: item
            for item in self.store.announcements_for_user("member-a", "user")
        }
        self.assertFalse(member_state[first["id"]]["should_show"])
        self.assertTrue(member_state[second["id"]]["should_show"])
        self.assertTrue(
            all(
                item["should_show"]
                for item in self.store.announcements_for_user("member-b", "pending")
            )
        )
        self.assertFalse(
            any(
                item["should_show"]
                for item in self.store.announcements_for_user("admin", "admin")
            )
        )
        legacy_current = self.store.announcement_for_user("legacy-client", "user")
        self.assertEqual(legacy_current["id"], second["id"])
        self.store.dismiss_current_announcement("legacy-client", "user", 1)
        self.assertEqual(
            self.store.announcement_for_user("legacy-client", "user")["id"],
            first["id"],
        )

        unchanged = self.store.update_announcement(
            first["id"],
            title="如何订阅",
            body_markdown="## 第一步\n\n请联系管理员。",
            enabled=True,
            updated_by="another-admin",
        )
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["revision"], 1)

        updated = self.store.update_announcement(
            first["id"],
            title="如何订阅",
            body_markdown="## 第一步\n\n请在订阅页面联系管理员。",
            enabled=True,
            updated_by="admin",
        )
        self.assertEqual(updated["revision"], 2)
        refreshed = {
            item["id"]: item
            for item in self.store.announcements_for_user("member-a", "user")
        }
        self.assertTrue(refreshed[first["id"]]["should_show"])
        with self.assertRaises(ChatAnnouncementConflict):
            self.store.dismiss_announcement("member-a", first["id"], 1)

    def test_announcement_disable_reenable_and_soft_delete(self):
        announcement = self.store.create_announcement(
            title="通知",
            body_markdown="正文",
            enabled=True,
            updated_by="admin",
        )
        self.store.dismiss_announcement(
            "member",
            announcement["id"],
            announcement["revision"],
        )
        disabled = self.store.update_announcement(
            announcement["id"],
            title="通知",
            body_markdown="正文",
            enabled=False,
            updated_by="admin",
        )
        self.assertEqual(disabled["revision"], 2)
        self.assertEqual(self.store.announcements_for_user("member", "user"), [])
        enabled = self.store.update_announcement(
            announcement["id"],
            title="通知",
            body_markdown="正文",
            enabled=True,
            updated_by="admin",
        )
        self.assertEqual(enabled["revision"], 3)
        self.assertTrue(
            self.store.announcements_for_user("member", "user")[0]["should_show"]
        )
        with self.store._connect() as connection:
            receipts = connection.execute(
                """
                SELECT COUNT(*)
                  FROM chat_announcement_item_receipt
                 WHERE user_id = ? AND announcement_id = ?
                """,
                ("member", announcement["id"]),
            ).fetchone()[0]
        self.assertEqual(receipts, 1)
        deleted = self.store.delete_announcement(
            announcement["id"],
            deleted_by="admin",
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.store.announcements_admin(), [])
        self.assertEqual(self.store.announcements_for_user("member", "user"), [])
        with self.assertRaises(ChatAnnouncementNotFound):
            self.store.delete_announcement(
                announcement["id"],
                deleted_by="admin",
            )

    def test_legacy_singleton_announcement_is_migrated_without_loss(self):
        now = int(time.time())
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_announcement
                    (id, revision, title, body_markdown, enabled, updated_by,
                     created_at, updated_at)
                VALUES (1, 4, '旧公告', '旧正文', 1, 'legacy-admin', ?, ?)
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO chat_announcement_receipt
                    (user_id, revision, dismissed_at)
                VALUES ('legacy-user', 4, ?)
                """,
                (now,),
            )
        migrated = ChatStore(self.path)
        items = migrated.announcements_admin()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "legacy-singleton")
        self.assertEqual(items[0]["revision"], 4)
        self.assertFalse(
            migrated.announcements_for_user("legacy-user", "user")[0][
                "should_show"
            ]
        )
        self.assertTrue(
            migrated.announcements_for_user("another-user", "user")[0][
                "should_show"
            ]
        )

    def test_announcement_validation_and_markdown_safety(self):
        with self.assertRaises(ChatPolicyError):
            self.store.create_announcement(
                title="",
                body_markdown="正文",
                enabled=True,
                updated_by="admin",
            )
        with self.assertRaises(ChatPolicyError):
            self.store.create_announcement(
                title="通知",
                body_markdown="x" * (ANNOUNCEMENT_BODY_MAX + 1),
                enabled=False,
                updated_by="admin",
            )

        rendered = render_announcement_markdown(
            "<script>alert(1)</script>\n\n"
            "[危险](javascript:alert(1))\n\n"
            "![跟踪](https://example.com/pixel.gif)\n\n"
            "| 套餐 | 时长 |\n| --- | --- |\n| Basic | 30 天 |\n\n"
            "[帮助](https://example.com/help)"
        )
        self.assertNotIn("<script", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("<table>", rendered)
        self.assertIn('href="https://example.com/help"', rendered)

    def test_subscription_defaults_to_thirty_days_end_of_beijing_day(self):
        with patch(
            "open_webui.turtle_chat.store._now",
            return_value=1_753_392_345,
        ):
            subscription = self.store.subscription_for_user("member", "user")
        self.assertTrue(subscription["active"])
        self.assertEqual(subscription["status"], "active")
        self.assertEqual(subscription["starts_at"], 1_753_392_345)
        duration = subscription["expires_at"] - subscription["starts_at"]
        self.assertGreaterEqual(duration, 30 * 24 * 60 * 60)
        self.assertLess(duration, 31 * 24 * 60 * 60)
        from datetime import datetime
        from zoneinfo import ZoneInfo

        local_expiry = datetime.fromtimestamp(
            subscription["expires_at"],
            ZoneInfo("Asia/Shanghai"),
        )
        self.assertEqual(
            (local_expiry.hour, local_expiry.minute, local_expiry.second),
            (23, 59, 59),
        )
        self.assertEqual(len(self.store.subscription_events("member")), 1)
        self.assertEqual(
            self.store.subscription_events("member")[0]["action"],
            "default_provision",
        )

    def test_pending_user_is_negative_and_never_auto_activates(self):
        pending = self.store.subscription_for_user("pending", "pending")
        self.assertFalse(pending["active"])
        self.assertFalse(pending["configured"])
        self.assertEqual(pending["status"], "pending")
        with self.store._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM chat_subscription WHERE user_id = ?",
                ("pending",),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_subscription_activation_expiry_cancel_and_renew_are_audited(self):
        configured = self.store.set_subscription(
            "pending",
            "pending",
            starts_at=100,
            expires_at=200,
            updated_by="admin",
        )
        self.assertEqual(configured["status"], "pending")
        with patch("open_webui.turtle_chat.store._now", return_value=150):
            active = self.store.subscription_for_user("pending", "user")
        self.assertTrue(active["active"])
        cancelled = self.store.cancel_subscription(
            "pending",
            "user",
            updated_by="admin",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        renewed = self.store.extend_subscription(
            "pending",
            "user",
            days=30,
            updated_by="admin",
        )
        self.assertEqual(renewed["status"], "active")
        actions = [item["action"] for item in self.store.subscription_events("pending")]
        self.assertEqual(set(actions), {"activate", "cancel", "renew"})

    def test_expired_subscription_fails_closed(self):
        self.store.set_subscription(
            "expired",
            "user",
            starts_at=100,
            expires_at=200,
            updated_by="admin",
        )
        with patch("open_webui.turtle_chat.store._now", return_value=201):
            subscription = self.store.subscription_for_user("expired", "user")
            with self.assertRaises(ChatSubscriptionError) as denied:
                self.store.require_active_subscription("expired", "user")
        self.assertEqual(subscription["status"], "expired")
        self.assertEqual(denied.exception.subscription["status"], "expired")

    def test_regular_users_do_not_receive_pro_by_default(self):
        policy = self.store.policy_for_user("regular", "user")
        self.assertIn("latest:medium", policy["allowed"])
        self.assertIn("claude-sonnet-5:standard", policy["allowed"])
        self.assertIn("gpt-5-3:standard", policy["allowed"])
        self.assertIn("o3:standard", policy["allowed"])
        self.assertNotIn("latest:pro", policy["allowed"])
        self.assertNotIn("claude-opus-4-8:standard", policy["allowed"])

    def test_admin_can_assign_exact_levels(self):
        self.store.set_policy(
            "regular",
            allowed=["gpt-5-5:instant", "latest:medium"],
            updated_by="admin",
        )
        policy = self.store.policy_for_user("regular", "user")
        self.assertEqual(policy["allowed"], ["latest:medium", "gpt-5-5:instant"])
        self.assertNotIn("metered", policy)
        with self.assertRaises(ChatPolicyError):
            self.store.reserve(
                "regular", "user", version="latest", level="pro"
            )

    def test_background_tasks_choose_the_lightest_allowed_lane(self):
        selection = self.store.task_selection("regular", "user")
        self.assertEqual(selection["key"], "gpt-5-5:instant")

    def test_claude_defaults_and_provider_isolation(self):
        default = self.store.default_selection("regular", "user", "claude-web")
        task = self.store.task_selection("regular", "user", "claude-web")
        self.assertEqual(default["key"], "claude-sonnet-5:standard")
        self.assertEqual(task["key"], "claude-haiku-4-5:fast")
        with self.assertRaises(ChatPolicyError):
            self.store.reserve(
                "regular",
                "user",
                version="latest",
                level="medium",
                model_id="claude-web",
            )

    def test_retired_points_rows_are_preserved_but_never_used(self):
        self.store.set_policy(
            "legacy-points",
            allowed=["latest:medium"],
            updated_by="admin",
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE chat_policy SET metered = 1 WHERE user_id = ?",
                ("legacy-points",),
            )
            connection.execute(
                """
                INSERT INTO chat_ledger (id, user_id, delta, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy-entry", "legacy-points", 4, "historical", "admin", 1),
            )

        reservation = self.store.reserve(
            "legacy-points", "user", version="latest", level="medium"
        )
        self.store.finalize(reservation.id, "committed")
        quota = self.store.quota_summary("legacy-points", "user")
        self.assertNotIn("metered", quota)
        self.assertNotIn("remaining_points", quota)
        self.assertEqual(quota["request_count"], 1)
        with self.store._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT delta FROM chat_ledger WHERE id = 'legacy-entry'"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT cost FROM chat_usage WHERE id = ?",
                    (reservation.id,),
                ).fetchone()[0],
                0,
            )

    def test_database_contains_no_message_content_columns(self):
        with self.store._connect() as connection:
            columns = {
                row[1]
                for table in ("chat_policy", "chat_ledger", "chat_usage")
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
        self.assertFalse({"prompt", "response", "message", "email", "name"} & columns)

    def test_admin_summary_contains_only_aggregate_and_route_metadata(self):
        committed = self.store.reserve(
            "first-user", "user", version="latest", level="medium"
        )
        self.store.finalize(committed.id, "committed")
        released = self.store.reserve(
            "second-user",
            "user",
            model_id="claude-web",
            version="claude-sonnet-5",
            level="standard",
        )
        self.store.finalize(released.id, "released")

        summary = self.store.admin_summary()
        self.assertEqual(summary["all_time_requests"], 1)
        self.assertEqual(summary["requests_24h"], 1)
        self.assertEqual(summary["released_24h"], 1)
        providers = {item["family"]: item for item in summary["providers"]}
        self.assertEqual(providers["gpt"]["requests"], 1)
        self.assertEqual(len(summary["recent"]), 2)
        self.assertFalse(
            {"prompt", "response", "message", "email", "name"}
            & set(summary["recent"][0])
        )

    @staticmethod
    def _group_rules(*, medium_limit=1, instant_limit=2):
        enabled = {"latest:medium", "gpt-5-5:instant"}
        return [
            {
                "selection_key": key,
                "enabled": key in enabled,
                "limit_count": medium_limit
                if key == "latest:medium"
                else instant_limit
                if key == "gpt-5-5:instant"
                else None,
                "window_seconds": 3600,
                "fallback_key": "gpt-5-5:instant"
                if key == "latest:medium"
                else None,
            }
            for key in (
                "latest:medium",
                "latest:high",
                "latest:xhigh",
                "latest:pro",
                "gpt-5-5:instant",
                "gpt-5-3:standard",
                "o3:standard",
            )
        ]

    def test_groups_are_seeded_and_assignment_controls_permissions(self):
        groups = {group["id"]: group for group in self.store.list_groups()}
        self.assertEqual(set(groups), {"basic", "admin"})
        model_groups = {
            group["id"]: group for group in self.store.list_model_groups()
        }
        self.assertEqual(
            set(model_groups),
            {
                "gpt-basic",
                "gpt-admin",
                "gpt-disabled",
                "gpt-free-plan",
                "gpt-go-plan",
                "gpt-plus-plan",
                "gpt-pro-5x",
                "gpt-pro-20x",
                "claude-basic",
                "claude-admin",
                "claude-disabled",
                "claude-free-plan",
                "claude-pro-plan",
                "claude-max-5x",
                "claude-max-20x",
            },
        )
        self.assertEqual(
            model_groups["claude-basic"]["account_pool_id"],
            "claude-default",
        )
        self.store.assign_group("regular", "pro", assigned_by="admin")
        policy = self.store.policy_for_user("regular", "user")
        self.assertEqual(policy["group"]["id"], "pro")
        self.assertEqual(policy["provider_groups"]["gpt"]["id"], "gpt-pro")
        self.assertEqual(
            policy["provider_groups"]["claude"]["id"], "claude-pro"
        )
        self.assertIn("latest:pro", policy["allowed"])
        self.assertTrue(self.store.group_by_id("pro")["is_retired"])
        self.assertIn("pro", {group["id"] for group in self.store.list_groups()})
        self.assertIn(
            "gpt-pro",
            {group["id"] for group in self.store.list_model_groups("gpt")},
        )

    def test_gpt_account_pool_is_created_updated_and_inherited_from_group(self):
        group = self._create_group(
            name="pool routing",
            description="test",
            gpt_account_pool_id="pool-a",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.store.assign_group("regular", group["id"], assigned_by="admin")
        self.assertEqual(group["gpt_account_pool_id"], "pool-a")
        self.assertEqual(
            self.store.concurrency_for_user("regular", "user")["gpt_account_pool_id"],
            "pool-a",
        )

        updated = self.store.update_group(
            group["id"],
            name=group["name"],
            description=group["description"],
            storage_quota_bytes=group["storage_quota_bytes"],
            max_concurrency=group["max_concurrency"],
            default_user_concurrency=group["default_user_concurrency"],
            gpt_account_pool_id="pool-b",
            rules=group["rules"],
            updated_by="admin",
        )
        self.assertEqual(updated["gpt_account_pool_id"], "pool-b")
        self.assertEqual(
            self.store.concurrency_for_user("regular", "user")["gpt_account_pool_id"],
            "pool-b",
        )

    def test_user_can_combine_one_group_per_provider(self):
        self.store.assign_resource_group(
            "regular", "basic", assigned_by="admin"
        )
        self.store.assign_model_group(
            "regular", "gpt", "gpt-pro", assigned_by="admin"
        )
        self.store.assign_model_group(
            "regular", "claude", "claude-basic", assigned_by="admin"
        )
        policy = self.store.policy_for_user("regular", "user")
        self.assertEqual(policy["resource_group"]["id"], "basic")
        self.assertEqual(policy["provider_groups"]["gpt"]["id"], "gpt-pro")
        self.assertEqual(
            policy["provider_groups"]["claude"]["id"], "claude-basic"
        )
        self.assertIn("latest:pro", policy["allowed"])
        self.assertNotIn("claude-opus-4-8:standard", policy["allowed"])

        self.store.assign_model_group(
            "regular", "claude", "claude-pro", assigned_by="admin"
        )
        changed = self.store.policy_for_user("regular", "user")
        self.assertEqual(changed["provider_groups"]["gpt"]["id"], "gpt-pro")
        self.assertEqual(
            changed["provider_groups"]["claude"]["id"], "claude-pro"
        )
        self.assertIn("claude-opus-4-8:standard", changed["allowed"])

        self.store.assign_resource_group("regular", "pro", assigned_by="admin")
        resource_changed = self.store.policy_for_user("regular", "user")
        self.assertEqual(resource_changed["resource_group"]["id"], "pro")
        self.assertEqual(
            resource_changed["provider_groups"]["gpt"]["id"], "gpt-pro"
        )
        self.assertEqual(
            resource_changed["provider_groups"]["claude"]["id"], "claude-pro"
        )

    def test_gpt_model_group_controls_account_pool_without_changing_resources(self):
        group = self.store.create_model_group(
            provider_family="gpt",
            name="isolated pool",
            description="test",
            account_pool_id="pool-a",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.store.assign_resource_group("regular", "basic", assigned_by="admin")
        self.store.assign_model_group(
            "regular", "gpt", group["id"], assigned_by="admin"
        )
        before = self.store.concurrency_for_user("regular", "user")
        self.assertEqual(before["group_id"], "basic")
        self.assertEqual(before["gpt_account_pool_id"], "pool-a")

        updated = self.store.update_model_group(
            group["id"],
            provider_family="gpt",
            name=group["name"],
            description=group["description"],
            account_pool_id="pool-b",
            rules=group["rules"],
            updated_by="admin",
        )
        self.assertEqual(updated["account_pool_id"], "pool-b")
        after = self.store.concurrency_for_user("regular", "user")
        self.assertEqual(after["group_id"], "basic")
        self.assertEqual(after["gpt_account_pool_id"], "pool-b")

    def test_claude_model_group_controls_its_own_account_pool(self):
        group = self.store.create_model_group(
            provider_family="claude",
            name="Claude pool group",
            description="test",
            account_pool_id="claude-private",
            rules=[
                {
                    "selection_key": selection["key"],
                    "enabled": True,
                    "limit_count": None,
                    "window_seconds": 0,
                    "fallback_key": None,
                }
                for selection in SELECTIONS
                if selection["family"] == "claude"
            ],
            updated_by="admin",
        )
        self.store.assign_model_group(
            "regular",
            "claude",
            group["id"],
            assigned_by="admin",
        )
        concurrency = self.store.concurrency_for_user("regular", "user")
        self.assertEqual(
            concurrency["claude_account_pool_id"],
            "claude-private",
        )
        self.assertEqual(
            concurrency["account_pool_ids"]["claude"],
            "claude-private",
        )

    def test_model_group_assignment_rejects_a_different_provider(self):
        with self.assertRaises(ChatPolicyError):
            self.store.assign_model_group(
                "regular", "gpt", "claude-pro", assigned_by="admin"
            )

    def test_disabled_provider_group_does_not_disable_other_providers(self):
        self.store.assign_model_group(
            "regular", "claude", "claude-disabled", assigned_by="admin"
        )
        policy = self.store.policy_for_user("regular", "user")
        self.assertIn("latest:medium", policy["allowed"])
        self.assertFalse(
            any(key.startswith("claude-") for key in policy["allowed"])
        )
        with self.assertRaises(ChatPolicyError):
            self.store.default_selection("regular", "user", "claude-web")

    def test_usage_records_resource_and_model_groups_separately(self):
        self.store.assign_resource_group("regular", "basic", assigned_by="admin")
        self.store.assign_model_group(
            "regular", "gpt", "gpt-pro", assigned_by="admin"
        )
        reservation = self.store.reserve(
            "regular", "user", version="latest", level="pro"
        )
        self.assertEqual(reservation.group_id, "basic")
        self.assertEqual(reservation.model_group_id, "gpt-pro")
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT group_id, model_group_id FROM chat_usage WHERE id = ?",
                (reservation.id,),
            ).fetchone()
        self.assertEqual(row["group_id"], "basic")
        self.assertEqual(row["model_group_id"], "gpt-pro")
        self.store.finalize(reservation.id, "released")

    def test_legacy_combined_assignments_migrate_to_both_providers(self):
        legacy_path = Path(self.temp.name) / "legacy-chat.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE chat_group (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                default_role TEXT,
                is_system INTEGER NOT NULL,
                updated_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE chat_group_rule (
                group_id TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                limit_count INTEGER,
                window_seconds INTEGER NOT NULL,
                fallback_key TEXT,
                PRIMARY KEY (group_id, selection_key)
            );
            CREATE TABLE chat_user_group (
                user_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                assigned_by TEXT NOT NULL,
                assigned_at INTEGER NOT NULL
            );
            INSERT INTO chat_group
                (id, name, description, default_role, is_system,
                 updated_by, created_at, updated_at)
            VALUES ('legacy-custom', '旧组合组', 'migration test', NULL, 0,
                    'admin', 1, 1);
            INSERT INTO chat_group_rule
                (group_id, selection_key, enabled, limit_count,
                 window_seconds, fallback_key)
            VALUES ('legacy-custom', 'latest:medium', 1, 9, 3600, NULL),
                   ('legacy-custom', 'claude-haiku-4-5:fast', 1, 12, 3600, NULL);
            INSERT INTO chat_user_group
                (user_id, group_id, assigned_by, assigned_at)
            VALUES ('legacy-user', 'legacy-custom', 'admin', 1);
            """
        )
        connection.close()

        migrated = ChatStore(legacy_path)
        policy = migrated.policy_for_user("legacy-user", "user")
        self.assertEqual(policy["resource_group"]["id"], "legacy-custom")
        self.assertEqual(
            policy["provider_groups"]["gpt"]["id"], "gpt-legacy-custom"
        )
        self.assertEqual(
            policy["provider_groups"]["claude"]["id"],
            "claude-legacy-custom",
        )
        self.assertIn("latest:medium", policy["allowed"])
        self.assertIn("claude-haiku-4-5:fast", policy["allowed"])

    def test_subscription_presets_cover_current_plans_and_validate(self):
        presets = {preset["id"]: preset for preset in chat_plan_presets()}
        self.assertEqual(
            list(presets),
            ["free", "go", "plus", "pro-5x", "pro-20x"],
        )
        for preset in presets.values():
            self.assertEqual(len(preset["rules"]), len(SELECTIONS))
            self.assertTrue(preset["official_note"])
            self.assertTrue(preset["recommendation_note"])
            created = self._create_group(
                name=f"preset {preset['id']}",
                description="preset validation",
                rules=preset["rules"],
                updated_by="admin",
            )
            self.assertEqual(len(created["rules"]), len(SELECTIONS))

    def test_subscription_preset_official_values_and_multipliers(self):
        presets = {preset["id"]: preset for preset in chat_plan_presets()}

        def rules(plan_id):
            return {rule["selection_key"]: rule for rule in presets[plan_id]["rules"]}

        free = rules("free")
        go = rules("go")
        plus = rules("plus")
        pro_5x = rules("pro-5x")
        pro_20x = rules("pro-20x")
        self.assertEqual(
            (go["gpt-5-5:instant"]["limit_count"], go["gpt-5-5:instant"]["window_seconds"]),
            (160, 3 * 60 * 60),
        )
        self.assertTrue(free["gpt-5-5:instant"]["enabled"])
        self.assertEqual(free["gpt-5-5:instant"]["limit_count"], 10)
        self.assertEqual(free["gpt-5-5:instant"]["source"], "site_rule")
        self.assertEqual(
            free["gpt-5-5:instant"]["published_window_seconds"],
            5 * 60 * 60,
        )
        self.assertFalse(go["latest:medium"]["enabled"])
        self.assertFalse(go["gpt-5-3:standard"]["enabled"])
        self.assertFalse(go["o3:standard"]["enabled"])
        self.assertTrue(plus["latest:medium"]["enabled"])
        self.assertTrue(plus["latest:high"]["enabled"])
        self.assertEqual(
            (
                plus["latest:medium"]["limit_count"],
                plus["latest:medium"]["window_seconds"],
            ),
            (150, 3 * 60 * 60),
        )
        self.assertEqual(plus["latest:medium"]["source"], "site_rule")
        self.assertEqual(
            (
                plus["latest:high"]["limit_count"],
                plus["latest:high"]["window_seconds"],
            ),
            (100, 7 * 24 * 60 * 60),
        )
        self.assertFalse(plus["latest:xhigh"]["enabled"])
        self.assertFalse(plus["latest:pro"]["enabled"])
        self.assertTrue(plus["gpt-5-3:standard"]["enabled"])
        self.assertEqual(
            (
                free["image:create"]["limit_count"],
                go["image:create"]["limit_count"],
                plus["image:create"]["limit_count"],
                pro_5x["image:create"]["limit_count"],
                pro_20x["image:create"]["limit_count"],
            ),
            (2, 10, 40, 200, 800),
        )
        self.assertTrue(
            all(
                rules["image:create"]["window_seconds"] > 0
                for rules in (free, go, plus, pro_5x, pro_20x)
            )
        )

        self.assertEqual(
            (
                plus["gpt-5-3:standard"]["limit_count"],
                plus["gpt-5-3:standard"]["window_seconds"],
            ),
            (160, 3 * 60 * 60),
        )
        self.assertEqual(
            (
                plus["o3:standard"]["limit_count"],
                plus["o3:standard"]["window_seconds"],
            ),
            (100, 7 * 24 * 60 * 60),
        )
        for key in (
            "latest:medium",
            "latest:high",
            "latest:xhigh",
            "latest:pro",
            "gpt-5-5:instant",
            "gpt-5-3:standard",
            "o3:standard",
        ):
            self.assertTrue(pro_5x[key]["enabled"])
            self.assertTrue(pro_20x[key]["enabled"])
            self.assertIsNotNone(pro_5x[key]["limit_count"])
            self.assertIsNotNone(pro_20x[key]["limit_count"])
            self.assertEqual(pro_5x[key]["source"], "site_rule")
            self.assertEqual(pro_20x[key]["source"], "site_rule")
        self.assertTrue(pro_5x["latest:pro"]["enabled"])
        self.assertEqual(
            pro_5x["gpt-5-5:instant"]["limit_count"],
            800,
        )
        self.assertEqual(
            pro_20x["gpt-5-5:instant"]["limit_count"],
            3_200,
        )
        groups = {
            group["id"]: group
            for group in self.store.list_model_groups("gpt")
        }
        for group_id in (
            "gpt-free-plan",
            "gpt-go-plan",
            "gpt-plus-plan",
            "gpt-pro-5x",
            "gpt-pro-20x",
        ):
            self.assertIn(group_id, groups)
            self.assertEqual(groups[group_id]["account_pool_id"], "gpt-default")
            self.assertTrue(groups[group_id]["is_plan_template"])

    def test_image_routing_is_plan_strict_and_separate_from_text(self):
        self.store.assign_model_group(
            "plus-user",
            "gpt",
            "gpt-plus-plan",
            assigned_by="admin",
        )
        plus = self.store.image_routing_for_user("plus-user", "user")
        self.assertEqual(plus["required_quota_profiles"], ["plus"])

        self.store.assign_model_group(
            "pro-user",
            "gpt",
            "gpt-pro-5x",
            assigned_by="admin",
        )
        pro = self.store.image_routing_for_user("pro-user", "user")
        self.assertEqual(
            pro["required_quota_profiles"],
            ["pro-5x", "pro-20x"],
        )

        reservation = self.store.reserve(
            "plus-user",
            "user",
            model_id="gpt-image",
            version="image",
            level="create",
        )
        self.assertEqual(reservation.selection_key, "image:create")
        self.store.finalize(reservation.id, "committed")
        summary = self.store.quota_summary("plus-user", "user")["models"]
        self.assertEqual(summary["image:create"]["remaining_count"], 39)
        self.assertEqual(
            summary["gpt-5-5:instant"]["remaining_count"],
            160,
        )

    def test_gpt_plan_groups_are_listed_from_smallest_to_largest(self):
        ordered = [
            group["id"]
            for group in self.store.list_model_groups("gpt")
            if group["id"].startswith("gpt-") and group["is_plan_template"]
        ]
        self.assertEqual(
            ordered,
            [
                "gpt-free-plan",
                "gpt-go-plan",
                "gpt-plus-plan",
                "gpt-pro-5x",
                "gpt-pro-20x",
            ],
        )

    def test_official_plan_templates_are_repaired_to_the_canonical_baseline(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE chat_model_group SET name = ? WHERE id = ?",
                ("被误改的名称", "gpt-plus-plan"),
            )
            connection.execute(
                """
                UPDATE chat_model_group_rule
                   SET enabled = 0, limit_count = 1
                 WHERE group_id = ? AND selection_key = ?
                """,
                ("gpt-plus-plan", "latest:medium"),
            )
        repaired_store = ChatStore(self.path)
        repaired = repaired_store.model_group_by_id("gpt-plus-plan")
        self.assertEqual(repaired["name"], "Plus 组")
        medium = next(
            rule
            for rule in repaired["rules"]
            if rule["selection_key"] == "latest:medium"
        )
        expected = next(
            rule
            for preset in chat_plan_presets("gpt")
            if preset["id"] == "plus"
            for rule in preset["rules"]
            if rule["selection_key"] == "latest:medium"
        )
        self.assertEqual(medium["enabled"], expected["enabled"])
        self.assertEqual(medium["limit_count"], expected["limit_count"])
        with self.assertRaisesRegex(ChatPolicyError, "先复制"):
            repaired_store.update_model_group(
                "gpt-plus-plan",
                provider_family="gpt",
                name="不能直接修改",
                description=repaired["description"],
                account_pool_id=repaired["account_pool_id"],
                rules=repaired["rules"],
                updated_by="admin",
            )

    def test_claude_subscription_presets_and_seeded_groups(self):
        presets = {
            preset["id"]: preset
            for preset in chat_plan_presets("claude")
        }
        self.assertEqual(
            list(presets),
            ["free", "pro", "max-5x", "max-20x"],
        )
        self.assertTrue(
            all(
                len(preset["rules"]) == 5
                and preset["provider_family"] == "claude"
                and preset["official_note"]
                and preset["recommendation_note"]
                for preset in presets.values()
            )
        )
        pro = {
            rule["selection_key"]: rule
            for rule in presets["pro"]["rules"]
        }
        max_5x = {
            rule["selection_key"]: rule
            for rule in presets["max-5x"]["rules"]
        }
        max_20x = {
            rule["selection_key"]: rule
            for rule in presets["max-20x"]["rules"]
        }
        for key in pro:
            self.assertEqual(
                max_5x[key]["limit_count"],
                pro[key]["limit_count"] * 5,
            )
            self.assertEqual(
                max_20x[key]["limit_count"],
                pro[key]["limit_count"] * 20,
            )
        groups = {
            group["id"]: group
            for group in self.store.list_model_groups("claude")
        }
        for group_id in (
            "claude-free-plan",
            "claude-pro-plan",
            "claude-max-5x",
            "claude-max-20x",
        ):
            self.assertIn(group_id, groups)
            self.assertEqual(groups[group_id]["account_pool_id"], "claude-default")
            self.assertTrue(groups[group_id]["is_plan_template"])

    def test_model_windows_are_independent_per_user_and_report_reset(self):
        group = self._create_group(
            name="one request",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        for user_id in ("first", "second"):
            self.store.assign_group(user_id, group["id"], assigned_by="admin")
        reservation = self.store.reserve(
            "first", "user", version="latest", level="medium"
        )
        self.store.finalize(reservation.id, "committed")
        first = self.store.quota_summary("first", "user")["models"]["latest:medium"]
        second = self.store.quota_summary("second", "user")["models"]["latest:medium"]
        self.assertEqual(first["remaining_count"], 0)
        self.assertFalse(first["available"])
        self.assertIsInstance(first["reset_at"], int)
        self.assertEqual(second["remaining_count"], 1)
        self.assertTrue(second["available"])

    def test_parallel_store_instances_cannot_overbook_a_model_window(self):
        group = self._create_group(
            name="parallel",
            description="test",
            rules=self._group_rules(medium_limit=1, instant_limit=2),
            updated_by="admin",
        )
        self.store.assign_group("user", group["id"], assigned_by="admin")
        other_store = ChatStore(self.path)
        gate = threading.Barrier(2)

        def reserve(store):
            gate.wait()
            return store.reserve("user", "user", version="latest", level="medium")

        with ThreadPoolExecutor(max_workers=2) as pool:
            reservations = list(pool.map(reserve, (self.store, other_store)))

        selections = sorted(item.selection_key for item in reservations)
        self.assertEqual(selections, ["gpt-5-5:instant", "latest:medium"])

    def test_exhausted_model_automatically_falls_back(self):
        group = self._create_group(
            name="fallback",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.store.assign_group("user", group["id"], assigned_by="admin")
        first = self.store.reserve("user", "user", version="latest", level="medium")
        self.store.finalize(first.id, "committed")
        second = self.store.reserve("user", "user", version="latest", level="medium")
        self.assertEqual(second.selection_key, "gpt-5-5:instant")
        self.assertEqual(second.fallback_from, "latest:medium")
        self.store.finalize(second.id, "committed")
        recent = self.store.recent_usage("user")
        fallback_rows = [item for item in recent if item["fallback_from"]]
        self.assertEqual(len(fallback_rows), 1)
        self.assertEqual(fallback_rows[0]["requested_selection_key"], "latest:medium")
        self.assertEqual(fallback_rows[0]["fallback_from"], "latest:medium")

    def test_reset_starts_a_fresh_user_window(self):
        group = self._create_group(
            name="resettable",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.store.assign_group("user", group["id"], assigned_by="admin")
        reservation = self.store.reserve("user", "user", version="latest", level="medium")
        self.store.finalize(reservation.id, "committed")
        self.store.reset_quota_windows("user", "latest:medium")
        summary = self.store.quota_summary("user", "user")["models"]["latest:medium"]
        self.assertEqual(summary["remaining_count"], 1)
        self.assertIsNone(summary["reset_at"])

        immediate = self.store.reserve(
            "user", "user", version="latest", level="medium"
        )
        self.assertEqual(immediate.selection_key, "latest:medium")

    def test_exhausted_chain_reports_the_earliest_recovery(self):
        rules = self._group_rules(medium_limit=1, instant_limit=1)
        for rule in rules:
            if rule["selection_key"] == "latest:medium":
                rule["window_seconds"] = 60
            elif rule["selection_key"] == "gpt-5-5:instant":
                rule["window_seconds"] = 3600
        group = self._create_group(
            name="earliest recovery",
            description="test",
            rules=rules,
            updated_by="admin",
        )
        self.store.assign_group("user", group["id"], assigned_by="admin")
        for version, level in (("latest", "medium"), ("gpt-5-5", "instant")):
            reservation = self.store.reserve(
                "user", "user", version=version, level=level
            )
            self.store.finalize(reservation.id, "committed")
        medium_reset = self.store.quota_summary("user", "user")["models"][
            "latest:medium"
        ]["reset_at"]
        with self.assertRaises(ChatModelQuotaError) as exhausted:
            self.store.reserve("user", "user", version="latest", level="medium")
        self.assertEqual(exhausted.exception.selection_key, "latest:medium")
        self.assertEqual(exhausted.exception.reset_at, medium_reset)

    def test_group_validation_rejects_fallback_cycles(self):
        rules = self._group_rules()
        for rule in rules:
            if rule["selection_key"] == "gpt-5-5:instant":
                rule["fallback_key"] = "latest:medium"
        with self.assertRaises(ChatPolicyError):
            self._create_group(
                name="cycle",
                description="test",
                rules=rules,
                updated_by="admin",
            )

    def test_group_validation_rejects_cross_provider_fallback(self):
        rules = self._group_rules()
        rules.append(
            {
                "selection_key": "claude-haiku-4-5:fast",
                "enabled": True,
                "limit_count": 10,
                "window_seconds": 3600,
                "fallback_key": None,
            }
        )
        for rule in rules:
            if rule["selection_key"] == "latest:medium":
                rule["fallback_key"] = "claude-haiku-4-5:fast"
        with self.assertRaises(ChatPolicyError):
            self._create_group(
                name="cross provider",
                description="test",
                rules=rules,
                updated_by="admin",
            )

    def test_provider_group_accepts_only_its_own_rules_and_protects_members(self):
        with self.assertRaises(ChatPolicyError):
            self.store.create_model_group(
                provider_family="gpt",
                name="wrong provider",
                description="test",
                rules=[
                    {
                        "selection_key": "claude-haiku-4-5:fast",
                        "enabled": True,
                        "limit_count": 10,
                        "window_seconds": 3600,
                        "fallback_key": None,
                    }
                ],
                updated_by="admin",
            )

        group = self.store.create_model_group(
            provider_family="gpt",
            name="GPT custom",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.assertEqual(group["provider_family"], "gpt")
        self.assertEqual(
            len(group["rules"]),
            sum(selection["family"] == "gpt" for selection in SELECTIONS),
        )
        self.assertIn(
            "image:create",
            {rule["selection_key"] for rule in group["rules"]},
        )
        self.store.assign_model_group(
            "user", "gpt", group["id"], assigned_by="admin"
        )
        with self.assertRaises(ChatPolicyError):
            self.store.delete_model_group(group["id"])

    def test_group_with_members_cannot_be_deleted(self):
        group = self._create_group(
            name="in use",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        self.store.assign_group("user", group["id"], assigned_by="admin")
        with self.assertRaisesRegex(ChatPolicyError, "1 位用户"):
            self.store.delete_group(group["id"])

    def test_bulk_group_assignment_is_atomic_and_updates_member_counts(self):
        resource = self.store.create_resource_group(
            name="批量资源组",
            description="test",
            storage_quota_bytes=3 * 1024**3,
            max_concurrency=4,
            default_user_concurrency=2,
            updated_by="admin",
        )
        gpt_group = self.store.create_model_group(
            provider_family="gpt",
            name="批量 GPT 组",
            description="test",
            rules=self._group_rules(),
            updated_by="admin",
        )
        updated = self.store.bulk_assign_groups(
            ["user-b", "user-a", "user-a"],
            resource_group_id=resource["id"],
            model_group_ids={"gpt": gpt_group["id"]},
            assigned_by="admin",
        )
        self.assertEqual(updated, 2)
        for user_id in ("user-a", "user-b"):
            policy = self.store.policy_for_user(user_id, "user")
            self.assertEqual(policy["resource_group"]["id"], resource["id"])
            self.assertEqual(
                policy["provider_groups"]["gpt"]["id"],
                gpt_group["id"],
            )
        self.assertEqual(
            self.store.group_by_id(resource["id"])["member_count"],
            2,
        )
        self.assertEqual(
            self.store.model_group_by_id(gpt_group["id"])["member_count"],
            2,
        )

        untouched = self.store.create_resource_group(
            name="原子性检查资源组",
            description="test",
            storage_quota_bytes=1024**3,
            max_concurrency=2,
            default_user_concurrency=1,
            updated_by="admin",
        )
        with self.assertRaisesRegex(ChatPolicyError, "不属于所选 Provider"):
            self.store.bulk_assign_groups(
                ["user-a", "user-b"],
                resource_group_id=untouched["id"],
                model_group_ids={"gpt": "claude-basic"},
                assigned_by="admin",
            )
        for user_id in ("user-a", "user-b"):
            self.assertEqual(
                self.store.policy_for_user(user_id, "user")["resource_group"]["id"],
                resource["id"],
            )


@unittest.skipUnless(
    os.getenv("TURTLE_RUN_POSTGRES_TESTS") == "1",
    "PostgreSQL integration test is opt-in",
)
class PostgresChatStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database_url = runtime_database_url()
        self.schema = f"turtle_chat_test_{uuid.uuid4().hex}"
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"CREATE SCHEMA {quote_identifier(self.schema)}")
        parsed_url = make_url(normalized_postgres_url(self.database_url)).update_query_dict(
            {"options": f"-csearch_path={self.schema}"}
        )
        self.test_database_url = parsed_url.render_as_string(hide_password=False)
        self.user_id = f"postgres-test-user-{uuid.uuid4()}"
        try:
            self.store = ChatStore(database_url=self.test_database_url)
            self.group = self.store.create_group(
                name=f"postgres-test-{uuid.uuid4().hex[:12]}",
                description="PostgreSQL transaction and advisory-lock test",
                rules=ChatStoreTests._group_rules(medium_limit=1, instant_limit=2),
                storage_quota_bytes=2 * 1024**3,
                max_concurrency=2,
                default_user_concurrency=1,
                updated_by="integration-test",
            )
            self.store.assign_group(
                self.user_id,
                self.group["id"],
                assigned_by="integration-test",
            )
        except Exception:
            dispose_postgres_engine(self.test_database_url)
            with connect_postgres(self.database_url) as connection:
                connection.execute(f"DROP SCHEMA {quote_identifier(self.schema)} CASCADE")
            raise

    def tearDown(self):
        dispose_postgres_engine(self.test_database_url)
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"DROP SCHEMA {quote_identifier(self.schema)} CASCADE")

    def test_parallel_instances_do_not_overbook_postgres_window(self):
        other = ChatStore(database_url=self.test_database_url)
        gate = threading.Barrier(2)

        def reserve(store):
            gate.wait()
            return store.reserve(
                self.user_id,
                "user",
                version="latest",
                level="medium",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            reservations = list(pool.map(reserve, (self.store, other)))

        self.assertEqual(
            sorted(item.selection_key for item in reservations),
            ["gpt-5-5:instant", "latest:medium"],
        )
        for reservation in reservations:
            self.store.finalize(reservation.id, "released")

    def test_provider_group_composition_is_enforced_in_postgres(self):
        self.store.assign_resource_group(
            self.user_id,
            "basic",
            assigned_by="integration-test",
        )
        self.store.assign_model_group(
            self.user_id,
            "gpt",
            "gpt-pro",
            assigned_by="integration-test",
        )
        self.store.assign_model_group(
            self.user_id,
            "claude",
            "claude-basic",
            assigned_by="integration-test",
        )
        policy = self.store.policy_for_user(self.user_id, "user")
        self.assertEqual(policy["resource_group"]["id"], "basic")
        self.assertEqual(policy["provider_groups"]["gpt"]["id"], "gpt-pro")
        self.assertEqual(
            policy["provider_groups"]["claude"]["id"],
            "claude-basic",
        )
        with self.store._connect() as connection:
            assignments = connection.execute(
                """
                SELECT provider_family, group_id
                  FROM chat_user_model_group
                 WHERE user_id = ?
                 ORDER BY provider_family
                """,
                (self.user_id,),
            ).fetchall()
        self.assertEqual(
            [(row["provider_family"], row["group_id"]) for row in assignments],
            [("claude", "claude-basic"), ("gpt", "gpt-pro")],
        )
        with self.assertRaises(ChatPolicyError):
            self.store.assign_model_group(
                self.user_id,
                "gpt",
                "claude-pro",
                assigned_by="integration-test",
            )

    def test_subscription_state_and_audit_persist_across_postgres_instances(self):
        now = int(time.time())
        configured = self.store.set_subscription(
            self.user_id,
            "user",
            starts_at=now - 1,
            expires_at=now + 3_600,
            updated_by="integration-test",
        )
        self.assertTrue(configured["active"])
        other = ChatStore(database_url=self.test_database_url)
        self.assertTrue(other.subscription_for_user(self.user_id, "user")["active"])
        cancelled = other.cancel_subscription(
            self.user_id,
            "user",
            updated_by="integration-test",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            {item["action"] for item in self.store.subscription_events(self.user_id)},
            {"activate", "cancel"},
        )

    def test_announcement_list_and_receipts_persist_across_postgres_instances(self):
        first = self.store.create_announcement(
            title="订阅说明",
            body_markdown="请查看管理员提供的订阅方式。",
            enabled=True,
            updated_by="integration-test",
        )
        second = self.store.create_announcement(
            title="使用规则",
            body_markdown="请勿共享账号。",
            enabled=True,
            updated_by="integration-test",
        )
        other = ChatStore(database_url=self.test_database_url)
        self.assertEqual(
            {item["id"] for item in other.announcements_for_user(self.user_id, "user")},
            {first["id"], second["id"]},
        )
        other.dismiss_announcement(
            self.user_id,
            first["id"],
            first["revision"],
        )
        state = {
            item["id"]: item
            for item in self.store.announcements_for_user(self.user_id, "user")
        }
        self.assertFalse(state[first["id"]]["should_show"])
        self.assertTrue(state[second["id"]]["should_show"])


@unittest.skipUnless(
    os.getenv("TURTLE_RUN_POSTGRES_TESTS") == "1",
    "PostgreSQL integration test is opt-in",
)
class PostgresLegacyGroupMigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database_url = runtime_database_url()
        self.schema = f"turtle_chat_migration_test_{uuid.uuid4().hex}"
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"CREATE SCHEMA {quote_identifier(self.schema)}")
        parsed_url = make_url(normalized_postgres_url(self.database_url)).update_query_dict(
            {"options": f"-csearch_path={self.schema}"}
        )
        self.test_database_url = parsed_url.render_as_string(hide_password=False)

    def tearDown(self):
        dispose_postgres_engine(self.test_database_url)
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"DROP SCHEMA {quote_identifier(self.schema)} CASCADE")

    def test_legacy_combined_group_projects_to_both_providers(self):
        with connect_postgres(self.test_database_url) as connection:
            connection.executescript(
                """
                CREATE TABLE chat_group (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    default_role TEXT,
                    is_system INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE chat_group_rule (
                    group_id TEXT NOT NULL,
                    selection_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    limit_count INTEGER,
                    window_seconds INTEGER NOT NULL,
                    fallback_key TEXT,
                    PRIMARY KEY (group_id, selection_key)
                );
                CREATE TABLE chat_user_group (
                    user_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    assigned_by TEXT NOT NULL,
                    assigned_at INTEGER NOT NULL
                );
                INSERT INTO chat_group
                    (id, name, description, default_role, is_system,
                     updated_by, created_at, updated_at)
                VALUES ('legacy-custom', '旧组合组', 'postgres migration test',
                        NULL, 0, 'admin', 1, 1);
                INSERT INTO chat_group_rule
                    (group_id, selection_key, enabled, limit_count,
                     window_seconds, fallback_key)
                VALUES ('legacy-custom', 'latest:medium', 1, 9, 3600, NULL),
                       ('legacy-custom', 'claude-haiku-4-5:fast', 1, 12, 3600, NULL);
                INSERT INTO chat_user_group
                    (user_id, group_id, assigned_by, assigned_at)
                VALUES ('legacy-user', 'legacy-custom', 'admin', 1);
                """
            )

        migrated = ChatStore(database_url=self.test_database_url)
        policy = migrated.policy_for_user("legacy-user", "user")
        self.assertEqual(policy["resource_group"]["id"], "legacy-custom")
        self.assertEqual(
            policy["provider_groups"]["gpt"]["id"],
            "gpt-legacy-custom",
        )
        self.assertEqual(
            policy["provider_groups"]["claude"]["id"],
            "claude-legacy-custom",
        )
        self.assertIn("latest:medium", policy["allowed"])
        self.assertIn("claude-haiku-4-5:fast", policy["allowed"])


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, tuple[str, float | None]] = {}
        self.ttls: dict[str, int] = {}

    def _purge(self, key: str) -> None:
        entry = self.values.get(key)
        if entry is not None and entry[1] is not None and time.monotonic() >= entry[1]:
            self.values.pop(key, None)

    async def get(self, key: str):
        self._purge(key)
        entry = self.values.get(key)
        return entry[0] if entry is not None else None

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ):
        self._purge(key)
        if nx and key in self.values:
            return False
        ttl = int(ex) if ex is not None else max(1, int((px or 0) / 1000))
        expires = time.monotonic() + ttl if ex is not None or px is not None else None
        self.values[key] = (value, expires)
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    async def delete(self, *keys: str):
        deleted = 0
        for key in keys:
            if key in self.values:
                deleted += 1
            self.values.pop(key, None)
        return deleted

    async def eval(
        self,
        _script: str,
        _keys: int,
        key: str,
        token: str,
        ttl_ms: int | None = None,
    ):
        if await self.get(key) == token:
            if ttl_ms is not None:
                value = self.values[key][0]
                self.values[key] = (
                    value,
                    time.monotonic() + max(1, int(ttl_ms)) / 1000,
                )
                return 1
            return await self.delete(key)
        return 0


class _FailingRedis:
    async def get(self, _key: str):
        raise ConnectionError("test Redis outage")


class SubscriptionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_cache_miss_single_flights_database_read(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            cache = SubscriptionCache(store)
            redis = _FakeRedis()
            with (
                patch.object(cache, "_redis", AsyncMock(return_value=redis)),
                patch.object(
                    store,
                    "subscription_for_user",
                    wraps=store.subscription_for_user,
                ) as database_read,
            ):
                results = await asyncio.gather(
                    *(cache.get("member", "user") for _ in range(24))
                )
                again = await cache.get("member", "user")
            self.assertTrue(all(item["active"] for item in results))
            self.assertTrue(again["active"])
            self.assertEqual(database_read.call_count, 1)
            value_key = cache._cache_key("member", "user")
            self.assertGreaterEqual(redis.ttls[value_key], 1)
            self.assertLessEqual(redis.ttls[value_key], cache.max_ttl_seconds)

    async def test_pending_negative_cache_uses_short_ttl(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            cache = SubscriptionCache(store)
            redis = _FakeRedis()
            with (
                patch.object(cache, "_redis", AsyncMock(return_value=redis)),
                patch.object(
                    store,
                    "subscription_for_user",
                    wraps=store.subscription_for_user,
                ) as database_read,
            ):
                first = await cache.get("pending", "pending")
                second = await cache.get("pending", "pending")
            self.assertEqual(first["status"], "pending")
            self.assertEqual(second["status"], "pending")
            self.assertEqual(database_read.call_count, 1)
            self.assertLessEqual(
                redis.ttls[cache._cache_key("pending", "pending")],
                cache.negative_ttl_seconds,
            )

    async def test_distributed_lock_is_renewed_during_slow_database_read(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            first = SubscriptionCache(store)
            second = SubscriptionCache(store)
            first.lock_ttl_ms = second.lock_ttl_ms = 1_000
            first.lock_wait_ms = second.lock_wait_ms = 2_000
            redis = _FakeRedis()
            original_read = store.subscription_for_user

            def slow_database_read(*args, **kwargs):
                time.sleep(1.2)
                return original_read(*args, **kwargs)

            with (
                patch.object(first, "_redis", AsyncMock(return_value=redis)),
                patch.object(second, "_redis", AsyncMock(return_value=redis)),
                patch.object(
                    store,
                    "subscription_for_user",
                    side_effect=slow_database_read,
                ) as database_read,
            ):
                first_read = asyncio.create_task(first.get("member", "user"))
                await asyncio.sleep(0.05)
                second_read = asyncio.create_task(second.get("member", "user"))
                results = await asyncio.gather(first_read, second_read)
            self.assertTrue(all(item["active"] for item in results))
            self.assertEqual(database_read.call_count, 1)
            self.assertNotIn(first._lock_key("member"), redis.values)

    async def test_cache_ttl_never_outlives_subscription_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            now = int(time.time())
            store.set_subscription(
                "short",
                "user",
                starts_at=now - 1,
                expires_at=now + 5,
                updated_by="admin",
            )
            cache = SubscriptionCache(store)
            redis = _FakeRedis()
            with patch.object(cache, "_redis", AsyncMock(return_value=redis)):
                subscription = await cache.get("short", "user")
            self.assertTrue(subscription["active"])
            self.assertLessEqual(
                redis.ttls[cache._cache_key("short", "user")],
                5,
            )

    async def test_mutation_replaces_cached_authorization(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            cache = SubscriptionCache(store)
            redis = _FakeRedis()
            with patch.object(cache, "_redis", AsyncMock(return_value=redis)):
                active = await cache.get("member", "user")
                cancelled = await cache.cancel_subscription(
                    "member",
                    "user",
                    updated_by="admin",
                )
                reread = await cache.get("member", "user")
            self.assertTrue(active["active"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(reread["status"], "cancelled")

    async def test_redis_failure_falls_back_to_bounded_database_read(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            cache = SubscriptionCache(store)
            with (
                patch.object(
                    cache,
                    "_redis",
                    AsyncMock(return_value=_FailingRedis()),
                ),
                patch.object(
                    store,
                    "subscription_for_user",
                    wraps=store.subscription_for_user,
                ) as database_read,
            ):
                subscription = await cache.get("member", "user")
            self.assertTrue(subscription["active"])
            self.assertEqual(database_read.call_count, 1)
            self.assertEqual(cache.snapshot()["redis_errors"], 1)
            self.assertTrue(cache.snapshot()["redis_backoff"])

    async def test_database_failure_never_reuses_stale_authorization(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            cache = SubscriptionCache(store)
            with (
                patch.object(
                    cache,
                    "_redis",
                    AsyncMock(return_value=_FailingRedis()),
                ),
                patch.object(
                    store,
                    "subscription_for_user",
                    side_effect=RuntimeError("test database outage"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "database outage"):
                    await cache.get("member", "user")


@unittest.skipUnless(
    os.getenv("TURTLE_RUN_REDIS_TESTS") == "1",
    "Redis integration test is opt-in",
)
class RedisSubscriptionCacheIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_redis_ttl_and_distributed_single_flight(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatStore(Path(temp) / "chat.db")
            first = SubscriptionCache(store)
            second = SubscriptionCache(store)
            prefix = f"turtle:test:subscription:{uuid.uuid4().hex}"
            first.prefix = prefix
            second.prefix = prefix
            redis = await first._redis()
            self.assertIsNotNone(redis)
            keys = [
                first._cache_key("member", role)
                for role in ("pending", "user", "admin")
            ]
            lock_key = first._lock_key("member")
            await redis.delete(*keys, lock_key)
            try:
                with patch.object(
                    store,
                    "subscription_for_user",
                    wraps=store.subscription_for_user,
                ) as database_read:
                    results = await asyncio.gather(
                        *(
                            (first if index % 2 == 0 else second).get(
                                "member",
                                "user",
                            )
                            for index in range(24)
                        )
                    )
                self.assertTrue(all(item["active"] for item in results))
                self.assertEqual(database_read.call_count, 1)
                ttl = await redis.ttl(first._cache_key("member", "user"))
                self.assertGreaterEqual(ttl, 1)
                self.assertLessEqual(ttl, first.max_ttl_seconds)
                self.assertFalse(await redis.exists(lock_key))
            finally:
                await redis.delete(*keys, lock_key)
                await redis.aclose()
                CHAT_CONCURRENCY._redis_client = None


class MeteringTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _fake_concurrency():
        def create_lease(**kwargs):
            return SimpleNamespace(
                request_id=kwargs["request_id"],
                queued_at_ms=1,
                admitted_at_ms=2,
                release=AsyncMock(),
            )

        coordinator = SimpleNamespace(
            normalize_request_id=lambda value: value or str(uuid.uuid4()),
            acquire=AsyncMock(side_effect=create_lease),
        )
        return coordinator

    def setUp(self):
        # Unit-level metering tests exercise quota/policy behavior with a fake
        # concurrency coordinator. Keep them independent from the deployment's
        # live Gateway capacity probe; account-pool admission has its own
        # focused integration coverage.
        self.environment = patch.dict(
            os.environ,
            {"TURTLE_ACCOUNT_POOL_ADMISSION_ENABLED": "false"},
        )
        self.environment.start()
        self.concurrency_patcher = patch(
            "open_webui.turtle_chat.metering.CHAT_CONCURRENCY",
            self._fake_concurrency(),
        )
        self.concurrency_patcher.start()

    def tearDown(self):
        self.concurrency_patcher.stop()
        self.environment.stop()

    async def test_one_official_image_task_uses_one_independent_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            store.assign_model_group(
                "image-user",
                "gpt",
                "gpt-plus-plan",
                assigned_by="admin",
            )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                context = await metering.prepare_image_generation(
                    SimpleNamespace(id="image-user", role="user"),
                    chat_id="chat-sticky",
                )
                self.assertEqual(
                    context.required_quota_profiles,
                    ["plus"],
                )
                self.assertEqual(
                    context.routing_payload()["turtle_chat_id"],
                    "chat-sticky",
                )
                await metering.finalize_image_generation(context, True)
                recent = store.recent_usage("image-user")
                self.assertEqual(
                    [item["status"] for item in recent],
                    ["committed"],
                )
                summary = store.quota_summary("image-user", "user")["models"]
                self.assertEqual(
                    summary["image:create"]["remaining_count"],
                    39,
                )
                self.assertEqual(
                    summary["gpt-5-5:instant"]["remaining_count"],
                    160,
                )
            finally:
                metering.CHAT_STORE = original

    async def test_existing_chat_accepts_only_its_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            chat = SimpleNamespace(
                user_id="user",
                chat={"models": ["gpt-5-web"]},
                meta={"turtle_provider": "gpt"},
            )
            try:
                with (
                    patch.object(metering, "CHAT_CONCURRENCY", self._fake_concurrency()),
                    patch(
                        "open_webui.models.chats.Chats.get_chat_by_id",
                        new=AsyncMock(return_value=chat),
                    ),
                ):
                    context = await metering.prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        {
                            "model": "gpt-5-web",
                            "turtle_model_version": "latest",
                            "turtle_thinking_level": "medium",
                        },
                        chat_id="gpt-chat",
                    )
                    self.assertEqual(context.selection_key, "latest:medium")
                    await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_existing_gpt_chat_rejects_claude(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            chat = SimpleNamespace(
                user_id="user",
                chat={"models": ["gpt-5-web"]},
                meta={"turtle_provider": "gpt"},
            )
            try:
                with (
                    patch.object(metering, "CHAT_CONCURRENCY", self._fake_concurrency()),
                    patch(
                        "open_webui.models.chats.Chats.get_chat_by_id",
                        new=AsyncMock(return_value=chat),
                    ),
                ):
                    with self.assertRaises(HTTPException) as denied:
                        await metering.prepare_chat_request(
                            SimpleNamespace(id="user", role="user"),
                            {
                                "model": "claude-web",
                                "turtle_claude_model": "claude-sonnet-5",
                                "turtle_claude_thinking": "standard",
                            },
                            chat_id="gpt-chat",
                        )
                self.assertEqual(denied.exception.status_code, 409)
                self.assertEqual(denied.exception.detail["code"], "chat_provider_mismatch")
                self.assertEqual(store.quota_summary("user", "user")["request_count"], 0)
            finally:
                metering.CHAT_STORE = original

    async def test_forbidden_pro_selection_returns_403(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                with self.assertRaises(HTTPException) as denied:
                    await metering.prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        {
                            "model": "gpt-5-web",
                            "turtle_model_version": "latest",
                            "turtle_thinking_level": "pro",
                        },
                    )
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(denied.exception.detail["code"], "chat_selection_forbidden")
            finally:
                metering.CHAT_STORE = original

    async def test_expired_subscription_is_rejected_before_concurrency(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            store.set_subscription(
                "user",
                "user",
                starts_at=100,
                expires_at=200,
                updated_by="admin",
            )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            coordinator = self._fake_concurrency()
            try:
                with patch.object(metering, "CHAT_CONCURRENCY", coordinator):
                    with self.assertRaises(HTTPException) as denied:
                        await metering.prepare_chat_request(
                            SimpleNamespace(id="user", role="user"),
                            {
                                "model": "gpt-5-web",
                                "turtle_model_version": "latest",
                                "turtle_thinking_level": "medium",
                            },
                        )
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(
                    denied.exception.detail["code"],
                    "chat_subscription_expired",
                )
                coordinator.acquire.assert_not_awaited()
                self.assertEqual(
                    store.quota_summary("user", "user")["request_count"],
                    0,
                )
            finally:
                metering.CHAT_STORE = original

    async def test_expired_subscription_blocks_unrecognized_model_route(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            store.set_subscription(
                "user",
                "user",
                starts_at=100,
                expires_at=200,
                updated_by="admin",
            )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                with self.assertRaises(HTTPException) as denied:
                    await metering.prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        {"model": "unrecognized-model"},
                    )
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(
                    denied.exception.detail["code"],
                    "chat_subscription_expired",
                )
            finally:
                metering.CHAT_STORE = original

    async def test_retired_legacy_points_flag_never_blocks_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            store.set_policy(
                "user",
                allowed=["latest:medium"],
                updated_by="admin",
            )
            with store._connect() as connection:
                connection.execute(
                    "UPDATE chat_policy SET metered = 1 WHERE user_id = 'user'"
                )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                context = await metering.prepare_chat_request(
                    SimpleNamespace(id="user", role="user"),
                    {
                        "model": "gpt-5-web",
                        "turtle_model_version": "latest",
                        "turtle_thinking_level": "medium",
                    },
                )
                self.assertIsNotNone(context.reservation)
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_internal_task_uses_safe_lane_without_charging(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {"model": "gpt-5-web"}
                context = await metering.prepare_chat_request(
                    SimpleNamespace(id="user", role="user"),
                    payload,
                    internal_task=True,
                )
                self.assertIsNotNone(context)
                self.assertIsNone(context.reservation)
                self.assertEqual(payload["turtle_model_version"], "gpt-5-5")
                self.assertEqual(payload["turtle_thinking_level"], "instant")
                self.assertEqual(store.quota_summary("user", "user")["request_count"], 0)
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_internal_task_does_not_claim_foreground_chat_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {
                    "model": "gpt-5-web",
                    "turtle_chat_id": "stale-client-value",
                }
                with patch(
                    "open_webui.models.chats.Chats.get_chat_by_id",
                    new=AsyncMock(return_value=None),
                ):
                    context = await metering.prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        payload,
                        internal_task=True,
                        chat_id="foreground-chat",
                    )
                self.assertIsNotNone(context)
                self.assertIsNone(context.reservation)
                self.assertNotIn("turtle_chat_id", payload)
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_claude_uses_independent_fields_and_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {"model": "claude-web"}
                context = await metering.prepare_chat_request(
                    SimpleNamespace(id="user", role="user"),
                    payload,
                )
                self.assertEqual(context.selection_key, "claude-sonnet-5:standard")
                self.assertEqual(payload["turtle_claude_model"], "claude-sonnet-5")
                self.assertEqual(payload["turtle_claude_thinking"], "standard")
                self.assertEqual(
                    payload["turtle_account_pool_id"],
                    "claude-default",
                )
                self.assertEqual(payload["turtle_user_id"], "user")
                self.assertEqual(
                    payload["turtle_request_id"],
                    context.lease.request_id,
                )
                self.assertNotIn("turtle_chat_id", payload)
                await metering.release_chat_request(context, outcome="test_cleanup")

                with self.assertRaises(HTTPException) as denied:
                    await metering.prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        {
                            "model": "claude-web",
                            "turtle_claude_model": "claude-opus-4-8",
                            "turtle_claude_thinking": "extended",
                        },
                    )
                self.assertEqual(denied.exception.status_code, 403)
            finally:
                metering.CHAT_STORE = original

    async def test_gpt_request_injects_only_opaque_account_routing_hints(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            group = store.create_group(
                name="routed users",
                description="test",
                rules=ChatStoreTests._group_rules(),
                storage_quota_bytes=2 * 1024**3,
                max_concurrency=2,
                default_user_concurrency=1,
                gpt_account_pool_id="pool-private",
                updated_by="admin",
            )
            store.assign_group("opaque-user", group["id"], assigned_by="admin")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {
                    "model": "gpt-5-web",
                    "turtle_model_version": "latest",
                    "turtle_thinking_level": "medium",
                }
                with patch(
                    "open_webui.models.chats.Chats.get_chat_by_id",
                    new=AsyncMock(return_value=None),
                ):
                    context = await metering.prepare_chat_request(
                        SimpleNamespace(id="opaque-user", role="user"),
                        payload,
                        chat_id="opaque-chat",
                    )
                self.assertEqual(payload["turtle_account_pool_id"], "pool-private")
                self.assertEqual(payload["turtle_user_id"], "opaque-user")
                self.assertEqual(payload["turtle_chat_id"], "opaque-chat")
                self.assertEqual(payload["turtle_request_id"], context.lease.request_id)
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_claude_internal_task_defers_to_worker_verified_default(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {"model": "claude-web"}
                context = await metering.prepare_chat_request(
                    SimpleNamespace(id="user", role="user"),
                    payload,
                    internal_task=True,
                )
                self.assertIsNotNone(context)
                self.assertIsNone(context.reservation)
                self.assertNotIn("turtle_claude_model", payload)
                self.assertNotIn("turtle_claude_thinking", payload)
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_stream_commits_only_after_effective_content(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            reservation = store.reserve(
                "user", "user", version="latest", level="medium"
            )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                async def source():
                    yield b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
                    yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                    yield b"data: [DONE]\n\n"

                chunks = [chunk async for chunk in tracked_chat_stream(source(), reservation)]
                self.assertEqual(len(chunks), 3)
                self.assertEqual(store.quota_summary("user", "user")["request_count"], 1)
            finally:
                metering.CHAT_STORE = original

    async def test_zero_content_stream_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            reservation = store.reserve(
                "user", "user", version="latest", level="medium"
            )
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                async def source():
                    yield b"data: [DONE]\n\n"

                _ = [chunk async for chunk in tracked_chat_stream(source(), reservation)]
                summary = store.quota_summary("user", "user")
                self.assertEqual(summary["request_count"], 0)
                with store._connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT status FROM chat_usage WHERE id = ?",
                            (reservation.id,),
                        ).fetchone()[0],
                        "released",
                    )
            finally:
                metering.CHAT_STORE = original

    async def test_prepare_mutates_an_exhausted_selection_to_its_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            rules = ChatStoreTests._group_rules()
            group = store.create_group(
                name="fallback",
                description="test",
                rules=rules,
                storage_quota_bytes=2 * 1024**3,
                max_concurrency=2,
                default_user_concurrency=1,
                updated_by="admin",
            )
            store.assign_group("user", group["id"], assigned_by="admin")
            first = store.reserve("user", "user", version="latest", level="medium")
            store.finalize(first.id, "committed")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                payload = {
                    "model": "gpt-5-web",
                    "turtle_model_version": "latest",
                    "turtle_thinking_level": "medium",
                }
                context = await prepare_chat_request(
                    SimpleNamespace(id="user", role="user"), payload
                )
                self.assertEqual(context.selection_key, "gpt-5-5:instant")
                self.assertEqual(payload["turtle_model_version"], "gpt-5-5")
                self.assertEqual(payload["turtle_thinking_level"], "instant")
                await metering.release_chat_request(context, outcome="test_cleanup")
            finally:
                metering.CHAT_STORE = original

    async def test_exhausted_fallback_chain_returns_reset_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            from . import metering

            store = ChatStore(Path(temp) / "chat.db")
            group = store.create_group(
                name="all exhausted",
                description="test",
                rules=ChatStoreTests._group_rules(instant_limit=1),
                storage_quota_bytes=2 * 1024**3,
                max_concurrency=2,
                default_user_concurrency=1,
                updated_by="admin",
            )
            store.assign_group("user", group["id"], assigned_by="admin")
            medium = store.reserve("user", "user", version="latest", level="medium")
            store.finalize(medium.id, "committed")
            instant = store.reserve("user", "user", version="gpt-5-5", level="instant")
            store.finalize(instant.id, "committed")
            original = metering.CHAT_STORE
            metering.CHAT_STORE = store
            try:
                with self.assertRaises(HTTPException) as denied:
                    await prepare_chat_request(
                        SimpleNamespace(id="user", role="user"),
                        {
                            "model": "gpt-5-web",
                            "turtle_model_version": "latest",
                            "turtle_thinking_level": "medium",
                        },
                    )
                self.assertEqual(denied.exception.status_code, 429)
                self.assertEqual(
                    denied.exception.detail["code"], "chat_model_quota_exceeded"
                )
                self.assertIsInstance(denied.exception.detail["reset_at"], int)
            finally:
                metering.CHAT_STORE = original

    def test_nonstream_content_detection(self):
        self.assertTrue(
            response_has_effective_content(
                {"choices": [{"message": {"content": "hello"}}]}
            )
        )
        self.assertFalse(
            response_has_effective_content(
                {"choices": [{"message": {"content": ""}}]}
            )
        )


if __name__ == "__main__":
    unittest.main()
