"""D3 练习：用 fake model 写一个最小工具循环。只修改 run_minimal_loop。"""

from collections.abc import Callable


def run_minimal_loop(
    question: str,
    model: Callable[[list[dict]], dict],
    handlers: dict[str, Callable[[dict], str]],
    max_steps: int = 3,
) -> tuple[str, list[dict]]:
    """返回最终答案和完整消息列表；细则见 review/18_D3工具循环与会话归属实作.md。"""
    messages = [{"role": "user", "content": question}]

    for _ in range(max_steps):
        response = model(messages)
        tool_calls = response.get("tool_calls") or []

        if not tool_calls:
            answer = response.get("content", "")
            return answer, messages

        assistant_tool_calls = []
        tool_observation_messages = []
        
        for tool_call in tool_calls:
            name = tool_call.get("name", "")
            if name == "finish":
                answer = tool_call["arguments"]["answer"]
                return answer, messages

            if name not in handlers:
                raise ValueError(f"未知工具 {name}")
            
            assistant_tool_calls.append({
                "id": tool_call["id"],
                "name": name,
                "arguments": tool_call["arguments"],
            })

            observation = handlers[name](tool_call["arguments"])
            tool_observation_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": observation,
            })

        messages.append({"role": "assistant", "tool_calls": assistant_tool_calls})
        messages.extend(tool_observation_messages)
        
    raise RuntimeError("ReAct 循环超过最大步数")


def _fake_model(*responses: dict):
    replies = iter(responses)
    seen_messages = []

    def call(messages):
        seen_messages.append([dict(message) for message in messages])
        return next(replies)

    return call, seen_messages


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _self_check() -> None:
    model, seen = _fake_model(
        {"tool_calls": [_call("c1", "echo", {"text": "hello"})]},
        {"tool_calls": [_call("c2", "finish", {"answer": "完成"})]},
    )
    print("model:", model)
    print("seen:", seen)
    answer, messages = run_minimal_loop(
        "测试", model, {"echo": lambda args: args["text"].upper()}
    )
    print("seen:", seen)
    print("answer:", answer)
    print("messages:", messages)
    assert answer == "完成"
    assert len(seen) == 2
    assert seen[1][-1] == {"role": "tool", "tool_call_id": "c1", "content": "HELLO"}
    assert messages[-1]["tool_call_id"] == "c1"

    model, seen = _fake_model(
        {"tool_calls": [
            _call("left", "echo", {"text": "A"}),
            _call("right", "echo", {"text": "B"}),
        ]},
        {"tool_calls": [_call("end", "finish", {"answer": "AB"})]},
    )
    answer, _ = run_minimal_loop("双工具", model, {"echo": lambda args: args["text"]})
    assert answer == "AB"
    assert [item["tool_call_id"] for item in seen[1] if item["role"] == "tool"] == ["left", "right"]
    assert [item["id"] for item in seen[1][-3]["tool_calls"]] == ["left", "right"]

    model, _ = _fake_model({"tool_calls": [_call("bad", "missing", {})]})
    try:
        run_minimal_loop("未知工具", model, {})
    except ValueError:
        pass
    else:
        raise AssertionError("未知工具应抛 ValueError")

    model, _ = _fake_model({"tool_calls": [_call("c1", "echo", {})]})
    try:
        run_minimal_loop("步数限制", model, {"echo": lambda args: "ok"}, max_steps=1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("达到 max_steps 后应抛 RuntimeError")

    print("D3 minimal loop: 4 scenarios passed")


if __name__ == "__main__":
    _self_check()
