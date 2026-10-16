"""D4 当前审批缺口诊断：不调用真实 LLM，不执行外部投递。

打印的是当前行为，不是安全测试通过。修复后应按 review/19 的目标重新验收。
所有 patch 限于这个 Python 进程，结束后恢复；不启动应用 lifespan。
跳过 trace_stream 包装，避免同步生成器的 OTel 上下文问题干扰审批诊断。
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.approval import ApprovalStore
from app.auth_security import create_access_token
from app.main import app
from app.quota import QuotaDecision
from app.react import _execute_tool
from app.streaming import sse_event
from app.tools import Tool


def headers_for(subject: str) -> dict:
    token = create_access_token(subject, ["user"], "tenant-d4")
    return {"Authorization": f"Bearer {token}"}


def probe_preapproval() -> None:
    store = ApprovalStore()
    store.decide("not-issued-d4", True)
    print("1 preapproval_consumed =", store.wait("not-issued-d4", timeout=0.01))


def probe_other_owner() -> None:
    class BoundedStore(ApprovalStore):
        def wait(self, request_id, timeout=120):
            return super().wait(request_id, timeout=2)

    store = BoundedStore()
    pending = Event()
    results = []

    def fake_stream(question, approve_tool_call=None, approval_request_id=None, **_kwargs):
        args = {"company": "D4-A", "position": "Agent"}
        yield sse_event("approval", json.dumps({
            "request_id": approval_request_id, "tool": "apply_job", "arguments": args,
        }))
        pending.set()
        approved = approve_tool_call("apply_job", args)
        results.append(approved)
        yield sse_event("answer", "fake handler allowed" if approved else "denied")
        yield sse_event("done", "")

    quota = MagicMock()
    quota.check_request = AsyncMock(return_value=QuotaDecision(allowed=True))
    quota.consume_tokens = AsyncMock(return_value=QuotaDecision(allowed=True))

    def start_a():
        api = TestClient(app)
        try:
            return api.post("/agent/stream", headers=headers_for("user-a"), json={
                "request_id": "d4-a-run", "question": "D4 fake approval probe",
            }).status_code
        finally:
            api.close()

    with patch("app.main.approval_store", store), patch(
        "app.main.stream_react_loop", fake_stream
    ), patch("app.main.trace_stream", lambda _store, _question, _run_id, stream: stream), patch.object(
        app.state, "quota_service", quota, create=True
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            a_request = executor.submit(start_a)
            if not pending.wait(timeout=5):
                raise RuntimeError("A 未进入 fake 审批分支")
            api = TestClient(app)
            try:
                response = api.post("/agent/approve", headers=headers_for("user-b"), json={
                    "request_id": "d4-a-run", "approved": True,
                })
            finally:
                api.close()
            print("2 user_b_approval_http =", response.status_code)
            print("2 user_a_stream_http =", a_request.result(timeout=5))
            print("2 user_a_callback_approved =", results == [True])


def probe_arguments_not_bound() -> None:
    store = ApprovalStore()
    executed_companies = []

    def fake_handler(arguments, retriever=None):
        executed_companies.append(arguments["company"])
        return "fake handler only"

    tool = Tool("apply_job", "D4 fake write", {}, fake_handler, requires_approval=True)
    # 假设用户看到 company=A 后批准；队列实际只保存 bool，没有参数快照。
    store.decide("shared-d4-run", True)
    _execute_tool(
        tool, {"company": "B", "position": "Agent"}, None,
        lambda _name, _args: store.wait("shared-d4-run", timeout=0.01),
    )
    print("3 executed_company_after_approval_for_a =", executed_companies)


if __name__ == "__main__":
    probe_preapproval()
    probe_other_owner()
    probe_arguments_not_bound()
    print("以上是缺口诊断，不表示安全验收通过；按 review/19 作答。")
