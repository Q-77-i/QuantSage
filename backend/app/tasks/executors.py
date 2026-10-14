"""任务执行体：按 `kind` 派发，吃**任务行里的冻结材料**，不吃请求上下文。

E1a 只注册 `backtest`（内置策略与用户策略两条分支）；E1b 会在这里加 `grid` / `batch`
（复用 `run_cells`，进度逐格推）。

**为什么执行体读任务行、不重新解析请求**（SPEC §2.1）：用户策略的源码随任务冻结。
同步路径里「读源码 → 跑」在同一请求内，异步化后出现了「提交后被改源码」的窗口——
不冻结就会跑出与提交时不同的策略，而且没人看得出来。

落 `backtest_runs.request` 的是**用户可见的那一份**（`_EXECUTION_ONLY` 之外的字段），
与今天同步路径的 `_stored_request` 同形：源码只留 `code_sha256`，正文不进回测记录表。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from app.backtest.types import Mode
from app.strategy import USER_STRATEGY, code_sha256
from app.strategy.sandbox import run_user_strategy

#: 只进任务行、不进 `backtest_runs.request` 的执行字段。分界线是「用户看得见吗」：
#: 源码/事件开关/对比开关都是执行细节，回测记录里由 `code_sha256` 与报告 meta 表达。
_EXECUTION_ONLY = frozenset({"source", "strategy_name", "uses_events", "compare_pit"})

#: 执行体签名：`(业务库, 任务行) -> 落进 tasks.result 的那块`
Executor = Callable[[Any, Mapping[str, Any]], Awaitable[dict[str, Any]]]


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
    """落 `backtest_runs.request` 的那一份：请求 + 解析后的区间，去掉执行字段。"""
    return {key: value for key, value in request.items() if key not in _EXECUTION_ONLY}


async def execute_backtest(db: Any, task: Mapping[str, Any]) -> dict[str, Any]:
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


#: `kind` → 执行体。E1b 在这里加 `grid` / `batch`。
EXECUTORS: dict[str, Executor] = {"backtest": execute_backtest}

__all__ = ["EXECUTORS", "Executor", "execute_backtest", "visible_request"]
