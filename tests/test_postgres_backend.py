import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db_cli import run_migrations
from app.postgres import create_postgres_pool
from app.postgres_session_store import PostgresSessionStore, SessionNotFound


class AsyncContextManager:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def make_pool():
    pool = MagicMock()
    conn = MagicMock()
    cur = AsyncMock()

    pool.connection.return_value = AsyncContextManager(conn)
    conn.cursor.return_value = AsyncContextManager(cur)
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()

    return pool, conn, cur


def test_create_session_commits_owner_row():
    async def scenario():
        pool, conn, cur = make_pool()
        store = PostgresSessionStore(pool)
        session_id = await store.create_session("tenant-a", "user-a")
        sql, params = cur.execute.await_args.args
        assert "INSERT INTO chat_sessions" in sql
        assert params == (session_id, "tenant-a", "user-a")
        assert conn.commit.await_count == 1
    asyncio.run(scenario())


def test_create_session_collision_never_overwrites_owner():
    async def scenario():
        pool, conn, cur = make_pool()
        cur.execute.side_effect = RuntimeError("duplicate session_id")
        store = PostgresSessionStore(pool)
        with pytest.raises(RuntimeError, match="duplicate session_id"):
            await store.create_session("tenant-b", "user-b")
        assert "ON CONFLICT DO UPDATE" not in cur.execute.await_args.args[0]
        assert conn.commit.await_count == 0
        assert conn.rollback.await_count == 1
    asyncio.run(scenario())


def test_authorize_session_rejects_other_owner():
    async def scenario():
        pool, _conn, cur = make_pool()
        cur.fetchone.return_value = None
        store = PostgresSessionStore(pool)
        with pytest.raises(SessionNotFound):
            await store.authorize_session("s1", "tenant-a", "user-b")
        sql, params = cur.execute.await_args.args
        assert "FROM chat_sessions" in sql
        assert params == ("s1", "tenant-a", "user-b")
    asyncio.run(scenario())


def test_get_messages_returns_only_owned_chat_history():
    async def scenario():
        pool, _conn, cur = make_pool()
        cur.fetchall.return_value = [("user", "你好"), ("assistant", "你好")]
        store = PostgresSessionStore(pool)
        messages = await store.get_messages("s1", "tenant-a", "user-a")
        assert messages == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好"},
        ]
        sql, params = cur.execute.await_args.args
        assert "JOIN chat_sessions" in sql
        assert "s.tenant_id" in sql and "s.owner_sub" in sql
        assert "ORDER BY m.id ASC" in sql
        assert params == ("s1", "tenant-a", "user-a")
    asyncio.run(scenario())


def test_append_turn_checks_owner_and_commits_two_messages():
    async def scenario():
        pool, conn, cur = make_pool()
        cur.fetchone.return_value = (1,)
        store = PostgresSessionStore(pool)
        await store.append_turn("s1", "tenant-a", "user-a", "问题", "答案")
        assert cur.execute.await_count == 3  # 归属检查 + 两次 INSERT
        assert "FOR UPDATE" in cur.execute.await_args_list[0].args[0]
        assert conn.commit.await_count == 1
        assert conn.rollback.await_count == 0
    asyncio.run(scenario())


def test_append_turn_rolls_back_when_second_insert_fails():
    async def scenario():
        pool, conn, cur = make_pool()
        cur.fetchone.return_value = (1,)
        cur.execute.side_effect = [None, None, RuntimeError("database insert failed")]
        store = PostgresSessionStore(pool)
        with pytest.raises(RuntimeError, match="database insert failed"):
            await store.append_turn("s1", "tenant-a", "user-a", "问题", "答案")
        assert conn.rollback.await_count == 1
        assert conn.commit.await_count == 0
    asyncio.run(scenario())


def test_postgres_roundtrip_requires_test_database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")

    async def scenario():
        pool = await create_postgres_pool(dsn)
        session_id = None
        try:
            await run_migrations(pool)
            store = PostgresSessionStore(pool)
            session_id = await store.create_session("tenant-test", "user-a")
            await store.append_turn(
                session_id, "tenant-test", "user-a", "你好", "你好"
            )
            messages = await store.get_messages(
                session_id, "tenant-test", "user-a"
            )
            assert messages == [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好"},
            ]
            with pytest.raises(SessionNotFound):
                await store.authorize_session(session_id, "tenant-test", "user-b")
            with pytest.raises(SessionNotFound):
                await store.authorize_session(session_id, "other-tenant", "user-a")
        finally:
            try:
                if session_id is not None:
                    async with pool.connection() as conn, conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM chat_messages WHERE session_id = %s",
                            (session_id,),
                        )
                        await cur.execute(
                            "DELETE FROM chat_sessions WHERE session_id = %s",
                            (session_id,),
                        )
                        await conn.commit()
            finally:
                await pool.close()

    asyncio.run(scenario())