"""D2：当前会话归属缺口的期望行为测试，修复前应 xfail。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.auth_security import create_access_token
from app.main import app
from app.quota import QuotaDecision


class FakeSessionStore:
    def __init__(self):
        self.messages_by_session = {}

    async def get_messages(self, session_id):
        return list(self.messages_by_session.get(session_id, []))

    async def append_turn(self, session_id, question, answer):
        self.messages_by_session.setdefault(session_id, []).extend(
            [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        )


def headers_for(subject):
    token = create_access_token(subject, ["user"], "tenant-d2")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.xfail(
    strict=True,
    reason="D2 已确认：聊天路由与 session store 尚未校验用户归属",
)
def test_user_b_cannot_read_user_a_session(monkeypatch):
    """修复目标：同租户用户 B 猜中 A 的 session_id 也应被拒绝。"""

    seen_messages = []

    async def fake_completion(**kwargs):
        seen_messages.append(kwargs["messages"])

        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="已收到"))
                ]
            )

        return chunks()

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_completion)
        )
    )
    quota = MagicMock()
    quota.check_request = AsyncMock(
        return_value=QuotaDecision(allowed=True)
    )
    quota.consume_tokens = AsyncMock(
        return_value=QuotaDecision(allowed=True)
    )

    monkeypatch.setattr(app.state, "async_client", client, raising=False)
    monkeypatch.setattr(app.state, "llm_semaphore", None, raising=False)
    monkeypatch.setattr(
        app.state, "session_store", FakeSessionStore(), raising=False
    )
    monkeypatch.setattr(app.state, "quota_service", quota, raising=False)
    limiter = MagicMock()
    limiter.allow = AsyncMock(return_value=True)
    monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://d2.test"
        ) as api:
            first = await api.post(
                "/chat/stream",
                headers=headers_for("user-a"),
                json={
                    "session_id": "shared-d2-id",
                    "question": "A_PRIVATE_D2",
                    "scene": "general",
                },
            )
            second = await api.post(
                "/chat/stream",
                headers=headers_for("user-b"),
                json={
                    "session_id": "shared-d2-id",
                    "question": "B 问题",
                    "scene": "general",
                },
            )

        assert first.status_code == 200
        assert second.status_code in {403, 404}, (
            "B 被放行；第二次模型输入包含 A 的历史："
            f"{any(message.get('content') == 'A_PRIVATE_D2' for message in seen_messages[1])}"
        )

    asyncio.run(scenario())
