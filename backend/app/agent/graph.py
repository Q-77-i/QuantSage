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
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)

# 只认主模型节点的 token：将来若加摘要一类中间件，它内部也会调模型，
# 不过滤就会把内部摘要的文本混进用户可见的回答
MODEL_NODE = "model"

#: 工具节点名（`create_agent` 的固定命名）。补历史时用它标明「这条 ToolMessage 来自工具节点」
TOOLS_NODE = "tools"

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


def preview_text(text: str, limit: int = TOOL_RESULT_PREVIEW) -> str:
    """超长工具文本的截断口径。

    SSE 预览与历史回看必须同口径，否则同一个工具步骤在两个入口下显示不同内容。
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（已截断，原文 {len(text)} 字符）"


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


def _dangling_tool_calls(messages: Sequence[Any]) -> list[tuple[str, str]]:
    """找出「声明了 tool_calls、却没有对应工具结果」的调用：(call_id, 工具名)。"""
    answered = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }
    return [
        (call["id"], call.get("name") or "unknown")
        for message in messages
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
        if call.get("id") and call["id"] not in answered
    ]


async def heal_dangling_tool_calls(agent: CompiledStateGraph, config: dict[str, Any]) -> int:
    """补上悬空的工具调用，返回补了几条。

    **为什么必须治**：一次运行若被取消在「模型节点已写出 tool_calls、工具节点还没执行」
    之间（客户端断连、进程被杀都行），checkpoint 里就留下一条悬空调用。此后**每一轮**都会
    把这段畸形历史发给模型，而 DeepSeek 对这种历史一律 400 → 该会话永久回「内部错误」
    （2026-10-09 实测：断连后同一会话再问，0.2s 内 400，且状态一直如此）。

    这里的修法是补一条**错误结果**（而不是删掉那条 AIMessage）：消息通道是 append-only
    的 reducer，补结果是最小侵入，且用户回看时能看到「上次在某个工具执行前中断」，
    比整轮凭空消失更好解释。补完历史重新合法，下一轮照常跑。
    """
    snapshot = await agent.aget_state(config)
    messages = (snapshot.values or {}).get("messages") or []
    dangling = _dangling_tool_calls(messages)
    if not dangling:
        return 0
    patches = [
        ToolMessage(
            content=f"（上一轮在「{name}」执行前中断，本次没有取到结果；请重新判断是否需要再查）",
            tool_call_id=call_id,
            status="error",
        )
        for call_id, name in dangling
    ]
    # `as_node` 必须给：消息通道来自哪个节点有歧义时 LangGraph 会直接拒（实测）
    await agent.aupdate_state(config, {"messages": patches}, as_node=TOOLS_NODE)
    log.warning("历史里有 %d 处悬空工具调用，已补错误结果（会话 %s）", len(patches), config)
    return len(patches)


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

    # 起跑前体检：历史里若有悬空工具调用（上一轮被中断留下的），先补平——否则这一轮
    # 也会被模型 400 拒掉，会话永远醒不过来
    await heal_dangling_tool_calls(agent, config)

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
            text = preview_text(_tool_result_text(output))
            yield "tool_result", {
                "id": event["run_id"],
                "name": tool_names.get(event["run_id"]) or event.get("name") or "unknown",
                "content": text,
                "is_error": getattr(output, "status", None) == "error",
            }

    yield "done", {"thread_id": thread_id, "content": "".join(answer), "usage": usage}
