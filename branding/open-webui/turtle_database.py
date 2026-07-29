"""Shared PostgreSQL plumbing for Turtle-owned durable state.

Open WebUI already owns the primary ``DATABASE_URL`` and its Alembic schema.
Turtle tables use the same dedicated PostgreSQL database, but keep their own
table names and initialization so a pinned Open WebUI upgrade cannot silently
drop product-specific quota or storage state.

SQLite remains available only when an explicit file path is passed by a unit
test or migration tool. Production selects PostgreSQL through ``DATABASE_URL``.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator, Mapping
from typing import Any


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENGINE_CACHE: dict[str, Any] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def runtime_database_url() -> str:
    """Return Turtle's explicit URL or Open WebUI's resolved database URL."""

    override = str(os.getenv("TURTLE_DATABASE_URL") or "").strip()
    if override:
        return override
    try:
        from open_webui.env import DATABASE_URL

        return str(DATABASE_URL or "").strip()
    except ImportError:
        return str(os.getenv("DATABASE_URL") or "").strip()


def is_postgres_url(value: str | None) -> bool:
    normalized = str(value or "").lower()
    return normalized.startswith(("postgresql://", "postgresql+psycopg://", "postgres://"))


def normalized_postgres_url(value: str) -> str:
    """Force psycopg v3 so sync and async Open WebUI paths use one driver."""

    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError("Turtle durable storage requires a PostgreSQL URL")
    return value


def quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(str(value)):
        raise ValueError("invalid SQL identifier")
    return f'"{value}"'


class HybridRow(Mapping[str, Any]):
    """Small row object compatible with sqlite3.Row and ``dict(row)``."""

    __slots__ = ("_columns", "_index", "_values")

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]):
        self._columns = columns
        self._index = {name: index for index, name in enumerate(columns)}
        self._values = values

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)


def _hybrid_row_factory(cursor):
    if cursor.description is None:
        return tuple
    columns = tuple(column.name for column in cursor.description)

    def make_row(values):
        return HybridRow(columns, tuple(values))

    return make_row


def _adapt_qmark_sql(statement: str) -> str:
    # Turtle SQL contains no question marks in literals. Keeping qmark in the
    # business layer lets the same statements run against SQLite unit tests.
    return statement.replace("?", "%s")


def _engine(database_url: str):
    normalized = normalized_postgres_url(database_url)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(normalized)
        if cached is not None:
            return cached
        from sqlalchemy import create_engine

        pool_size = max(1, int(os.getenv("TURTLE_DATABASE_POOL_SIZE", "5")))
        max_overflow = max(0, int(os.getenv("TURTLE_DATABASE_POOL_MAX_OVERFLOW", "5")))
        created = create_engine(
            normalized,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        _ENGINE_CACHE[normalized] = created
        return created


def dispose_postgres_engine(database_url: str) -> None:
    """Dispose one explicitly scoped engine, primarily for isolated tests."""

    normalized = normalized_postgres_url(database_url)
    with _ENGINE_CACHE_LOCK:
        engine = _ENGINE_CACHE.pop(normalized, None)
    if engine is not None:
        engine.dispose()


class PostgresConnection:
    """Pooled psycopg connection with the subset used by Turtle stores."""

    backend = "postgresql"

    def __init__(self, database_url: str):
        self._pooled = _engine(database_url).raw_connection()
        self._connection = self._pooled.driver_connection
        self._connection.autocommit = True
        self._connection.row_factory = _hybrid_row_factory
        self._closed = False

    def execute(self, statement: str, parameters: tuple[Any, ...] | list[Any] = ()):
        return self._connection.execute(_adapt_qmark_sql(statement), parameters)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def begin(self, *, lock_key: str | None = None) -> None:
        self.execute("BEGIN")
        if lock_key:
            self.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                (str(lock_key),),
            )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        if self._closed:
            return
        try:
            if int(self._connection.info.transaction_status) != 0:
                self._connection.rollback()
        finally:
            self._closed = True
            self._pooled.close()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            try:
                self.rollback()
            except Exception:
                pass
        self.close()
        return False


def connect_postgres(database_url: str | None = None) -> PostgresConnection:
    resolved = str(database_url or runtime_database_url()).strip()
    if not is_postgres_url(resolved):
        raise RuntimeError("Turtle PostgreSQL connection is not configured")
    return PostgresConnection(resolved)
