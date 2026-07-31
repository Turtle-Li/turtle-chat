from __future__ import annotations

import asyncio
from unittest.mock import patch

from chatgpt_web_gateway.image_stage import ImageStageRegistry


def test_stage_registry_merges_media_and_allows_one_bound_claim() -> None:
    async def run() -> None:
        registry = ImageStageRegistry(ttl_seconds=600)
        profiles = frozenset({"plus", "pro-5x"})

        async with registry.session_lock(
            user_id="user-a",
            pool_id="gpt-default",
            session_id="session-0123456789abcdef",
        ):
            first = registry.remember(
                user_id="user-a",
                pool_id="gpt-default",
                session_id="session-0123456789abcdef",
                conversation_id="turtle-v1-" + "a" * 64,
                account_id="account-a",
                required_quota_profiles=profiles,
                media_ids={"media-a"},
            )
            current = registry.current(
                user_id="user-a",
                pool_id="gpt-default",
                session_id="session-0123456789abcdef",
                required_quota_profiles=profiles,
            )
            second = registry.remember(
                user_id="user-a",
                pool_id="gpt-default",
                session_id="session-0123456789abcdef",
                conversation_id=first.conversation_id,
                account_id="account-a",
                required_quota_profiles=profiles,
                media_ids={"media-b"},
                existing=current,
            )

        assert second is first
        assert second.media_ids == {"media-a", "media-b"}
        assert registry.claim(
            token=first.token,
            user_id="user-a",
            pool_id="gpt-default",
            required_quota_profiles=profiles,
            media_ids={"media-b"},
        ) is first
        assert registry.claim(
            token=first.token,
            user_id="user-a",
            pool_id="gpt-default",
            required_quota_profiles=profiles,
            media_ids={"media-b"},
        ) is None

    asyncio.run(run())


def test_stage_registry_rejects_cross_scope_and_expired_claims() -> None:
    registry = ImageStageRegistry(ttl_seconds=60)
    with patch("chatgpt_web_gateway.image_stage.time.time", return_value=1_000):
        stage = registry.remember(
            user_id="user-a",
            pool_id="gpt-default",
            session_id="session-0123456789abcdef",
            conversation_id="turtle-v1-" + "b" * 64,
            account_id="account-a",
            required_quota_profiles=frozenset({"plus"}),
            media_ids={"media-a"},
        )

    assert registry.claim(
        token=stage.token,
        user_id="user-b",
        pool_id="gpt-default",
        required_quota_profiles=frozenset({"plus"}),
        media_ids={"media-a"},
    ) is None
    assert registry.claim(
        token=stage.token,
        user_id="user-a",
        pool_id="gpt-default",
        required_quota_profiles=frozenset({"pro-5x"}),
        media_ids={"media-a"},
    ) is None
    assert registry.claim(
        token=stage.token,
        user_id="user-a",
        pool_id="gpt-default",
        required_quota_profiles=frozenset({"plus"}),
        media_ids={"unknown-media"},
    ) is None
    with patch("chatgpt_web_gateway.image_stage.time.time", return_value=1_061):
        assert registry.claim(
            token=stage.token,
            user_id="user-a",
            pool_id="gpt-default",
            required_quota_profiles=frozenset({"plus"}),
            media_ids={"media-a"},
        ) is None
