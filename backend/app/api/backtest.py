"""回测 API：提交任务（P3-E1a）+ 我的回测（M1c）+ 用户策略分支（M4c）。

**P3-E1a 起这里不再同步跑回测**：`POST /api/v1/backtest` 只做闸门与冻结，建一条 `tasks`
行后回 **202 + `{task_id}`**；执行在 `app/tasks/worker.py`，报告经
`GET /api/v1/backtest/runs/{run_id}` 取回（深链 `?run=` 一直在用这条路）。

**闸门顺序不变、且必须在入队前**（SPEC §2.2）：归属 404 → 静态检查 `error`=0 → 参数校验
→ `resolve_window`。错误若挪进 worker，HTTP 就只剩 202 一种回应，M4c「运行前带行号给话术」
（422 + findings）当场失效——所以本文件的 try/except 依然是零。

**冻结执行材料**（SPEC §2.1）：任务行里的 `request` = 用户可见入参 + 执行字段（用户策略的
**源码全文**在内）。异步化出现了「提交后被改源码」的窗口，不冻结就会跑出与提交时不同的策略。
落 `backtest_runs.request` 时执行字段由执行体剔掉（`tasks/executors.py`）。

错误映射由 `main.py` 的异常处理器统一完成：`DataNotReady` → 503、`NoDataError` → 404、
其余 `BacktestError` → 400。

M4c 的用户策略分支一字未改其闸门：`strategy="user"` 时**只认库里的源码**（先保存才能跑），
`compare_pit` 的判据是源码里的 `USES_EVENTS`（`uses_events_of`）。
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
from app.backtest.report import resolve_window
from app.backtest.strategies import EventDriven, available_strategies, validate_params
from app.core.auth import require_db, require_user
from app.strategy import USER_STRATEGY, StrategyCheckFailed
from app.strategy.api import parse_meta
from app.strategy.params import validate_params as validate_user_params
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
        # 开关逻辑只有一处实现（`CostModel.from_options`）——任务执行体从冻结的
        # `costs` 字典重建时走的是同一个函数
        return CostModel.from_options(
            fees=self.fees, slippage=self.slippage, slippage_bps=self.slippage_bps
        )


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


def _stored_request(body: BacktestRequest, start: date, end: date, **execution: Any) -> dict[str, Any]:
    """任务行的 `request` = **解析后的 config**（区间已填好）+ 冻结的执行字段。

    `mode="json"` 不是可选项：`start`/`end` 是 `date`，原样塞进 Jsonb 会在 dump 时 TypeError。
    `adjust` 不在请求体里（引擎写死 qfq），补上；`costs` 用**请求的** `CostOptions`
    ——`config.costs` 是 `CostModel`，不可序列化；`pit_mode` 存请求值（`both` 也是合法记录），
    不存归一后的 `Mode.PIT`，否则回看时分不清当初请求的是哪种。

    这份字典两处用：任务行（worker 照它执行）与 `backtest_runs.request`（执行体剔掉执行字段
    后落库）——**同一份材料，不给执行另造一套形状**。
    """
    stored = body.model_dump(mode="json")
    stored["start"] = start.isoformat()
    stored["end"] = end.isoformat()
    stored["adjust"] = "qfq"
    stored.update(execution)
    return stored


def normalize_run_id(raw: str) -> str:
    """非 UUID 直接 422：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="run_id 必须是 UUID") from exc


async def submit_user_backtest(
    request: Request, body: BacktestRequest, user: dict[str, Any]
) -> dict[str, Any]:
    """用户策略分支（M4c）：`先保存才能跑`，跑的是**库里那条**源码（提交时冻结进任务行）。

    闸门顺序（SPEC §5 M4c，环环不可省）：归属 404 → 静态检查 `error`=0 → 参数校验 → 入队。
    静态检查必须在这里（**入队前**）：那正是「运行前带行号给话术」成立的前提。
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
    return await _enqueue(
        db,
        user_id,
        body,
        start,
        end,
        # 冻结：源码全文与参数校验结果一起进任务行，执行体照它跑（SPEC §2.1）
        source=code,
        strategy_name=row["name"],
        uses_events=meta.uses_events,
        compare_pit=compare_pit,
        params=params,
    )


async def _enqueue(
    db: Any,
    user_id: int,
    body: BacktestRequest,
    start: date,
    end: date,
    **execution: Any,
) -> dict[str, Any]:
    """建任务行并回 `{task_id, status}`。**队列实体就是任务表**（SPEC §2.2）：
    这里只落一行 `queued`，worker 自己会来取——不往任何进程内队列里塞东西，
    因此离线用例（模块级 `TestClient`，每请求一个事件循环）不会碰到跨 loop 的队列。
    """
    task_id = str(uuid.uuid4())
    await db.create_task(
        task_id, user_id, "backtest", _stored_request(body, start, end, **execution)
    )
    return {"task_id": task_id, "status": "queued"}


@router.post("", status_code=202)
async def run_backtest(
    request: Request,
    body: BacktestRequest,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """提交一次回测：**202 + `{task_id}`**，不等引擎。轮询 `GET /api/v1/tasks/{task_id}`。"""
    if body.strategy == USER_STRATEGY:
        return await submit_user_backtest(request, body, user)

    start, end = await asyncio.to_thread(
        resolve_window, body.symbol, body.strategy, body.start, body.end
    )

    # ma_cross 不消费事件语料，两模式必然同结果，跑第二遍只会误导（与 CLI 同口径）
    compare_pit = body.pit_mode == "both" and body.strategy == EventDriven.name
    return await _enqueue(
        require_db(request),
        int(user["id"]),
        body,
        start,
        end,
        compare_pit=compare_pit,
    )


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
