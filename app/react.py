import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from openai import APIConnectionError, APITimeoutError, RateLimitError

from app.agent import get_retriever
from app.agent_state import AgentState, save_checkpoint
from app.approval import ApprovalContext, ApprovalTicket, canonical_arguments
from app.guards import (
    validate_final_answer,
    validate_tool_arguments,
)
from app.llm import client
from app.model_registry import get_model_name
from app.models import ReactResult, ReactStep
from app.prompt_guard import guard_user_input
from app.streaming import sse_event
from app.tools import FINISH_TOOL_NAME, build_default_registry

REACT_SYSTEM_PROMPT = """你是 AI 岗位咨询 Agent。
先用 search_knowledge 或 list_knowledge_titles 了解知识库，再根据结果回答。
如果用户明确要求投递岗位（例如“投递”“帮我投递”），必须调用 apply_job。
公司名可以从用户描述或知识库中推断；实在没有时填“未知公司”。
岗位名从用户描述中提取，例如“AI Agent”。
如果调用 apply_job，必须先得到用户确认。
只有当你已经能给出最终答案时，才调用 finish。"""


def _tool_call_payload(tool_call, arguments, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _prepare_approval(tool, arguments, call_id, request_id, approval_context):
    if approval_context is not None:
        ticket = approval_context.issue(tool.name, arguments, call_id)
        payload = ticket.public_payload()
    else:
        # 仅保留本地 CLI/fake 回调契约；HTTP 入口必须传 ApprovalContext。
        ticket = None
        payload = {
            "request_id": request_id or "local-only",
            "tool_call_id": call_id,
            "tool": tool.name,
            "arguments": json.loads(canonical_arguments(arguments)),
        }
    return ticket, sse_event("approval", json.dumps(payload, ensure_ascii=False))


def _execute_tool(tool, arguments, retriever, approve_tool_call, *, approval_context: ApprovalContext | None = None, approval_ticket: ApprovalTicket | None = None, tool_call_id: str | None = None):
    try:
        # 新建独立快照；核验与 handler 使用同一份参数，外部改原字典不影响它。
        actual_arguments = json.loads(canonical_arguments(arguments))
        if tool.requires_approval:
            if approval_context is not None:
                if approval_ticket is None or tool_call_id is None:
                    return f"工具 {tool.name} 缺少审批票据，未执行。"
                if not approval_context.wait_and_consume(
                    approval_ticket, tool.name, actual_arguments, tool_call_id,
                ):
                    return f"工具 {tool.name} 已被用户拒绝或审批已取消，未执行。"
            else:
                if approve_tool_call is None:
                    return f"工具 {tool.name} 需要人工确认，但当前没有审批处理程序。"
                if not approve_tool_call(tool.name, actual_arguments):
                    return f"工具 {tool.name} 已被用户拒绝。"
        return tool.handler(actual_arguments, retriever=retriever)
    except Exception as exc:
        return f"工具 {tool.name} 执行失败: {exc}"


def _wants_apply_job(question: str) -> bool:
    normalized = question.lower()
    return any(keyword in normalized for keyword in ("投递", "apply", "应聘"))


def _infer_apply_arguments(question: str) -> dict:
    position = "AI Agent" if "ai agent" in question.lower() else "未知岗位"
    return {"company": "未知公司", "position": position}


def _ensure_apply_job_events(
    question: str,
    steps: list[ReactStep],
    registry,
    retriever,
    approve_tool_call,
    approval_request_id,
    approval_context=None,
):
    tool = registry.get_tool("apply_job")

    if tool is None:
        return

    arguments = _infer_apply_arguments(question)
    action_input = arguments.get(tool.input_field) or question
    guard_error = validate_tool_arguments(tool.name, arguments)

    call_id = f"forced_{uuid.uuid4().hex}"
    ticket = None
    if not guard_error and tool.requires_approval:
        ticket, event = _prepare_approval(
            tool, arguments, call_id, approval_request_id, approval_context,
        )
        yield event

    if guard_error:
        observation = f"工具 {tool.name} 参数校验失败: {guard_error}"
    else:
        observation = _execute_tool(
            tool, arguments, retriever, approve_tool_call, approval_context=approval_context, approval_ticket=ticket, tool_call_id=call_id,
        )

    steps.append(
        ReactStep(
            action=tool.name,
            action_input=action_input,
            observation=observation,
        )
    )

    yield sse_event(
        "step",
        json.dumps(
            {
                "id": call_id,
                "tool": tool.name,
                "input": action_input,
                "observation": observation,
            },
            ensure_ascii=False,
        ),
    )


# 负责重试
def _call_model(messages, tools, max_retries, timeout):
    last_error = None

    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(
                model=get_model_name("agent", os.getenv("OPENAI_MODEL")),
                messages=messages,
                tools=tools,
                tool_choice="auto",
                timeout=timeout,
            )
        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            last_error = exc

            if attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))

    raise RuntimeError(f"模型调用失败，已重试 {max_retries} 次") from last_error


# llm_max_retries 和 timeout 通过参数传入，方便测试和以后调整
def run_react_loop(
    question: str, 
    retriever=None, 
    max_steps: int = 5,
    llm_max_retries: int = 3,
    timeout: int = 10,
    approve_tool_call: Callable[[str, dict], bool] | None = None, # 是一个回调函数，返回 True 表示用户批准，False 表示拒绝
    state: AgentState | None = None,
    checkpoint_path: Path | None = None,
    registry=None,
    system_prompt=REACT_SYSTEM_PROMPT,
) -> ReactResult:
    if state is None:
        state = AgentState(question=question)

    state.question = question
    state.mark_running()

    try:
        input_guard = guard_user_input(question)

        if not input_guard.safe:
            state.mark_finished("我无法处理包含指令注入的内容。")

            if checkpoint_path:
                save_checkpoint(state, checkpoint_path)

            return ReactResult(answer="我无法处理包含指令注入的内容。", steps=[])

        question = input_guard.text

        retriever = retriever or get_retriever()
        registry = registry or build_default_registry()
        tools = registry.to_openai_tools()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]
        steps: list[ReactStep] = []

        for _ in range(max_steps):
            response = _call_model(messages, tools, llm_max_retries, timeout)

            message = response.choices[0].message

            if not message.tool_calls:
                raise RuntimeError("模型没有返回 tool_calls")

            assistant_tool_calls = []
            tool_result_messages = []

            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments or "{}")

                if tool_name == FINISH_TOOL_NAME:
                    answer = validate_final_answer(arguments.get("answer", ""))
                    state.steps = list(steps)
                    state.mark_finished(answer)

                    if checkpoint_path:
                        save_checkpoint(state, checkpoint_path)

                    return ReactResult(answer=answer, steps=steps)

                tool = registry.get_tool(tool_name)

                if tool is None:
                    raise RuntimeError(f"未知工具: {tool_name}")

                action_input = ""

                if tool.input_field:
                    action_input = arguments.get(tool.input_field) or question

                guard_error = validate_tool_arguments(tool_name, arguments)

                if guard_error:
                    observation = f"工具 {tool_name} 参数校验失败: {guard_error}"
                else:
                    observation = _execute_tool(
                        tool,
                        arguments,
                        retriever,
                        approve_tool_call,
                    )

                call_id = getattr(tool_call, "id", None) or f"call_{len(steps)}"

                steps.append(
                    ReactStep(
                        action=tool_name,
                        action_input=action_input,
                        observation=observation,
                    )
                )

                assistant_tool_calls.append(
                    _tool_call_payload(tool_call, arguments, call_id)
                )
                tool_result_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": observation,
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": assistant_tool_calls,
                }
            )
            messages.extend(tool_result_messages)

            state.steps = list(steps)

            if checkpoint_path:
                save_checkpoint(state, checkpoint_path)

        state.steps = list(steps)
        raise RuntimeError("ReAct 循环超过最大步数")

    except Exception:
        state.mark_failed()

        if checkpoint_path:
            try:
                save_checkpoint(state, checkpoint_path)
            except OSError:
                pass

        raise


def stream_react_loop(
    question: str,
    retriever=None,
    max_steps: int = 5,
    llm_max_retries: int = 3,
    timeout: int = 10,
    approve_tool_call: Callable[[str, dict], bool] | None = None,
    approval_request_id: str | None = None,
    state: AgentState | None = None,
    checkpoint_path: Path | None = None,
    registry=None,
    system_prompt=REACT_SYSTEM_PROMPT,
    approval_context: ApprovalContext | None = None,
) -> Iterator[str]:
    if state is None:
        state = AgentState(question=question)

    state.question = question
    state.mark_running()

    try:
        retriever = retriever or get_retriever()
        registry = registry or build_default_registry()
        tools = registry.to_openai_tools()
    except Exception as exc:
        state.mark_failed()
        yield sse_event("error", f"Agent 初始化失败：{exc}")
        yield sse_event("done", "")
        return

    input_guard = guard_user_input(question)

    if not input_guard.safe:
        answer = "我无法处理包含指令注入的内容。"
        state.mark_finished(answer)

        if checkpoint_path:
            save_checkpoint(state, checkpoint_path)

        yield sse_event("answer", answer)
        yield sse_event("done", "")
        return

    question = input_guard.text

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    steps: list[ReactStep] = []

    try:
        for _ in  range(max_steps):
            response = _call_model(messages, tools, llm_max_retries, timeout)
            message = response.choices[0].message

            if not message.tool_calls:
                if message.content:
                    answer = validate_final_answer(message.content)

                    if _wants_apply_job(question) and not any(
                        step.action == "apply_job" for step in steps
                    ):
                        yield from _ensure_apply_job_events(
                            question,
                            steps,
                            registry,
                            retriever,
                            approve_tool_call,
                            approval_request_id,
                            approval_context,
                        )

                    state.steps = list(steps)
                    state.mark_finished(answer)

                    if checkpoint_path:
                        save_checkpoint(state, checkpoint_path)

                    yield sse_event("answer", answer)
                    yield sse_event("done", "")
                    return

                raise RuntimeError("模型没有返回 tool_calls")

            assistant_tool_calls = []
            tool_result_messages = []

            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments or "{}")

                if tool_name == FINISH_TOOL_NAME:
                    answer = validate_final_answer(arguments.get("answer", ""))

                    if _wants_apply_job(question) and not any(
                        step.action == "apply_job" for step in steps
                    ):
                        yield from _ensure_apply_job_events(
                            question,
                            steps,
                            registry,
                            retriever,
                            approve_tool_call,
                            approval_request_id,
                            approval_context,
                        )

                    state.steps = list(steps)
                    state.mark_finished(answer)

                    if checkpoint_path:
                        save_checkpoint(state, checkpoint_path)

                    yield sse_event("answer", answer)
                    yield sse_event("done", "")
                    return

                tool = registry.get_tool(tool_name)

                if tool is None:
                    raise RuntimeError(f"未知工具: {tool_name}")

                action_input = ""

                if tool.input_field:
                    action_input = arguments.get(tool.input_field) or question

                guard_error = validate_tool_arguments(tool_name, arguments)

                call_id = getattr(tool_call, "id", None) or f"call_{len(steps)}"
                ticket = None
                if not guard_error and tool.requires_approval:
                    ticket, event = _prepare_approval(
                        tool, arguments, call_id, approval_request_id, approval_context,
                    )
                    yield event

                if guard_error:
                    observation = f"工具 {tool_name} 参数校验失败: {guard_error}"
                else:
                    observation = _execute_tool(
                        tool, 
                        arguments, 
                        retriever, 
                        approve_tool_call, 
                        approval_context=approval_context,
                        approval_ticket=ticket, 
                        tool_call_id=call_id,
                    )

                steps.append(
                    ReactStep(
                        action=tool_name,
                        action_input=action_input,
                        observation=observation,
                    )
                )

                yield sse_event(
                    "step",
                    json.dumps(
                        {
                            "id": call_id,
                            "tool": tool_name,
                            "input": action_input,
                            "observation": observation,
                        },
                        ensure_ascii=False,
                    ),
                )

                assistant_tool_calls.append(
                    _tool_call_payload(tool_call, arguments, call_id)
                )
                tool_result_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": observation,
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": assistant_tool_calls,
                }
            )
            messages.extend(tool_result_messages)

            state.steps = list(steps)

            if checkpoint_path:
                save_checkpoint(state, checkpoint_path)

        state.steps = list(steps)
        raise RuntimeError("ReAct 循环超过最大步数")

    except Exception as exc:
        state.mark_failed()

        if checkpoint_path:
            try:
                save_checkpoint(state, checkpoint_path)
            except OSError:
                pass

        message = str(exc).strip()
        yield sse_event("error", f"Agent 执行失败：{message}")
        yield sse_event("done", "")
