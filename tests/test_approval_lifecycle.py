from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from app.approval import ApprovalConflict, ApprovalContext, ApprovalNotFound, ApprovalStore
from app.react import _execute_tool
from app.tools import Tool

OWNER = {"tenant_id": "tenant-d5", "owner_sub": "user-a"}
ARGS = {"company": "A", "position": "X"}


def issue(store, ttl=2):
    return store.issue(run_id="run-a", tool_call_id="call-a", tool_name="apply_job",
                       arguments=ARGS, ttl=ttl, **OWNER)


def execute(store, ticket, handler):
    return _execute_tool(
        Tool("apply_job", "fake", {}, handler, requires_approval=True),
        ARGS, None, None,
        approval_context=ApprovalContext(store, "run-a", timeout=2, **OWNER),
        approval_ticket=ticket, tool_call_id="call-a",
    )


@pytest.mark.parametrize("now,allowed", [(100.9, True), (101.0, False)])
def test_expiry_boundary_uses_independent_initial_ticket(now, allowed):
    clock = [100.0]
    store = ApprovalStore(clock=lambda: clock[0])
    ticket = issue(store, ttl=1)
    calls = []
    clock[0] = now
    if allowed:
        assert store.decide(ticket.approval_id, True, **OWNER) == "approved"
        execute(store, ticket, lambda args, **_kw: calls.append(args) or "ok")
        assert len(calls) == 1
    else:
        with pytest.raises(ApprovalConflict):
            store.decide(ticket.approval_id, True, **OWNER)
        result = execute(store, ticket, lambda args, **_kw: calls.append(args) or "ok")
        assert "已过期" in result
        assert calls == []


def test_decision_race_accepts_exactly_one_winner():
    store = ApprovalStore()
    ticket = issue(store)
    barrier = Barrier(2, timeout=2)

    def decide(value):
        barrier.wait()
        try:
            return store.decide(ticket.approval_id, value, **OWNER)
        except ApprovalConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, (True, False)))
    assert outcomes.count("conflict") == 1
    assert sum(item in {"approved", "rejected"} for item in outcomes) == 1
    winner = store.status(ticket.approval_id, **OWNER)
    calls = []
    execute(store, ticket, lambda args, **_kw: calls.append(args) or "ok")
    assert len(calls) == (1 if winner == "approved" else 0)


def test_concurrent_consumers_get_only_one_permission():
    store = ApprovalStore()
    ticket = issue(store)
    store.decide(ticket.approval_id, True, **OWNER)
    barrier = Barrier(2, timeout=2)
    calls = []

    def run(_):
        barrier.wait()
        return execute(store, ticket, lambda args, **_kw: calls.append(args) or "ok")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert len(calls) == 1 and outcomes.count("ok") == 1


def test_explicit_close_wakes_a_confirmed_waiter_without_execution():
    store = ApprovalStore()
    ticket = issue(store)
    real_event = store._records[ticket.approval_id].event
    entered = Event()

    class ObservedEvent:
        def wait(self, timeout=None):
            entered.set()
            return real_event.wait(timeout)

        def set(self):
            return real_event.set()

    # 仅测试探针：在业务 Event.wait 的入口确认顺序，不靠 sleep 猜测。
    store._records[ticket.approval_id].event = ObservedEvent()
    calls = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, store, ticket, lambda args, **_kw: calls.append(args) or "ok")
        assert entered.wait(timeout=2)
        store.close_run(run_id="run-a", **OWNER)
        result = future.result(timeout=2)
    assert calls == [] and "执行失败" in result
    with pytest.raises(ApprovalNotFound):
        store.status(ticket.approval_id, **OWNER)


def test_handler_failure_never_restores_consumed_permission():
    store = ApprovalStore()
    ticket = issue(store)
    store.decide(ticket.approval_id, True, **OWNER)
    calls = []

    def fail(arguments, **_kwargs):
        calls.append(arguments)
        raise RuntimeError("fake handler failure")

    assert "执行失败" in execute(store, ticket, fail)
    assert store.status(ticket.approval_id, **OWNER) == "consumed"
    execute(store, ticket, fail)
    assert len(calls) == 1
