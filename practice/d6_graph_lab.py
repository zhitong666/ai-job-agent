"""受信本地验收工具：查真实 SQLite；可选写一条独立演示 memory。

不调用 LLM，不执行 Worker。CLI 身份是操作员声明，不能替代 HTTP JWT 验收。
"""

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

from app.graph_run_store import GRAPH_RUN_DB_PATH, GraphRunNotFound
from app.shared_memory import DEFAULT_MEMORY_DB_PATH, SharedMemoryStore
from app.supervisor_graph import CHECKPOINT_DB_PATH, authorize_graph_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--owner-sub", required=True)
    parser.add_argument("--seed-memory", action="store_true")
    args = parser.parse_args()
    try:
        record = authorize_graph_run(args.run_id, tenant_id=args.tenant_id, owner_sub=args.owner_sub)
    except GraphRunNotFound:
        parser.exit(1, "graph run not found\n")
    print("OWNER DB =", GRAPH_RUN_DB_PATH)
    print("OWNED RECORD =", json.dumps(asdict(record), ensure_ascii=False))
    with closing(sqlite3.connect(CHECKPOINT_DB_PATH)) as conn:
        has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone()
        count = (conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (record.checkpoint_thread_id,)).fetchone()[0]
                 if has_table else 0)
    print("CHECKPOINT rows for registered internal key =", count)
    with closing(SharedMemoryStore(DEFAULT_MEMORY_DB_PATH)) as memory:
        if args.seed_memory:
            memory.set(record.memory_namespace, "d6_probe", {"message": "D6 local lab"})
        records = memory.list_namespace(record.memory_namespace)
        print("MEMORY DB =", DEFAULT_MEMORY_DB_PATH)
        print("MEMORY keys =", [item.key for item in records])
    print("没有执行模型或 Worker；HTTP owner/B/C 验收请在浏览器独立完成。")


if __name__ == "__main__":
    main()
