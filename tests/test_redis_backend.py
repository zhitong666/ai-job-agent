import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from app.redis_rate_limiter import RedisRateLimiter
from app.redis_session_cache import RedisSessionCache
from app.postgres_session_store import SessionNotFound


def test_session_cache_hit_still_authorizes_owner():
    async def scenario():
        redis = AsyncMock()
        postgres = AsyncMock()
        redis.get.return_value = json.dumps([{"role": "user", "content": "你好"}])
        store = RedisSessionCache(redis, postgres)
        messages = await store.get_messages("s1", "tenant-a", "user-a")
        assert messages == [{"role": "user", "content": "你好"}]
        postgres.authorize_session.assert_awaited_once_with(
            "s1", "tenant-a", "user-a"
        )
        postgres.get_messages.assert_not_awaited()
        redis.set.assert_not_awaited()
    asyncio.run(scenario())


def test_session_cache_miss_reads_owned_postgres_and_sets_key():
    async def scenario():
        redis = AsyncMock()
        postgres = AsyncMock()
        redis.get.return_value = None
        postgres.get_messages.return_value = [{"role": "user", "content": "问题"}]
        store = RedisSessionCache(redis, postgres)
        messages = await store.get_messages("s1", "tenant-a", "user-a")
        assert len(messages) == 1
        postgres.authorize_session.assert_awaited_once()
        postgres.get_messages.assert_awaited_once_with(
            "s1", "tenant-a", "user-a"
        )
        assert redis.set.await_args.args[0] == (
            "chat:session:tenant-a:user-a:s1:messages"
        )
    asyncio.run(scenario())


@pytest.mark.parametrize("cached", [True, False])
def test_other_owner_denied_before_cache_read(cached):
    async def scenario():
        redis = AsyncMock()
        postgres = AsyncMock()
        redis.get.return_value = "[]" if cached else None
        postgres.authorize_session.side_effect = SessionNotFound()
        store = RedisSessionCache(redis, postgres)
        with pytest.raises(SessionNotFound):
            await store.get_messages("s1", "tenant-a", "user-b")
        redis.get.assert_not_awaited()
        postgres.get_messages.assert_not_awaited()
    asyncio.run(scenario())


def test_session_cache_invalidates_only_after_successful_append():
    async def scenario():
        redis = AsyncMock()
        postgres = AsyncMock()
        store = RedisSessionCache(redis, postgres)
        await store.append_turn("s1", "tenant-a", "user-a", "问题", "答案")
        postgres.append_turn.assert_awaited_once_with(
            "s1", "tenant-a", "user-a", "问题", "答案"
        )
        assert redis.delete.await_args.args[0] == (
            "chat:session:tenant-a:user-a:s1:messages"
        )

        redis.reset_mock()
        postgres.append_turn.side_effect = RuntimeError("PG failed")
        with pytest.raises(RuntimeError, match="PG failed"):
            await store.append_turn("s1", "tenant-a", "user-a", "问题", "答案")
        redis.delete.assert_not_awaited()
    asyncio.run(scenario())


def test_rate_limiter_allows_until_limit_then_blocks():
    async def scenario():
        redis = AsyncMock()
        redis.eval.side_effect = [1, 2, 3]

        limiter = RedisRateLimiter(redis)

        first = await limiter.allow("chat:rate:s1", limit=2, window_seconds=60)
        second = await limiter.allow("chat:rate:s1", limit=2, window_seconds=60)
        third = await limiter.allow("chat:rate:s1", limit=2, window_seconds=60)

        assert first is True
        assert second is True
        assert third is False

    asyncio.run(scenario())


def test_rate_limiter_uses_atomic_lua_script():
    async def scenario():
        redis = AsyncMock()
        redis.eval.return_value = 1

        limiter = RedisRateLimiter(redis)
        await limiter.allow("chat:rate:s1", limit=5, window_seconds=30)

        script = redis.eval.await_args.args[0]
        assert "INCR" in script
        assert "EXPIRE" in script
        assert redis.eval.await_args.args[2] == "chat:rate:s1"

    asyncio.run(scenario())


def test_redis_integration_requires_test_redis_url():
    url = os.getenv("TEST_REDIS_URL")

    if not url:
        pytest.skip("TEST_REDIS_URL not set")

    async def scenario():
        from app.redis_client import create_redis_client

        redis = create_redis_client(url)
        await redis.set("test-key", "ok", ex=5)
        value = await redis.get("test-key")

        assert value == "ok"

        await redis.aclose()

    asyncio.run(scenario())