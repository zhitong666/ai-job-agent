import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from pydantic import BaseModel, Field

from app.sensitive_data import mask_value
from app.streaming import sse_event
from app.tracing import get_tracer

tracer = get_tracer("ai-job-agent.agent")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TraceNotFound(LookupError):
    pass


class TraceConflict(ValueError):
    pass


class TraceEvent(BaseModel):
    event_type: str
    timestamp: str
    data: dict = Field(default_factory=dict)


class AgentTrace(BaseModel):
    trace_id: str = Field(frozen=True)
    # None 仅用于表示旧记录；新建入口不允许缺归属。
    tenant_id: str | None = Field(default=None, frozen=True)
    owner_sub: str | None = Field(default=None, frozen=True)
    question: str
    events: list[TraceEvent] = Field(default_factory=list)


class ObservabilityStore:
    def __init__(self):
        self._traces: dict[str, AgentTrace] = {}
        self._lock = threading.Lock()

    def _owned_locked(self, trace_id, tenant_id, owner_sub) -> AgentTrace:
        item = self._traces.get(trace_id)
        if (
            not tenant_id or not owner_sub or item is None
            or not item.tenant_id or not item.owner_sub
            or (item.tenant_id, item.owner_sub) != (tenant_id, owner_sub)
        ):
            raise TraceNotFound("trace not found")
        return item

    def start_trace(self, question: str, trace_id: str | None = None, *, tenant_id: str, owner_sub: str) -> str:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(owner_sub, str) or not owner_sub.strip():
            raise ValueError("owner_sub is required")
        trace_id = trace_id or str(uuid.uuid4())
        item = AgentTrace(
            trace_id=trace_id, tenant_id=tenant_id, owner_sub=owner_sub, question=mask_value(question),
        )
        with self._lock:
            if trace_id in self._traces:
                raise TraceConflict("trace already exists")
            self._traces[trace_id] = item
        return trace_id

    def record(self, trace_id: str, event_type: str, *, tenant_id: str, owner_sub: str, **data) -> None:
        event = TraceEvent(event_type=event_type, timestamp=_now(), data=mask_value(data))
        with self._lock:
            item = self._owned_locked(trace_id, tenant_id, owner_sub)
            item.events.append(event)

    def get_trace(self, trace_id: str, *, tenant_id: str, owner_sub: str) -> AgentTrace | None:
        with self._lock:
            try:
                item = self._owned_locked(trace_id, tenant_id, owner_sub)
            except TraceNotFound:
                return None
            return item.model_copy(deep=True)

    def event_counts(self, trace_id: str, *, tenant_id: str, owner_sub: str) -> dict[str, int]:
        item = self.get_trace(trace_id, tenant_id=tenant_id, owner_sub=owner_sub)
        if item is None:
            return {}
        counts: dict[str, int] = {}
        for event in item.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        return counts

    def export_jsonl(self, path: Path, *, tenant_id: str, owner_sub: str) -> None:
        # 锁内取得该 owner 的快照，文件 I/O 放锁外；不再默认全量导出。
        with self._lock:
            items = [
                item.model_copy(deep=True) for item in self._traces.values()
                if tenant_id and owner_sub
                and item.tenant_id == tenant_id and item.owner_sub == owner_sub
            ]
        with path.open("w", encoding="utf-8") as file:
            for item in items:
                file.write(item.model_dump_json() + "\n")


def sse_to_event(raw_event: str) -> tuple[str, object]:
    event_name = "message"
    data_lines = []
    for line in raw_event.splitlines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    text = "\n".join(data_lines)
    if event_name in {"step", "approval"}:
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"text": text}
    else:
        data = text
    return event_name, data


def _record_stream_event(store, trace_id, event_name, data, owner):
    if event_name in {"step", "approval"}:
        if not isinstance(data, dict):
            raise ValueError("invalid SSE event payload")
        if event_name == "step":
            store.record(
                trace_id, "step", **owner, id=data.get("id"), tool=data.get("tool"),
                input=data.get("input"), observation=data.get("observation"),
            )
        else:
            store.record(
                trace_id, "approval", **owner, approval_id=data.get("approval_id"),
                tool_call_id=data.get("tool_call_id"), tool=data.get("tool"),
                arguments=data.get("arguments"),
            )
    elif event_name == "answer":
        store.record(trace_id, "answer", answer=data, **owner)
    elif event_name == "error":
        store.record(trace_id, "error", message=data, **owner)
    elif event_name == "done":
        store.record(trace_id, "done", **owner)


def trace_stream(store, question, trace_id, stream, *, tenant_id, owner_sub):
    owner = {"tenant_id": tenant_id, "owner_sub": owner_sub}
    iterator = iter(stream)
    span = None
    created = False
    finished = False
    try:
        # 放在 try 内；失败时不向已有 trace 写入错误，也不消费底层工具流。
        store.start_trace(question, trace_id, **owner)
        created = True
        span = tracer.start_span("agent.trace")
        span.set_attribute("agent.trace_id", trace_id)
        span.set_attribute("agent.question", mask_value(question))
        yield sse_event("trace", trace_id)

        while True:
            # 只在一次 next/记录期间激活 Span，退出上下文后才 yield。
            # 这样能给工具子 Span 传父上下文，又不跨 yield 持有 ContextVar token。
            with trace.use_span(
                span, end_on_exit=False,
                record_exception=False, set_status_on_exception=False,
            ):
                try:
                    raw_event = next(iterator)
                except StopIteration:
                    break
                event_name, data = sse_to_event(raw_event)
                _record_stream_event(store, trace_id, event_name, data, owner)
                span.add_event(
                    event_name,
                    attributes={"data": json.dumps(mask_value(data), ensure_ascii=False)},
                )
                if event_name == "error":
                    span.set_status(trace.Status(trace.StatusCode.ERROR))
                if event_name == "done":
                    finished = True
            yield raw_event

        if not finished:
            store.record(trace_id, "done", **owner)
            span.add_event("done")
            yield sse_event("done", "")
    except Exception as exc:
        # 不回传可能含其他对象内容的异常详情；只保留错误类别。
        if span is not None:
            span.set_status(trace.Status(trace.StatusCode.ERROR))
        if created:
            try:
                store.record(trace_id, "error", failure_type=type(exc).__name__, **owner)
                store.record(trace_id, "done", **owner)
            except TraceNotFound:
                pass
        yield sse_event("error", "Agent 执行失败，未继续执行。")
        yield sse_event("done", "")
    finally:
        # 不在 finally yield，也不吞掉 GeneratorExit。
        try:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        finally:
            if span is not None:
                span.end()


observability_store = ObservabilityStore()
