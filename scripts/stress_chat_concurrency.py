#!/usr/bin/env python3
"""Isolated Redis admission stress checks for Turtle Chat.

Run inside the Open WebUI service environment. Every invocation uses a unique
test-only Redis prefix and removes it before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections import Counter

from open_webui.turtle_chat.concurrency import ChatConcurrencyCoordinator


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def lease_arguments(index: int, capacity: int) -> dict[str, object]:
    return {
        "request_id": str(uuid.uuid4()),
        "user_id": f"ha-user-{index}",
        "group_id": f"ha-group-{index}",
        "provider": "gpt",
        "user_limit": 1,
        "group_limit": 1,
        "account_pool_id": "ha-pool",
        "account_pool_limit": capacity,
        "provider_limit": max(capacity, 24),
        "global_limit": max(capacity, 32),
    }


async def cleanup(
    coordinator: ChatConcurrencyCoordinator,
    tasks: list[asyncio.Task],
    leases: list,
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for lease in leases:
        if not lease._released:
            await lease.release("test_cleanup")
    redis = await coordinator._redis()
    keys = [
        key
        async for key in redis.scan_iter(match=f"{coordinator.prefix}:*")
    ]
    if keys:
        await redis.unlink(*keys)
    await coordinator.close()


async def run_load(args: argparse.Namespace) -> dict[str, object]:
    coordinator = ChatConcurrencyCoordinator()
    coordinator.prefix = f"turtle-ha-load:{uuid.uuid4()}"
    coordinator.queue_timeout_seconds = args.queue_timeout
    coordinator.lease_seconds = 30
    coordinator.status_ttl_seconds = 10
    tasks: list[asyncio.Task] = []
    waits: list[float] = []
    errors: list[str] = []
    active = 0
    max_active = 0
    guard = asyncio.Lock()
    started = time.perf_counter()

    async def one(index: int) -> None:
        nonlocal active, max_active
        began = time.perf_counter()
        try:
            lease = await coordinator.acquire(
                **lease_arguments(index, args.capacity)
            )
            waits.append((time.perf_counter() - began) * 1000)
            async with guard:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(args.hold_ms / 1000)
            async with guard:
                active -= 1
            await lease.release("completed")
        except Exception as exc:
            errors.append(type(exc).__name__)

    try:
        tasks = [
            asyncio.create_task(one(index))
            for index in range(args.clients)
        ]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        snapshot = await coordinator.snapshot(
            account_pools=[
                {
                    "id": "ha-pool",
                    "name": "HA",
                    "limit": args.capacity,
                }
            ]
        )
        return {
            "scenario": "load",
            "clients": args.clients,
            "capacity": args.capacity,
            "success": len(waits),
            "errors": dict(Counter(errors)),
            "elapsed_s": round(elapsed, 3),
            "throughput_rps": round(len(waits) / elapsed, 2),
            "max_active": max_active,
            "wait_p50_ms": round(percentile(waits, 0.50), 1),
            "wait_p95_ms": round(percentile(waits, 0.95), 1),
            "wait_p99_ms": round(percentile(waits, 0.99), 1),
            "listener_tasks": sum(
                1
                for task in asyncio.all_tasks()
                if task.get_name() == "turtle-chat-capacity-events"
                and not task.done()
            ),
            "final_global": snapshot["global"],
            "final_pool": snapshot["account_pools"][0],
        }
    finally:
        await cleanup(coordinator, tasks, [])


async def run_cancel(args: argparse.Namespace) -> dict[str, object]:
    coordinator = ChatConcurrencyCoordinator()
    coordinator.prefix = f"turtle-ha-cancel:{uuid.uuid4()}"
    coordinator.queue_timeout_seconds = args.queue_timeout
    coordinator.lease_seconds = 30
    coordinator.status_ttl_seconds = 10
    holders = []
    tasks: list[asyncio.Task] = []

    async def waiter(index: int) -> None:
        lease = await coordinator.acquire(
            **lease_arguments(index + args.capacity, args.capacity)
        )
        await lease.release("completed")

    try:
        for index in range(args.capacity):
            holders.append(
                await coordinator.acquire(
                    **lease_arguments(index, args.capacity)
                )
            )
        tasks = [
            asyncio.create_task(waiter(index))
            for index in range(args.clients)
        ]

        settle_deadline = time.monotonic() + args.queue_timeout / 2
        queued = 0
        while time.monotonic() < settle_deadline:
            snapshot = await coordinator.snapshot(
                account_pools=[
                    {
                        "id": "ha-pool",
                        "name": "HA",
                        "limit": args.capacity,
                    }
                ]
            )
            queued = snapshot["account_pools"][0]["queued"]
            if queued == args.clients:
                break
            await asyncio.sleep(0.05)
        if queued != args.clients:
            raise RuntimeError(
                f"only {queued}/{args.clients} waiters entered Redis FIFO"
            )

        cancel_started = time.perf_counter()
        for task in tasks[: args.cancel_count]:
            task.cancel()
        cancelled_results = await asyncio.wait_for(
            asyncio.gather(
                *tasks[: args.cancel_count],
                return_exceptions=True,
            ),
            timeout=args.queue_timeout / 2,
        )
        cancel_ms = (time.perf_counter() - cancel_started) * 1000
        after_cancel = await coordinator.snapshot(
            account_pools=[
                {
                    "id": "ha-pool",
                    "name": "HA",
                    "limit": args.capacity,
                }
            ]
        )

        for lease in holders:
            await lease.release("completed")
        drain_started = time.perf_counter()
        remaining_results = await asyncio.wait_for(
            asyncio.gather(
                *tasks[args.cancel_count :],
                return_exceptions=True,
            ),
            timeout=args.queue_timeout,
        )
        drain_ms = (time.perf_counter() - drain_started) * 1000
        final = await coordinator.snapshot(
            account_pools=[
                {
                    "id": "ha-pool",
                    "name": "HA",
                    "limit": args.capacity,
                }
            ]
        )
        return {
            "scenario": "cancel",
            "active_holders": args.capacity,
            "waiters": args.clients,
            "requested_cancellations": args.cancel_count,
            "cancelled": sum(
                isinstance(result, asyncio.CancelledError)
                for result in cancelled_results
            ),
            "cancel_errors": dict(
                Counter(
                    type(result).__name__
                    for result in cancelled_results
                    if not isinstance(result, asyncio.CancelledError)
                )
            ),
            "cancel_ms": round(cancel_ms, 1),
            "queued_after_cancel": after_cancel["account_pools"][0]["queued"],
            "remaining_success": sum(
                result is None for result in remaining_results
            ),
            "remaining_errors": dict(
                Counter(
                    type(result).__name__
                    for result in remaining_results
                    if result is not None
                )
            ),
            "drain_ms": round(drain_ms, 1),
            "final_global": final["global"],
            "final_pool": final["account_pools"][0],
        }
    finally:
        await cleanup(coordinator, tasks, holders)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("load", "cancel"))
    parser.add_argument("--clients", type=int, default=300)
    parser.add_argument("--capacity", type=int, default=14)
    parser.add_argument("--hold-ms", type=int, default=80)
    parser.add_argument("--cancel-count", type=int, default=100)
    parser.add_argument("--queue-timeout", type=int, default=120)
    values = parser.parse_args()
    if values.clients < 1 or values.capacity < 1:
        parser.error("clients and capacity must be positive")
    if not 0 <= values.cancel_count <= values.clients:
        parser.error("cancel-count must be between zero and clients")
    return values


async def main() -> None:
    args = arguments()
    result = (
        await run_load(args)
        if args.scenario == "load"
        else await run_cancel(args)
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
