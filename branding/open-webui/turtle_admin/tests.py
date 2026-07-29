"""Focused safety tests for the standalone administrator console API."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from . import router as admin_router
from .router import (
    delete_account_pool,
    get_providers,
    _health_url,
    _provider_health_snapshot,
    _provider_identity,
    update_user_role,
)


class AdminRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_health_paths_are_derived_without_credentials(self):
        self.assertEqual(
            _health_url("http://gateway:8000/v1"),
            "http://gateway:8000/healthz",
        )
        self.assertEqual(_provider_identity(1, "")[0], "claude")
        self.assertEqual(_provider_identity(2, "")[0], "claude")

    def test_provider_health_snapshot_is_non_blocking_copy(self):
        cached = [{"key": "gpt", "state": "degraded"}]
        with patch("open_webui.turtle_admin.router._PROVIDER_CACHE", cached):
            result = _provider_health_snapshot()
            result[0]["state"] = "ready"
            self.assertEqual(_provider_health_snapshot()[0]["state"], "degraded")

    async def test_provider_page_returns_snapshot_while_probe_runs_in_background(self):
        gate = asyncio.Event()

        async def slow_probe(*, force=False):
            await gate.wait()
            return [{"key": "gpt", "state": "ready"}]

        with (
            patch.object(admin_router, "_PROVIDER_CACHE", []),
            patch.object(admin_router, "_PROVIDER_CACHE_AT", 0.0),
            patch.object(admin_router, "_PROVIDER_TASK", None),
            patch.object(admin_router, "_provider_health", side_effect=slow_probe),
            patch.object(
                admin_router,
                "_gateway_json",
                AsyncMock(return_value={"pools": [], "accounts": []}),
            ),
        ):
            result = await get_providers(force=False, user=SimpleNamespace(id="admin"))
            self.assertEqual(result["items"], [])
            self.assertTrue(result["probing"])
            task = admin_router._PROVIDER_TASK
            self.assertIsNotNone(task)
            gate.set()
            await task

    async def test_delete_account_pool_is_forwarded_and_invalidates_capacity(self):
        gateway = AsyncMock(return_value={"id": "pool-custom", "deleted": True})
        invalidate = Mock()
        with (
            patch.object(admin_router, "_gateway_json", gateway),
            patch.object(admin_router.ACCOUNT_POOL_ADMISSION, "invalidate", invalidate),
        ):
            result = await delete_account_pool(
                "pool-custom",
                user=SimpleNamespace(id="admin"),
            )
        self.assertTrue(result["deleted"])
        gateway.assert_awaited_once_with(
            "DELETE",
            "/internal/account-pools/pool-custom",
        )
        invalidate.assert_called_once_with("pool-custom")

    async def test_current_admin_cannot_demote_self(self):
        actor = SimpleNamespace(id="admin", name="Admin", role="admin")
        target = SimpleNamespace(id="admin", name="Admin", email="admin@example.test", role="admin")
        with patch(
            "open_webui.turtle_admin.router.Users.get_user_by_id",
            AsyncMock(return_value=target),
        ):
            with self.assertRaises(HTTPException) as denied:
                await update_user_role(
                    "admin",
                    SimpleNamespace(role="user"),
                    user=actor,
                    db=None,
                )
        self.assertEqual(denied.exception.status_code, 400)

    async def test_last_admin_cannot_be_demoted(self):
        actor = SimpleNamespace(id="second-admin", name="Admin", role="admin")
        target = SimpleNamespace(id="first-admin", name="Owner", email="owner@example.test", role="admin")
        with (
            patch(
                "open_webui.turtle_admin.router.Users.get_user_by_id",
                AsyncMock(return_value=target),
            ),
            patch(
                "open_webui.turtle_admin.router.Users.get_users",
                AsyncMock(return_value={"users": [target], "total": 1}),
            ),
        ):
            with self.assertRaises(HTTPException) as denied:
                await update_user_role(
                    target.id,
                    SimpleNamespace(role="user"),
                    user=actor,
                    db=None,
                )
        self.assertEqual(denied.exception.status_code, 409)

    async def test_pending_user_can_be_approved_without_password_changes(self):
        actor = SimpleNamespace(id="admin", name="Admin", role="admin")
        target = SimpleNamespace(id="pending", name="Pending", email="pending@example.test", role="pending")
        approved = SimpleNamespace(id=target.id, name=target.name, email=target.email, role="user")
        update = AsyncMock(return_value=approved)
        subscription = AsyncMock(
            return_value={
                "configured": True,
                "state": "active",
                "expires_at": 4_102_444_800,
            }
        )
        invalidate = AsyncMock()
        with (
            patch(
                "open_webui.turtle_admin.router.Users.get_user_by_id",
                AsyncMock(return_value=target),
            ),
            patch(
                "open_webui.turtle_admin.router.Users.update_user_role_by_id",
                update,
            ),
            patch.object(admin_router.SUBSCRIPTION_CACHE, "get", subscription),
            patch.object(admin_router.SUBSCRIPTION_CACHE, "invalidate", invalidate),
        ):
            result = await update_user_role(
                target.id,
                SimpleNamespace(role="user"),
                user=actor,
                db=None,
            )
        self.assertEqual(result["role"], "user")
        update.assert_awaited_once_with(target.id, "user", db=None)
        subscription.assert_awaited_once_with(
            target.id,
            target.role,
            create_default=False,
        )
        invalidate.assert_awaited_once_with(target.id)

    async def test_pending_user_cannot_bypass_subscription_activation(self):
        actor = SimpleNamespace(id="admin", name="Admin", role="admin")
        target = SimpleNamespace(
            id="pending",
            name="Pending",
            email="pending@example.test",
            role="pending",
        )
        update = AsyncMock()
        with (
            patch(
                "open_webui.turtle_admin.router.Users.get_user_by_id",
                AsyncMock(return_value=target),
            ),
            patch(
                "open_webui.turtle_admin.router.Users.update_user_role_by_id",
                update,
            ),
            patch.object(
                admin_router.SUBSCRIPTION_CACHE,
                "get",
                AsyncMock(return_value={"configured": False, "state": None}),
            ),
        ):
            with self.assertRaises(HTTPException) as denied:
                await update_user_role(
                    target.id,
                    SimpleNamespace(role="user"),
                    user=actor,
                    db=None,
                )
        self.assertEqual(denied.exception.status_code, 409)
        update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
