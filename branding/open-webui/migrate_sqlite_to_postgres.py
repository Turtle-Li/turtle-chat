"""Offline, count-verified Turtle SQLite to PostgreSQL migration.

Run this only while Open WebUI is stopped and after creating an online SQLite
backup. The command never prints row values, credentials, prompts, or messages.
The target must be a fresh Turtle-owned PostgreSQL database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


CHAT_TABLES = (
    "chat_group",
    "chat_group_rule",
    "chat_model_group",
    "chat_model_group_rule",
    "chat_policy",
    "chat_ledger",
    "chat_usage",
    "chat_quota_window",
    "chat_user_group",
    "chat_user_model_group",
    "chat_user_concurrency",
)
TARGET_EMPTY_GUARDS = (
    "user",
    "auth",
    "chat",
    "chat_message",
    "file",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate a frozen Turtle SQLite snapshot into fresh PostgreSQL",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/app/backend/data"),
        help="directory containing webui.db, turtle-chat.db and turtle-storage.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the migration; without this flag only preflight checks run",
    )
    return parser.parse_args()


def sqlite_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RuntimeError(f"required SQLite source is missing: {path.name}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name
            """
        )
    ]


def sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def pg_ident(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise RuntimeError("unsafe SQL identifier in migration schema")
    return f'"{value}"'


def target_columns(connection) -> dict[str, dict[str, str]]:
    rows = connection.execute(
        """
        SELECT table_name, column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = current_schema()
         ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    result: dict[str, dict[str, str]] = {}
    for table, column, data_type in rows:
        result.setdefault(str(table), {})[str(column)] = str(data_type)
    return result


def target_table_count(connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {pg_ident(table)}").fetchone()[0])


def adapt_value(value: Any, data_type: str):
    if value is None:
        return None
    if data_type == "boolean":
        return bool(value)
    if data_type in {"json", "jsonb"}:
        from psycopg.types.json import Json, Jsonb

        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
        return Jsonb(value) if data_type == "jsonb" else Json(value)
    if data_type == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if data_type == "timestamp without time zone" and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    return value


def copy_sqlite_tables(
    source: sqlite3.Connection,
    target,
    tables: list[str],
    schema: dict[str, dict[str, str]],
) -> dict[str, int]:
    copied: dict[str, int] = {}
    for table in tables:
        source_columns = sqlite_columns(source, table)
        target_definition = schema.get(table)
        if target_definition is None:
            raise RuntimeError(f"target schema is missing table {table}")
        missing = [column for column in source_columns if column not in target_definition]
        if missing:
            raise RuntimeError(f"target table {table} is missing source columns")
        rows = source.execute(f"SELECT * FROM {pg_ident(table)}").fetchall()
        if rows:
            placeholders = ", ".join("%s" for _ in source_columns)
            column_sql = ", ".join(pg_ident(column) for column in source_columns)
            statement = (
                f"INSERT INTO {pg_ident(table)} ({column_sql}) "
                f"VALUES ({placeholders})"
            )
            values = []
            for row in rows:
                converted = []
                for column in source_columns:
                    try:
                        converted.append(
                            adapt_value(row[column], target_definition[column])
                        )
                    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"source value cannot be converted for {table}.{column}"
                        ) from exc
                values.append(tuple(converted))
            with target.cursor() as cursor:
                cursor.executemany(statement, values)
        copied[table] = len(rows)
    return copied


def validate_counts(target, copied: dict[str, int]) -> None:
    mismatches = [
        table
        for table, expected in copied.items()
        if target_table_count(target, table) != expected
    ]
    if mismatches:
        raise RuntimeError(
            "row-count validation failed for: " + ", ".join(sorted(mismatches))
        )


def load_storage_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("required storage configuration is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("storage configuration cannot be read") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("storage configuration has an invalid shape")
    return payload


def main() -> int:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    webui_path = source_dir / "webui.db"
    chat_path = source_dir / "turtle-chat.db"
    storage_path = source_dir / "turtle-storage.json"

    webui_source = sqlite_connection(webui_path)
    chat_source = sqlite_connection(chat_path)
    storage_payload = load_storage_config(storage_path)

    # Importing config runs the pinned Open WebUI Alembic migrations against
    # the configured PostgreSQL target. Importing Turtle stores then creates
    # the product-owned tables before preflight checks begin.
    from open_webui import config as _open_webui_config  # noqa: F401
    from open_webui.env import DATABASE_URL
    from open_webui.turtle_chat.store import ChatStore
    from open_webui.turtle_storage.core import ConfigStore
    from open_webui.turtle_database import is_postgres_url

    if not is_postgres_url(DATABASE_URL):
        raise RuntimeError("migration target must be PostgreSQL")
    ChatStore(database_url=DATABASE_URL)
    ConfigStore(database_url=DATABASE_URL)

    import psycopg

    psycopg_url = str(DATABASE_URL).replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    target = psycopg.connect(psycopg_url)
    try:
        schema = target_columns(target)
        main_tables = [
            table
            for table in sqlite_tables(webui_source)
            if table != "alembic_version"
        ]
        chat_tables = [
            table for table in CHAT_TABLES if table in sqlite_tables(chat_source)
        ]
        for table in (*main_tables, *chat_tables, "turtle_storage_config", "turtle_storage_user_quota"):
            if table not in schema:
                raise RuntimeError(f"target schema is missing table {table}")

        source_revision = webui_source.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        target_revision = target.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        if source_revision != target_revision:
            raise RuntimeError("source and target Open WebUI schema revisions differ")

        occupied = [
            table
            for table in TARGET_EMPTY_GUARDS
            if target_table_count(target, table) > 0
        ]
        if occupied:
            raise RuntimeError(
                "target PostgreSQL database already contains user data; refusing migration"
            )

        main_total = sum(
            int(webui_source.execute(f"SELECT COUNT(*) FROM {pg_ident(table)}").fetchone()[0])
            for table in main_tables
        )
        chat_total = sum(
            int(chat_source.execute(f"SELECT COUNT(*) FROM {pg_ident(table)}").fetchone()[0])
            for table in chat_tables
        )
        quota_users = storage_payload.get("quota", {}).get("users", {})
        normalized_quota_users = {
            str(user_id): assignment
            for user_id, assignment in quota_users.items()
            if isinstance(assignment, dict)
        } if isinstance(quota_users, dict) else {}
        quota_user_count = len(normalized_quota_users)
        print(
            f"preflight ok: main_tables={len(main_tables)} main_rows={main_total} "
            f"chat_tables={len(chat_tables)} chat_rows={chat_total} "
            f"storage_users={quota_user_count} schema={source_revision}"
        )
        if not args.apply:
            print("dry-run only; rerun with --apply while Open WebUI is stopped")
            target.rollback()
            return 0

        target.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("turtle-sqlite-migration",))
        target.execute("SET LOCAL session_replication_role = replica")

        truncate_tables = [*main_tables, *CHAT_TABLES, "turtle_storage_config", "turtle_storage_user_quota"]
        target.execute(
            "TRUNCATE TABLE "
            + ", ".join(pg_ident(table) for table in truncate_tables)
            + " RESTART IDENTITY CASCADE"
        )

        copied = copy_sqlite_tables(webui_source, target, main_tables, schema)
        copied.update(copy_sqlite_tables(chat_source, target, chat_tables, schema))

        persisted_config = json.loads(json.dumps(storage_payload))
        raw_users = persisted_config.setdefault("quota", {}).get("users", {})
        persisted_config["quota"]["users"] = {}
        from psycopg.types.json import Jsonb

        target.execute(
            """
            INSERT INTO turtle_storage_config (id, payload, updated_at)
            VALUES (1, %s, %s)
            """,
            (Jsonb(persisted_config), int(dt.datetime.now().timestamp())),
        )
        for user_id, assignment in normalized_quota_users.items():
            target.execute(
                """
                INSERT INTO turtle_storage_user_quota
                    (user_id, tier, quota_bytes, updated_at)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    user_id,
                    str(assignment.get("tier") or "free"),
                    assignment.get("quota_bytes"),
                    int(dt.datetime.now().timestamp()),
                ),
            )

        validate_counts(target, copied)
        if target_table_count(target, "turtle_storage_user_quota") != quota_user_count:
            raise RuntimeError("storage quota assignment count validation failed")
        target.execute("SET LOCAL session_replication_role = origin")
        target.commit()
        print(
            f"migration committed: main_rows={sum(copied.get(table, 0) for table in main_tables)} "
            f"chat_rows={sum(copied.get(table, 0) for table in chat_tables)} "
            f"storage_users={quota_user_count}"
        )
        return 0
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        webui_source.close()
        chat_source.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        safe_message = str(exc) if isinstance(exc, RuntimeError) else "unexpected migration error"
        print(f"migration failed: {type(exc).__name__}: {safe_message}", file=sys.stderr)
        raise SystemExit(1)
