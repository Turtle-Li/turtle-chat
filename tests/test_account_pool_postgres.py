from __future__ import annotations

import os
import unittest
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from chatgpt_web_gateway.account_pool import (
    AccountPoolConflict,
    PostgresAccountStore,
    _new_account,
    _now,
)


TEST_DATABASE_URL = os.getenv("ACCOUNT_POOL_TEST_DATABASE_URL", "").strip()


def _schema_url(database_url: str, schema: str) -> str:
    parsed = urlsplit(database_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(parsed._replace(query=urlencode(query)))


@unittest.skipUnless(TEST_DATABASE_URL, "ACCOUNT_POOL_TEST_DATABASE_URL is not configured")
class PostgresAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from psycopg import sql

        self.schema = f"account_pool_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.store = PostgresAccountStore(_schema_url(TEST_DATABASE_URL, self.schema))
        self.store.initialize(
            _new_account(
                "acct-primary",
                pool_id="gpt-default",
                name="主账号",
                worker_endpoint="http://worker.test:8320/v1",
                health_path="/healthz",
                max_concurrency=1,
                priority=10,
                deployment_managed=True,
            )
        )

    def tearDown(self) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )

    def test_schema_snapshot_lease_and_state_transitions(self) -> None:
        initial = self.store.snapshot()
        self.assertEqual(initial["backend"], "postgresql")
        self.assertEqual(initial["pools"][0]["capacity"], 1)
        self.assertEqual(initial["pools"][0]["admission_capacity"], 1)
        self.assertEqual(initial["pools"][0]["available_slots"], 1)
        self.assertEqual(initial["pools"][0]["ready_count"], 1)

        account = self.store.acquire(
            pool_id="gpt-default",
            request_id="request-primary",
            user_id="user-a",
            chat_id="chat-a",
            lease_seconds=60,
        )
        self.assertEqual(account.id, "acct-primary")
        self.assertTrue(
            self.store.renew(
                "request-primary",
                "acct-primary",
                lease_seconds=60,
            )
        )
        busy = self.store.snapshot()
        self.assertEqual(busy["accounts"][0]["active"], 1)
        self.assertEqual(busy["pools"][0]["ready_count"], 0)
        self.assertEqual(busy["pools"][0]["admission_capacity"], 1)
        self.assertEqual(busy["pools"][0]["available_slots"], 0)

        self.store.release(
            "request-primary",
            "acct-primary",
            outcome="error",
            status_code=401,
            error_class="upstream_request",
            cooldown_seconds=30,
        )
        failed = self.store.snapshot()
        self.assertEqual(failed["accounts"][0]["active"], 0)
        self.assertEqual(failed["accounts"][0]["status"], "reauth_required")
        self.assertEqual(failed["accounts"][0]["session_state"], "expired")
        self.assertEqual(failed["pools"][0]["admission_capacity"], 0)

    def test_quota_aware_balance_affinity_and_lane_cooldown_are_durable(self) -> None:
        primary = self.store.snapshot()["accounts"][0]
        self.store.update_account(
            "acct-primary",
            name=primary["name"],
            worker_endpoint=primary["worker_endpoint"],
            health_path=primary["health_path"],
            max_concurrency=1,
            priority=10,
            enabled=True,
            quota_profile="plus",
        )
        secondary = self.store.create_account(
            pool_id="gpt-default",
            name="备用账号",
            worker_endpoint="http://worker.test:8321/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=0,
            quota_profile="plus",
        )
        self.store.mark_probe(
            secondary["id"],
            state="ready",
            http_status=200,
            latency_ms=1,
        )
        secondary = self.store.update_account(
            secondary["id"],
            name=secondary["name"],
            worker_endpoint=secondary["worker_endpoint"],
            health_path=secondary["health_path"],
            max_concurrency=1,
            priority=0,
            enabled=True,
            quota_profile="plus",
        )

        first = self.store.acquire(
            pool_id="gpt-default",
            request_id="request-balance-first",
            user_id="user-a",
            chat_id="chat-a",
            lease_seconds=60,
            selection_key="latest:medium",
        )
        self.store.release(
            "request-balance-first",
            first.id,
            outcome="success",
            status_code=200,
            error_class=None,
            cooldown_seconds=30,
        )
        sticky = self.store.acquire(
            pool_id="gpt-default",
            request_id="request-balance-sticky",
            user_id="user-a",
            chat_id="chat-a",
            lease_seconds=60,
            selection_key="latest:medium",
        )
        self.assertEqual(sticky.id, first.id)
        self.store.release(
            "request-balance-sticky",
            sticky.id,
            outcome="success",
            status_code=200,
            error_class=None,
            cooldown_seconds=30,
        )
        other = self.store.acquire(
            pool_id="gpt-default",
            request_id="request-balance-other",
            user_id="user-b",
            chat_id="chat-b",
            lease_seconds=60,
            selection_key="latest:medium",
        )
        self.assertNotEqual(other.id, first.id)
        self.store.release(
            "request-balance-other",
            other.id,
            outcome="error",
            status_code=429,
            error_class="upstream_request",
            cooldown_seconds=30,
        )

        snapshot = self.store.snapshot()
        limited = next(item for item in snapshot["accounts"] if item["id"] == other.id)
        lanes = {
            item["selection_key"]: item for item in limited["quota"]["lanes"]
        }
        self.assertEqual(limited["status"], "ready")
        self.assertEqual(lanes["latest:medium"]["state"], "cooldown")
        self.assertTrue(lanes["latest:high"]["available"])
        self.assertEqual(snapshot["pools"][0]["sticky_chat_count"], 2)

        with self.store._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chat_account_lane_state
                   SET blocked_until = %s
                 WHERE account_id = %s AND selection_key = %s
                """,
                (_now() - 1, other.id, "latest:medium"),
            )
            connection.commit()

        still_isolated = self.store.snapshot()
        isolated = next(
            item for item in still_isolated["accounts"] if item["id"] == other.id
        )
        isolated_lane = next(
            item
            for item in isolated["quota"]["lanes"]
            if item["selection_key"] == "latest:medium"
        )
        self.assertEqual(isolated_lane["state"], "cooldown")
        self.assertFalse(isolated_lane["available"])

        recoveries = self.store.claim_rate_limit_recoveries(
            limit=1,
            claim_seconds=60,
        )
        self.assertEqual(len(recoveries), 1)
        recovery = recoveries[0]
        self.assertEqual(recovery.account_id, other.id)
        self.assertEqual(recovery.selection_key, "latest:medium")
        claimed = self.store.snapshot()
        claimed_account = next(
            item for item in claimed["accounts"] if item["id"] == other.id
        )
        self.assertEqual(claimed_account["active"], 1)
        claimed_lane = next(
            item
            for item in claimed_account["quota"]["lanes"]
            if item["selection_key"] == "latest:medium"
        )
        self.assertFalse(claimed_lane["available"])

        self.store.release(
            recovery.request_id,
            recovery.account_id,
            outcome="success",
            status_code=200,
            error_class=None,
            cooldown_seconds=30,
        )
        recovered = self.store.snapshot()
        recovered_account = next(
            item for item in recovered["accounts"] if item["id"] == other.id
        )
        self.assertEqual(recovered_account["active"], 0)
        recovered_lane = next(
            item
            for item in recovered_account["quota"]["lanes"]
            if item["selection_key"] == "latest:medium"
        )
        self.assertTrue(recovered_lane["available"])

    def test_delete_pool_rejects_accounts_and_policy_references(self) -> None:
        empty = self.store.create_pool(
            provider="gpt",
            name="Postgres 空池",
            description="safe deletion",
        )
        self.assertTrue(self.store.delete_pool(empty["id"])["deleted"])

        referenced = self.store.create_pool(
            provider="gpt",
            name="Postgres 引用池",
            description="bound by policy",
        )
        with self.store._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE chat_model_group (
                    id TEXT PRIMARY KEY,
                    account_pool_id TEXT
                )
                """
            )
            cursor.execute(
                "INSERT INTO chat_model_group (id, account_pool_id) VALUES (%s, %s)",
                ("group-a", referenced["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(
            AccountPoolConflict, "仍被模型或资源分组引用"
        ):
            self.store.delete_pool(referenced["id"])

        occupied = self.store.create_pool(
            provider="gpt",
            name="Postgres 有账号池",
            description="contains account",
        )
        self.store.create_account(
            pool_id=occupied["id"],
            name="备用账号",
            worker_endpoint="http://worker.test:8399/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=0,
        )
        with self.assertRaisesRegex(AccountPoolConflict, "仍有账号"):
            self.store.delete_pool(occupied["id"])


if __name__ == "__main__":
    unittest.main()
