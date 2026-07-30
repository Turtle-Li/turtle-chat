from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from chatgpt_web_gateway.project_usage import PostgresProjectUsageStore
from chatgpt_web_gateway.upstream_cleanup import PostgresUpstreamCleanupStore


ROOT = Path(__file__).resolve().parents[1]
PROJECT_PROXY = (
    ROOT / "branding" / "open-webui" / "turtle_project_api" / "router.py"
)


def test_open_webui_project_proxy_reuses_a_bounded_internal_client() -> None:
    source = PROJECT_PROXY.read_text(encoding="utf-8")
    proxy_body = source[
        source.index("async def _project_gateway_proxy")
        : source.index("@proxy_router.get")
    ]

    assert "async def _project_gateway_client()" in source
    assert "max_connections=64" in source
    assert "max_keepalive_connections=32" in source
    assert "keepalive_expiry=30.0" in source
    assert "client = await _project_gateway_client()" in source
    assert "async def _close_project_gateway_client()" in source
    assert "await upstream.aclose()" in source
    assert "await client.aclose()" not in proxy_body


def test_unknown_project_api_routes_fail_as_openai_style_json() -> None:
    source = PROJECT_PROXY.read_text(encoding="utf-8")

    assert '"/{path:path}"' in source
    assert "status_code=status.HTTP_404_NOT_FOUND" in source
    assert '"type": "invalid_request_error"' in source
    assert '"code": "unsupported_endpoint"' in source


def test_postgres_project_usage_uses_and_closes_a_bounded_pool() -> None:
    pool = MagicMock()
    lease = object()
    pool.connection.return_value = lease

    with patch("psycopg_pool.ConnectionPool", return_value=pool) as constructor:
        store = PostgresProjectUsageStore(
            "postgresql://project-test.invalid/turtle",
            "project-test-master",
        )

    assert constructor.call_args.kwargs["min_size"] == 2
    assert constructor.call_args.kwargs["max_size"] == 16
    assert constructor.call_args.kwargs["open"] is True
    assert store._connect() is lease

    store.close()
    pool.close.assert_called_once_with(timeout=5.0)


def test_project_usage_can_borrow_the_gateway_pool_without_owning_it() -> None:
    shared_pool = MagicMock()
    lease = object()
    shared_pool.connection.return_value = lease

    with patch("psycopg_pool.ConnectionPool") as constructor:
        store = PostgresProjectUsageStore(
            "postgresql://project-test.invalid/turtle",
            "project-test-master",
            connection_pool=shared_pool,
        )

    constructor.assert_not_called()
    assert store._connect() is lease
    store.close()
    shared_pool.close.assert_not_called()


def test_stream_cleanup_tracking_reuses_a_bounded_pool_before_done() -> None:
    pool = MagicMock()
    lease = object()
    pool.connection.return_value = lease

    with patch("psycopg_pool.ConnectionPool", return_value=pool) as constructor:
        store = PostgresUpstreamCleanupStore(
            "postgresql://cleanup-test.invalid/turtle",
            ttl_seconds=3600,
            conversation_action="delete",
        )

    assert constructor.call_args.kwargs["min_size"] == 1
    assert constructor.call_args.kwargs["max_size"] == 16
    assert store._connect() is lease

    store.close()
    pool.close.assert_called_once_with(timeout=5.0)


def test_cleanup_tracking_can_borrow_the_gateway_pool_without_owning_it() -> None:
    shared_pool = MagicMock()
    lease = object()
    shared_pool.connection.return_value = lease

    with patch("psycopg_pool.ConnectionPool") as constructor:
        store = PostgresUpstreamCleanupStore(
            "postgresql://cleanup-test.invalid/turtle",
            ttl_seconds=3600,
            conversation_action="delete",
            connection_pool=shared_pool,
        )

    constructor.assert_not_called()
    assert store._connect() is lease
    store.close()
    shared_pool.close.assert_not_called()
