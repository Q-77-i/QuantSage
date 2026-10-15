"""Agent 图：装配 + 事件流翻译。

**图必须在 lifespan 内构建**：checkpointer 在 compile 期就固化进图，导入期建图
会永久拿不到运行时的 saver（历史不落库，且不报错）。

本模块只做两件事：把模型/工具/checkpointer 装成图；把图的事件流翻译成
`(事件名, 载荷)` 序列。SSE 帧编码是 API 层的事，这样翻译逻辑可以离线单测。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.deep.budget import Budget, Limits
from app.agent.prompts import SYSTEM_PROMPT
from app.core.config import get_settings
from app.core.langfuse import TRACE_NAME_CHAT, make_langfuse_metadata

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

#: 四个计数器的名字 → 给人看的说法（到限文案要能读懂）
_BUDGET_LABELS = {
    "llm_calls": "模型调用次数",
    "tokens": "token 用量",
    "seconds": "耗时",
    "cost_usd": "成本",
}


def chat_limits() -> Limits:
    """单轮对话的硬上限（E4c / L6）。

    **步数由图自己管**（`RECURSION_LIMIT` 拦 super-step），这里管另外三样：token、成本、
    墙钟——它们的用处是让「一轮跑飞」有个确定的终点，而不是靠用户盯着点停止。
    复用深路径的 `Limits`（SPEC §5.6：**定义不搬家**，两条路径用同一件）。
    """
    settings = get_settings()
    return Limits(
        llm_calls=RECURSION_LIMIT,
        tokens=settings.chat_max_tokens,
        seconds=settings.chat_max_seconds,
        cost_usd=settings.chat_max_cost_usd,
    )


def budget_message(reason: str) -> str:
    """到限文案。**部分结果 + 明确告知**：说清停在哪、以及已经产出的那部分还在。"""
    label = _BUDGET_LABELS.get(reason, reason)
    return f"本轮达到{label}上限，已停止——以上是已产出的部分。"


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


def _model_of(output: Any) -> str:
    """这次调用用的模型名（给成本计价用）。

    取 `response_metadata.model_name`——ChatLiteLLM 写的是**实例自己的**模型名。chat 跑的是
    flash 档，而 flash **没有下一档**（降级链只从 pro 往 flash 走），所以这里与「实际用的」
    一致。取不到就回空串：`estimate_cost_usd` 对不认识的模型返回 0（**宁可少算，不猜价**）。
    """
    metadata = getattr(output, "response_metadata", None) or {}
    return str(metadata.get("model_name") or "")


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
    user_id: int | None = None,
    callbacks: Sequence[Any] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """驱动图并产出 `(事件名, 载荷)`：token / tool_call / tool_result / done。

    error 事件不在这里产出——图抛出的异常留给调用方决定怎么收尾（API 层转 error 帧，
    脚本直接打印），避免这里吞掉异常让上层失去判断依据。

    **这是 chat 唯一的 config 构造点**（E7c）：Langfuse 的四个运行期字段（会话 / 用户 /
    业务名 / 标签）经 `metadata` 从这里下去。`user_id` 缺省 None 时键改传空串——
    SDK 见非 str 会**静默丢弃**该字段，宁可显式空着也别给个会被丢的类型
    （脚本 `run_chat.py` 就是这么跑的）。
    """
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "metadata": make_langfuse_metadata(
            session_id=thread_id,
            user_id=user_id if user_id is not None else "",
            trace_name=TRACE_NAME_CHAT,
            tags=("chat",),
        ),
    }
    if callbacks:
        config["callbacks"] = list(callbacks)

    # 起跑前体检：历史里若有悬空工具调用（上一轮被中断留下的），先补平——否则这一轮
    # 也会被模型 400 拒掉，会话永远醒不过来
    await heal_dangling_tool_calls(agent, config)

    answer: list[str] = []
    tool_names: dict[str, str] = {}  # run_id → 工具名（on_tool_end 不带名字）
    usage: dict[str, Any] | None = None
    # 四道硬上限（E4c）：逐次模型调用**累加**——此前这里只留最后一次的 usage，
    # 一轮多步的真实用量根本看不见（`done` 帧回给前端的那份仍是最后一次，语义不动）
    limits = chat_limits()
    budget = Budget(limits=limits)
    started = time.monotonic()

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
            output = event["data"].get("output")
            meta = getattr(output, "usage_metadata", None)
            if meta:
                usage = dict(meta)
                budget.record_llm(_model_of(output), usage)
            # 与深路径同口径：**在边界上问一次**（不打断已经发出去的调用）。
            # 到限就如实说、停下——半截回答比一句「已达上限」更让人困惑。
            reason = budget.exceeded()
            if reason is None and time.monotonic() - started >= limits.seconds:
                reason = "seconds"
            if reason is not None:
                log.warning("对话达到 %s 上限（thread=%s）", reason, thread_id)
                yield "error", {"code": "budget", "message": budget_message(reason)}
                return  # 不再发 done：这一轮是被上限停住的，别让前端当成正常收尾

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
