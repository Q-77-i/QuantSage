"""Agent 图：装配 + 事件流翻译。

**图必须在 lifespan 内构建**：checkpointer 在 compile 期就固化进图，导入期建图
会永久拿不到运行时的 saver（历史不落库，且不报错）。

本模块只做两件事：把模型/工具/checkpointer 装成图；把图的事件流翻译成
`(事件名, 载荷)` 序列。SSE 帧编码是 API 层的事，这样翻译逻辑可以离线单测。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)

# 只认主模型节点的 token：将来若加摘要一类中间件，它内部也会调模型，
# 不过滤就会把内部摘要的文本混进用户可见的回答
MODEL_NODE = "model"

# 工具结果进事件流前的截断长度：MCP 返回可达上百 KB，整包塞给前端会拖垮渲染
TOOL_RESULT_PREVIEW = 1500

# 单轮对话的 super-step 上限（默认 25 太宽松，防工具循环烧钱）
RECURSION_LIMIT = 12


def build_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """装配 ReAct 图。工具为空时退化为纯对话（不挂工具节点）。"""
    return create_agent(
        model,
        tools=list(tools),
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


def _tool_result_text(output: Any) -> str:
    """从 on_tool_end 的 output（ToolMessage）里取可读文本。

    MCP 工具走 content_and_artifact，`content` 是内容块列表而非字符串——
    直接 str() 会得到 Python repr。
    """
    content = getattr(output, "content", output)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


async def astream_chat(
    agent: CompiledStateGraph,
    *,
    message: str,
    thread_id: str,
    callbacks: Sequence[Any] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """驱动图并产出 `(事件名, 载荷)`：token / tool_call / tool_result / done。

    error 事件不在这里产出——图抛出的异常留给调用方决定怎么收尾（API 层转 error 帧，
    脚本直接打印），避免这里吞掉异常让上层失去判断依据。
    """
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
    if callbacks:
        config["callbacks"] = list(callbacks)

    answer: list[str] = []
    tool_names: dict[str, str] = {}  # run_id → 工具名（on_tool_end 不带名字）
    usage: dict[str, Any] | None = None

    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": message}]}, config, version="v2"
    ):
        kind = event.get("event")

        if kind == "on_chat_model_stream":
            if event.get("metadata", {}).get("langgraph_node") != MODEL_NODE:
                continue
            text = getattr(event["data"].get("chunk"), "text", "")
            if not text:
                continue  # 工具调用轮与 usage 尾包的 content 为空
            answer.append(text)
            yield "token", {"text": text}

        elif kind == "on_chat_model_end":
            meta = getattr(event["data"].get("output"), "usage_metadata", None)
            if meta:
                usage = dict(meta)

        elif kind == "on_tool_start":
            # input 是**纯工具参数**（注入参数已被框架滤掉），工具名在 event["name"]；
            # 事件里拿不到 tool_call_id，用 run_id 关联 start/end——同一个 run 两端一致
            name = event.get("name") or "unknown"
            tool_names[event["run_id"]] = name
            yield "tool_call", {
                "id": event["run_id"],
                "name": name,
                "args": event["data"].get("input") or {},
            }

        elif kind == "on_tool_end":
            output = event["data"].get("output")
            text = _tool_result_text(output)
            if len(text) > TOOL_RESULT_PREVIEW:
                text = f"{text[:TOOL_RESULT_PREVIEW]}…（已截断，原文 {len(text)} 字符）"
            yield "tool_result", {
                "id": event["run_id"],
                "name": tool_names.get(event["run_id"]) or event.get("name") or "unknown",
                "content": text,
                "is_error": getattr(output, "status", None) == "error",
            }

    yield "done", {"thread_id": thread_id, "content": "".join(answer), "usage": usage}
