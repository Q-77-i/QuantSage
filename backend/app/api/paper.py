"""模拟盘 API（M6）：七个端点。业务全在 `app.paper`，这里只做校验、编排与形状转换。

三条把关写在**端点之前**（响应形状一旦出去就改不了了）：

1. **用户策略的闸门**与 `POST /backtest` 同一顺序——归属 404 → 静态检查 `error`=0 → 参数校验。
   模拟盘只认库里的源码（先保存才能跑），且每只标的的数据在开跑前就要在本地存在。
2. **创建期校验**：区间 2–250 个交易日、`end` 不得晚于池子最后一根 bar、
   **每只配额 ≥ 最近收盘价 × 100**（否则那只标的一辈子不出手，而用户看不出为什么）。
3. **推进走乐观并发**：`as_of` 对不上就是 409（双击「推进」不会推进两次）。

「推进」是**从会话起点全量重放**（见 `app.paper.replay` 的模块 docstring）：内置策略在
线程里跑（CPU + DuckDB IO，不能在 async 端点里直接跑），用户策略整段丢进沙箱子进程。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.backtest import CostOptions
from app.api.strategies import normalize_strategy_id
from app.backtest.strategies import available_strategies, validate_params
from app.core.auth import require_db, require_user
from app.data import calendar as cal
from app.data import duckdb_client as dc
from app.paper import MAX_SESSIONS, MAX_SYMBOLS, PaperConflict
from app.paper.replay import replay
from app.paper.store import (
    account_from_row,
    account_summary_from_row,
    config_from_payload,
    config_to_payload,
    decision_from_row,
    decision_to_payload,
    decision_to_row,
    equity_from_row,
    equity_to_row,
    position_from_row,
    position_to_row,
)
from app.paper.types import Decision, DecisionStatus, PaperConfig, ReplayResult
from app.strategy import USER_STRATEGY, StrategyCheckFailed
from app.strategy.api import parse_meta
from app.strategy.params import validate_params as validate_user_params
from app.strategy.sandbox import run_user_paper
from app.strategy.static_check import check_source, has_errors

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])

SYMBOL_PATTERN = r"^\d{6}$"

#: 决策流水的返回上限。池子 ≤20、区间 ≤250 个交易日，实测一轮 20 标的 × 181 个交易日
#: 只有约 110 条信号——这个上限是防呆，不是分页。
DECISION_LIMIT = 500

#: 会话状态：`active` 可继续推进；`finished` 已跑到区间末端。
ACTIVE = "active"
FINISHED = "finished"


class PaperAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000.0)
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS)
    strategy: str
    strategy_id: str | None = None
    params: dict[str, float] = Field(default_factory=dict)
    start: date
    #: 缺省取**池子最后一根 bar 的交易日**（数据到哪模拟到哪）；给了但晚于该日 → 422，不静默截断
    end: date | None = None
    costs: CostOptions = Field(default_factory=CostOptions)

    @field_validator("symbols")
    @classmethod
    def _clean_symbols(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for symbol in value:
            if not symbol or len(symbol) != 6 or not symbol.isdigit():
                raise ValueError(f"标的代码必须是六位数字（收到 {symbol!r}）")
            if symbol not in cleaned:
                cleaned.append(symbol)
        return cleaned

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        if value != USER_STRATEGY and value not in available_strategies():
            names = "、".join([*available_strategies(), USER_STRATEGY])
            raise ValueError(f"未知策略 {value!r}；可用策略：{names}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.strategy == USER_STRATEGY:
            if not self.strategy_id:
                raise ValueError('strategy="user" 时必须带 strategy_id——策略要先保存才能跑')
        elif self.strategy_id is not None:
            raise ValueError('strategy_id 只能与 strategy="user" 一起用')
        if self.strategy != USER_STRATEGY:
            errors = validate_params(self.strategy, self.params)
            if errors:
                raise ValueError("；".join(errors))
        return self


class PaperRunRequest(BaseModel):
    """一键跑到结束。`all` = 全批（**等价于回测**，用于出完整曲线）；`none` = 全驳。"""

    model_config = ConfigDict(extra="forbid")
    approve: Literal["all", "none"] = "all"


class PaperDecisionAction(BaseModel):
    """审批动作暂时没有参数（不做审批时改数量/改价，SPEC §7 明确不做）。

    留一个空模型而不是裸 POST：将来要加备注一类字段时，请求形状不用改。
    """

    model_config = ConfigDict(extra="forbid")


def normalize_decision_id(raw: str) -> str:
    """非 UUID 直接 422：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="决策 id 必须是 UUID") from exc


async def _strategy_source(
    request: Request, user_id: int, body: PaperAccountRequest
) -> tuple[str | None, str | None]:
    """用户策略的闸门（同 `POST /backtest` 的顺序）：返回 `(源码, 策略名)`。

    内置策略返回 `(None, None)`。静态检查必须在 spawn 之前——那正是「运行前带行号给话术」成立的前提。
    """
    if body.strategy != USER_STRATEGY:
        return None, None
    db = require_db(request)
    strategy_id = normalize_strategy_id(body.strategy_id or "")
    row = await db.get_strategy(user_id, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    findings = check_source(row["code"])
    if has_errors(findings):
        raise StrategyCheckFailed(findings)  # main.py 翻 422 + findings
    meta = parse_meta(row["code"])  # error=0 时不该抛；真抛了由既有处理器兜成 422
    validate_user_params(meta.params, body.params)  # 越界抛 StrategyRejected → 422
    return row["code"], row["name"]


async def _run_replay(
    *,
    source: str | None,
    config: PaperConfig,
    account_id: str,
    decisions: tuple[Decision, ...] = (),
    through: date | None = None,
    auto: DecisionStatus | None = None,
) -> ReplayResult:
    """跑一次重放。内置策略在**线程**里跑，用户策略整段丢进**沙箱子进程**（D5）。"""
    if source is not None:
        return await run_user_paper(
            source,
            config=config,
            account_id=account_id,
            decisions=decisions,
            through=through,
            auto=auto.value if auto else None,
            strategy_name=config.strategy_name,
        )
    return await asyncio.to_thread(
        replay, config, decisions, through=through, auto=auto, account_id=account_id
    )


async def _source_for_account(
    request: Request, user_id: int, account_row: dict[str, Any]
) -> str | None:
    """老会话继续推进时把源码取回来——**策略被删了就没法重放**，如实 409 而不是静默换策略。"""
    config = config_from_payload(account_row["config"])
    if config.strategy != USER_STRATEGY:
        return None
    db = require_db(request)
    strategies = await db.list_strategies(user_id)
    match = next((s for s in strategies if s["name"] == config.strategy_name), None)
    row = await db.get_strategy(user_id, match["id"]) if match else None
    if row is None:
        raise PaperConflict(
            f"会话用的策略「{config.strategy_name}」已不在你的策略库里——"
            "模拟盘只认库里的源码，请先恢复这条策略"
        )
    return row["code"]


async def _detail(request: Request, user_id: int, account_id: str) -> dict[str, Any]:
    """会话详情：账户卡 + 持仓 + 决策流水 + 待审批 + 净值曲线 + 规则生效情况。"""
    db = require_db(request)
    row = await db.get_paper_account(user_id, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在")

    config = config_from_payload(row["config"])
    decisions = tuple(decision_from_row(r) for r in await db.paper_decisions(account_id, DECISION_LIMIT))
    equity = [equity_from_row(r) for r in await db.paper_equity(account_id)]
    days = cal.sessions(config.start, config.end)

    return {
        "account": account_from_row(row),
        "progress": {
            "start": config.start.isoformat(),
            "end": config.end.isoformat(),
            "as_of": row["as_of"].isoformat(),
            "days_total": len(days),
            "days_done": sum(1 for day in days if day <= row["as_of"]),
        },
        "valuation": equity[-1] if equity else None,
        "positions": [position_from_row(r) for r in await db.paper_positions(account_id)],
        "decisions": [decision_to_payload(d) for d in decisions],
        "pending": [
            decision_to_payload(d) for d in decisions if d.status is DecisionStatus.PENDING
        ],
        "equity_curve": equity,
    }


def _new_decisions(before: tuple[Decision, ...], result: ReplayResult) -> tuple[Decision, ...]:
    """本次重放**新生成**的决策（按 id 差集）——`step` 的响应用它。"""
    known = {d.id for d in before}
    return tuple(d for d in result.decisions if d.id not in known)


def _moved(day_outcome: Any, status: DecisionStatus) -> list[dict[str, Any]]:
    return [
        decision_to_payload(d)
        for d in (*day_outcome.filled, *day_outcome.expired, *day_outcome.unfilled)
        if d.status is status
    ]


@router.post("/accounts", status_code=201)
async def create_account(
    request: Request,
    body: PaperAccountRequest,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """建会话：校验 → 定窗口 → 重放第一天 → 落库（一个事务）。"""
    user_id = int(user["id"])
    source, strategy_name = await _strategy_source(request, user_id, body)

    latest = await asyncio.to_thread(dc.latest_closes, list(body.symbols))
    missing = [s for s in body.symbols if s not in latest]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"本地没有 {'、'.join(missing)} 的行情数据；本地行情覆盖 5,798 只 A 股，"
            "换标的或先跑 scripts/download_bars.py",
        )
    pool_last = max(entry["trade_date"] for entry in latest.values())
    if body.end is not None and body.end > pool_last:
        raise HTTPException(
            status_code=422,
            detail=f"区间终点 {body.end} 晚于本地行情最后一根 bar（{pool_last}）——"
            "模拟盘只能回放已经发生的日子",
        )
    end = body.end or pool_last

    try:
        days = cal.sessions(body.start, end)
    except cal.CalendarOutOfRange as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(days) < 2:
        raise HTTPException(
            status_code=422,
            detail=f"区间内只有 {len(days)} 个交易日；模拟盘至少要两个"
            "（T 日生成决策、T+1 开盘成交）",
        )
    if len(days) > MAX_SESSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"区间内有 {len(days)} 个交易日，超过上限 {MAX_SESSIONS}——"
            "拆分区间，别让一次会话跑太久",
        )

    quota = body.initial_cash / len(body.symbols)
    too_small = [
        f"{s}（配额 {quota:,.0f} 元 < 一手 {latest[s]['close'] * 100:,.0f} 元）"
        for s in body.symbols
        if quota < latest[s]["close"] * 100
    ]
    if too_small:
        raise HTTPException(
            status_code=422,
            detail="每只标的的配额买不起一手："
            + "；".join(too_small)
            + "——把初始资金调大，或把池子缩小",
        )

    config = PaperConfig(
        initial_cash=body.initial_cash,
        symbols=tuple(body.symbols),
        strategy=body.strategy,
        start=days[0],
        end=end,
        params=dict(body.params),
        costs=body.costs.to_model(),
        strategy_name=strategy_name,
    )
    account_id = str(uuid.uuid4())
    result = await _run_replay(
        source=source, config=config, account_id=account_id, through=days[0]
    )

    await require_db(request).create_paper_account(
        account_id=account_id,
        user_id=user_id,
        name=body.name,
        config=config_to_payload(config),
        cash=result.state.cash,
        as_of=result.as_of,
        rules={k: dict(v) for k, v in result.rules.items()},
        positions=[
            position_to_row(account_id, symbol, position)
            for symbol, position in result.state.positions.items()
        ],
        decisions=[_row(account_id, d) for d in result.decisions],
        equity=[equity_to_row(account_id, mark) for mark in result.equity_curve],
    )
    return await _detail(request, user_id, account_id)


def _row(account_id: str, decision: Decision) -> dict[str, Any]:
    return decision_to_row(decision)


@router.get("/accounts")
async def list_accounts(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """我的会话（最近创建在前）。"""
    rows = await require_db(request).list_paper_accounts(int(user["id"]), limit)
    return [account_summary_from_row(row) for row in rows]


@router.get("/accounts/{account_id}")
async def get_account(
    request: Request,
    account_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """会话详情。越权与不存在同为 404，不泄露存在性。"""
    return await _detail(request, int(user["id"]), normalize_decision_id(account_id))


@router.post("/accounts/{account_id}/step")
async def step_account(
    request: Request,
    account_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """推进**一个**交易日：成交上一日的批准单 → 结算 → 生成本日决策（待审批）。"""
    user_id = int(user["id"])
    normalized = normalize_decision_id(account_id)
    db = require_db(request)
    row = await db.get_paper_account(user_id, normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在")
    if row["status"] != ACTIVE:
        raise PaperConflict("会话已跑到区间末端，不能再推进")

    config = config_from_payload(row["config"])
    days = cal.sessions(config.start, config.end)
    current = row["as_of"]
    nxt = next((day for day in days if day > current), None)
    if nxt is None:  # 理论上 status 已置 finished；这里是兜底
        raise PaperConflict("已经到区间末端了")

    source = await _source_for_account(request, user_id, row)
    before = tuple(decision_from_row(r) for r in await db.paper_decisions(normalized, DECISION_LIMIT))
    result = await _run_replay(
        source=source, config=config, account_id=normalized, decisions=before, through=nxt
    )

    advanced = await db.paper_advance(
        account_id=normalized,
        user_id=user_id,
        expected_as_of=current,
        cash=result.state.cash,
        realized_pnl=result.state.realized_pnl,
        as_of=nxt,
        status=FINISHED if nxt >= days[-1] else ACTIVE,
        rules={k: dict(v) for k, v in result.rules.items()},
        positions=[
            position_to_row(normalized, symbol, position)
            for symbol, position in result.state.positions.items()
        ],
        decisions=[_row(normalized, d) for d in result.decisions],
        equity=[equity_to_row(normalized, m) for m in result.equity_curve if m.trade_date > current],
    )
    if not advanced:
        raise PaperConflict("会话已被推进过（并发）——刷新后再看")

    detail = await _detail(request, user_id, normalized)
    detail["this_step"] = {
        "trade_date": nxt.isoformat(),
        "filled": _moved(result.last, DecisionStatus.FILLED),
        "expired": _moved(result.last, DecisionStatus.EXPIRED),
        "unfilled": _moved(result.last, DecisionStatus.UNFILLED),
        "generated": [decision_to_payload(d) for d in _new_decisions(before, result)],
    }
    return detail


@router.post("/accounts/{account_id}/run")
async def run_account(
    request: Request,
    account_id: str,
    body: PaperRunRequest,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """一键跑到区间末端。`approve="all"` 等价于回测（用于出完整净值曲线）。"""
    user_id = int(user["id"])
    normalized = normalize_decision_id(account_id)
    db = require_db(request)
    row = await db.get_paper_account(user_id, normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在")
    if row["status"] != ACTIVE:
        raise PaperConflict("会话已跑到区间末端")

    auto = DecisionStatus.APPROVED if body.approve == "all" else DecisionStatus.REJECTED
    # 手上这批待审批的先按同一口径定掉——它们已经生成，只等一个裁决
    await db.set_paper_decisions_bulk(normalized, auto.value)

    config = config_from_payload(row["config"])
    source = await _source_for_account(request, user_id, row)
    before = tuple(decision_from_row(r) for r in await db.paper_decisions(normalized, DECISION_LIMIT))
    result = await _run_replay(
        source=source, config=config, account_id=normalized, decisions=before,
        through=config.end, auto=auto,
    )

    advanced = await db.paper_advance(
        account_id=normalized,
        user_id=user_id,
        expected_as_of=row["as_of"],
        cash=result.state.cash,
        realized_pnl=result.state.realized_pnl,
        as_of=config.end,
        status=FINISHED,
        rules={k: dict(v) for k, v in result.rules.items()},
        positions=[
            position_to_row(normalized, symbol, position)
            for symbol, position in result.state.positions.items()
        ],
        decisions=[_row(normalized, d) for d in result.decisions],
        equity=[
            equity_to_row(normalized, m)
            for m in result.equity_curve
            if m.trade_date > row["as_of"]
        ],
    )
    if not advanced:
        raise PaperConflict("会话已被推进过（并发）——刷新后再看")
    return await _detail(request, user_id, normalized)


@router.post("/decisions/{decision_id}/approve")
async def approve_decision(
    request: Request,
    decision_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """批准一张待审批的决策。**成交要等推进到下一交易日**——批的这一刻成交价还不知道。"""
    return await _decide(request, decision_id, user, DecisionStatus.APPROVED)


@router.post("/decisions/{decision_id}/reject")
async def reject_decision(
    request: Request,
    decision_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """驳回一张待审批的决策。"""
    return await _decide(request, decision_id, user, DecisionStatus.REJECTED)


async def _decide(
    request: Request,
    decision_id: str,
    user: dict[str, Any],
    status: DecisionStatus,
) -> dict[str, Any]:
    normalized = normalize_decision_id(decision_id)
    db = require_db(request)
    row = await db.paper_decision(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="决策不存在")
    current = DecisionStatus(row["status"])
    if current is not DecisionStatus.PENDING:
        raise PaperConflict(
            f"这张决策单是「{decision_from_row(row).label}」，不能改——"
            "只有待审批的决策能批准或驳回"
        )
    if not await db.set_paper_decision(normalized, status.value):
        raise PaperConflict("这张决策单刚被处理过（并发）")
    updated = await db.paper_decision(int(user["id"]), normalized)
    assert updated is not None
    return decision_to_payload(decision_from_row(updated))
