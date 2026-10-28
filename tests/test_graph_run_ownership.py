import sqlite3
import sys
from contextlib import ExitStack, closing
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app import main, supervisor_graph as sg
from app.auth_security import create_access_token
from app.graph_run_store import (
    GraphRunConflict,
    GraphRunNotFound,
    GraphRunStorageError,
    GraphRunStore,
)
from app.models import SupervisorDecision, WorkerResult
from app.shared_memory import SharedMemoryStore
from app.workers import WorkerRegistry, WorkerSpec


def headers(subject="a", tenant="tenant-a"):
    return {"Authorization": "Bearer " + create_access_token(subject, ["user"], tenant)}


@pytest.fixture
def env(tmp_path):
    store = GraphRunStore(tmp_path / "runs.sqlite")
    memory_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(tmp_path / "checkpoints.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)
    counts = dict(worker=0, finalize=0, build=0, invoke=0, read=0, memory=0)
    configs = []
    committed = []
    original_build = sg.build_supervisor_graph
    original_finalize = sg.finalize_node

    def worker(decision, retriever, approve_tool_call=None):
        counts["worker"] += 1
        return WorkerResult(worker="demo", answer=f"fake: {decision.context}")

    def decide(question, registry):
        return SupervisorDecision(worker="demo", goal="demo", context=question)

    def finalize(state):
        counts["finalize"] += 1
        return original_finalize(state)

    class MeasuredGraph:
        def __init__(self, graph):
            self.graph = graph

        def invoke(self, state, config):
            counts["invoke"] += 1
            configs.append(config)
            if state is not None:
                # 另开连接，证明登记已提交，而非仅存在于尚未提交的事务。
                with closing(sqlite3.connect(store.db_path)) as db:
                    row = db.execute(
                        "SELECT owner_sub FROM graph_runs WHERE checkpoint_thread_id=?",
                        (config["configurable"]["thread_id"],),
                    ).fetchone()
                committed.append(row)
            return self.graph.invoke(state, config=config)

        def get_state(self, config):
            counts["read"] += 1
            return self.graph.get_state(config)

    def build(*args, **kwargs):
        counts["build"] += 1
        return MeasuredGraph(original_build(*args, **kwargs))

    def memory_factory():
        counts["memory"] += 1
        return SharedMemoryStore(memory_path)

    registry = WorkerRegistry()
    registry.register(WorkerSpec("demo", "fake worker", worker))
    overrides = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.get_quota_service] = lambda: object()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(sg, "get_graph_run_store", return_value=store))
            stack.enter_context(patch.object(sg, "DEFAULT_CHECKPOINTER", saver))
            stack.enter_context(patch.object(sg, "build_default_worker_registry", return_value=registry))
            stack.enter_context(patch.object(sg, "get_retriever", return_value=object()))
            stack.enter_context(patch.object(sg, "decide_worker", new=decide))
            stack.enter_context(patch.object(sg, "finalize_node", new=finalize))
            stack.enter_context(patch.object(sg, "build_supervisor_graph", new=build))
            stack.enter_context(patch.object(main, "enforce_quota", new=AsyncMock()))
            stack.enter_context(patch.object(main, "get_memory_store", new=memory_factory))
            api = TestClient(main.app)  # 不进入 lifespan，不连接外部服务。
            stack.callback(api.close)
            yield SimpleNamespace(
                api=api, store=store, memory_path=memory_path, counts=counts,
                configs=configs, committed=committed, original_build=original_build,
                memory_factory=memory_factory,
                checkpoint_path=tmp_path / "checkpoints.sqlite",
            )
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        conn.close()


def start(env, subject="a", tenant="tenant-a"):
    response = env.api.post(
        "/agent/graph/start", headers=headers(subject, tenant),
        json={"question": "A private question"},
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def test_store_survives_new_instance_and_separates_ids(tmp_path):
    path = tmp_path / "runs.sqlite"
    record = GraphRunStore(path).create(tenant_id="tenant-a", owner_sub="a")
    loaded = GraphRunStore(path).authorize(record.run_id, tenant_id="tenant-a", owner_sub="a")
    assert loaded == record
    assert record.run_id != record.checkpoint_thread_id
    with pytest.raises(FrozenInstanceError):
        record.owner_sub = "b"


@pytest.mark.parametrize("tenant,subject", [("tenant-a", "b"), ("other", "a"), ("other", "c")])
def test_store_wrong_owner_is_not_found(tmp_path, tenant, subject):
    store = GraphRunStore(tmp_path / "runs.sqlite")
    record = store.create(tenant_id="tenant-a", owner_sub="a")
    with pytest.raises(GraphRunNotFound):
        store.authorize(record.run_id, tenant_id=tenant, owner_sub=subject)


@pytest.mark.parametrize("tenant,subject", [("", "a"), ("tenant-a", ""), (None, "a")])
def test_store_requires_nonempty_identity(tmp_path, tenant, subject):
    with pytest.raises(ValueError):
        GraphRunStore(tmp_path / "runs.sqlite").create(tenant_id=tenant, owner_sub=subject)


def test_duplicate_id_rolls_back_without_changing_owner(tmp_path):
    store = GraphRunStore(tmp_path / "runs.sqlite")
    values = [SimpleNamespace(hex=x) for x in ["run", "cp-a", "run", "cp-b"]]
    with patch("app.graph_run_store.uuid.uuid4", side_effect=values):
        record = store.create(tenant_id="tenant-a", owner_sub="a")
        with pytest.raises(GraphRunConflict):
            store.create(tenant_id="tenant-a", owner_sub="b")
    assert store.authorize("run", tenant_id="tenant-a", owner_sub="a") == record
    with pytest.raises(GraphRunNotFound):
        store.authorize("run", tenant_id="tenant-a", owner_sub="b")


def test_owner_start_read_resume_real_graph_and_sqlite(env):
    run_id = start(env)
    record = env.store.authorize(run_id, tenant_id="tenant-a", owner_sub="a")
    assert env.committed == [("a",)]
    assert env.configs[0]["configurable"]["thread_id"] == record.checkpoint_thread_id
    result = env.api.get(f"/agent/graph/state/{run_id}", headers=headers()).json()
    assert result["state"]["question"] == "A private question"
    assert result["next_nodes"] == ["finalize"]
    assert "checkpoint_thread_id" not in result
    assert env.counts["worker"] == 1
    assert env.counts["finalize"] == 0
    response = env.api.post("/agent/graph/resume", headers=headers(), json={"run_id": run_id})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"]["answer"] == "fake: A private question"
    assert env.counts["worker"] == 1
    assert env.counts["finalize"] == 1


@pytest.mark.parametrize("subject,tenant", [("b", "tenant-a"), ("c", "other")])
@pytest.mark.parametrize("operation", ["read", "resume"])
def test_denied_request_does_not_build_or_access_checkpoint(env, subject, tenant, operation):
    run_id = start(env)
    before = env.counts.copy()
    if operation == "read":
        response = env.api.get(f"/agent/graph/state/{run_id}", headers=headers(subject, tenant))
    else:
        response = env.api.post("/agent/graph/resume", headers=headers(subject, tenant), json={"run_id": run_id})
    assert response.status_code == 404
    assert "A private question" not in response.text
    assert env.counts == before


@pytest.mark.parametrize("operation", ["read", "resume"])
def test_unknown_run_is_not_created(env, operation):
    before = env.counts.copy()
    response = (env.api.get("/agent/graph/state/unknown", headers=headers())
                if operation == "read" else env.api.post(
                    "/agent/graph/resume", headers=headers(), json={"run_id": "unknown"}))
    assert response.status_code == 404
    assert env.counts == before
    with pytest.raises(GraphRunNotFound):
        env.store.authorize("unknown", tenant_id="tenant-a", owner_sub="a")


def test_legacy_checkpoint_is_not_automatically_claimed(env):
    graph = env.original_build(interrupt_before=["finalize"])
    config = {"configurable": {"thread_id": "legacy-no-owner"}}
    graph.invoke({"question": "legacy", "handoffs": [], "handoff_count": 0}, config=config)
    before = env.counts.copy()
    assert env.api.get("/agent/graph/state/legacy-no-owner", headers=headers()).status_code == 404
    assert env.api.post("/agent/graph/resume", headers=headers(), json={"run_id": "legacy-no-owner"}).status_code == 404
    assert env.counts == before
    assert graph.get_state(config).values["question"] == "legacy"
    with pytest.raises(GraphRunNotFound):
        env.store.authorize("legacy-no-owner", tenant_id="tenant-a", owner_sub="a")


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/agent/graph/start", {"question": "demo"}),
    ("POST", "/agent/graph/resume", {"run_id": "x"}),
    ("GET", "/agent/graph/state/x", None),
    ("GET", "/agent/memory/x", None),
    ("GET", "/agent/memory/x/key", None),
    ("DELETE", "/agent/memory/x/key", None),
])
def test_anonymous_is_401(env, method, path, body):
    before = env.counts.copy()
    response = env.api.request(method, path, json=body)
    assert response.status_code == 401
    assert env.counts == before


@pytest.mark.parametrize("field,value", [("run_id", "existing"), ("tenant_id", "other"), ("owner_sub", "b")])
def test_start_rejects_client_identity_and_run_id(env, field, value):
    run_id = start(env)
    before = env.counts.copy()
    response = env.api.post("/agent/graph/start", headers=headers("b"), json={
        "question": "overwrite", field: run_id if field == "run_id" else value,
    })
    assert response.status_code == 422
    assert env.counts == before
    assert env.api.get(f"/agent/graph/state/{run_id}", headers=headers()).json()["state"]["question"] == "A private question"


def test_storage_failure_is_503_before_graph_execution(env):
    before = env.counts.copy()
    with patch.object(env.store, "_connection", side_effect=sqlite3.OperationalError("private database detail")):
        response = env.api.post("/agent/graph/start", headers=headers(), json={"question": "demo"})
    assert response.status_code == 503
    assert "private database detail" not in response.text
    assert env.counts == before


def test_bad_storage_path_has_domain_error(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    with pytest.raises(GraphRunStorageError):
        GraphRunStore(blocked / "runs.sqlite")


@pytest.mark.parametrize("subject,tenant", [("b", "tenant-a"), ("c", "other")])
@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/key"), ("DELETE", "/key")])
def test_memory_denied_before_open_or_query(env, subject, tenant, method, suffix):
    run_id = start(env)
    record = env.store.authorize(run_id, tenant_id="tenant-a", owner_sub="a")
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        memory.set(record.memory_namespace, "key", "private memory")
    before = env.counts.copy()
    response = env.api.request(method, f"/agent/memory/{run_id}{suffix}", headers=headers(subject, tenant))
    assert response.status_code == 404
    assert "private memory" not in response.text
    assert env.counts == before
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        assert memory.get(record.memory_namespace, "key").value == "private memory"


def test_owner_memory_read_and_delete_persisted_row(env):
    run_id = start(env)
    record = env.store.authorize(run_id, tenant_id="tenant-a", owner_sub="a")
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        memory.set(record.memory_namespace, "key", {"value": 42})
    assert env.api.get(f"/agent/memory/{run_id}", headers=headers()).json()[0]["key"] == "key"
    assert env.api.get(f"/agent/memory/{run_id}/key", headers=headers()).json()["value"] == {"value": 42}
    assert env.api.delete(f"/agent/memory/{run_id}/key", headers=headers()).status_code == 200
    assert env.api.get(f"/agent/memory/{run_id}/key", headers=headers()).status_code == 404
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        assert memory.get(record.memory_namespace, "key") is None


def test_internal_serial_reuse_is_scoped_by_owner(env):
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        first = sg.start_graph_run("demo-a", memory_store=memory, request_id="same", tenant_id="tenant-a", owner_sub="a")
        again = sg.start_graph_run("demo-a", memory_store=memory, request_id="same", tenant_id="tenant-a", owner_sub="a")
        other = sg.start_graph_run("demo-b", memory_store=memory, request_id="same", tenant_id="tenant-a", owner_sub="b")
    assert first.run_id == again.run_id
    assert other.run_id != first.run_id
    assert other.state["question"] == "demo-b"
    assert env.counts["worker"] == 2


@pytest.mark.parametrize("command", ["state", "resume", "memory"])
def test_cli_denied_before_graph_or_memory_access(env, monkeypatch, command):
    from app import supervisor_graph_cli as cli
    run_id = start(env)
    args = ["graph-cli", command]
    if command == "memory":
        args += ["get"]
    args += [run_id]
    if command == "memory":
        args += ["key"]
    args += ["--tenant-id", "tenant-a", "--owner-sub", "b"]
    monkeypatch.setattr(sys, "argv", args)
    before = env.counts.copy()
    with patch.object(cli, "build_memory_store", new=env.memory_factory), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert env.counts == before


def test_cli_requires_declared_owner(env, monkeypatch):
    from app import supervisor_graph_cli as cli
    monkeypatch.setattr(sys, "argv", ["graph-cli", "start", "demo"])
    before = env.counts.copy()
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert env.counts == before


def test_sqlite_lab_reads_real_checkpoint_and_seeds_owned_memory(env, monkeypatch, capsys):
    from practice import d6_graph_lab as lab
    run_id = start(env)
    monkeypatch.setattr(lab, "CHECKPOINT_DB_PATH", env.checkpoint_path)
    monkeypatch.setattr(lab, "GRAPH_RUN_DB_PATH", env.store.db_path)
    monkeypatch.setattr(lab, "DEFAULT_MEMORY_DB_PATH", env.memory_path)
    monkeypatch.setattr(sys, "argv", [
        "d6-lab", run_id, "--tenant-id", "tenant-a", "--owner-sub", "a", "--seed-memory",
    ])
    before = env.counts.copy()
    lab.main()
    output = capsys.readouterr().out
    checkpoint_line = next(line for line in output.splitlines() if line.startswith("CHECKPOINT rows"))
    assert int(checkpoint_line.split("=")[-1]) > 0
    assert env.counts == before
    record = env.store.authorize(run_id, tenant_id="tenant-a", owner_sub="a")
    with closing(SharedMemoryStore(env.memory_path)) as memory:
        assert memory.get(record.memory_namespace, "d6_probe").value == {"message": "D6 local lab"}
