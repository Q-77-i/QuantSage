"""结算编排（M7b）：把「决策日志 + 行情」结算成决策记忆，并给出复盘载荷。

职责边界一句话：**能重算的不落库，不能重算的才进 Store**。
* 回合配对、pnl、alpha —— 纯函数（`memory/settle.py`），每次现算；
* 反思文本 —— 模型产物、不可重算 ⇒ 落 Store（`memory/decision_store.py`）；
* 本模块把两边接起来，并保证**幂等**：已平仓的回合永久作数（不再调 LLM），
  未平仓的随账户推进而重结（它的事实还在变）。

调用方两处：`api/reports.py`（复盘 / 结算端点，以及生成报告时的惰性结算）与
`memory/scheduler.py`（每日 job）。两条路走的是**同一个函数**——调度器与手动触发
不该有两套语义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.backtest.benchmark import market_benchmark
from app.data import duckdb_client as dc
from app.memory.decision_store import DecisionMemory, is_current
from app.memory.reflection import build_reflection
from app.memory.settle import mark_open_trips, pair_trips, settle_trips
from app.paper.store import config_from_payload, decision_from_row, equity_from_row
from app.paper.types import Decision, DecisionStatus
from app.report.builder import equity_dates
from app.report.evidence import evidence_key, resolve_evidence

#: 未成交的三种「定了但没交易」的状态（待审批 / 已批准属于在途，不进复盘）
UNFILLED_STATES = (DecisionStatus.REJECTED, DecisionStatus.EXPIRED, DecisionStatus.UNFILLED)

#: 决策流水上限（与研报端点同规：防呆，不是分页）
DECISION_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class SettlementRun:
    """一次结算的读数（端点回执与调度器日志共用）。"""

    settled: int  # 已到期（已平仓）的回合数
    open: int  # 未到期的回合数
    unfilled: int  # 定了没交易的决策数
    saved: int  # 本次新写（或重写）的记忆条数
    reused: int  # 直接复用既有记忆的条数（没花钱）
    lessons: int  # 记忆里有反思文本的条数
    as_of: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "settled": self.settled,
            "open": self.open,
            "unfilled": self.unfilled,
            "saved": self.saved,
            "reused": self.reused,
            "lessons": self.lessons,
            "as_of": self.as_of,
        }


def _evidence_index(
    decisions: Sequence[Decision], data_dir: Any = None
) -> dict[str, tuple[str | None, Any]]:
    """决策 id → （证据键, 证据项）。一次批量查语料，不逐条查。"""
    items = resolve_evidence(
        [d.sources if d.sources else None for d in decisions],
        data_dir=data_dir,
        decision_ids=[d.id for d in decisions],
    )
    index: dict[str, tuple[str | None, Any]] = {}
    for decision in decisions:
        sources = decision.sources or {}
        key = evidence_key(
            str(sources["event_id"]) if sources.get("event_id") else None,
            str(sources["event_time"]) if sources.get("event_time") else None,
        )
        index[decision.id] = (key, items.get(key) if key else None)
    return index


async def settle_account(
    db: Any,
    memory: DecisionMemory,
    *,
    user_id: int,
    account_id: str,
    chat: Any = None,
    data_dir: Any = None,
    now: str | None = None,
) -> tuple[dict[str, Any], SettlementRun]:
    """结算一个账户并把复盘载荷交回去（幂等；只对**新到期**的回合调模型）。"""
    row = await db.get_paper_account(user_id, account_id)
    if row is None:
        raise LookupError("模拟盘会话不存在")
    config = config_from_payload(row["config"])
    decisions = [
        decision_from_row(item) for item in await db.paper_decisions(account_id, DECISION_LIMIT)
    ]
    equity = [equity_from_row(item) for item in await db.paper_equity(account_id)]
    as_of: date = row["as_of"]
    market_days = equity_dates(equity)

    closes = dc.closes_through(list(config.symbols), as_of, data_dir=data_dir)
    trips = mark_open_trips(pair_trips(decisions), closes)

    def benchmark_return(window: Sequence[date]) -> float | None:
        if not window:
            return None
        return market_benchmark(list(window), 1.0, data_dir=data_dir).total_return

    settled_trips = settle_trips(
        trips, as_of=as_of, market_days=market_days, benchmark_return=benchmark_return
    )

    by_decision = _evidence_index(decisions, data_dir)
    stamp = now or datetime.now(UTC).isoformat()

    sources_by_decision = {d.id: (dict(d.sources) if d.sources else None) for d in decisions}
    saved = reused = lessons = 0
    settled_items: list[dict[str, Any]] = []
    open_items: list[dict[str, Any]] = []
    for settled in settled_trips:
        key, item = by_decision.get(settled.decision_id, (None, None))
        stored = await memory.get(
            user_id=user_id, account_id=account_id, decision_id=settled.decision_id
        )
        if is_current(stored, settled, as_of=as_of):
            record = dict(stored or {})
            reused += 1
        else:
            reflection = (
                await build_reflection(settled.to_payload(), chat=chat) if settled.settled else None
            )
            record = await memory.save(
                user_id=user_id,
                account_id=account_id,
                settled=settled,
                reflection=reflection,
                direction=getattr(item, "direction_norm", None),
                evidence_key=key,
                as_of=as_of,
                settled_at=stamp,
            )
            saved += 1
        if (record.get("reflection") or {}).get("text"):
            lessons += 1
        entry = {
            **record,
            "sources": sources_by_decision.get(settled.decision_id),
            "evidence": item.to_payload() if item is not None else None,
        }
        (settled_items if settled.settled else open_items).append(entry)

    unfilled_items = [
        {
            "decision_id": decision.id,
            "symbol": decision.symbol,
            "trade_date": decision.trade_date.isoformat(),
            "side": decision.side.value,
            "status": decision.status.value,
            "status_label": decision.label,
            "reject_code": decision.reject_code,
            "reject_reason": decision.reject_reason,
            "sources": dict(decision.sources) if decision.sources else None,
            "evidence": (
                item.to_payload()
                if (item := by_decision.get(decision.id, (None, None))[1]) is not None
                else None
            ),
        }
        for decision in decisions
        if decision.status in UNFILLED_STATES
    ]

    market_end = dc.latest_dates(data_dir=data_dir)["latest_trade_date"]
    run = SettlementRun(
        settled=len(settled_items),
        open=len(open_items),
        unfilled=len(unfilled_items),
        saved=saved,
        reused=reused,
        lessons=lessons,
        as_of=as_of.isoformat(),
    )
    review = {
        "account": {
            "id": str(row["id"]),
            "name": row["name"],
            "status": row["status"],
            "strategy": config.strategy,
            "strategy_name": config.strategy_name,
            "symbols": list(config.symbols),
        },
        "as_of": as_of.isoformat(),
        "data_end": market_end,
        # 计数放 summary：`settled` / `open` / `unfilled` 三个键在下面被**列表**占用，
        # 平铺会互相覆盖（真写过一版，计数当场被列表盖掉）
        "summary": run.to_payload(),
        "settled": sorted(settled_items, key=lambda item: str(item["entry_date"]), reverse=True),
        "open": sorted(open_items, key=lambda item: str(item["entry_date"]), reverse=True),
        "unfilled": sorted(unfilled_items, key=lambda item: str(item["trade_date"]), reverse=True),
    }
    return review, run
