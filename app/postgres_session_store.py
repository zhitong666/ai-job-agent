import uuid


class SessionNotFound(Exception):
    """当前身份无权使用此会话，或会话不存在。"""

class PostgresSessionStore:
    def __init__(self, pool):
        self._pool = pool

    async def create_session(self, tenant_id: str, owner_sub: str) -> str:
        session_id = uuid.uuid4().hex
        async with self._pool.connection() as conn, conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO chat_sessions (session_id, tenant_id, owner_sub)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, tenant_id, owner_sub),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return session_id

    async def authorize_session(
        self, session_id: str, tenant_id: str, owner_sub: str
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM chat_sessions
                WHERE session_id = %s AND tenant_id = %s AND owner_sub = %s
                """,
                (session_id, tenant_id, owner_sub),
            )
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFound()

    async def get_messages(
        self, session_id: str, tenant_id: str, owner_sub: str
    ) -> list[dict]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT m.role, m.content
                FROM chat_messages AS m
                JOIN chat_sessions AS s ON s.session_id = m.session_id
                WHERE m.session_id = %s
                  AND s.tenant_id = %s
                  AND s.owner_sub = %s
                ORDER BY m.id ASC
                """,
                (session_id, tenant_id, owner_sub),
            )
            rows = await cur.fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    async def append_turn(
        self,
        session_id: str,
        tenant_id: str,
        owner_sub: str,
        question: str,
        answer: str,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    SELECT 1 FROM chat_sessions
                    WHERE session_id = %s AND tenant_id = %s AND owner_sub = %s
                    FOR UPDATE
                    """,
                    (session_id, tenant_id, owner_sub),
                )
                if await cur.fetchone() is None:
                    raise SessionNotFound()
                await cur.execute(
                    """
                    INSERT INTO chat_messages (session_id, role, content)
                    VALUES (%s, 'user', %s)
                    """,
                    (session_id, question),
                )
                await cur.execute(
                    """
                    INSERT INTO chat_messages (session_id, role, content)
                    VALUES (%s, 'assistant', %s)
                    """,
                    (session_id, answer),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise