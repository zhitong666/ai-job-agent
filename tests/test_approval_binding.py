import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main, mcp_agent, react
from app.approval import (
    ApprovalConflict,
    ApprovalContext,
    ApprovalNotFound,
    ApprovalStore,
    arguments_hash,
)
from app.auth_security import create_access_token
from app.quota import QuotaDecision
from app.tools import Tool, ToolRegistry

ARGS = {"company": "A", "position": "X"}
OWNER = {"tenant_id": "tenant-d4", "owner_sub": "user-a"}


def headers(subject="user-a", tenant="tenant-d4"):
    token = create_access_token(subject, ["user"], tenant)
    return {"Authorization": f"Bearer {token}"}


def issue(store, call="call-1", args=None, ttl=5):
    return store.issue(
        run_id="run-a", tool_call_id=call, tool_name="apply_job",
        arguments=ARGS if args is None else args, ttl=ttl, **OWNER,
    )


def context(store):
    return ApprovalContext(store, run_id="run-a", timeout=5, **OWNER)


def tool_and_calls():
    calls = []

    def handler(arguments, retriever=None):
        calls.append(copy.deepcopy(arguments))
        return "fake write succeeded"

    tool = Tool("apply_job", "fake write", {}, handler, requires_approval=True)
    return tool, calls


def event_data(event):
    return json.loads(next(line[6:] for line in event.splitlines() if line.startswith("data: ")))


def response(calls=None, content=None):
    tool_calls = [
        SimpleNamespace(id=call_id, function=SimpleNamespace(
            name=name, arguments=json.dumps(args),
        )) for call_id, name, args in calls or []
    ]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        tool_calls=tool_calls, content=content,
    ))])


def test_legitimate_owner_and_reordered_arguments_execute_once():
    store = ApprovalStore()
    ticket = issue(store)
    reordered = {"position": "X", "company": "A"}
    assert arguments_hash(ARGS) == arguments_hash(reordered)
    assert store.decide(ticket.approval_id, True, **OWNER) == "approved"
    tool, calls = tool_and_calls()
    result = react._execute_tool(
        tool, reordered, object(), None,
        approval_context=context(store), approval_ticket=ticket, tool_call_id=ticket.tool_call_id,
    )
    assert result == "fake write succeeded"
    assert calls == [reordered]
    assert store.status(ticket.approval_id, **OWNER) == "consumed"
    react._execute_tool(
        tool, ARGS, object(), None,
        approval_context=context(store), approval_ticket=ticket, tool_call_id=ticket.tool_call_id,
    )
    assert len(calls) == 1


@pytest.mark.parametrize("subject,tenant", [("user-b", "tenant-d4"), ("user-c", "tenant-other")])
def test_other_owner_http_cannot_change_pending(monkeypatch, subject, tenant):
    store = ApprovalStore()
    ticket = issue(store)
    monkeypatch.setattr(main, "approval_store", store)
    api = TestClient(main.app)
    try:
        res = api.post("/agent/approve", headers=headers(subject, tenant), json={
            "approval_id": ticket.approval_id, "approved": True,
        })
    finally:
        api.close()
    assert res.status_code == 404
    assert store.status(ticket.approval_id, **OWNER) == "pending"


def test_unknown_preapproval_and_legacy_body_are_rejected(monkeypatch):
    store = ApprovalStore()
    monkeypatch.setattr(main, "approval_store", store)
    api = TestClient(main.app)
    try:
        res = api.post("/agent/approve", headers=headers(), json={
            "approval_id": "not-issued", "approved": True,
        })
        old = api.post("/agent/approve", headers=headers(), json={
            "request_id": "run-a", "approved": True,
        })
        forged = api.post("/agent/approve", headers=headers(), json={
            "approval_id": "not-issued", "approved": True, "owner_sub": "user-a",
        })
    finally:
        api.close()
    assert res.status_code == 404
    assert old.status_code == 422
    assert forged.status_code == 422
    with pytest.raises(ApprovalNotFound):
        store.status("not-issued", **OWNER)
    ticket = issue(store)
    assert store.wait_and_consume(
        ticket.approval_id, run_id="run-a", tool_call_id="call-1",
        tool_name="apply_job", arguments=ARGS, timeout=0, **OWNER,
    ) is False


def test_changed_company_is_not_changed_user_and_never_executes():
    store = ApprovalStore()
    ticket = issue(store)
    store.decide(ticket.approval_id, True, **OWNER)
    tool, calls = tool_and_calls()
    result = react._execute_tool(
        tool, {"company": "B", "position": "Y"}, object(), None,
        approval_context=context(store), approval_ticket=ticket, tool_call_id=ticket.tool_call_id,
    )
    assert "不匹配" in result
    assert calls == []
    assert store.status(ticket.approval_id, **OWNER) == "approved"
    context(store).close()
    with pytest.raises(ApprovalNotFound):
        store.status(ticket.approval_id, **OWNER)


def test_payload_is_a_copy_and_not_the_stored_snapshot():
    store = ApprovalStore()
    args = dict(ARGS)
    ticket = issue(store, args=args)
    args["company"] = "B"
    payload = ticket.public_payload()
    payload["arguments"]["company"] = "C"
    assert json.loads(ticket.arguments_json) == ARGS
    assert ticket.public_payload()["arguments"] == ARGS


def test_actual_call_id_must_match_ticket_even_when_parameters_are_equal():
    store = ApprovalStore()
    ticket = issue(store)
    store.decide(ticket.approval_id, True, **OWNER)
    tool, calls = tool_and_calls()
    result = react._execute_tool(
        tool, ARGS, object(), None, approval_context=context(store),
        approval_ticket=ticket, tool_call_id="another-call",
    )
    assert "不匹配" in result
    assert calls == []


def test_two_calls_same_run_second_rejected_both_messages_returned(monkeypatch):
    store = ApprovalStore()
    tool, calls = tool_and_calls()
    registry = ToolRegistry()
    registry.register(tool)
    seen = []
    responses = iter([
        response([("call-1", "apply_job", ARGS), ("call-2", "apply_job", ARGS)]),
        response(content="done"),
    ])

    def fake_model(messages, *_args):
        seen.append(copy.deepcopy(messages))
        return next(responses)

    monkeypatch.setattr(react, "_call_model", fake_model)
    stream = react.stream_react_loop(
        "执行演示", retriever=object(), registry=registry,
        approval_context=context(store),
    )
    first = event_data(next(stream))
    assert store.status(first["approval_id"], **OWNER) == "pending"
    store.decide(first["approval_id"], True, **OWNER)
    assert next(stream).startswith("event: step")
    second = event_data(next(stream))
    assert first["run_id"] == second["run_id"]
    assert first["approval_id"] != second["approval_id"]
    assert store.status(second["approval_id"], **OWNER) == "pending"
    store.decide(second["approval_id"], False, **OWNER)
    events = list(stream)
    assert len(calls) == 1
    assert any(item.startswith("event: done") for item in events)
    tools = [item for item in seen[1] if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in tools] == ["call-1", "call-2"]
    assert "拒绝" in tools[1]["content"]


def test_forced_apply_helper_also_uses_ticket(monkeypatch):
    store = ApprovalStore()
    tool, calls = tool_and_calls()
    registry = ToolRegistry()
    registry.register(tool)
    monkeypatch.setattr(react, "_call_model", lambda *_args: response(content="done"))
    stream = react.stream_react_loop(
        "帮我投递", retriever=object(), registry=registry,
        approval_context=context(store),
    )
    payload = event_data(next(stream))
    assert payload["tool_call_id"].startswith("forced_")
    store.decide(payload["approval_id"], False, **OWNER)
    assert any(event.startswith("event: done") for event in list(stream))
    assert calls == []


def test_expired_and_duplicate_decisions_fail():
    now = [100.0]
    store = ApprovalStore(clock=lambda: now[0])
    ticket = issue(store, ttl=1)
    store.decide(ticket.approval_id, True, **OWNER)
    with pytest.raises(ApprovalConflict):
        store.decide(ticket.approval_id, True, **OWNER)
    now[0] = 101.0
    with pytest.raises(ApprovalConflict):
        store.decide(ticket.approval_id, True, **OWNER)


def test_two_execution_attempts_only_one_consumes_permission():
    store = ApprovalStore()
    ticket = issue(store)
    store.decide(ticket.approval_id, True, **OWNER)
    tool, calls = tool_and_calls()

    def execute():
        return react._execute_tool(
            tool, ARGS, object(), None,
            approval_context=context(store), approval_ticket=ticket, tool_call_id=ticket.tool_call_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: execute(), range(2)))
    assert len(calls) == 1
    assert results.count("fake write succeeded") == 1


@pytest.mark.parametrize("path,factory", [
    ("/agent/plan/stream", "stream_plan_execute"),
    ("/agent/supervisor/stream", "stream_supervisor"),
    ("/agent/graph/stream", "stream_graph_supervisor"),
])
def test_other_http_modes_do_not_use_old_queue(monkeypatch, path, factory):
    quota = MagicMock()
    quota.check_request = AsyncMock(return_value=QuotaDecision(allowed=True))
    quota.consume_tokens = AsyncMock(return_value=QuotaDecision(allowed=True))
    monkeypatch.setattr(main.app.state, "quota_service", quota, raising=False)
    monkeypatch.setattr(main, "get_retriever", lambda: object())
    observed = []

    def fake_stream(question, approve_tool_call=None, **_kwargs):
        observed.append(approve_tool_call("apply_job", dict(ARGS)))
        yield "event: done\ndata: \n\n"

    monkeypatch.setattr(main, factory, fake_stream)
    api = TestClient(main.app)
    try:
        res = api.post(path, headers=headers(), json={"question": "test"})
    finally:
        api.close()
    assert res.status_code == 200
    assert observed == [False]


def test_mcp_adapter_forwards_bound_context_without_external_mcp(monkeypatch):
    ctx = context(ApprovalStore())
    monkeypatch.setattr(mcp_agent, "_build_registry_and_prompt", lambda: (object(), "fake"))
    captured = []

    def fake_react(question, **kwargs):
        captured.append(kwargs["approval_context"])
        return iter(["event: done\ndata: \n\n"])

    monkeypatch.setattr(mcp_agent, "stream_react_loop", fake_react)
    list(mcp_agent.stream_mcp_react_loop("test", retriever=object(), approval_context=ctx))
    assert captured == [ctx]


def test_http_stream_real_loop_waits_for_owner_and_cleans_up(monkeypatch):
    pending = Event()
    issued = []

    class ObservedStore(ApprovalStore):
        def issue(self, **kwargs):
            kwargs["ttl"] = 2
            ticket = super().issue(**kwargs)
            issued.append(ticket)
            pending.set()
            return ticket

    store = ObservedStore()
    tool, calls = tool_and_calls()
    registry = ToolRegistry()
    registry.register(tool)
    quota = MagicMock()
    quota.check_request = AsyncMock(return_value=QuotaDecision(allowed=True))
    quota.consume_tokens = AsyncMock(return_value=QuotaDecision(allowed=True))
    monkeypatch.setattr(main.app.state, "quota_service", quota, raising=False)
    monkeypatch.setattr(main, "approval_store", store)
    monkeypatch.setattr(main, "trace_stream", lambda _s, _q, _id, stream: stream)
    monkeypatch.setattr(react, "get_retriever", lambda: object())
    monkeypatch.setattr(react, "build_default_registry", lambda: registry)
    model = MagicMock(side_effect=[
        response([("call-http", "apply_job", ARGS)]), response(content="done"),
    ])
    monkeypatch.setattr(react, "_call_model", model)

    def start_a():
        api = TestClient(main.app)
        try:
            return api.post("/agent/stream", headers=headers(), json={
                "question": "演示", "request_id": "client-chosen-run",
            })
        finally:
            api.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(start_a)
        assert pending.wait(timeout=5)
        ticket = issued[0]
        assert ticket.run_id != "client-chosen-run"
        api = TestClient(main.app)
        try:
            for subject, tenant in [("user-b", "tenant-d4"), ("user-c", "tenant-other")]:
                denied = api.post("/agent/approve", headers=headers(subject, tenant), json={
                    "approval_id": ticket.approval_id, "approved": True,
                })
                assert denied.status_code == 404
                assert calls == []
                assert store.status(ticket.approval_id, **OWNER) == "pending"
            approved = api.post("/agent/approve", headers=headers(), json={
                "approval_id": ticket.approval_id, "approved": True,
            })
        finally:
            api.close()
        result = future.result(timeout=5)
    assert approved.status_code == 200
    assert result.status_code == 200
    assert "event: approval" in result.text
    assert "event: step" in result.text
    assert "event: done" in result.text
    assert calls == [ARGS]
    with pytest.raises(ApprovalNotFound):
        store.status(ticket.approval_id, **OWNER)
