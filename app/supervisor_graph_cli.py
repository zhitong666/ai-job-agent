import argparse
import json
from contextlib import closing

from app.graph_run_store import GraphRunNotFound
from app.shared_memory import DEFAULT_MEMORY_DB_PATH, SharedMemoryStore
from app.supervisor_graph import (
    authorize_graph_run,
    get_graph_run,
    resume_graph_run,
    start_graph_run,
)


def build_memory_store():
    return SharedMemoryStore(DEFAULT_MEMORY_DB_PATH)


def print_run(run) -> None:
    print("=" * 60)
    print(f"run_id={run.run_id}")
    print(f"request_id={run.request_id}")
    print(f"tenant_id={run.tenant_id}")
    print(f"status={run.status}")
    print(f"next_nodes={run.next_nodes}")

    if run.error:
        print(f"error={run.error}")

    if run.result is not None:
        print(f"worker={run.result.worker_result.worker}")
        print(f"worker_status={run.result.worker_result.status}")
        print(f"answer={run.result.answer}")

    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("question")
    start_parser.add_argument(
        "--interrupt-before",
        action="append",
        default=None,
        help="节点名前暂停，例如 finalize",
    )
    start_parser.add_argument("--request-id", default=None)
    start_parser.add_argument("--tenant-id", default="default")
    start_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
    )

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--tenant-id", default="default")
    resume_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
    )

    state_parser = subparsers.add_parser("state")
    state_parser.add_argument("run_id")
    state_parser.add_argument("--tenant-id", default="default")

    memory_parser = subparsers.add_parser("memory")
    memory_subparsers = memory_parser.add_subparsers(
        dest="memory_command",
        required=True,
    )

    memory_list_parser = memory_subparsers.add_parser("list")
    memory_list_parser.add_argument("run_id")
    memory_list_parser.add_argument("--tenant-id", default="default")

    memory_get_parser = memory_subparsers.add_parser("get")
    memory_get_parser.add_argument("run_id")
    memory_get_parser.add_argument("key")
    memory_get_parser.add_argument("--tenant-id", default="default")

    memory_delete_parser = memory_subparsers.add_parser("delete")
    memory_delete_parser.add_argument("run_id")
    memory_delete_parser.add_argument("key")
    memory_delete_parser.add_argument("--tenant-id", default="default")

    # 这是受信本地运维 CLI，身份由操作员声明；不是公网 JWT 认证替代品。
    for command_parser in (start_parser, resume_parser, state_parser, memory_list_parser, memory_get_parser, memory_delete_parser):
        command_parser.add_argument("--owner-sub", required=True)
    args = parser.parse_args()

    try:
        if args.command == "memory":
            record = authorize_graph_run(
                args.run_id, tenant_id=args.tenant_id, owner_sub=args.owner_sub,
            )
            with closing(build_memory_store()) as memory:
                namespace = record.memory_namespace
                if args.memory_command == "list":
                    for item in memory.list_namespace(namespace):
                        print(f"{item.key}: {json.dumps(item.value, ensure_ascii=False)}")
                elif args.memory_command == "get":
                    item = memory.get(namespace, args.key)
                    if item is None:
                        raise GraphRunNotFound("graph run not found")
                    print(json.dumps(item.value, ensure_ascii=False, indent=2))
                else:
                    if not memory.delete(namespace, args.key):
                        raise GraphRunNotFound("graph run not found")
                    print("deleted=True")
            return

        # state 不初始化 memory；授权在 get/resume 内部先于图构建。
        if args.command == "state":
            print_run(get_graph_run(
                args.run_id, tenant_id=args.tenant_id, owner_sub=args.owner_sub,
            ))
            return

        if args.command == "resume":
            authorize_graph_run(
                args.run_id, tenant_id=args.tenant_id, owner_sub=args.owner_sub,
            )
        with closing(build_memory_store()) as memory:
            if args.command == "start":
                run = start_graph_run(
                    args.question,
                    interrupt_before=args.interrupt_before,
                    memory_store=memory,
                    request_id=args.request_id,
                    tenant_id=args.tenant_id,
                    owner_sub=args.owner_sub,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                run = resume_graph_run(
                    args.run_id,
                    memory_store=memory,
                    tenant_id=args.tenant_id,
                    owner_sub=args.owner_sub,
                    timeout_seconds=args.timeout_seconds,
                )
            print_run(run)
    except GraphRunNotFound as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
