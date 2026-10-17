"""Agent 工具：本地行情查询 + 本地事件语义检索（PIT）+ 小石 MCP（白名单制）。

白名单不是可选项：小石暴露 11 个工具，其中动作型的（`plan_history_download`
生成下载计划、`prepare_local_research` 准备本地研究）一旦被 LLM 误调用会触发
昂贵的下载或副作用。只读查询类才允许进工具集。

工具返回值是**给模型看的文本**，不是给程序消费的结构——所以这里做可读化与
截断，并保证「查不到」也返回一句人话，而不是抛异常打断整个图。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.core.untrusted import wrap_untrusted
from app.data import duckdb_client
from app.data.duckdb_client import DataNotReady

log = logging.getLogger(__name__)

# 只读查询类白名单。以 MCP tools/list 的实际结果为准：缺项时启动告警，
# 不静默当作「没问题」（工具改名会让白名单悄悄失效）
XIAOSHI_ALLOWLIST: frozenset[str] = frozenset(
    {
        "get_live_quote",
        "get_event_timeline",
        "search_financial_news",
        "get_data_catalog",
    }
)

# 单次回给模型的明细条数上限，防止一个工具结果吃掉大半个上下文
MAX_ROWS = 60

# 示例标的：只用于提示词与「查不到」时的引导，**不是可用标的的全集**。
# M2a 起本地行情库覆盖全市场 A 股（约 5500 只），这里写死任何清单都只是举例。
SAMPLE_SYMBOLS = ("600519", "300750", "600036")


@tool
async def query_market_bars(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
) -> str:
    """查询本地行情库的日线（前复权），返回区间概览与最近若干交易日的开高低收。

    行情库覆盖全市场 A 股（约 5500 只，2020-01-02 起），任意六位代码都可查；
    下面的示例标的只是举例，不是可用范围。

    Args:
        symbol: 六位股票代码，如 600519（贵州茅台）、300750（宁德时代）、600036（招商银行）
        start: 起始日期 YYYY-MM-DD（含端点），可省略
        end: 结束日期 YYYY-MM-DD（含端点），可省略
        limit: 返回最近多少个交易日的明细，默认 20，上限 60
    """
    code = symbol.strip()
    try:
        # DuckDB 是同步 IO（每次新建连接 + 扫 Parquet），卸到线程避免阻塞事件循环
        rows = await asyncio.to_thread(duckdb_client.bars, code, start=start, end=end)
    except DataNotReady as exc:
        return f"本地行情库不可用：{exc}"
    except Exception as exc:  # noqa: BLE001 —— 查询失败要让模型能自纠，而不是炸掉整条流
        log.warning("行情查询失败 symbol=%s (%s)", code, type(exc).__name__)
        return f"行情查询失败（{type(exc).__name__}），请确认代码形如 600519。"

    if not rows:
        return (
            f"本地行情库没有 {code} 的数据。行情库覆盖全市场 A 股日线（2020-01-02 起），"
            f"请确认代码是六位数字且已上市，例如 {' / '.join(SAMPLE_SYMBOLS)}。"
        )

    size = max(1, min(int(limit), MAX_ROWS))
    first, last = rows[0], rows[-1]
    change = (last["close"] / first["close"] - 1) * 100 if first["close"] else 0.0

    lines = [
        f"{code} 前复权日线：{first['trade_date']} ~ {last['trade_date']}，"
        f"共 {len(rows)} 个交易日",
        f"区间首末收盘：{first['close']:.2f} → {last['close']:.2f}（{change:+.2f}%）",
        f"最近 {min(size, len(rows))} 个交易日：",
    ]
    for row in rows[-size:]:
        pct = row.get("change_pct")
        pct_text = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        # 停牌日的成交量在源数据里就是空（不等于 0 股），如实留空而不是编一个 0
        volume = row.get("volume")
        volume_text = f"{volume / 1e4:.0f} 万股" if isinstance(volume, (int, float)) else "—"
        flag = "（停牌）" if row.get("is_suspended") else ""
        lines.append(
            f"{row['trade_date']}  收 {row['close']:.2f}  涨跌 {pct_text}  "
            f"高 {row['high']:.2f}  低 {row['low']:.2f}  量 {volume_text}{flag}"
        )
    return "\n".join(lines)


@tool
async def search_events(
    query: str,
    as_of: str | None = None,
    symbol: str | None = None,
    event_type: str | None = None,
    top_k: int = 5,
) -> str:
    """语义检索本地事件语料（新闻 / 公告 / 政策 / 人物 / 研报），**按可得时间设卡**。

    与在线接口的区别：本地库带 PIT 语义——`as_of` 之后才「可得」的事件一律不返回，
    所以问「某个时点当时能看到什么」时必须用它，而不是用在线检索。

    Args:
        query: 自然语言问题或关键词，如「央行降准对银行股的影响」
        as_of: 截止时点 YYYY-MM-DD 或完整 ISO 时间；**省略表示「现在」**。
            问历史时点（如「8 月 20 日当时市场看到什么」）必须显式传，否则会用到之后才公开的消息
        symbol: 限定相关标的的六位代码，如 600519
        event_type: 限定事件类型：news / announcement / policy / person / research
        top_k: 返回条数，默认 5，上限 10
    """
    from app.rag import RagNotReady
    from app.rag.correct import correction_note, search_with_correction
    from app.rag.retrieve import SearchQuery, coverage_window

    moment = _parse_as_of(as_of)
    if moment is _INVALID:
        return f"时间格式无法解析：{as_of!r}。请用 YYYY-MM-DD 或完整 ISO 时间。"

    size = max(1, min(int(top_k), 10))
    try:
        # 检索是同步 IO + 模型推理，卸到线程（在 `search_with_correction` 内做）；
        # 零命中时它会改写一次查询，但 as_of / symbol / event_type / top_k 逐字保留
        corrected = await search_with_correction(
            SearchQuery(
                query=query,
                as_of=moment,
                symbol=symbol or None,
                event_type=event_type or None,
                top_k=size,
            ),
        )
        hits = corrected.hits
        note = correction_note(corrected.correction)
        start, end = await asyncio.to_thread(coverage_window)
    except RagNotReady as exc:
        # 降级要说人话，且**不能说成「查不到」**——那是两回事
        return f"本地事件检索不可用：{exc}"
    except Exception as exc:  # noqa: BLE001 —— 查询失败要让模型能自纠，而不是炸掉整条流
        log.warning("事件检索失败 (%s)", type(exc).__name__)
        return f"事件检索失败（{type(exc).__name__}），可改用小石的在线新闻检索。"

    window = f"本地语料覆盖 {start} ~ {end}" if start else "本地语料为空"
    if not hits:
        extra = f"{note}；" if note else ""
        return (
            f"{window}。{extra}该条件下没有检索到事件（可能是时点太早、标的无事件，"
            f"或语料窗口外的历史）。"
        )

    kind = hits[0].score_kind
    lines = [f"{window}｜检索到 {len(hits)} 条{'（未精排，按融合排序）' if kind == 'rrf' else ''}："]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"\n{index}. [{hit.event_type}] {hit.title}")
        lines.append(
            f"   事发 {_short(hit.event_time)}｜可得 {_short(hit.available_at)}"
            f"｜方向 {hit.direction_norm or '—'}｜重要度 {_score(hit.importance_score)}"
        )
        if hit.summary:
            lines.append(f"   摘要：{_clip(hit.summary, 120)}")
        sources = " / ".join(x for x in (hit.source, hit.original_source) if x)
        targets = "/".join(hit.symbols[:6]) if hit.symbols else "—"
        lines.append(f"   来源：{sources or '—'}｜标的：{targets}")
    # 语料是**外部数据**，进 prompt 前打上不可信边界（E8c，SPEC §8.5）。
    # 只包命中的清单，不包上面那些「查不到 / 不可用 / 已改写」的话——那是我们自己的说明。
    body = wrap_untrusted("\n".join(lines), source="本地事件语料")
    return f"{note}\n{body}" if note else body


@tool
async def list_paper_accounts() -> str:
    """列出**当前登录用户**的模拟盘会话（id / 名称 / 状态 / 截至日 / 现金 / 标的池）。

    Agent 在提案前先用它找到 `account_id`。只读；没有会话时返回一句人话，
    不要把「还没有会话」说成「查询失败」。
    """
    from app.agent.context import current_agent_context
    from app.paper.store import account_summary_from_row

    ctx = current_agent_context()
    if ctx is None:
        return "模拟盘不可用：当前不是登录用户发起的对话。"
    try:
        rows = await ctx.db.list_paper_accounts(ctx.user_id, 20)
    except Exception as exc:  # noqa: BLE001 —— 工具失败要让模型能自纠，不炸整条流
        log.warning("模拟盘会话列表失败 (%s)", type(exc).__name__)
        return "模拟盘会话查询失败，请稍后再试。"

    if not rows:
        return "你还没有模拟盘会话。先到模拟盘页新建一个会话，再让我提案。"
    lines = ["你的模拟盘会话（新在前）："]
    for row in rows:
        summary = account_summary_from_row(row)
        lines.append(
            f"- {summary['name']}（id={summary['id']}）｜状态 {summary['status']}｜"
            f"截至 {summary['as_of']}｜现金 {summary['cash']:,.0f}｜"
            f"池子 {'、'.join(summary['symbols'])}"
        )
    return "\n".join(lines)


@tool
async def propose_paper_order(account_id: str, symbol: str, side: str, reason: str) -> str:
    """为**当前登录用户**的模拟盘创建一条**待审批**决策（买/卖），不成交、不批准。

    这是写提案，不是下单：决策会以「待审批」出现在模拟盘页，用户必须逐条批准/驳回；
    批准后下一次「推进一天」才按 T+1 开盘价成交。Agent 不能批准、不能改数量、不能改风控。
    只支持会话池子里的标的；买入按等额配额、卖出全仓（M6 不支持部分成交）。

    Args:
        account_id: 模拟盘会话 id（先用 list_paper_accounts 查）
        symbol: 六位股票代码，且必须在该会话的标的池里
        side: `buy` 或 `sell`
        reason: 一句话理由（≤200 字），会显示在决策卡与审计里
    """
    from app.agent.context import current_agent_context
    from app.backtest.types import Side
    from app.paper import PaperError
    from app.paper.proposal import propose_agent_decision

    ctx = current_agent_context()
    if ctx is None:
        return "模拟盘不可用：当前不是登录用户发起的对话。"
    wanted = side.strip().lower()
    if wanted not in {"buy", "sell"}:
        return "side 只能是 buy 或 sell。"
    try:
        outcome = await propose_agent_decision(
            ctx.db,
            user_id=ctx.user_id,
            account_id=account_id.strip(),
            symbol=symbol.strip(),
            side=Side.BUY if wanted == "buy" else Side.SELL,
            reason=reason,
        )
    except PaperError as exc:
        return f"提案未创建：{exc}"
    except Exception as exc:  # noqa: BLE001 —— 提案失败不能让整条对话断掉
        log.warning("Agent 提案失败 (%s)", type(exc).__name__)
        return f"提案失败（{type(exc).__name__}），请稍后再试。"

    if outcome.decision is not None:
        d = outcome.decision
        action = "买入" if d["side"] == "buy" else "卖出"
        return (
            f"已创建待审批的 Agent 提案：{d['symbol']} {action} {d['est_qty']} 股，"
            f"决策日 {d['trade_date']}（按当日收盘价预估，实际成交价在 T+1 开盘才知道）。"
            f"请到模拟盘页（会话 {outcome.account_id}）批准或驳回；"
            "批准后点「推进一天」才成交。Agent 不能批准或成交。"
        )
    if outcome.blocked:
        return f"提案被风控拦截：{outcome.detail}。未生成决策；详情见模拟盘风控日志。"
    return "提案未生成：该标的已有挂单或资金买不起一手。"


class _Invalid:
    """`as_of` 解析失败时的哨兵（与「未传」区分开）。"""


_INVALID = _Invalid()


def _parse_as_of(value: str | None):
    """`YYYY-MM-DD` 或完整 ISO 时间 → datetime；未传返回 None；解析失败返回哨兵。

    解析规则**不在本模块**：`rag.retrieve.parse_as_of` 是唯一实现（对外 MCP Server 用的是
    同一份——接受哪些写法是对外契约）。这里只把它的 `ValueError` 翻成本工具的哨兵。
    """
    from app.rag.retrieve import parse_as_of

    try:
        return parse_as_of(value)
    except ValueError:
        return _INVALID


def _short(value: str | None) -> str:
    return (value or "—")[:16].replace("T", " ")


def _score(value: float | None) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "—"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def filter_allowlist(tools: list[BaseTool]) -> tuple[list[BaseTool], set[str]]:
    """按白名单挑工具，返回 (选中的工具, 白名单里缺失的名字)。

    缺项要显式返回而不是静默忽略：工具改名或下架时，白名单会**悄悄失效**，
    那种「看起来一切正常、其实少了一个工具」的状态最难排查。
    """
    names = {t.name for t in tools}
    picked = [t for t in tools if t.name in XIAOSHI_ALLOWLIST]
    return picked, XIAOSHI_ALLOWLIST - names


def guard_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """把工具调用纳入可靠性策略（E4b）：小石 MCP 连不上时，撞几次之后**快速失败**，
    不再每次调用都去起一个注定失败的子进程。

    做法是换掉工具的 `coroutine`（MCP 适配器造出来的都是 `StructuredTool`，
    `coroutine` 就是它自己的 `call_tool`），`model_copy` 不动其余字段——名字、描述、
    参数 schema、`response_format` 全部原样，**LLM 看到的东西一个字节都不变**。
    """
    return [_guarded(tool) for tool in tools]


def mark_online(result: Any) -> Any:
    """给小石（在线）工具的返回打上不可信边界（E8c，SPEC §8.5）。

    **形状感知**：MCP 适配器造出来的是 `content_and_artifact`——`content` 是给模型看的
    （字符串或内容块列表），`artifact` 是给程序用的结构化数据。**只包 content**：
    artifact 是我们自己下游要解析的（`graph.py` 的预览与历史回看按它取文本），
    包了会连着把下游一起弄坏。
    """
    if isinstance(result, tuple) and len(result) == 2:
        content, artifact = result
        return (mark_online(content), artifact)
    if isinstance(result, str):
        return wrap_untrusted(result, source="小石在线接口", kind="online")
    if isinstance(result, list):  # 内容块列表：逐块包文本块，其余块原样
        return [
            {**block, "text": wrap_untrusted(block["text"], source="小石在线接口", kind="online")}
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            else block
            for block in result
        ]
    return result  # 不是我们认识的形状：原样返回，宁可不包也不改语义


def _guarded(tool: BaseTool) -> BaseTool:
    from langchain_core.tools import StructuredTool

    inner = getattr(tool, "coroutine", None)
    if not isinstance(tool, StructuredTool) or inner is None:
        return tool  # 不是「一次协程调用」形状的工具：不动它（宁可不包装，不可改语义）

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        from app.core import reliability

        result = await reliability.guard("xiaoshi-mcp", lambda: inner(*args, **kwargs))
        # 小石的返回是**外部数据**：熔断包装之后再打边界（顺序无所谓，但两件事都要做）
        return mark_online(result)

    return tool.model_copy(update={"coroutine": guarded})


async def load_xiaoshi_tools() -> tuple[list[BaseTool], set[str]]:
    """从小石 MCP 拉取工具并按白名单过滤。

    MCP 不可用时**抛出**异常，由调用方决定降级姿态（与 checkpointer 一致，
    不在这里悄悄吞掉）。
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.data.xiaoshi import adapter_connection

    client = MultiServerMCPClient(adapter_connection())
    tools = await client.get_tools()  # 注意：不能 async with，0.3.2 已移除该用法
    picked, missing = filter_allowlist(tools)
    return guard_tools(picked), missing
