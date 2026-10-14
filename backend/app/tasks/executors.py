"""任务执行体：按 `kind` 派发，吃**任务行里的冻结材料**，不吃请求上下文。

E1a 注册了 `backtest`（内置与用户策略两条分支）；E1b 加上 `grid` / `batch`（复用既有的
`run_cells`，逐格进度经 `ctx.publish` 广播）。

**为什么执行体读任务行、不重新解析请求**（SPEC §2.1）：用户策略的源码随任务冻结。
同步路径里「读源码 → 跑」在同一请求内，异步化后出现了「提交后被改源码」的窗口——
不冻结就会跑出与提交时不同的策略，而且没人看得出来。网格/批量同理：**展开后的格**
（100 个 `CellSpec`）与选项在提交时冻结，执行体不再碰请求上下文。

落 `backtest_runs.request` / `optimization_runs.request` 的是**用户可见的那一份**：
回测剔掉 `_EXECUTION_ONLY`，网格剔掉整个 `exec` 子键（见 `visible_request`）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.backtest.batch import CellSpec, RunOptions, run_cells
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from app.backtest.types import Mode
from app.strategy import USER_STRATEGY, code_sha256
from app.strategy.sandbox import run_user_strategy
from app.tasks.stream import done_payload

#: 只进任务行、不进 `backtest_runs.request` 的执行字段。分界线是「用户看得见吗」：
#: 源码/事件开关/对比开关都是执行细节，回测记录里由 `code_sha256` 与报告 meta 表达。
_EXECUTION_ONLY = frozenset({"source", "strategy_name", "uses_events", "compare_pit"})

#: 网格/批量的执行材料整块挂在这个子键下（`optimization_runs.request` 落库时剔掉）
EXEC_KEY = "exec"

#: 执行体在**协作式中止**时的返回：任务标 `cancelled`，**结果不落库**
#: （「中止」不是「一份可以引用的记录」，与 M5b 的既有口径一致）
CANCELLED: dict[str, Any] = {"cancelled": True}


@dataclass(slots=True)
class RunContext:
    """一次执行拿得到的东西：任务行本身 + 中止信号 + 广播口。

    `stop` 由 worker 建、`request_stop` 置位——**协作式中止**：已派发的格跑完，
    不再派发新的（`run_cells` 的既有语义）。执行体不认识 HTTP，也不认识连接。
    """

    task: Mapping[str, Any]
    stop: asyncio.Event
    publish: Callable[[str, object], None]
    progress: Callable[[int], Awaitable[None]]

    @property
    def total(self) -> int:
        return int((self.task.get("progress") or {}).get("total") or 1)


#: 执行体签名：`(业务库, 任务行, 运行上下文) -> 落进 tasks.result 的那块`
Executor = Callable[[Any, Mapping[str, Any], RunContext], Awaitable[dict[str, Any]]]


def _config_of(request: Mapping[str, Any]) -> BacktestConfig:
    """由冻结请求重建引擎配置。区间已是 `resolve_window` 解析后的确定值（提交时解析）。"""
    return BacktestConfig(
        symbol=request["symbol"],
        strategy=request["strategy"],
        start=date.fromisoformat(request["start"]),
        end=date.fromisoformat(request["end"]),
        adjust=request["adjust"],
        # `both` 存的是请求原值（回看时分得清当初请求的是哪种），跑的时候按既有口径取 PIT 那一边
        pit_mode=Mode.PIT if request["pit_mode"] == "both" else Mode(request["pit_mode"]),
        costs=CostModel.from_options(**request["costs"]),
        params=dict(request["params"]),
    )


def visible_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """落记录表的那一份：请求 + 解析后的区间，去掉执行材料。

    回测剔 `_EXECUTION_ONLY` 那几个平铺字段；网格/批量的执行材料整块挂在 `exec` 子键下，
    一并剔掉——记录表里留下的是**用户可见的入参**，与今天逐字同形。
    """
    return {
        key: value
        for key, value in request.items()
        if key not in _EXECUTION_ONLY and key != EXEC_KEY
    }


async def execute_backtest(db: Any, task: Mapping[str, Any], ctx: RunContext) -> dict[str, Any]:
    """跑一次回测并落记录，返回 `{"run_id": ...}`。

    两条分支与 `api/backtest.py` 的同步时代逐字同源（内置 `build_report` / 用户策略沙箱），
    差别只在于材料来自任务行而不是请求体。**失败不落记录**（与今天异常路径不留半条同语义），
    由 worker 标 `failed`。
    """
    request = task["request"]
    config = _config_of(request)
    run_id = str(uuid.uuid4())
    user_strategy = request["strategy"] == USER_STRATEGY

    if user_strategy:
        outcome = await run_user_strategy(
            request["source"],
            config=config,
            strategy_name=request.get("strategy_name"),
            uses_events=bool(request.get("uses_events")),
            compare_pit=bool(request.get("compare_pit")),
        )
        report = outcome.report
    else:
        report = await asyncio.to_thread(
            build_report, config, compare_pit=bool(request.get("compare_pit"))
        )

    await db.save_backtest_run(
        run_id,
        int(task["user_id"]),
        visible_request(request),
        report,
        strategy_id=request.get("strategy_id") if user_strategy else None,
        code_sha256=code_sha256(request["source"]) if user_strategy else None,
    )
    return {"run_id": run_id}


# ── 网格 / 批量（E1b）───────────────────────────────────────


def freeze_specs(specs: list[CellSpec]) -> list[dict[str, Any]]:
    """`CellSpec[]` → 可 JSON 化的列表（冻结进任务行）。`frozen=True` 的 dataclass，
    `asdict` 出来的就是纯数据。"""
    return [asdict(spec) for spec in specs]


def thaw_specs(payload: list[dict[str, Any]]) -> list[CellSpec]:
    return [CellSpec(**item) for item in payload]


def freeze_options(options: RunOptions) -> dict[str, Any]:
    """`RunOptions` → JSON（日期转 ISO、`CostModel` 只留三个开关——与 API 的既有口径一致）。"""
    return {
        "costs": {
            "fees": options.costs.fee_enabled,
            "slippage": options.costs.slippage_enabled,
            "slippage_bps": options.costs.slippage_bps,
        },
        "pit_mode": Mode(options.pit_mode).value,
        "start": options.start.isoformat() if options.start else None,
        "end": options.end.isoformat() if options.end else None,
        "adjust": options.adjust,
        "initial_cash": options.initial_cash,
        "data_dir": str(options.data_dir) if options.data_dir else None,
    }


def thaw_options(payload: Mapping[str, Any]) -> RunOptions:
    from pathlib import Path

    return RunOptions(
        costs=CostModel.from_options(**payload["costs"]),
        pit_mode=Mode(payload["pit_mode"]),
        start=date.fromisoformat(payload["start"]) if payload.get("start") else None,
        end=date.fromisoformat(payload["end"]) if payload.get("end") else None,
        adjust=payload.get("adjust", "qfq"),
        initial_cash=float(payload.get("initial_cash", 1_000_000.0)),
        data_dir=Path(payload["data_dir"]) if payload.get("data_dir") else None,
    )


async def _execute_cells(
    db: Any, task: Mapping[str, Any], ctx: RunContext, *, kind: str
) -> dict[str, Any]:
    """网格与批量的共同执行体（它们除展开方式外完全同构——M5b 起就是这个口径）。"""
    exec_payload = task["request"][EXEC_KEY]
    specs = thaw_specs(exec_payload["cells"])
    options = thaw_options(exec_payload["options"])
    finished = 0

    async def on_cell(cell: dict[str, Any]) -> None:
        nonlocal finished
        finished += 1
        ctx.publish("cell", cell)  # 广播给订阅者（断线重连的靠它补齐）
        await ctx.progress(finished)  # 落任务行（列表/状态端点看得到「跑到第几格」）

    summary = await run_cells(specs, options, kind=kind, on_cell=on_cell, stop=ctx.stop)

    if ctx.stop.is_set():
        # 协作式中止：**不落库**，任务由 worker 标 cancelled
        return dict(CANCELLED)

    run_id = str(uuid.uuid4())
    await db.save_optimization_run(
        run_id, int(task["user_id"]), visible_request(task["request"]), summary
    )
    # 返回的就是 `done` 帧的载荷：worker 拿它既落 `tasks.result` 又广播（一处构造，两处用）
    return done_payload(run_id, summary)


async def execute_grid(db: Any, task: Mapping[str, Any], ctx: RunContext) -> dict[str, Any]:
    return await _execute_cells(db, task, ctx, kind="grid")


async def execute_batch(db: Any, task: Mapping[str, Any], ctx: RunContext) -> dict[str, Any]:
    return await _execute_cells(db, task, ctx, kind="batch")


#: `kind` → 执行体（E1a 落 `backtest`；E1b 落网格/批量）
EXECUTORS: dict[str, Executor] = {
    "backtest": execute_backtest,
    "grid": execute_grid,
    "batch": execute_batch,
}

#: 支持**协作式中止**的 kind（单次回测中止不了：它跑在线程/子进程里，见 SPEC §2.3）
COOPERATIVE_KINDS = frozenset({"grid", "batch"})

__all__ = [
    "CANCELLED",
    "COOPERATIVE_KINDS",
    "EXECUTORS",
    "EXEC_KEY",
    "Executor",
    "RunContext",
    "execute_backtest",
    "freeze_options",
    "freeze_specs",
    "thaw_options",
    "thaw_specs",
    "visible_request",
]
