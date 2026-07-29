from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from chatgpt_web_gateway.project_usage import PostgresProjectUsageStore


TEST_DATABASE_URL = os.getenv("PROJECT_USAGE_TEST_DATABASE_URL", "").strip()


def _schema_url(database_url: str, schema: str) -> str:
    parsed = urlsplit(database_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(parsed._replace(query=urlencode(query)))


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "PROJECT_USAGE_TEST_DATABASE_URL is not configured",
)
class PostgresProjectUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from psycopg import sql

        self.schema = f"project_usage_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
        self.store = PostgresProjectUsageStore(
            _schema_url(TEST_DATABASE_URL, self.schema),
            "postgres-project-usage-test-master",
        )
        self.store.initialize()
        self.store.set_permission(
            "user-a",
            enabled=True,
            updated_by="admin-1",
        )
        self.key = self.store.create_key("user-a", "并发项目")

    def tearDown(self) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )

    def _grant_once(self) -> dict:
        return self.store.grant_credit(
            "user-a",
            1_000_000,
            reason="并发发放",
            idempotency_key="postgres-concurrent-grant-0001",
            updated_by="admin-1",
        )

    def _settle(self, index: int) -> dict:
        request_id = f"postgres-concurrent-request-{index:04d}"
        self.store.begin_request(
            self.key["id"],
            request_id,
            authorization_microusd=5_000,
        )
        return self.store.record(self._entry(request_id))

    def _entry(self, request_id: str) -> dict:
        return {
            "request_id": request_id,
            "key_id": self.key["id"],
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

    def test_grants_usage_counts_and_balance_are_atomic(self) -> None:
        with ThreadPoolExecutor(max_workers=12) as executor:
            grants = list(executor.map(lambda _index: self._grant_once(), range(24)))
        self.assertEqual(len({item["id"] for item in grants}), 1)
        self.assertEqual(
            self.store.permission("user-a")["balance_microusd"],
            1_000_000,
        )

        with ThreadPoolExecutor(max_workers=12) as executor:
            settled = list(executor.map(self._settle, range(100)))
        self.assertTrue(all(item["recorded"] for item in settled))
        permission = self.store.permission("user-a")
        self.assertEqual(permission["balance_microusd"], 900_000)
        self.assertEqual(permission["reserved_microusd"], 0)
        self.assertEqual(
            self.store.list_keys("user-a")[0]["request_count"],
            100,
        )
        self.assertEqual(
            len(self.store.list_credit_ledger("user-a", limit=200)),
            101,
        )
        summary = self.store.summary(24, owner_user_id="user-a")
        self.assertEqual(summary["totals"]["requests"], 100)
        self.assertEqual(summary["totals"]["actual_cost_microusd"], 100_000)

        duplicate = self.store.record(
            self._entry("postgres-concurrent-request-0000")
        )
        self.assertFalse(duplicate["recorded"])
        self.assertEqual(
            self.store.permission("user-a")["balance_microusd"],
            900_000,
        )
