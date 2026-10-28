"""运行归属登记；每次短连接/事务，禁止更新或认领已有 owner。"""

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

GRAPH_RUN_DB_PATH = Path("data/langgraph_checkpoints/graph_runs.sqlite")


class GraphRunNotFound(LookupError):
    pass


class GraphRunConflict(ValueError):
    pass


class GraphRunStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphRunRecord:
    run_id: str
    tenant_id: str
    owner_sub: str
    checkpoint_thread_id: str
    created_at: str

    @property
    def memory_namespace(self) -> str:
        return f"tenant:{self.tenant_id}:run:{self.run_id}"


class GraphRunStore:
    def __init__(self, db_path: str | Path = GRAPH_RUN_DB_PATH):
        if str(db_path) == ":memory:":
            raise ValueError("GraphRunStore requires a SQLite file path")
        self.db_path = Path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS graph_runs (
                        run_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        owner_sub TEXT NOT NULL,
                        checkpoint_thread_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    )
                """)
        except (sqlite3.Error, OSError) as exc:
            raise GraphRunStorageError("graph run storage unavailable") from exc

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def validate_owner(tenant_id, owner_sub) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(owner_sub, str) or not owner_sub.strip():
            raise ValueError("owner_sub is required")

    def create(self, *, tenant_id: str, owner_sub: str) -> GraphRunRecord:
        self.validate_owner(tenant_id, owner_sub)
        record = GraphRunRecord(
            run_id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            owner_sub=owner_sub,
            checkpoint_thread_id=uuid.uuid4().hex,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO graph_runs VALUES (?, ?, ?, ?, ?)",
                    (record.run_id, record.tenant_id, record.owner_sub, record.checkpoint_thread_id, record.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise GraphRunConflict("graph run conflict") from exc
        except (sqlite3.Error, OSError) as exc:
            raise GraphRunStorageError("graph run storage unavailable") from exc
        return record

    def authorize(self, run_id: str, *, tenant_id: str, owner_sub: str) -> GraphRunRecord:
        self.validate_owner(tenant_id, owner_sub)
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """SELECT run_id, tenant_id, owner_sub, checkpoint_thread_id, created_at
                    FROM graph_runs
                    WHERE run_id = ? AND tenant_id = ? AND owner_sub = ?""",
                    (run_id, tenant_id, owner_sub),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise GraphRunStorageError("graph run storage unavailable") from exc
        if row is None:
            raise GraphRunNotFound("graph run not found")
        return GraphRunRecord(*row)


@cache
def get_graph_run_store() -> GraphRunStore:
    return GraphRunStore()
