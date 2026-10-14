from urllib.parse import quote

from app.redis_cache import RedisCache


class RedisSessionCache:
    def __init__(self, redis, postgres_store, ttl: int = 300):
        self._cache = RedisCache(redis)
        self._postgres_store = postgres_store
        self._ttl = ttl

    def _key(self, session_id: str, tenant_id: str, owner_sub: str) -> str:
        # guest 的 sub 含冒号；逐字段编码，避免不同三元组拼出同一个 key。
        return (
            f"chat:session:{quote(tenant_id, safe='')}:"
            f"{quote(owner_sub, safe='')}:{quote(session_id, safe='')}:messages"
        )

    async def create_session(self, tenant_id: str, owner_sub: str) -> str:
        return await self._postgres_store.create_session(tenant_id, owner_sub)

    async def authorize_session(
        self, session_id: str, tenant_id: str, owner_sub: str
    ) -> None:
        await self._postgres_store.authorize_session(
            session_id, tenant_id, owner_sub
        )

    async def get_messages(
        self, session_id: str, tenant_id: str, owner_sub: str
    ) -> list[dict]:
        await self.authorize_session(session_id, tenant_id, owner_sub)
        key = self._key(session_id, tenant_id, owner_sub)
        cached = await self._cache.get_json(key)
        if cached is not None:
            return cached
        messages = await self._postgres_store.get_messages(
            session_id, tenant_id, owner_sub
        )
        await self._cache.set_json(key, messages, self._ttl)
        return messages

    async def append_turn(
        self,
        session_id: str,
        tenant_id: str,
        owner_sub: str,
        question: str,
        answer: str,
    ) -> None:
        await self._postgres_store.append_turn(
            session_id, tenant_id, owner_sub, question, answer
        )
        await self._cache.delete(self._key(session_id, tenant_id, owner_sub))