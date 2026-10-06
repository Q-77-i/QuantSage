"""checkpoint 消息 → 前端历史视图。

对话页回看要看到与实时 SSE **同一种**消息结构，所以这里做的不是逐条直译，而是
按回合合并。两个真实形态逼着必须这么做：模型常在**同一条** AIMessage 里既给正文
又给 tool_calls；一次提问也常产出多条 AIMessage（说话 → 调工具 → 再说话）。
实时流里这些全是同一个助手气泡，直译却会拆成好几个。

不依赖 FastAPI 与 checkpointer：入参就是 `channel_values["messages"]`，纯函数好单测。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.agent.graph import preview_text

# 模型偶尔不给 tool_call id。前端契约要求它是 string，且要能配对调用与结果，
# 所以补一个仅在本会话内唯一的合成 id
CALL_ID_FALLBACK = "call-{index}-{position}"


def _assistant(content: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tools": tools}


def _tool_steps(message: Any, index: int) -> list[dict[str, Any]]:
    """从 AIMessage 取出工具步骤。结果留待 ToolMessage 按 id 回填。"""
    steps = []
    for position, call in enumerate(getattr(message, "tool_calls", None) or []):
        steps.append(
            {
                "id": call.get("id") or CALL_ID_FALLBACK.format(index=index, position=position),
                "name": call.get("name") or "unknown",
                "args": call.get("args") or {},
                "content": None,
                "is_error": False,
            }
        )
    return steps


def messages_to_history(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """转换成前端可渲染的消息列表。合并语义见 SPEC §6。

    正文用 `BaseMessage.text`：它会拼 content blocks 并排除 reasoning 块，
    正好是本项目要的「给用户看的部分」。
    """
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None  # 本回合的助手消息：一个 human 之后只留一条
    filled: dict[str, dict[str, Any]] = {}  # 步骤 id → 步骤对象（归属后仍可回填结果）

    def flush() -> None:
        nonlocal current
        if current is not None and (current["content"].strip() or current["tools"]):
            out.append(current)
        current = None

    for index, message in enumerate(messages):
        kind = getattr(message, "type", None)

        if kind == "human":
            flush()  # 上一回合到此为止（含「调了工具没等到正文」的中断形态）
            text = message.text
            if text.strip():
                out.append({"role": "user", "content": text, "tools": []})

        elif kind == "ai":
            # 真实模型常常在**同一条** AIMessage 里既给正文又给 tool_calls（"我先查一下" +
            # 调用），拿到结果后再发一条带正文的——实时流里它们是同一个气泡，
            # 所以这里按回合合并，不能按消息条数拆。
            if current is None:
                current = _assistant("", [])
            current["content"] += message.text
            steps = _tool_steps(message, index)
            for step in steps:
                filled[step["id"]] = step
            current["tools"].extend(steps)

        elif kind == "tool":
            if current is None:
                continue
            step = filled.get(getattr(message, "tool_call_id", None) or "")
            if step is not None:  # 找不到调用的结果是孤儿，丢弃
                step["content"] = preview_text(message.text)
                step["is_error"] = getattr(message, "status", None) == "error"

        # system 与未知类型跳过

    flush()
    return out
