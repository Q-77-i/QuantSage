"""Agent 下单提案（P3 收尾项 5a）：只生成**当前交易日、待审批**的模拟盘决策。

设计口径（SPEC 附录 A.1）：

* **不新建意图表**：Agent 的决策就是 `paper_decisions` 里一条 `origin="agent"` 的 pending 行，
  与策略决策同表同六态；用户用既有 approve/reject 端点逐条裁决。
* **不另写一套定价/风控**：用 `ScriptedAgentStrategy` 在当前 `as_of` 日跑一次 replay，
  `est_qty` / `evaluate_buy` / `decision_id` / 审计全部与策略路径同源。
* **不推进账户**：只写决策 + 当日该标的的 Agent 审计；下一次 `step` 才按 T+1 开盘成交。
* **不覆盖策略**：该日该标的已有任何决策 → 拒绝（策略优先）；同一自然键由既有 UNIQUE 兜底。

`add_paper_decision` 的 `as_of` 守卫是这里的关键：提案生成期间账户若被推进，插入 0 行，
调用方拿到 409，而不是把决策写到错的日子。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.backtest.types import BarContext, Side, Signal
from app.paper import PaperConflict, PaperError
from app.paper.replay import replay
from app.paper.store import (
    config_from_payload,
    decision_from_row,
    decision_to_payload,
    decision_to_row,
    risk_event_to_row,
)
from app.paper.types import DecisionStatus, decision_id

#: 每个账户每个交易日最多几条待审批 Agent 提案（防一次性刷屏/刷单）
MAX_AGENT_PROPOSALS_PER_DAY = 5
#: 理由长度上限：给模型/用户看的自由文本，不需要小作文
REASON_MAX = 200


class ScriptedAgentStrategy:
    """只在指定日、指定标的返回一条 Agent 信号；其余 bar 一律空仓。

    `name` 是 `Strategy` 协议的一部分（引擎/报告用它标识策略）；这里不是产品策略，
    取固定值，避免被误认为用户策略。
    """

    name = "agent_proposal"

    def __init__(self, symbol: str, trade_date: date, side: Side, reason: str) -> None:
        self.symbol = symbol
        self.trade_date = trade_date
        self.side = side
        self.reason = reason

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if ctx.bar.symbol != self.symbol or ctx.bar.trade_date != self.trade_date:
            return []
        return [Signal(self.side, reason=self.reason, origin="agent")]


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """提案结果：`decision` 非空 = 已创建待审批决策；`blocked` = 被 E5 闸门拦截但已记审计。"""

    account_id: str
    as_of: date
    decision: dict[str, Any] | None
    blocked: bool = False
    detail: str = ""


def _clean_reason(reason: str) -> str:
    text = " ".join((reason or "").split())
    if not text:
        raise PaperError("提案理由不能为空——写清楚为什么买/卖")
    if len(text) > REASON_MAX:
        raise PaperError(f"提案理由最长 {REASON_MAX} 个字符")
    return text


async def propose_agent_decision(
    db: Any,
    *,
    user_id: int,
    account_id: str,
    symbol: str,
    side: Side,
    reason: str,
    data_dir: Path | None = None,
) -> ProposalOutcome:
    """生成一条 Agent 待审批决策（不成交、不推进账户）。状态不满足时抛 `PaperConflict`。"""
    try:
        uuid.UUID(account_id)
    except ValueError as exc:
        raise PaperError("account_id 必须是 UUID（先用 list_paper_accounts 查）") from exc
    row = await db.get_paper_account(user_id, account_id)
    if row is None:
        raise PaperConflict("模拟盘会话不存在")
    if row["status"] != "active":
        raise PaperConflict("会话已跑到区间末端，不能再提案")

    config = config_from_payload(row["config"])
    as_of: date = row["as_of"]
    if as_of >= config.end:
        raise PaperConflict("会话已到区间末端，没有 T+1 可成交")
    if symbol not in config.symbols:
        raise PaperError(f"{symbol} 不在本会话的标的池里（池子：{'、'.join(config.symbols)}）")
    text = _clean_reason(reason)

    decisions = tuple(
        decision_from_row(r) for r in await db.paper_decisions(account_id, 500)
    )
    same_day = [d for d in decisions if d.trade_date == as_of and d.symbol == symbol]
    if same_day:
        raise PaperConflict(f"{symbol} 在 {as_of.isoformat()} 已有决策（策略优先），不再生成第二张")
    pending_agent = [
        d
        for d in decisions
        if d.trade_date == as_of
        and d.origin == "agent"
        and d.status is DecisionStatus.PENDING
    ]
    if len(pending_agent) >= MAX_AGENT_PROPOSALS_PER_DAY:
        raise PaperConflict(
            f"每个会话每个交易日最多 {MAX_AGENT_PROPOSALS_PER_DAY} 条待审批 Agent 提案"
        )

    scripted = ScriptedAgentStrategy(symbol, as_of, side, text)
    # 重放要取数 + 重建账户，扔线程，不占事件循环（同 step 的取数口径）
    result = await asyncio.to_thread(
        replay,
        config,
        decisions,
        through=as_of,
        strategy=scripted,
        account_id=account_id,
        data_dir=data_dir,
    )

    target_id = decision_id(account_id, as_of, symbol, side)
    # 只认 origin=agent 的那张：同日同标的的止损单可能撞同一个 id（方向相同），
    # 那是风控的决策，不是 Agent 的提案，不能拿来冒充。
    target = next(
        (d for d in result.decisions if d.id == target_id and d.origin == "agent"), None
    )
    if target is not None:
        if target.side is Side.SELL and target.est_qty <= 0:
            raise PaperError(f"{symbol} 当前没有可卖持仓")
        audits = [
            risk_event_to_row(event)
            for event in result.risk_events
            if event.origin == "agent" and event.decision_id == target_id
        ]
        written = await db.add_paper_decision(
            account_id=account_id,
            user_id=user_id,
            expected_as_of=as_of,
            decision=decision_to_row(target),
            risk_events=audits,
        )
        if not written:
            raise PaperConflict("会话刚被推进过（并发）——刷新后再看")
        return ProposalOutcome(
            account_id=account_id,
            as_of=as_of,
            decision=decision_to_payload(target),
        )

    # 没有决策：可能是风控 block（有审计）、也可能是一手都买不起/已有挂单（无审计）
    audits = [
        event
        for event in result.risk_events
        if event.origin == "agent"
        and event.trade_date == as_of
        and event.symbol == symbol
        and event.verdict == "block"
    ]
    if audits:
        event = audits[0]
        waits = await db.add_paper_decision(
            account_id=account_id,
            user_id=user_id,
            expected_as_of=as_of,
            decision=None,
            risk_events=[risk_event_to_row(event)],
        )
        if not waits:
            raise PaperConflict("会话刚被推进过（并发）——刷新后再看")
        return ProposalOutcome(
            account_id=account_id,
            as_of=as_of,
            decision=None,
            blocked=True,
            detail=event.detail,
        )

    raise PaperConflict("提案未生成：该标的已有挂单或资金买不起一手")


__all__ = [
    "MAX_AGENT_PROPOSALS_PER_DAY",
    "ProposalOutcome",
    "ScriptedAgentStrategy",
    "propose_agent_decision",
]
