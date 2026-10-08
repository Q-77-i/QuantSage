"""回测 API：SPEC §5 报告结构的同步出口 + 我的回测（M1c）+ 用户策略分支（M4c）。

`build_report` 是**同步 CPU + DuckDB IO**，必须卸载到线程——直接在 async 端点里跑会
阻塞事件循环（T3 在 DuckDB 工具上已踩过同一个坑）。

错误映射由 `main.py` 的异常处理器统一完成：`DataNotReady` → 503、`NoDataError` → 404、
其余 `BacktestError` → 400，这里不写 try/except。

M1c 起本组端点**纳入鉴权**：跑完要落库，归属就是必要信息。响应随之改信封
`{run_id, report}`——报告结构本身没动（P1 SPEC §6），信封只是把「这次跑的是哪一条记录」
一并交出去，「我的回测」才重开得起来。落库放在 `build_report` **成功之后**：
异常路径（404 / 503 / 400）不该留下半条记录。

M4c 起多一条分支：`strategy="user"` 时**只认库里的源码**（先保存才能跑），依次过
「归属 404 → 静态检查 `error`=0 → 参数校验 → 沙箱子进程」，落库时记 `strategy_id` +
`code_sha256`。**闸门顺序写在 SPEC §5 M4c 的表里，环环不可省**——尤其是静态检查必须
在 spawn 之前（那正是它能给出可读报错的前提）。`compare_pit` 的判据从「策略名 =
event_driven」泛化为源码里的 `USES_EVENTS`（`uses_events_of`）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.strategies import normalize_strategy_id
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report, resolve_window
from app.backtest.strategies import EventDriven, available_strategies, validate_params
from app.backtest.types import Mode
from app.core.auth import require_db, require_user
from app.strategy import USER_STRATEGY, StrategyCheckFailed, code_sha256
from app.strategy.api import parse_meta
from app.strategy.params import validate_params as validate_user_params
from app.strategy.sandbox import run_user_strategy
from app.strategy.static_check import check_source, has_errors

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

SYMBOL_PATTERN = r"^\d{6}$"


class CostOptions(BaseModel):
    """费用与滑点，映射到 `CostModel` 的两个独立开关 + 滑点档位。

    开关保持独立：T4 的验收项之一就是「手续费/滑点各有可见影响」，合并成一个
    `costs: bool` 会让这项验收没法在前端复现。
    """

    fees: bool = True
    slippage: bool = True
    slippage_bps: float = Field(default=5.0, ge=0.0, le=100.0)

    def to_model(self) -> CostModel:
        model = CostModel(slippage_bps=self.slippage_bps)
        if not self.fees:
            model = model.without_fees()
        if not self.slippage:
            model = model.without_slippage()
        return model


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    #: 仅 `strategy="user"` 时使用（M4c）：要跑的是**库里那条**源码，不是请求里现传的
    strategy_id: str | None = None
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    start: date | None = None
    end: date | None = None
    costs: CostOptions = Field(default_factory=CostOptions)
    pit_mode: Literal["pit", "non_pit", "both"] = "pit"
    params: dict[str, float] = Field(default_factory=dict)

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        # `user` 是 M4c 的分支：它不在内置注册表里，但要求必须带 `strategy_id`（见下）
        if value != USER_STRATEGY and value not in available_strategies():
            names = "、".join([*available_strategies(), USER_STRATEGY])
            raise ValueError(f"未知策略 {value!r}；可用策略：{names}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.strategy == USER_STRATEGY:
            if not self.strategy_id:
                raise ValueError(
                    'strategy="user" 时必须带 strategy_id——策略要先保存才能跑（运行的是库里的源码）'
                )
        elif self.strategy_id is not None:
            raise ValueError('strategy_id 只能与 strategy="user" 一起用')

        # 未知键与取值一起挡在这里（M4 补齐后半）：前者会被 from_params 静默忽略，
        # 后者（`fast=20 / slow=5`）会静默跑出没有意义的结果——两条都是「用户以为生效了」。
        # 用户策略的 schema 来自源码，得先读到策略才知道，故那一段在端点里（静态检查之后）。
        if self.strategy != USER_STRATEGY:
            errors = validate_params(self.strategy, self.params)
            if errors:
                raise ValueError("；".join(errors))
        if self.start and self.end and self.start > self.end:
            raise ValueError(f"区间起点 {self.start} 晚于终点 {self.end}")
        return self


def _stored_request(body: BacktestRequest, start: date, end: date) -> dict[str, Any]:
    """落库的 `request` = **解析后的 config**（区间已填好），不是原始请求体。

    `mode="json"` 不是可选项：`start`/`end` 是 `date`，原样塞进 Jsonb 会在 dump 时 TypeError。
    `adjust` 不在请求体里（引擎写死 qfq），补上；`costs` 用**请求的** `CostOptions`
    ——`config.costs` 是 `CostModel`，不可序列化；`pit_mode` 存请求值（`both` 也是合法记录），
    不存归一后的 `Mode.PIT`，否则回看时分不清当初请求的是哪种。
    """
    stored = body.model_dump(mode="json")
    stored["start"] = start.isoformat()
    stored["end"] = end.isoformat()
    stored["adjust"] = "qfq"
    return stored


def normalize_run_id(raw: str) -> str:
    """非 UUID 直接 422：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="run_id 必须是 UUID") from exc


async def run_user_backtest(
    request: Request, body: BacktestRequest, user: dict[str, Any]
) -> dict[str, Any]:
    """用户策略分支（M4c）：`先保存才能跑`，跑的是**库里那条**源码。

    闸门顺序（SPEC §5 M4c，环环不可省）：归属 404 → 静态检查 `error`=0 → 参数校验 → 沙箱。
    静态检查必须在 spawn **之前**：那正是「运行前带行号给话术」成立的前提。
    """
    db = require_db(request)
    user_id = int(user["id"])
    strategy_id = normalize_strategy_id(body.strategy_id or "")
    row = await db.get_strategy(user_id, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    code = row["code"]
    findings = check_source(code)
    if has_errors(findings):
        # 由 `main.py` 的处理器翻成 422 + findings（编辑器与面板直接吃）
        raise StrategyCheckFailed(findings)

    meta = parse_meta(code)  # error=0 时不该抛；真抛了由 StrategyRejected 处理器兜成 422
    params = validate_user_params(meta.params, body.params)

    start, end = await asyncio.to_thread(
        resolve_window,
        body.symbol,
        USER_STRATEGY,
        body.start,
        body.end,
        uses_events=meta.uses_events,
    )
    # 同内置策略的口径：不消费事件的策略两模式必然同结果，跑第二遍只会误导
    compare_pit = body.pit_mode == "both" and meta.uses_events
    config = BacktestConfig(
        symbol=body.symbol,
        strategy=USER_STRATEGY,
        start=start,
        end=end,
        adjust="qfq",
        pit_mode=Mode.PIT if body.pit_mode == "both" else Mode(body.pit_mode),
        costs=body.costs.to_model(),
        params=params,
    )
    outcome = await run_user_strategy(
        code,
        config=config,
        strategy_name=row["name"],
        uses_events=meta.uses_events,
        compare_pit=compare_pit,
    )

    run_id = str(uuid.uuid4())
    await db.save_backtest_run(
        run_id,
        user_id,
        _stored_request(body, start, end),
        outcome.report,
        strategy_id=strategy_id,
        code_sha256=code_sha256(code),
    )
    return {"run_id": run_id, "report": outcome.report}


@router.post("")
async def run_backtest(
    request: Request,
    body: BacktestRequest,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """跑一次回测，落库并返回 `{run_id, report}`。"""
    if body.strategy == USER_STRATEGY:
        return await run_user_backtest(request, body, user)

    start, end = await asyncio.to_thread(
        resolve_window, body.symbol, body.strategy, body.start, body.end
    )

    # ma_cross 不消费事件语料，两模式必然同结果，跑第二遍只会误导（与 CLI 同口径）
    compare_pit = body.pit_mode == "both" and body.strategy == EventDriven.name

    config = BacktestConfig(
        symbol=body.symbol,
        strategy=body.strategy,
        start=start,
        end=end,
        adjust="qfq",
        pit_mode=Mode.PIT if body.pit_mode == "both" else Mode(body.pit_mode),
        costs=body.costs.to_model(),
        params=body.params,
    )
    report = await asyncio.to_thread(build_report, config, compare_pit=compare_pit)

    run_id = str(uuid.uuid4())
    await require_db(request).save_backtest_run(
        run_id, int(user["id"]), _stored_request(body, start, end), report
    )
    return {"run_id": run_id, "report": report}


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """我的回测（摘要），按时间倒序。只抽 JSONB 子集，不把整份报告拉回来。"""
    return await require_db(request).list_backtest_runs(int(user["id"]), limit)


@router.get("/runs/{run_id}")
async def get_run(
    request: Request, run_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """重开：按 id 取回完整报告。越权与不存在同返 404，不泄露存在性。"""
    normalized = normalize_run_id(run_id)
    row = await require_db(request).get_backtest_run(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return row
