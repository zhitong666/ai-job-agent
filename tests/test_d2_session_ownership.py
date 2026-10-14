"""D3：A 可续聊；B/C 持 A 的 ID 在模型与消息读取前被拒绝。"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.auth_security import create_access_token
from app.main import app
from app.postgres_session_store import SessionNotFound
from app.quota import QuotaDecision


class FakeSessionStore:
    def __init__(self):
        self.owners = {}
        self.messages = {}
        self.read_calls = 0
        self.write_calls = 0

    async def create_session(self, tenant_id, owner_sub):
        session_id = uuid.uuid4().hex
        self.owners[session_id] = (tenant_id, owner_sub)
        self.messages[session_id] = []
        return session_id

    async def authorize_session(self, session_id, tenant_id, owner_sub):
        if self.owners.get(session_id) != (tenant_id, owner_sub):
            raise SessionNotFound()

    async def get_messages(self, session_id, tenant_id, owner_sub):
        await self.authorize_session(session_id, tenant_id, owner_sub)
        self.read_calls += 1
        return list(self.messages[session_id])

    async def append_turn(
        self, session_id, tenant_id, owner_sub, question, answer
    ):
        await self.authorize_session(session_id, tenant_id, owner_sub)
        self.write_calls += 1
        self.messages[session_id].extend([
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ])


def headers_for(subject, tenant="tenant-d2"):
    token = create_access_token(subject, ["user"], tenant)
    return {"Authorization": f"Bearer {token}"}


def test_user_b_cannot_read_user_a_session(monkeypatch):
    seen_messages = []

    async def fake_completion(**kwargs):
        seen_messages.append(kwargs["messages"])
        if not kwargs.get("stream"):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="已收到")
            )])

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="已收到")
            )])

        return chunks()

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fake_completion)
    ))
    quota = MagicMock()
    quota.check_request = AsyncMock(return_value=QuotaDecision(allowed=True))
    quota.consume_tokens = AsyncMock(return_value=QuotaDecision(allowed=True))
    limiter = MagicMock()
    limiter.allow = AsyncMock(return_value=True)
    store = FakeSessionStore()
    monkeypatch.setattr(app.state, "async_client", client, raising=False)
    monkeypatch.setattr(app.state, "llm_semaphore", None, raising=False)
    monkeypatch.setattr(app.state, "session_store", store, raising=False)
    monkeypatch.setattr(app.state, "quota_service", quota, raising=False)
    monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://d3.test"
        ) as api:
            created = await api.post(
                "/chat/sessions", headers=headers_for("user-a")
            )
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            assert store.owners[session_id] == ("tenant-d2", "user-a")

            first = await api.post(
                "/chat/stream", headers=headers_for("user-a"),
                json={"session_id": session_id,
                    "question": "A_PRIVATE_D2", "scene": "general"},
            )
            assert first.status_code == 200
            before_model = len(seen_messages)
            before_read = store.read_calls
            before_write = store.write_calls
            history_before = list(store.messages[session_id])

            for endpoint in ("/chat", "/chat/stream"):
                denied = await api.post(
                    endpoint, headers=headers_for("user-b"),
                    json={"session_id": session_id,
                        "question": "B 问题", "scene": "general"},
                )
                assert denied.status_code == 404
                assert denied.json()["detail"] == "session not found"

            cross_tenant = await api.post(
                "/chat/stream",
                headers=headers_for("user-c", "other-tenant"),
                json={"session_id": session_id,
                    "question": "C 问题", "scene": "general"},
            )
            assert cross_tenant.status_code == 404
            assert len(seen_messages) == before_model
            assert store.read_calls == before_read
            assert store.write_calls == before_write
            assert store.messages[session_id] == history_before

            second = await api.post(
                "/chat/stream", headers=headers_for("user-a"),
                json={"session_id": session_id,
                    "question": "A 第二问", "scene": "general"},
            )
            assert second.status_code == 200
            assert any(
                message.get("content") == "A_PRIVATE_D2"
                for message in seen_messages[-1]
            )
            assert len(store.messages[session_id]) == 4

            legacy_id = "legacy-no-owner"
            store.messages[legacy_id] = [{"role": "user", "content": "LEGACY_PRIVATE"}]
            for endpoint in ("/chat", "/chat/stream"):
                legacy = await api.post(
                    endpoint,
                    headers=headers_for("user-a"),
                    json={"session_id": legacy_id, "question": "旧历史", "scene": "general"},
                )
                assert legacy.status_code == 404
            assert legacy_id not in store.owners

    asyncio.run(scenario())