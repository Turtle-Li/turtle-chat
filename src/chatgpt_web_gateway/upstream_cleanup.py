"""Tracked cleanup for ChatGPT resources created by Turtle.

Only opaque identifiers captured from successful Turtle responses enter this
store. The cleanup worker never lists an account, so unrelated personal chats
and files cannot become cleanup candidates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from .account_pool import AccountPoolRouter
from .upstream import UpstreamFailure, UpstreamResourceMetadata


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True, slots=True)
class CleanupResource:
    id: int
    account_id: str
    pool_id: str
    resource_type: str
    resource_id: str
    attempts: int


class PostgresUpstreamCleanupStore:
    def __init__(
        self,
        database_url: str,
        *,
        ttl_seconds: int,
        conversation_action: str,
        connection_pool: Any | None = None,
    ) -> None:
        self.database_url = database_url
        self.ttl_seconds = int(ttl_seconds)
        self.conversation_action = str(conversation_action)
        self._owns_connection_pool = connection_pool is None
        if connection_pool is not None:
            self._connection_pool = connection_pool
            return
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - production dependency
            raise RuntimeError(
                "psycopg and psycopg_pool are required for upstream cleanup"
            ) from exc

        # Successful streams record only opaque upstream resource identifiers,
        # but that write happens before [DONE]. Reopening PostgreSQL for every
        # response adds avoidable tail latency, so reuse a small bounded pool.
        self._connection_pool = ConnectionPool(
            conninfo=self.database_url,
            kwargs={"row_factory": dict_row},
            min_size=1,
            max_size=16,
            timeout=5.0,
            max_idle=300.0,
            max_lifetime=1800.0,
            check=ConnectionPool.check_connection,
            name="turtle-upstream-cleanup",
            open=True,
        )

    def _connect(self):
        return self._connection_pool.connection()

    def close(self) -> None:
        if self._owns_connection_pool:
            self._connection_pool.close(timeout=5.0)

    def initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS chat_upstream_resource (
                id BIGSERIAL PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'gpt'
                    CHECK (provider = 'gpt'),
                account_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                user_id TEXT,
                chat_id TEXT,
                resource_type TEXT NOT NULL
                    CHECK (
                        resource_type IN (
                            'conversation',
                            'conversation_cache',
                            'input_file',
                            'generated_asset'
                        )
                    ),
                resource_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active'
                    CHECK (
                        state IN (
                            'active',
                            'pending',
                            'processing',
                            'retry',
                            'deleted'
                        )
                    ),
                delete_reason TEXT,
                first_seen_at BIGINT NOT NULL,
                last_seen_at BIGINT NOT NULL,
                delete_after BIGINT NOT NULL,
                next_attempt_at BIGINT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error_class TEXT,
                last_attempt_at BIGINT,
                deleted_at BIGINT,
                updated_at BIGINT NOT NULL,
                UNIQUE(account_id, resource_type, resource_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_upstream_resource_due_idx
                ON chat_upstream_resource (state, next_attempt_at, delete_after)
                WHERE state IN ('active', 'pending', 'retry')
            """,
            """
            ALTER TABLE chat_upstream_resource
                DROP CONSTRAINT IF EXISTS
                    chat_upstream_resource_resource_type_check
            """,
            """
            ALTER TABLE chat_upstream_resource
                ADD CONSTRAINT chat_upstream_resource_resource_type_check
                CHECK (
                    resource_type IN (
                        'conversation',
                        'conversation_cache',
                        'input_file',
                        'generated_asset'
                    )
                )
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_upstream_resource_chat_idx
                ON chat_upstream_resource (chat_id)
                WHERE state <> 'deleted'
            """,
            """
            CREATE INDEX IF NOT EXISTS chat_upstream_resource_user_idx
                ON chat_upstream_resource (user_id)
                WHERE state <> 'deleted'
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_upstream_cleanup_config (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                retention_seconds BIGINT NOT NULL
                    CHECK (
                        retention_seconds >= 300
                        AND retention_seconds <= 31536000
                    ),
                conversation_action TEXT NOT NULL
                    CHECK (conversation_action IN ('archive', 'delete')),
                updated_by TEXT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            now = int(time.time())
            cursor.execute(
                """
                INSERT INTO chat_upstream_cleanup_config (
                    id, retention_seconds, conversation_action,
                    updated_by, updated_at
                )
                VALUES (1, %s, %s, 'deployment_default', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (self.ttl_seconds, self.conversation_action, now),
            )
            cursor.execute(
                """
                SELECT retention_seconds, conversation_action
                  FROM chat_upstream_cleanup_config
                 WHERE id = 1
                """
            )
            row = cursor.fetchone()
            if row is not None:
                self.ttl_seconds = int(row["retention_seconds"])
                self.conversation_action = str(row["conversation_action"])
            connection.commit()

    def policy(self) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT retention_seconds, conversation_action,
                       updated_by, updated_at
                  FROM chat_upstream_cleanup_config
                 WHERE id = 1
                """
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("上游清理策略尚未初始化")
        self.ttl_seconds = int(row["retention_seconds"])
        self.conversation_action = str(row["conversation_action"])
        return {
            "retention_seconds": self.ttl_seconds,
            "conversation_action": self.conversation_action,
            "updated_by": str(row["updated_by"]),
            "updated_at": int(row["updated_at"]),
        }

    def update_policy(
        self,
        *,
        retention_seconds: int,
        conversation_action: str,
        updated_by: str,
    ) -> dict[str, Any]:
        retention_seconds = int(retention_seconds)
        conversation_action = str(conversation_action)
        if not 300 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("retention_seconds is out of range")
        if conversation_action not in {"archive", "delete"}:
            raise ValueError("invalid conversation_action")
        now = int(time.time())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_upstream_cleanup_config (
                    id, retention_seconds, conversation_action,
                    updated_by, updated_at
                )
                VALUES (1, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    retention_seconds = excluded.retention_seconds,
                    conversation_action = excluded.conversation_action,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    retention_seconds,
                    conversation_action,
                    str(updated_by)[:120],
                    now,
                ),
            )
            cursor.execute(
                """
                UPDATE chat_upstream_resource
                   SET delete_after = last_seen_at + %s,
                       updated_at = %s
                 WHERE state = 'active'
                """,
                (retention_seconds, now),
            )
            affected_resources = cursor.rowcount
            connection.commit()
        self.ttl_seconds = retention_seconds
        self.conversation_action = conversation_action
        return {
            "retention_seconds": retention_seconds,
            "conversation_action": conversation_action,
            "updated_by": str(updated_by)[:120],
            "updated_at": now,
            "active_resources_rescheduled": affected_resources,
        }

    def record(
        self,
        *,
        account_id: str,
        pool_id: str,
        user_id: str | None,
        chat_id: str | None,
        metadata: UpstreamResourceMetadata,
        ttl_seconds: int | None = None,
    ) -> int:
        resources: list[tuple[str, str]] = []
        if metadata.conversation_id:
            resources.append(("conversation", metadata.conversation_id))
        if metadata.conversation_cache_key:
            resources.append(
                ("conversation_cache", metadata.conversation_cache_key)
            )
        resources.extend(("input_file", value) for value in metadata.input_file_ids)
        resources.extend(
            ("generated_asset", value)
            for value in metadata.generated_asset_ids
        )
        if not resources:
            return 0
        now = int(time.time())
        retention_seconds = (
            self.ttl_seconds
            if ttl_seconds is None
            else max(60, min(self.ttl_seconds, int(ttl_seconds)))
        )
        delete_after = now + retention_seconds
        with self._connect() as connection, connection.cursor() as cursor:
            for resource_type, resource_id in resources:
                cursor.execute(
                    """
                    INSERT INTO chat_upstream_resource (
                        provider, account_id, pool_id, user_id, chat_id,
                        resource_type, resource_id, state,
                        first_seen_at, last_seen_at, delete_after, updated_at
                    )
                    VALUES (
                        'gpt', %s, %s, %s, %s, %s, %s, 'active',
                        %s, %s, %s, %s
                    )
                    ON CONFLICT(account_id, resource_type, resource_id)
                    DO UPDATE SET
                        pool_id = excluded.pool_id,
                        user_id = excluded.user_id,
                        chat_id = excluded.chat_id,
                        state = 'active',
                        delete_reason = NULL,
                        last_seen_at = excluded.last_seen_at,
                        delete_after = excluded.delete_after,
                        next_attempt_at = NULL,
                        attempts = 0,
                        last_error_class = NULL,
                        deleted_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_id,
                        pool_id,
                        user_id,
                        chat_id,
                        resource_type,
                        resource_id,
                        now,
                        now,
                        delete_after,
                        now,
                    ),
                )
            connection.commit()
        return len(resources)

    def schedule(
        self,
        *,
        chat_id: str | None,
        user_id: str | None,
        reason: str,
    ) -> int:
        if not chat_id and not user_id:
            return 0
        now = int(time.time())
        clauses: list[str] = []
        parameters: list[Any] = [reason, now, now]
        if chat_id:
            clauses.append("chat_id = %s")
            parameters.append(chat_id)
        if user_id:
            clauses.append("user_id = %s")
            parameters.append(user_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE chat_upstream_resource
                   SET state = 'pending', delete_reason = %s,
                       next_attempt_at = %s, updated_at = %s
                 WHERE state <> 'deleted'
                   AND ({' AND '.join(clauses)})
                """,
                tuple(parameters),
            )
            count = cursor.rowcount
            connection.commit()
        return count

    def mark_due(self) -> dict[str, int]:
        now = int(time.time())
        recovered_count = 0
        ttl_count = 0
        orphan_count = 0
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chat_upstream_resource
                   SET state = 'retry', next_attempt_at = %s,
                       last_error_class = 'cleanup_interrupted',
                       updated_at = %s
                 WHERE state = 'processing'
                   AND COALESCE(last_attempt_at, 0) <= %s
                """,
                (now, now, now - 15 * 60),
            )
            recovered_count = cursor.rowcount
            cursor.execute(
                """
                UPDATE chat_upstream_resource
                   SET state = 'pending', delete_reason = 'retention_ttl',
                       next_attempt_at = %s, updated_at = %s
                 WHERE state = 'active' AND delete_after <= %s
                """,
                (now, now, now),
            )
            ttl_count = cursor.rowcount
            cursor.execute("SELECT to_regclass('public.chat') AS chat_table")
            row = cursor.fetchone()
            if row and row["chat_table"] is not None:
                cursor.execute(
                    """
                    UPDATE chat_upstream_resource AS resource
                       SET state = 'pending',
                           delete_reason = 'local_chat_missing',
                           next_attempt_at = %s,
                           updated_at = %s
                     WHERE resource.state = 'active'
                       AND resource.chat_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1
                             FROM chat AS local_chat
                            WHERE local_chat.id = resource.chat_id
                       )
                    """,
                    (now, now),
                )
                orphan_count = cursor.rowcount
            connection.commit()
        return {
            "recovered": recovered_count,
            "ttl": ttl_count,
            "orphan": orphan_count,
        }

    def claim_due(self, limit: int) -> list[CleanupResource]:
        now = int(time.time())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, account_id, pool_id, resource_type, resource_id,
                       attempts
                  FROM chat_upstream_resource
                 WHERE state IN ('pending', 'retry')
                   AND COALESCE(next_attempt_at, 0) <= %s
                 ORDER BY
                       CASE resource_type
                           WHEN 'conversation' THEN 0
                           ELSE 1
                       END,
                       id
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (now, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            if rows:
                cursor.execute(
                    """
                    UPDATE chat_upstream_resource
                       SET state = 'processing', attempts = attempts + 1,
                           last_attempt_at = %s, updated_at = %s
                     WHERE id = ANY(%s)
                    """,
                    (now, now, [int(row["id"]) for row in rows]),
                )
            connection.commit()
        return [
            CleanupResource(
                id=int(row["id"]),
                account_id=str(row["account_id"]),
                pool_id=str(row["pool_id"]),
                resource_type=str(row["resource_type"]),
                resource_id=str(row["resource_id"]),
                attempts=int(row["attempts"] or 0) + 1,
            )
            for row in rows
        ]

    def complete(self, resource_id: int) -> None:
        now = int(time.time())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chat_upstream_resource
                   SET state = 'deleted', deleted_at = %s,
                       next_attempt_at = NULL, last_error_class = NULL,
                       updated_at = %s
                 WHERE id = %s AND state = 'processing'
                """,
                (now, now, resource_id),
            )
            connection.commit()

    def retry(
        self,
        resource_id: int,
        *,
        error_class: str,
        attempts: int,
        delay_seconds: int | None = None,
    ) -> None:
        now = int(time.time())
        delay = (
            max(30, int(delay_seconds))
            if delay_seconds is not None
            else min(6 * 60 * 60, 60 * (2 ** min(max(0, attempts - 1), 7)))
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chat_upstream_resource
                   SET state = 'retry', next_attempt_at = %s,
                       last_error_class = %s, updated_at = %s
                 WHERE id = %s AND state = 'processing'
                """,
                (now + delay, error_class[:80], now, resource_id),
            )
            connection.commit()

    def status(self) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT resource_type, state, COUNT(*)::INTEGER AS count
                  FROM chat_upstream_resource
                 GROUP BY resource_type, state
                 ORDER BY resource_type, state
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT MIN(COALESCE(next_attempt_at, delete_after)) AS next_due_at
                  FROM chat_upstream_resource
                 WHERE state IN ('active', 'pending', 'retry')
                """
            )
            next_row = cursor.fetchone()
        return {
            "counts": rows,
            "next_due_at": (
                int(next_row["next_due_at"])
                if next_row and next_row["next_due_at"] is not None
                else None
            ),
        }


class UpstreamCleanupManager:
    def __init__(
        self,
        *,
        database_url: str | None,
        account_pool: AccountPoolRouter,
        enabled: bool,
        execute: bool,
        ttl_seconds: int,
        conversation_action: str,
        interval_seconds: int,
        batch_size: int,
        connection_pool: Any | None = None,
    ) -> None:
        self.enabled = bool(enabled and database_url)
        self.execute = bool(execute and self.enabled)
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.ttl_seconds = int(ttl_seconds)
        self.conversation_action = str(conversation_action)
        self.account_pool = account_pool
        self.store = (
            PostgresUpstreamCleanupStore(
                str(database_url),
                ttl_seconds=ttl_seconds,
                conversation_action=conversation_action,
                connection_pool=connection_pool,
            )
            if self.enabled and database_url
            else None
        )
        self._task: asyncio.Task[None] | None = None
        self.last_run_at: int | None = None
        self.last_result: dict[str, Any] | None = None

    async def start(self) -> None:
        if not self.enabled or self.store is None:
            return
        await asyncio.to_thread(self.store.initialize)
        self._task = asyncio.create_task(
            self._run_forever(),
            name="upstream-resource-cleanup",
        )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.store is not None:
            await asyncio.to_thread(self.store.close)

    async def record(
        self,
        *,
        account_id: str,
        pool_id: str,
        user_id: str | None,
        chat_id: str | None,
        metadata: UpstreamResourceMetadata,
        ttl_seconds: int | None = None,
    ) -> int:
        if not self.enabled or self.store is None or metadata.empty:
            return 0
        try:
            return await asyncio.to_thread(
                self.store.record,
                account_id=account_id,
                pool_id=pool_id,
                user_id=user_id,
                chat_id=chat_id,
                metadata=metadata,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            logger.exception(
                "upstream_cleanup_record_failed account=%s",
                account_id,
            )
            return 0

    async def schedule(
        self,
        *,
        chat_id: str | None,
        user_id: str | None,
        reason: str,
    ) -> int:
        if not self.enabled or self.store is None:
            return 0
        return await asyncio.to_thread(
            self.store.schedule,
            chat_id=chat_id,
            user_id=user_id,
            reason=reason,
        )

    async def status(self) -> dict[str, Any]:
        store_status = (
            await asyncio.to_thread(self.store.status)
            if self.enabled and self.store is not None
            else {"counts": [], "next_due_at": None}
        )
        policy = (
            await asyncio.to_thread(self.store.policy)
            if self.enabled and self.store is not None
            else {
                "retention_seconds": self.ttl_seconds,
                "conversation_action": self.conversation_action,
                "updated_by": "deployment_default",
                "updated_at": None,
            }
        )
        return {
            "enabled": self.enabled,
            "execute": self.execute,
            "mode": "execute" if self.execute else "dry_run",
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "policy": policy,
            **store_status,
        }

    async def update_policy(
        self,
        *,
        retention_seconds: int,
        conversation_action: str,
        updated_by: str,
    ) -> dict[str, Any]:
        if not self.enabled or self.store is None:
            raise RuntimeError("上游清理服务尚未启用")
        policy = await asyncio.to_thread(
            self.store.update_policy,
            retention_seconds=retention_seconds,
            conversation_action=conversation_action,
            updated_by=updated_by,
        )
        self.ttl_seconds = int(policy["retention_seconds"])
        self.conversation_action = str(policy["conversation_action"])
        return {
            "ok": True,
            "enabled": self.enabled,
            "execute": self.execute,
            "policy": policy,
        }

    async def run_once(self) -> dict[str, Any]:
        if not self.enabled or self.store is None:
            result = {
                "marked": {"recovered": 0, "ttl": 0, "orphan": 0},
                "deleted": 0,
                "retried": 0,
            }
            self.last_run_at = int(time.time())
            self.last_result = result
            return result
        marked = await asyncio.to_thread(self.store.mark_due)
        if not self.execute:
            result = {
                "marked": marked,
                "deleted": 0,
                "retried": 0,
                "dry_run": True,
            }
            self.last_run_at = int(time.time())
            self.last_result = result
            return result

        resources = await asyncio.to_thread(
            self.store.claim_due,
            self.batch_size,
        )
        policy = await asyncio.to_thread(self.store.policy)
        conversation_action = str(policy["conversation_action"])
        snapshot = await self.account_pool.snapshot()
        accounts = {
            str(item["id"]): item
            for item in snapshot.get("accounts", [])
            if isinstance(item, dict)
        }
        deleted = 0
        retried = 0
        for resource in resources:
            account_state = accounts.get(resource.account_id)
            if (
                account_state is None
                or account_state.get("provider") != "gpt"
                or int(account_state.get("active") or 0) > 0
                or account_state.get("status") != "ready"
                or account_state.get("session_state") != "valid"
            ):
                await asyncio.to_thread(
                    self.store.retry,
                    resource.id,
                    error_class="account_not_idle",
                    attempts=resource.attempts,
                    delay_seconds=300,
                )
                retried += 1
                continue
            account = await self.account_pool.account(resource.account_id)
            if account is None:
                await asyncio.to_thread(
                    self.store.retry,
                    resource.id,
                    error_class="account_missing",
                    attempts=resource.attempts,
                    delay_seconds=3600,
                )
                retried += 1
                continue
            try:
                client = await self.account_pool.client_for(account)
                await client.cleanup_resource(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    dry_run=False,
                    conversation_action=conversation_action,
                )
            except UpstreamFailure as exc:
                await asyncio.to_thread(
                    self.store.retry,
                    resource.id,
                    error_class=f"upstream_{exc.status_code}",
                    attempts=resource.attempts,
                    delay_seconds=3600 if exc.status_code in {401, 403} else None,
                )
                retried += 1
            except Exception:
                await asyncio.to_thread(
                    self.store.retry,
                    resource.id,
                    error_class="cleanup_worker",
                    attempts=resource.attempts,
                )
                retried += 1
            else:
                await asyncio.to_thread(self.store.complete, resource.id)
                deleted += 1
        result = {
            "marked": marked,
            "claimed": len(resources),
            "deleted": deleted,
            "retried": retried,
            "dry_run": False,
            "conversation_action": conversation_action,
        }
        self.last_run_at = int(time.time())
        self.last_result = result
        return result

    async def _run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("upstream_cleanup_cycle_failed")
            await asyncio.sleep(self.interval_seconds)
