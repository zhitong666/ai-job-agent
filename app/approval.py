import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field


class ApprovalNotFound(LookupError):
    pass


class ApprovalConflict(ValueError):
    pass


def canonical_arguments(arguments: dict) -> str:
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return json.dumps(
        arguments, 
        sort_keys=True, 
        separators=(",", ":"),
        ensure_ascii=False, 
        allow_nan=False,
    )


def arguments_hash(arguments: dict) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalTicket:
    approval_id: str
    run_id: str
    tool_call_id: str
    tenant_id: str
    owner_sub: str
    tool_name: str
    arguments_json: str
    args_hash: str
    created_at: float
    expires_at: float

    def public_payload(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "request_id": self.run_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool": self.tool_name,
            "arguments": json.loads(self.arguments_json),
            "status": "pending",
            "expires_at": self.expires_at,
        }


@dataclass
class _Record:
    ticket: ApprovalTicket
    status: str = "pending"
    event: threading.Event = field(default_factory=threading.Event)


class ApprovalStore:
    """单进程审批 Store；锁只保护短状态转换，不在锁内等待用户。"""

    def __init__(self, *, clock: Callable[[], float] = time.time, max_records=1024):
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._max_records = max_records

    def issue(self, *, run_id, tool_call_id, tenant_id, owner_sub, tool_name, arguments, ttl=120) -> ApprovalTicket:
        if not all((run_id, tool_call_id, tenant_id, owner_sub, tool_name)) or ttl <= 0:
            raise ValueError("审批绑定字段及 ttl 必须有效")
        snapshot = canonical_arguments(arguments)
        now = self._clock()
        ticket = ApprovalTicket(
            approval_id=str(uuid.uuid4()), 
            run_id=run_id, 
            tool_call_id=tool_call_id,
            tenant_id=tenant_id, 
            owner_sub=owner_sub, 
            tool_name=tool_name,
            arguments_json=snapshot,
            args_hash=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            created_at=now, 
            expires_at=now + ttl,
        )
        with self._lock:
            for key, record in list(self._records.items()):
                if record.ticket.expires_at <= now:
                    record.status = "expired"
                    record.event.set()
                    del self._records[key]
            if len(self._records) >= self._max_records:
                raise ApprovalConflict("审批数量达到上限，请稍后重试")
            self._records[ticket.approval_id] = _Record(ticket)
        return ticket

    def _owned_locked(self, approval_id, tenant_id, owner_sub) -> _Record:
        record = self._records.get(approval_id)
        if record is None or (
            record.ticket.tenant_id != tenant_id or record.ticket.owner_sub != owner_sub
        ):
            raise ApprovalNotFound("审批不存在或无权访问")
        if self._clock() >= record.ticket.expires_at:
            record.status = "expired"
            record.event.set()
            raise ApprovalConflict("审批已过期，请重新发起任务")
        return record

    @staticmethod
    def _match(record, *, run_id, tool_call_id, tool_name, arguments):
        ticket = record.ticket
        if (ticket.run_id != run_id or ticket.tool_call_id != tool_call_id or ticket.tool_name != tool_name or ticket.args_hash != arguments_hash(arguments)):
            raise ApprovalConflict("审批与实际工具调用不匹配，未执行")

    def decide(self, approval_id, approved: bool, *, tenant_id, owner_sub) -> str:
        if type(approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        with self._lock:
            record = self._owned_locked(approval_id, tenant_id, owner_sub)
            if record.status != "pending":
                raise ApprovalConflict("审批已处理，请勿重复提交")
            record.status = "approved" if approved else "rejected"
            record.event.set()
            return record.status

    def wait_and_consume(self, approval_id, *, tenant_id, owner_sub, run_id, tool_call_id, tool_name, arguments, timeout=120) -> bool:
        binding = dict(
            run_id=run_id, 
            tool_call_id=tool_call_id, 
            tool_name=tool_name, 
            arguments=arguments
        )
        with self._lock:
            record = self._owned_locked(approval_id, tenant_id, owner_sub)
            self._match(record, **binding)
            wait_for = min(max(0, timeout), record.ticket.expires_at - self._clock())
        # 不持有锁等待，审批 POST 才能进入 decide。
        signaled = record.event.wait(timeout=max(0, wait_for))
        with self._lock:
            record = self._owned_locked(approval_id, tenant_id, owner_sub)
            self._match(record, **binding)
            if not signaled:
                record.status = "cancelled"
                record.event.set()
                return False
            if record.status in {"rejected", "cancelled"}:
                return False
            if record.status != "approved":
                raise ApprovalConflict("审批不可重复消费")
            # 在 handler 之前原子消费许可；这不等于外部副作用恰好一次。
            record.status = "consumed"
            return True

    def status(self, approval_id, *, tenant_id, owner_sub) -> str:
        with self._lock:
            return self._owned_locked(approval_id, tenant_id, owner_sub).status

    def close_run(self, *, run_id, tenant_id, owner_sub) -> None:
        with self._lock:
            for key, record in list(self._records.items()):
                ticket = record.ticket
                if (ticket.run_id, ticket.tenant_id, ticket.owner_sub) == (run_id, tenant_id, owner_sub,):
                    record.status = "cancelled"
                    record.event.set()
                    del self._records[key]


@dataclass(frozen=True)
class ApprovalContext:
    store: ApprovalStore
    run_id: str
    tenant_id: str
    owner_sub: str
    timeout: float = 120

    def issue(self, tool_name, arguments, tool_call_id):
        return self.store.issue(
            run_id=self.run_id, 
            tenant_id=self.tenant_id, 
            owner_sub=self.owner_sub,
            tool_call_id=tool_call_id, 
            tool_name=tool_name, 
            arguments=arguments,
            ttl=self.timeout,
        )

    def wait_and_consume(self, ticket, tool_name, arguments, tool_call_id):
        return self.store.wait_and_consume(
            ticket.approval_id, 
            tenant_id=self.tenant_id, 
            owner_sub=self.owner_sub,
            run_id=self.run_id, 
            tool_call_id=tool_call_id,
            tool_name=tool_name, 
            arguments=arguments, 
            timeout=self.timeout,
        )

    def close(self):
        self.store.close_run(
            run_id=self.run_id, 
            tenant_id=self.tenant_id, 
            owner_sub=self.owner_sub,
        )


approval_store = ApprovalStore()
