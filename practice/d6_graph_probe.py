"""D6：观察真实 Graph/SQLite/HTTP 路由的边界，不执行真实模型或工具。

从项目根目录运行：.venv/bin/python -m practice.d6_graph_probe
临时切换工作目录后才导入 app，模块默认 SQLite 也只落在临时目录。
安全项只打印当前行为，不把暴露出的缺陷写成应当通过的断言。
"""

import logging
import os
import sqlite3
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch


def probe_routes_and_checkpoint() -> None:
    # 此函数只能由下方 main 在临时工作目录中调用，避免打开项目的真实 checkpoint。
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.sqlite import SqliteSaver

    from app import main, supervisor_graph
    from app.auth_security import create_access_token
    from app.graph_run_store import GraphRunStore
    from app.models import SupervisorDecision, WorkerResult
    from app.shared_memory import SharedMemoryStore
    from app.workers import WorkerRegistry, WorkerSpec

    run_store = GraphRunStore(Path("probe-runs.sqlite"))
    counts = {"worker": 0, "finalize": 0}
    original_finalize = supervisor_graph.finalize_node
    checkpoint_conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(checkpoint_conn)
    memory = SharedMemoryStore(":memory:")

    def fake_worker(decision, retriever, approve_tool_call=None):
        counts["worker"] += 1
        return WorkerResult(
            worker="demo", status="completed", answer=f"fake answer: {decision.context}",
        )

    def fake_decide(question, worker_registry):
        return SupervisorDecision(worker="demo", goal="D6 fake work", context=question)

    def counted_finalize(state):
        counts["finalize"] += 1
        return original_finalize(state)

    def headers(subject, tenant):
        token = create_access_token(subject, ["user"], tenant)
        return {"Authorization": f"Bearer {token}"}

    registry = WorkerRegistry()
    registry.register(WorkerSpec("demo", "D6 synthetic worker", fake_worker))
    a = headers("d6-a", "d6-tenant")
    b = headers("d6-b", "d6-tenant")
    c = headers("d6-c", "d6-other")
    overrides_before = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.get_quota_service] = lambda: object()

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(supervisor_graph, "get_graph_run_store", return_value=run_store))
            stack.enter_context(patch.object(supervisor_graph, "DEFAULT_CHECKPOINTER", saver))
            stack.enter_context(patch.object(supervisor_graph, "build_default_worker_registry", return_value=registry))
            stack.enter_context(patch.object(supervisor_graph, "get_retriever", return_value=object()))
            stack.enter_context(patch.object(supervisor_graph, "decide_worker", new=fake_decide))
            stack.enter_context(patch.object(supervisor_graph, "finalize_node", new=counted_finalize))
            stack.enter_context(patch.object(main, "enforce_quota", new=AsyncMock()))
            # 不用 with TestClient：不进入 lifespan，不连接 PG/Redis/队列。
            api = TestClient(main.app)
            stack.callback(api.close)

            first = api.post(
                "/agent/graph/start", headers=a,
                json={"question": "D6 synthetic A question"},
            )
            assert first.status_code == 200, first.text
            initial = first.json()
            owned = run_store.authorize(initial["run_id"], tenant_id="d6-tenant", owner_sub="d6-a")
            config = supervisor_graph._run_config(owned.checkpoint_thread_id)
            graph = supervisor_graph.build_supervisor_graph()
            snapshot = graph.get_state(config)
            print("PAUSE status/next/interrupts =", [
                initial["status"], initial["next_nodes"], len(snapshot.interrupts),
            ])
            print("PAUSE worker/finalize calls =", [counts["worker"], counts["finalize"]])

            path = f"/agent/graph/state/{initial['run_id']}"
            reads = [
                api.get(path), api.get(path, headers=a),
                api.get(path, headers=b), api.get(path, headers=c),
                api.get("/agent/graph/state/d6-missing-run", headers=a),
            ]
            print("READ anonymous/A/B/C/unknown HTTP =", [r.status_code for r in reads])
            print("READ B/C see A question =", [
                r.json().get("state", {}).get("question") == "D6 synthetic A question"
                for r in reads[2:4]
            ])
            print("READ C payload tenant_id =", reads[3].json().get("tenant_id"))

            before = counts["finalize"]
            resumed_b = api.post(
                "/agent/graph/resume", headers=b, json={"run_id": initial["run_id"]},
            )
            print("RESUME B HTTP/status/finalize delta =", [
                resumed_b.status_code, resumed_b.json().get("status"),
                counts["finalize"] - before,
            ])
            # C 用另一个 A 的暂停 run，避免用已完成的 run 混淆是否发生推进。
            second = api.post(
                "/agent/graph/start", headers=a,
                json={"question": "D6 second synthetic A question"},
            )
            assert second.status_code == 200, second.text
            before = counts["finalize"]
            resumed_c = api.post(
                "/agent/graph/resume", headers=c, json={"run_id": second.json()["run_id"]},
            )
            print("RESUME C HTTP/status/finalize delta =", [
                resumed_c.status_code, resumed_c.json().get("status"),
                counts["finalize"] - before,
            ])

            worker_before, finalize_before = counts["worker"], counts["finalize"]
            owner_resume = api.post("/agent/graph/resume", headers=a, json={"run_id": initial["run_id"]})
            print("RESUME A HTTP/status/worker delta/finalize delta =", [
                owner_resume.status_code, owner_resume.json().get("status"),
                counts["worker"] - worker_before, counts["finalize"] - finalize_before,
            ])
            with_patch_id = api.post("/agent/graph/start", headers=b, json={
                "question": "try reuse", "run_id": initial["run_id"],
            })
            print("START client run_id HTTP =", with_patch_id.status_code)
            seeded = SharedMemoryStore(main.MEMORY_DB_PATH)
            try:
                seeded.set(owned.memory_namespace, "d6_probe", {"value": "synthetic"})
            finally:
                seeded.close()
            memory_url = f"/agent/memory/{initial['run_id']}/d6_probe"
            print("MEMORY A/B/C read HTTP =", [api.get(memory_url, headers=h).status_code for h in (a, b, c)])
            print("MEMORY B delete / A still read HTTP =", [
                api.delete(memory_url, headers=b).status_code,
                api.get(memory_url, headers=a).status_code,
            ])

            before = counts["worker"]
            duplicate_body = {"question": "D6 repeated request", "request_id": "d6-duplicate"}
            repeated = [
                api.post("/agent/graph/start", headers=a, json=duplicate_body)
                for _ in range(2)
            ]
            assert all(r.status_code == 200 for r in repeated), [r.text for r in repeated]
            print("HTTP duplicate same run/worker delta =", [
                repeated[0].json()["run_id"] == repeated[1].json()["run_id"],
                counts["worker"] - before,
            ])

            before = counts["worker"]
            internal_runs = [
                supervisor_graph.start_graph_run(
                    "D6 internal sequential request", request_id="d6-memory-duplicate",
                    tenant_id="d6-tenant", owner_sub="d6-a", run_store=run_store, memory_store=memory,
                    interrupt_before=["finalize"],
                )
                for _ in range(2)
            ]
            print("INTERNAL memory same run/worker delta =", [
                internal_runs[0].run_id == internal_runs[1].run_id,
                counts["worker"] - before,
            ])
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides_before)
        memory.close()
        checkpoint_conn.close()
        # 模块导入创建的默认连接也位于临时目录；此诊断进程即将退出。
        supervisor_graph.DEFAULT_CHECKPOINTER.conn.close()


def probe_timeout() -> None:
    from app.supervisor_graph import _invoke_with_timeout

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowFakeGraph:
        def invoke(self, input_state, config=None):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("D6 fake worker release was not signalled")
            finished.set()
            return {"fake": "done"}

    timed_out = False
    try:
        try:
            _invoke_with_timeout(SlowFakeGraph(), {}, {}, timeout_seconds=0.03)
        except TimeoutError:
            timed_out = True
        was_entered = entered.wait(timeout=1)
        before_release = finished.is_set()
    finally:
        release.set()
        after_release = finished.wait(timeout=2)
    assert was_entered and timed_out and after_release, "D6 线程诊断未完成"
    print("TIMEOUT returned/before-release-finished/after-release-finished =", [
        timed_out, before_release, after_release,
    ])


def main() -> None:
    original_cwd = Path.cwd()
    original_logging_level = logging.root.manager.disable
    with tempfile.TemporaryDirectory(prefix="ai-job-agent-d6-") as directory:
        try:
            os.chdir(directory)
            logging.disable(logging.INFO)
            probe_routes_and_checkpoint()
            probe_timeout()
        finally:
            os.chdir(original_cwd)
            logging.disable(original_logging_level)
    print("D6 probe finished：真实路由/图/临时 SQLite；模型/检索/工具/额度为替身。")
    print("输出用于核对 D6 归属修复；HTTP 并发幂等/真实取消仍未实现。")


if __name__ == "__main__":
    main()
