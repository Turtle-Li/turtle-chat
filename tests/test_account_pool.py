from __future__ import annotations

import json
import unittest

import httpx

from chatgpt_web_gateway.account_quota import quota_lane, quota_profiles_payload
from chatgpt_web_gateway.account_pool import (
    AccountPoolConflict,
    AccountPoolRouter,
    AccountUnavailable,
    MemoryAccountStore,
    _new_account,
    _now,
)


class AccountQuotaProfileTests(unittest.TestCase):
    def test_free_profile_exposes_only_the_official_dynamic_instant_window(self) -> None:
        profiles = {
            item["id"]: item
            for item in quota_profiles_payload("gpt")
        }
        self.assertIn("free", profiles)
        self.assertEqual(profiles["free"]["label"], "Free 免费")

        instant = quota_lane("free", "gpt-5-5:instant")
        self.assertTrue(instant["enabled"])
        self.assertIsNone(instant["dispatch_budget_count"])
        self.assertEqual(instant["published_window_seconds"], 5 * 60 * 60)
        self.assertEqual(instant["reserve_count"], 0)
        self.assertEqual(instant["source"], "official_dynamic")

        for selection_key in (
            "latest:medium",
            "latest:high",
            "latest:xhigh",
            "latest:pro",
            "gpt-5-3:standard",
            "o3:standard",
        ):
            self.assertFalse(quota_lane("free", selection_key)["enabled"])

    def test_claude_profiles_track_each_verified_lane_with_plan_multipliers(self) -> None:
        profiles = {
            item["id"]: item
            for item in quota_profiles_payload("claude")
        }
        self.assertEqual(
            list(profiles),
            ["untracked", "free", "pro", "max-5x", "max-20x"],
        )
        pro = quota_lane("pro", "claude-sonnet-5:standard", "claude")
        max_5x = quota_lane("max-5x", "claude-sonnet-5:standard", "claude")
        max_20x = quota_lane("max-20x", "claude-sonnet-5:standard", "claude")
        self.assertEqual(pro["window_seconds"], 5 * 60 * 60)
        self.assertEqual(
            (
                max_5x["dispatch_budget_count"],
                max_20x["dispatch_budget_count"],
            ),
            (
                pro["dispatch_budget_count"] * 5,
                pro["dispatch_budget_count"] * 20,
            ),
        )
        self.assertFalse(
            quota_lane(
                "free",
                "claude-opus-4-8:extended",
                "claude",
            )["enabled"]
        )


class AccountPoolRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemoryAccountStore()
        self.default_account = _new_account(
            "acct-a",
            pool_id="gpt-default",
            name="主账号",
            worker_endpoint="http://worker.test:8320/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=10,
            deployment_managed=True,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/healthz":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "id": "upstream-id-must-not-be-exposed",
                        "name": "  海龟 管理员\n",
                    },
                )
            return httpx.Response(200, json={"object": "list", "data": []})

        self.router = AccountPoolRouter(
            store=self.store,
            upstream_api_key="test-key",
            upstream_timeout_seconds=10,
            lease_seconds=60,
            cooldown_seconds=30,
            allowed_hosts=("worker.test",),
            default_account=self.default_account,
            transport=httpx.MockTransport(handler),
        )
        await self.router.start()

    async def asyncTearDown(self) -> None:
        await self.router.close()

    async def add_ready_account(
        self,
        name: str = "备用账号",
        *,
        worker_port: int = 8321,
        quota_profile: str = "untracked",
    ) -> dict:
        account = await self.router.create_account(
            pool_id="gpt-default",
            name=name,
            worker_endpoint=f"http://worker.test:{worker_port}/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=0,
            quota_profile=quota_profile,
        )
        probe = await self.router.probe_account(account["id"])
        self.assertTrue(probe["ok"])
        snapshot = await self.router.snapshot()
        probed = next(item for item in snapshot["accounts"] if item["id"] == account["id"])
        self.assertFalse(probed["enabled"])
        self.assertEqual(probed["status"], "disabled")
        self.assertEqual(probed["session_state"], "valid")
        return await self.router.update_account(
            account["id"],
            name=account["name"],
            worker_endpoint=account["worker_endpoint"],
            health_path=account["health_path"],
            max_concurrency=account["max_concurrency"],
            priority=account["priority"],
            enabled=True,
            quota_profile=quota_profile,
        )

    async def test_per_account_concurrency_is_enforced_and_renewable(self) -> None:
        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-a",
            user_id="user-a",
            chat_id="chat-a",
        )
        self.assertTrue(await self.router.renew(first.request_id, first.account.id))
        with self.assertRaises(AccountUnavailable):
            await self.router.acquire(
                pool_id="gpt-default",
                request_id="request-b",
                user_id="user-b",
                chat_id="chat-b",
            )
        await first.release(outcome="success", status_code=200)
        with self.assertRaises(AccountUnavailable):
            await self.router.acquire(
                pool_id="gpt-default",
                request_id="request-a",
                user_id="user-a",
                chat_id="chat-a",
            )
        snapshot = await self.router.snapshot()
        self.assertEqual(snapshot["accounts"][0]["active"], 0)
        self.assertEqual(snapshot["pools"][0]["admission_capacity"], 1)
        self.assertEqual(snapshot["pools"][0]["available_slots"], 1)

    async def test_probe_persists_only_the_upstream_display_name(self) -> None:
        result = await self.router.probe_account("acct-a")
        self.assertEqual(result["upstream_display_name"], "海龟 管理员")
        self.assertNotIn("id", result)

        account = (await self.router.snapshot())["accounts"][0]
        self.assertEqual(account["upstream_display_name"], "海龟 管理员")
        self.assertIsNotNone(account["upstream_identity_updated_at"])
        self.assertNotIn("upstream_id", account)

    async def test_transient_probe_failures_do_not_immediately_evict_a_healthy_account(
        self,
    ) -> None:
        probe_state = {"status": 200}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/healthz":
                return httpx.Response(
                    probe_state["status"],
                    json={"ok": probe_state["status"] == 200},
                )
            return httpx.Response(200, json={"object": "list", "data": []})

        store = MemoryAccountStore()
        router = AccountPoolRouter(
            store=store,
            upstream_api_key="test-key",
            upstream_timeout_seconds=10,
            lease_seconds=60,
            cooldown_seconds=30,
            allowed_hosts=("worker.test",),
            default_account=self.default_account,
            transport=httpx.MockTransport(handler),
        )
        await router.start()
        try:
            self.assertTrue((await router.probe_account("acct-a"))["ok"])
            probe_state["status"] = 500
            for expected_failures in (1, 2):
                self.assertFalse((await router.probe_account("acct-a"))["ok"])
                account = (await router.snapshot())["accounts"][0]
                self.assertEqual(account["health_status"], "healthy")
                self.assertTrue(account["available"])
                self.assertEqual(account["consecutive_failures"], expected_failures)
                self.assertEqual(account["last_error_class"], "health_probe")

            self.assertFalse((await router.probe_account("acct-a"))["ok"])
            account = (await router.snapshot())["accounts"][0]
            self.assertEqual(account["health_status"], "degraded")
            self.assertFalse(account["available"])
            self.assertEqual(account["consecutive_failures"], 3)

            probe_state["status"] = 200
            self.assertTrue((await router.probe_account("acct-a"))["ok"])
            recovered = (await router.snapshot())["accounts"][0]
            self.assertEqual(recovered["health_status"], "healthy")
            self.assertEqual(recovered["consecutive_failures"], 0)
            self.assertTrue(recovered["available"])
        finally:
            await router.close()

    async def test_chat_affinity_is_soft_and_auth_failure_moves_next_request(self) -> None:
        secondary = await self.add_ready_account()
        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-a",
            user_id="user-a",
            chat_id="chat-a",
        )
        self.assertEqual(first.account.id, "acct-a")
        await first.release(
            outcome="error",
            status_code=401,
            error_class="upstream_request",
        )

        retry = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-b",
            user_id="user-a",
            chat_id="chat-a",
        )
        self.assertEqual(retry.account.id, secondary["id"])
        await retry.release(outcome="success", status_code=200)

        snapshot = await self.router.snapshot()
        primary = next(item for item in snapshot["accounts"] if item["id"] == "acct-a")
        self.assertEqual(primary["status"], "reauth_required")
        self.assertEqual(primary["session_state"], "expired")

    async def test_retry_exclusion_uses_a_distinct_account_and_audits_reason(
        self,
    ) -> None:
        secondary = await self.add_ready_account()
        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-exclude-a",
            user_id="user-a",
            chat_id="chat-exclude-a",
        )
        self.assertEqual(first.account.id, "acct-a")
        await first.release(
            outcome="cancelled",
            status_code=499,
            error_class="test_release",
        )

        retry = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-exclude-b",
            user_id="user-a",
            chat_id="chat-exclude-a",
            excluded_account_ids=frozenset({"acct-a"}),
            migration_reason_hint="failover_worker",
        )
        self.assertEqual(retry.account.id, secondary["id"])
        await retry.release(outcome="success", status_code=200)

        affinity = self.store.affinity[
            ("gpt-default", "chat-exclude-a")
        ]
        self.assertEqual(affinity["migration_count"], 1)
        self.assertEqual(
            affinity["last_migration_reason"],
            "failover_worker",
        )

    async def test_new_chats_balance_by_lane_pressure_but_existing_chat_stays_sticky(self) -> None:
        secondary = await self.add_ready_account()
        for account_id in ("acct-a", secondary["id"]):
            current = self.store.accounts[account_id]
            await self.router.update_account(
                account_id,
                name=current["name"],
                worker_endpoint=current["worker_endpoint"],
                health_path=current["health_path"],
                max_concurrency=current["max_concurrency"],
                priority=current["priority"],
                enabled=True,
                quota_profile="plus",
            )

        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-balance-a",
            user_id="user-a",
            chat_id="chat-balance-a",
            selection_key="latest:medium",
        )
        first_account = first.account.id
        await first.release(outcome="success", status_code=200)

        sticky = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-balance-sticky",
            user_id="user-a",
            chat_id="chat-balance-a",
            selection_key="latest:medium",
        )
        self.assertEqual(sticky.account.id, first_account)
        await sticky.release(outcome="success", status_code=200)

        second_chat = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-balance-b",
            user_id="user-b",
            chat_id="chat-balance-b",
            selection_key="latest:medium",
        )
        self.assertNotEqual(second_chat.account.id, first_account)
        await second_chat.release(outcome="success", status_code=200)

    async def test_new_chats_use_narrowest_capable_account_and_keep_affinity(self) -> None:
        current = self.store.accounts["acct-a"]
        await self.router.update_account(
            "acct-a",
            name=current["name"],
            worker_endpoint=current["worker_endpoint"],
            health_path=current["health_path"],
            max_concurrency=current["max_concurrency"],
            priority=100,
            enabled=True,
            quota_profile="pro-20x",
        )
        plus = await self.add_ready_account(
            "Plus",
            worker_port=8321,
            quota_profile="plus",
        )
        free = await self.add_ready_account(
            "Free",
            worker_port=8322,
            quota_profile="free",
        )

        instant = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-instant",
            user_id="user-with-pro-access",
            chat_id="chat-best-fit-instant",
            selection_key="gpt-5-5:instant",
        )
        self.assertEqual(instant.account.id, free["id"])
        await instant.release(outcome="success", status_code=200)

        instant_sticky = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-instant-sticky",
            user_id="user-with-pro-access",
            chat_id="chat-best-fit-instant",
            selection_key="gpt-5-5:instant",
        )
        self.assertEqual(instant_sticky.account.id, free["id"])
        await instant_sticky.release(outcome="success", status_code=200)

        medium = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-medium",
            user_id="user-with-pro-access",
            chat_id="chat-best-fit-medium",
            selection_key="latest:medium",
        )
        self.assertEqual(medium.account.id, plus["id"])
        await medium.release(outcome="success", status_code=200)

        pro = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-pro",
            user_id="user-with-pro-access",
            chat_id="chat-best-fit-pro",
            selection_key="latest:pro",
        )
        self.assertEqual(pro.account.id, "acct-a")
        await pro.release(outcome="success", status_code=200)

        pro_chat_switches_to_medium = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-pro-sticky",
            user_id="user-with-pro-access",
            chat_id="chat-best-fit-pro",
            selection_key="latest:medium",
        )
        self.assertEqual(pro_chat_switches_to_medium.account.id, "acct-a")
        await pro_chat_switches_to_medium.release(outcome="success", status_code=200)

        busy_plus = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-plus-busy",
            user_id="user-a",
            chat_id="chat-best-fit-plus-busy",
            selection_key="latest:medium",
        )
        self.assertEqual(busy_plus.account.id, plus["id"])
        overflow = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-overflow",
            user_id="user-b",
            chat_id="chat-best-fit-overflow",
            selection_key="latest:medium",
        )
        self.assertEqual(overflow.account.id, "acct-a")
        await overflow.release(outcome="success", status_code=200)
        await busy_plus.release(outcome="success", status_code=200)

    async def test_equal_capability_profiles_preserve_the_larger_budget_for_overflow(self) -> None:
        current = self.store.accounts["acct-a"]
        await self.router.update_account(
            "acct-a",
            name=current["name"],
            worker_endpoint=current["worker_endpoint"],
            health_path=current["health_path"],
            max_concurrency=current["max_concurrency"],
            priority=100,
            enabled=True,
            quota_profile="pro-20x",
        )
        pro_five = await self.add_ready_account(
            "5x Pro",
            worker_port=8321,
            quota_profile="pro-5x",
        )

        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-pro-five",
            user_id="user-a",
            chat_id="chat-best-fit-pro-five",
            selection_key="latest:pro",
        )
        self.assertEqual(first.account.id, pro_five["id"])

        overflow = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-best-fit-pro-twenty",
            user_id="user-b",
            chat_id="chat-best-fit-pro-twenty",
            selection_key="latest:pro",
        )
        self.assertEqual(overflow.account.id, "acct-a")
        await overflow.release(outcome="success", status_code=200)
        await first.release(outcome="success", status_code=200)

    async def test_same_chat_cannot_split_across_accounts_while_a_turn_is_active(self) -> None:
        await self.add_ready_account()
        first = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-chat-serial-a",
            user_id="user-a",
            chat_id="chat-serial",
            selection_key="latest:medium",
        )
        with self.assertRaisesRegex(AccountUnavailable, "同一会话"):
            await self.router.acquire(
                pool_id="gpt-default",
                request_id="request-chat-serial-b",
                user_id="user-a",
                chat_id="chat-serial",
                selection_key="latest:medium",
            )
        await first.release(outcome="success", status_code=200)

    async def test_reserve_zone_migrates_once_and_records_sanitized_reason(self) -> None:
        secondary = await self.add_ready_account()
        for account_id in ("acct-a", secondary["id"]):
            current = self.store.accounts[account_id]
            await self.router.update_account(
                account_id,
                name=current["name"],
                worker_endpoint=current["worker_endpoint"],
                health_path=current["health_path"],
                max_concurrency=current["max_concurrency"],
                priority=current["priority"],
                enabled=True,
                quota_profile="plus",
            )
        now = _now()
        for index in range(95):
            request_id = f"historic-{index}"
            self.store.leases[request_id] = {
                "request_id": request_id,
                "account_id": "acct-a",
                "pool_id": "gpt-default",
                "user_id": "historic-user",
                "chat_id": f"historic-chat-{index}",
                "selection_key": "o3:standard",
                "state": "completed",
                "leased_at": now - 60,
                "expires_at": now - 30,
                "completed_at": now - 20,
                "outcome": "success",
                "error_class": None,
            }
        self.store.affinity[("gpt-default", "chat-reserve")] = {
            "pool_id": "gpt-default",
            "chat_id": "chat-reserve",
            "user_id": "user-a",
            "preferred_account_id": "acct-a",
            "created_at": now - 100,
            "updated_at": now - 100,
            "last_routed_at": now - 100,
            "migration_count": 0,
            "last_migrated_at": None,
            "last_migration_reason": None,
        }

        lease = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-reserve-migrate",
            user_id="user-a",
            chat_id="chat-reserve",
            selection_key="o3:standard",
        )
        self.assertEqual(lease.account.id, secondary["id"])
        affinity = self.store.affinity[("gpt-default", "chat-reserve")]
        self.assertEqual(affinity["migration_count"], 1)
        self.assertEqual(affinity["last_migration_reason"], "quota_reserve")
        await lease.release(outcome="success", status_code=200)

    async def test_rate_limit_blocks_only_the_selected_lane(self) -> None:
        current = self.store.accounts["acct-a"]
        await self.router.update_account(
            "acct-a",
            name=current["name"],
            worker_endpoint=current["worker_endpoint"],
            health_path=current["health_path"],
            max_concurrency=current["max_concurrency"],
            priority=current["priority"],
            enabled=True,
            quota_profile="plus",
        )
        limited = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-lane-limit",
            user_id="user-a",
            chat_id="chat-lane-limit",
            selection_key="latest:medium",
        )
        await limited.release(
            outcome="error",
            status_code=429,
            error_class="upstream_request",
        )
        snapshot = await self.router.snapshot()
        account = snapshot["accounts"][0]
        lanes = {item["selection_key"]: item for item in account["quota"]["lanes"]}
        self.assertEqual(account["status"], "ready")
        self.assertEqual(lanes["latest:medium"]["state"], "cooldown")
        self.assertTrue(lanes["latest:high"]["available"])

        other_lane = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-other-lane",
            user_id="user-b",
            chat_id="chat-other-lane",
            selection_key="latest:high",
        )
        self.assertEqual(other_lane.account.id, "acct-a")
        await other_lane.release(outcome="success", status_code=200)

    async def test_rate_limited_lane_recovers_only_after_background_completion(
        self,
    ) -> None:
        observed_payloads: list[dict] = []
        recovery = {"available": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/healthz":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/chat/completions":
                observed_payloads.append(json.loads(request.content))
                if not recovery["available"]:
                    return httpx.Response(
                        429,
                        json={"error": {"message": "rate limited"}},
                    )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "OK"},
                                "finish_reason": "stop",
                            }
                        ],
                        "conversation": {
                            "conversation_id": "conversation_recovery_1234"
                        },
                    },
                )
            if request.url.path == "/api/OpenaiAccount/turtle/cleanup":
                return httpx.Response(
                    200,
                    json={"ok": True, "dry_run": False, "http_status": 204},
                )
            return httpx.Response(404)

        store = MemoryAccountStore()
        free_account = _new_account(
            "acct-free",
            pool_id="gpt-default",
            name="Free",
            worker_endpoint="http://worker.test:8323/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=10,
            quota_profile="free",
            deployment_managed=True,
        )
        router = AccountPoolRouter(
            store=store,
            upstream_api_key="test-key",
            upstream_timeout_seconds=10,
            lease_seconds=60,
            cooldown_seconds=30,
            allowed_hosts=("worker.test",),
            default_account=free_account,
            transport=httpx.MockTransport(handler),
        )
        await router.start()
        try:
            lease = await router.acquire(
                pool_id="gpt-default",
                request_id="request-free-limit",
                user_id="user-a",
                chat_id="chat-free-limit",
                selection_key="gpt-5-5:instant",
            )
            await lease.release(
                outcome="error",
                status_code=429,
                error_class="upstream_request",
            )
            block_key = ("acct-free", "gpt-5-5:instant")
            self.assertIn(block_key, store.lane_blocks)
            store.lane_blocks[block_key]["blocked_until"] = _now() - 1

            lane = next(
                item
                for item in (await router.snapshot())["accounts"][0]["quota"]["lanes"]
                if item["selection_key"] == "gpt-5-5:instant"
            )
            self.assertEqual(lane["state"], "cooldown")
            with self.assertRaises(AccountUnavailable):
                await router.acquire(
                    pool_id="gpt-default",
                    request_id="request-user-must-not-probe",
                    user_id="user-b",
                    chat_id="chat-user-must-not-probe",
                    selection_key="gpt-5-5:instant",
                )

            failed_claim = store.claim_rate_limit_recoveries(
                limit=1,
                claim_seconds=60,
            )[0]
            await router._probe_rate_limit_recovery(failed_claim)
            self.assertIn(block_key, store.lane_blocks)
            self.assertEqual(
                store.lane_blocks[block_key]["consecutive_failures"],
                2,
            )

            store.lane_blocks[block_key]["blocked_until"] = _now() - 1
            recovery["available"] = True
            successful_claim = store.claim_rate_limit_recoveries(
                limit=1,
                claim_seconds=60,
            )[0]
            await router._probe_rate_limit_recovery(successful_claim)
            self.assertNotIn(block_key, store.lane_blocks)

            recovered = await router.acquire(
                pool_id="gpt-default",
                request_id="request-after-monitor-recovery",
                user_id="user-c",
                chat_id="chat-after-monitor-recovery",
                selection_key="gpt-5-5:instant",
            )
            await recovered.release(outcome="success", status_code=200)
        finally:
            await router.close()

        self.assertEqual(len(observed_payloads), 2)
        self.assertTrue(all(item.get("temporary") is True for item in observed_payloads))
        self.assertTrue(
            all(item.get("model") == "gpt-5-5-instant" for item in observed_payloads)
        )

    async def test_outer_capacity_is_scoped_to_the_requested_lane(self) -> None:
        current = self.store.accounts["acct-a"]
        await self.router.update_account(
            "acct-a",
            name=current["name"],
            worker_endpoint=current["worker_endpoint"],
            health_path=current["health_path"],
            max_concurrency=current["max_concurrency"],
            priority=current["priority"],
            enabled=True,
            quota_profile="go",
        )
        unavailable = await self.router.capacity("gpt-default", "latest:medium")
        instant = await self.router.capacity("gpt-default", "gpt-5-5:instant")
        self.assertEqual(unavailable["admission_capacity"], 0)
        self.assertEqual(unavailable["provider_admission_capacity"], 0)
        self.assertEqual(unavailable["global_admission_capacity"], 1)
        self.assertEqual(instant["admission_capacity"], 1)
        self.assertEqual(instant["provider_admission_capacity"], 1)
        self.assertEqual(instant["global_admission_capacity"], 1)

    async def test_capacity_sums_each_healthy_accounts_configured_slots(self) -> None:
        secondary = await self.add_ready_account(quota_profile="plus")
        await self.router.update_account(
            secondary["id"],
            name=secondary["name"],
            worker_endpoint=secondary["worker_endpoint"],
            health_path=secondary["health_path"],
            max_concurrency=2,
            priority=secondary["priority"],
            enabled=True,
            quota_profile="plus",
        )

        capacity = await self.router.capacity("gpt-default", "latest:medium")
        self.assertEqual(capacity["admission_capacity"], 3)
        self.assertEqual(capacity["provider_admission_capacity"], 3)
        self.assertEqual(capacity["global_admission_capacity"], 3)
        self.assertEqual(capacity["available_slots"], 3)

    async def test_cancel_does_not_penalize_account_and_cooldown_recovers(self) -> None:
        lease = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-a",
            user_id="user-a",
            chat_id="chat-a",
        )
        await lease.release(
            outcome="cancelled",
            status_code=499,
            error_class="client_cancelled",
        )
        self.assertEqual(self.store.accounts["acct-a"]["consecutive_failures"], 0)
        self.assertEqual(self.store.accounts["acct-a"]["status"], "ready")

        self.store.accounts["acct-a"].update(
            status="cooldown",
            health_status="degraded",
            cooldown_until=_now() - 1,
        )
        snapshot = await self.router.snapshot()
        primary = next(item for item in snapshot["accounts"] if item["id"] == "acct-a")
        self.assertEqual(primary["status"], "ready")
        self.assertEqual(primary["health_status"], "unknown")
        self.assertTrue(primary["available"])
        capacity = await self.router.capacity("gpt-default")
        self.assertEqual(capacity["admission_capacity"], 1)
        self.assertEqual(capacity["available_slots"], 1)

    async def test_changing_worker_requires_a_new_probe(self) -> None:
        changed = await self.router.update_account(
            "acct-a",
            name="主账号",
            worker_endpoint="http://worker.test:8322/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=10,
            enabled=True,
        )
        self.assertTrue(changed["enabled"])
        self.assertEqual(changed["status"], "disabled")
        self.assertEqual(changed["session_state"], "unknown")

        await self.router.probe_account("acct-a")
        snapshot = await self.router.snapshot()
        primary = next(item for item in snapshot["accounts"] if item["id"] == "acct-a")
        self.assertEqual(primary["status"], "ready")
        self.assertTrue(primary["available"])

    async def test_reauth_waits_for_active_lease_and_requires_explicit_completion(self) -> None:
        lease = await self.router.acquire(
            pool_id="gpt-default",
            request_id="request-reauth",
            user_id="user-a",
            chat_id="chat-a",
        )
        with self.assertRaises(AccountPoolConflict):
            await self.router.begin_reauth("acct-a")
        await lease.release(outcome="cancelled", status_code=499)

        waiting = await self.router.begin_reauth("acct-a")
        self.assertEqual(waiting["status"], "reauth_required")
        self.assertFalse(waiting["available"])

        probe = await self.router.probe_account("acct-a")
        self.assertTrue(probe["ok"])
        still_waiting = (await self.router.snapshot())["accounts"][0]
        self.assertEqual(still_waiting["status"], "reauth_required")
        self.assertEqual(still_waiting["session_state"], "expired")

        self.store.mark_probe(
            "acct-a",
            state="ready",
            http_status=200,
            latency_ms=1,
            allow_reauth=True,
        )
        ready = (await self.router.snapshot())["accounts"][0]
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["available"])

    async def test_claude_pool_uses_claude_lanes_and_isolated_provider_accounts(self) -> None:
        pool = await self.router.create_pool(
            provider="claude",
            name="Claude 测试池",
            description="Claude isolated workers",
        )
        account = await self.router.create_account(
            pool_id=pool["id"],
            name="Claude 账号",
            worker_endpoint="http://worker.test:8330/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=0,
        )
        await self.router.probe_account(account["id"])
        await self.router.update_account(
            account["id"],
            name=account["name"],
            worker_endpoint=account["worker_endpoint"],
            health_path=account["health_path"],
            max_concurrency=1,
            priority=0,
            enabled=True,
            quota_profile="untracked",
        )

        lease = await self.router.acquire(
            pool_id=pool["id"],
            request_id="claude-request-1",
            user_id="user-claude",
            chat_id="chat-claude",
            selection_key="claude-sonnet-5:standard",
        )
        self.assertEqual(lease.account.provider, "claude")
        await lease.release(outcome="success", status_code=200)

        capacity = await self.router.capacity(
            pool["id"],
            "claude-sonnet-5:standard",
        )
        self.assertEqual(capacity["admission_capacity"], 1)
        with self.assertRaises(AccountPoolConflict):
            await self.router.capacity(pool["id"], "latest:medium")

    async def test_only_empty_custom_account_pools_can_be_deleted(self) -> None:
        empty_pool = await self.router.create_pool(
            provider="gpt",
            name="临时空池",
            description="safe to remove",
        )
        deleted = await self.router.delete_pool(empty_pool["id"])
        self.assertTrue(deleted["deleted"])
        self.assertFalse(
            any(
                item["id"] == empty_pool["id"]
                for item in (await self.router.snapshot())["pools"]
            )
        )

        with self.assertRaisesRegex(AccountPoolConflict, "默认账号池"):
            await self.router.delete_pool("gpt-default")

        used_pool = await self.router.create_pool(
            provider="gpt",
            name="仍有账号的池",
            description="must be retained",
        )
        await self.router.create_account(
            pool_id=used_pool["id"],
            name="池内账号",
            worker_endpoint="http://worker.test:8331/v1",
            health_path="/healthz",
            max_concurrency=1,
            priority=0,
        )
        with self.assertRaisesRegex(AccountPoolConflict, "仍有账号"):
            await self.router.delete_pool(used_pool["id"])


if __name__ == "__main__":
    unittest.main()
