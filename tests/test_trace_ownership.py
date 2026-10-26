import asyncio
import contextvars
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import main, mcp_agent, observability, react
from app.approval import ApprovalContext, ApprovalStore
from app.auth_security import create_access_token
from app.observability import AgentTrace, ObservabilityStore, TraceConflict, TraceNotFound, trace_stream
from app.quota import QuotaDecision
from app.tools import ToolRegistry

OWNER = {"tenant_id": "tenant-d5", "owner_sub": "user-a"}
OTHER = {"tenant_id": "tenant-d5", "owner_sub": "user-b"}


def headers(subject="user-a", tenant="tenant-d5"):
    token = create_access_token(subject, ["user"], tenant)
    return {"Authorization": f"Bearer {token}"}


def start(store, trace_id="trace-a", **owner):
    return store.start_trace("private-a test@example.com", trace_id, **(owner or OWNER))


def model_reply():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        tool_calls=[], content="fake answer",
    ))])


def trace_id_from(text):
    for block in text.split("\n\n"):
        if block.startswith("event: trace\n"):
            return next(line[6:] for line in block.splitlines() if line.startswith("data: "))
    raise AssertionError("missing trace SSE")


@pytest.fixture
def api_env(monkeypatch):
    store = ObservabilityStore()
    approvals = ApprovalStore()
    closed = []
    original_close = approvals.close_run

    def close_run(**kwargs):
        closed.append(kwargs)
        return original_close(**kwargs)

    monkeypatch.setattr(approvals, "close_run", close_run)
    monkeypatch.setattr(main, "observability_store", store)
    monkeypatch.setattr(main, "approval_store", approvals)
    quota = SimpleNamespace(
        check_request=AsyncMock(return_value=QuotaDecision(allowed=True)),
        consume_tokens=AsyncMock(return_value=QuotaDecision(allowed=True)),
    )
    monkeypatch.setattr(main.app.state, "quota_service", quota, raising=False)
    monkeypatch.setattr(react, "get_retriever", lambda: object())
    monkeypatch.setattr(react, "build_default_registry", ToolRegistry)
    monkeypatch.setattr(react, "_call_model", lambda *_args: model_reply())
    monkeypatch.setattr(mcp_agent, "get_retriever", lambda: object())
    client = TestClient(main.app)
    try:
        yield client, store, approvals, closed
    finally:
        client.close()


@pytest.mark.parametrize("owner", [OWNER, OTHER])
def test_duplicate_id_never_overwrites(owner):
    store = ObservabilityStore()
    start(store)
    store.record("trace-a", "answer", answer="original", **OWNER)
    before = store.get_trace("trace-a", **OWNER).model_dump()
    with pytest.raises(TraceConflict):
        store.start_trace("overwrite", "trace-a", **owner)
    assert store.get_trace("trace-a", **OWNER).model_dump() == before


def test_unknown_wrong_owner_and_legacy_writes_rejected():
    store = ObservabilityStore()
    start(store)
    store._traces["legacy"] = AgentTrace(trace_id="legacy", question="legacy-private")
    for trace_id, owner in [("unknown", OWNER), ("trace-a", OTHER), ("legacy", OWNER)]:
        with pytest.raises(TraceNotFound):
            store.record(trace_id, "step", value="bad", **owner)
    assert store.get_trace("legacy", **OWNER) is None
    assert store.get_trace("unknown", **OWNER) is None
    assert store.get_trace("trace-a", **OWNER).events == []
    with pytest.raises(ValueError):
        store.start_trace("bad owner", "unbound", tenant_id="", owner_sub="")
    assert "unbound" not in store._traces


def test_reads_are_snapshots_and_exports_are_owner_scoped(tmp_path):
    store = ObservabilityStore()
    start(store)
    start(store, "trace-b", **OTHER)
    store.record("trace-a", "step", observation="original", **OWNER)
    copy = store.get_trace("trace-a", **OWNER)
    copy.question = "changed"
    copy.events[0].data["observation"] = "changed"
    assert store.get_trace("trace-a", **OWNER).question != "changed"
    assert store.get_trace("trace-a", **OWNER).events[0].data["observation"] == "original"
    assert store.event_counts("trace-a", **OTHER) == {}
    path = tmp_path / "owned.jsonl"
    store.export_jsonl(path, **OWNER)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["trace_id"] for line in lines] == ["trace-a"]


def test_get_auth_and_legacy_are_fail_closed(api_env):
    api, store, _approvals, _closed = api_env
    start(store)
    store._traces["legacy"] = AgentTrace(trace_id="legacy", question="legacy-private")
    assert api.get("/agent/traces/trace-a").status_code == 401
    allowed = api.get("/agent/traces/trace-a", headers=headers())
    assert allowed.status_code == 200
    assert "[REDACTED_EMAIL]" in allowed.json()["question"]
    assert "owner_sub" not in allowed.json()
    for subject, tenant in [("user-b", "tenant-d5"), ("user-c", "tenant-other")]:
        denied = api.get("/agent/traces/trace-a", headers=headers(subject, tenant))
        assert denied.status_code == 404
        assert "private-a" not in denied.text
    for trace_id in ("unknown", "legacy"):
        denied = api.get(f"/agent/traces/{trace_id}", headers=headers())
        assert denied.status_code == 404
        assert "legacy-private" not in denied.text


@pytest.mark.parametrize("path", ["/agent/stream", "/agent/mcp/stream"])
def test_both_http_writers_bind_owner_with_real_trace_wrapper(api_env, monkeypatch, path):
    api, store, _approvals, closed = api_env
    calls = []

    async def fake_discovery():
        calls.append("discovery")
        return ToolRegistry(), "fake MCP system"

    # 内部 asyncio.run 必须发生在消费生成器的工作线程，而非 async endpoint。
    monkeypatch.setattr(mcp_agent, "_build_registry_and_prompt", lambda: asyncio.run(fake_discovery()))
    result = api.post(path, headers=headers(), json={
        "question": "contact test@example.com", "request_id": "client-id",
    })
    assert result.status_code == 200
    assert "event: error" not in result.text
    assert "event: answer" in result.text and "event: done" in result.text
    trace_id = trace_id_from(result.text)
    assert trace_id != "client-id"
    assert calls == (["discovery"] if path.endswith("mcp/stream") else [])
    assert len(closed) == 1
    item = store.get_trace(trace_id, **OWNER)
    assert item is not None and item.owner_sub == "user-a"
    assert "test@example.com" not in item.question
    assert [event.event_type for event in item.events] == ["answer", "done"]
    assert api.get(f"/agent/traces/{trace_id}", headers=headers()).status_code == 200
    assert api.get(f"/agent/traces/{trace_id}", headers=headers("user-b")).status_code == 404
    assert api.get(f"/agent/traces/{trace_id}", headers=headers("user-c", "tenant-other")).status_code == 404


def test_mcp_discovery_is_lazy():
    calls = []
    from unittest.mock import patch

    def fail():
        calls.append("discovery")
        raise RuntimeError("synthetic failure")

    with patch.object(mcp_agent, "_build_registry_and_prompt", fail):
        iterator = mcp_agent.stream_mcp_react_loop("test")
        assert calls == []
        with pytest.raises(RuntimeError):
            next(iterator)
        assert calls == ["discovery"]


def test_mcp_initialization_failure_is_owned_error_and_cleanup(api_env, monkeypatch):
    api, store, _approvals, closed = api_env

    def fail():
        raise RuntimeError("private-init-details")

    monkeypatch.setattr(mcp_agent, "_build_registry_and_prompt", fail)
    result = api.post("/agent/mcp/stream", headers=headers(), json={"question": "test"})
    assert "private-init-details" not in result.text
    assert "event: error" in result.text and "event: done" in result.text
    assert len(closed) == 1
    item = store.get_trace(trace_id_from(result.text), **OWNER)
    assert [event.event_type for event in item.events] == ["error", "done"]


def test_trace_creation_failure_does_not_run_tool_or_overwrite_other_owner(api_env, monkeypatch):
    api, store, approvals, closed = api_env
    store.start_trace("private-b", "collision", **OTHER)
    advanced = []

    def raw(_question, **_kwargs):
        advanced.append(True)
        yield "event: done\ndata: \n\n"

    monkeypatch.setattr(main, "_new_approval_context", lambda user: ApprovalContext(
        approvals, run_id="collision", tenant_id=user.tenant_id, owner_sub=user.sub,
    ))
    monkeypatch.setattr(main, "stream_react_loop", raw)
    result = api.post("/agent/stream", headers=headers(), json={"question": "test"})
    assert "event: error" in result.text and "event: done" in result.text
    assert "private-b" not in result.text
    assert advanced == []
    assert len(closed) == 1
    assert store.get_trace("collision", **OTHER).question == "private-b"
    assert store.get_trace("collision", **OTHER).events == []


def test_record_failure_stops_following_side_effect_and_closes_stream():
    store = ObservabilityStore()
    advanced, closed = [], []

    def raw():
        try:
            yield "event: step\ndata: []\n\n"
            advanced.append("must not run")
            yield "event: done\ndata: \n\n"
        finally:
            closed.append(True)

    output = list(trace_stream(store, "test", "bad-event", raw(), **OWNER))
    assert advanced == [] and closed == [True]
    assert any(item.startswith("event: error") for item in output)
    assert store.event_counts("bad-event", **OWNER) == {"error": 1, "done": 1}


def test_span_context_does_not_cross_yield_and_explicit_close_cleans_up(monkeypatch, caplog):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("d5-isolated-test")
    monkeypatch.setattr(observability, "tracer", local_tracer)
    closed = []

    def raw():
        try:
            with local_tracer.start_as_current_span("tool.child"):
                pass
            yield 'event: step\ndata: {"tool":"search_knowledge","input":"ok"}\n\n'
            yield "event: done\ndata: \n\n"
        finally:
            closed.append(True)

    store = ObservabilityStore()
    iterator = trace_stream(store, "test", "context-check", raw(), **OWNER)
    a, b = contextvars.Context(), contextvars.Context()
    with caplog.at_level(logging.ERROR, logger="opentelemetry.context"):
        assert a.run(next, iterator).startswith("event: trace")
        assert b.run(next, iterator).startswith("event: step")
        a.run(iterator.close)
    assert closed == [True]
    assert "Failed to detach context" not in caplog.text
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["agent.trace"].end_time is not None
    assert spans["tool.child"].parent.span_id == spans["agent.trace"].context.span_id
    provider.shutdown()


def test_store_record_refusal_and_failed_error_record_still_close_raw():
    class RejectingStore(ObservabilityStore):
        def record(self, *_args, **_kwargs):
            raise TraceNotFound("synthetic store rejection")

    store = RejectingStore()
    advanced, closed = [], []

    def raw():
        try:
            yield 'event: step\ndata: {"tool":"search_knowledge","input":"ok"}\n\n'
            advanced.append("must not execute")
            yield "event: done\ndata: \n\n"
        finally:
            closed.append(True)

    output = list(trace_stream(store, "test", "record-refused", raw(), **OWNER))
    assert advanced == [] and closed == [True]
    assert not any(item.startswith("event: step") for item in output)
    assert any(item.startswith("event: error") for item in output)
    assert output[-1].startswith("event: done")
    assert store.get_trace("record-refused", **OWNER).events == []


def test_missing_done_is_recorded_as_well_as_yielded():
    store = ObservabilityStore()
    raw = iter(["event: answer\ndata: done text\n\n"])
    output = list(trace_stream(store, "test", "fallback", raw, **OWNER))
    assert output[-1].startswith("event: done")
    assert store.event_counts("fallback", **OWNER) == {"answer": 1, "done": 1}
