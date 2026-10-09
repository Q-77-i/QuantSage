"""批量 / 网格执行器（M5b）：展开、并发、逐格隔离、汇总。

一段批处理 = 若干**格**。网格的格是 `(参数组合)`，批量的格是 `(标的, 策略)`——两者除
展开方式外完全同构，共用这一个执行器（SPEC §6 M5b 把「单格」的定义写死在一处，防两条口径漂移）。

三条刻意的取舍：

* **单格就是一次普通回测**：内置走 `build_report`、用户策略走 `run_user_strategy`，
  取的都是报告里的 `metrics` 与净值曲线。不另写一条「只算指标」的轻路径——两条路径迟早漂移，
  而省下的那点时间在 F1 实测里本来也不是瓶颈（瓶颈是重复读盘，见 SPEC §6 M5b 开头）。
* **并发上限是进程级全局的**（`asyncio.Semaphore(2)`）：SPEC 风险表写的是「网格把服务打满」，
  每请求一把会在两个用户各跑一个网格时变成 4 格同时在跑。它**不是**为了提速——实测
  并发 2 只快 10~17%（DuckDB 默认 `threads=8` 已吃满核心），它的价值是单格互不牵连、
  一个网格不独占服务。
* **断线不取消在跑的格**：`asyncio.to_thread` 本就取消不了，硬取消还会在沙箱里漏子进程。
  中止语义 = **停止派发新格**，已在跑的最多 2 格自然跑完（结果丢弃、不落库）。

本模块不做请求校验、不做 HTTP、不碰库——展开与执行是纯逻辑，端点只负责把它们接起来。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.overfit import daily_returns, grid_overfit, moments
from app.backtest.report import build_report, resolve_window
from app.backtest.types import BacktestError, Mode, NoDataError
from app.data.duckdb_client import DataNotReady
from app.strategy import SandboxError, StrategyRejected
from app.strategy.sandbox import default_limits, run_user_strategy

log = logging.getLogger(__name__)

#: 并发上限（SPEC §6 M5b）：进程级全局，见模块 docstring 第三条。
CONCURRENCY = 2

#: 单次运行的格数上限（SPEC §6 M5b）。用户拍板的「拟 100」在实施期原样落地。
MAX_CELLS = 100
#: 网格的轴数上限：>2 一律拒绝，不做「固定其余、切 2D 片」的隐式降维（SPEC §6 M5b）。
MAX_AXES = 2
MAX_SYMBOLS = 20
MAX_STRATEGIES = 10


class BatchRequestError(ValueError):
    """批处理的请求级错误：**整单拒绝**（端点翻 422），绝不静默跳过某一格。

    SPEC §6 M5b 写死：「展开后的每一格都过参数校验，任一格非法即整单 422，错误带
    哪一格、哪个参数、为什么」——静默跳过会让热力图上出现按不出原因的空洞。
    """


#: 单格失败的 `kind` → 展示文案。`SandboxError` 的五个 kind 与 `StrategyRejected`
#: 直接沿用各自的取值（它们是既有的对外契约），这里只补回测/数据层的几种。
CELL_ERROR_KINDS: dict[str, str] = {
    "rejected": "策略未通过运行前检查",
    "no_data": "区间内没有行情数据",
    "data_not_ready": "本地数据未就绪",
    "backtest": "回测配置或数据有问题",
    "internal": "这一格执行失败",
}


def _error_of(exc: BaseException) -> dict[str, str]:
    """把单格的异常翻成 `{kind, message}`。**不吞异常细节**——消息原样给出去。"""
    if isinstance(exc, SandboxError):
        return {"kind": exc.kind, "message": str(exc)}
    if isinstance(exc, StrategyRejected):
        return {"kind": "rejected", "message": str(exc)}
    if isinstance(exc, NoDataError):
        return {"kind": "no_data", "message": str(exc)}
    if isinstance(exc, DataNotReady):
        return {"kind": "data_not_ready", "message": str(exc)}
    if isinstance(exc, BacktestError):
        return {"kind": "backtest", "message": str(exc)}
    return {"kind": "internal", "message": f"{type(exc).__name__}: {exc}"}


# ── 展开（纯函数）─────────────────────────────────────────────


def cartesian(axes: Sequence[tuple[str, Sequence[float | int]]]) -> list[dict[str, float | int]]:
    """笛卡尔积，**按轴的书写顺序**展开（第一轴最慢、最后一轴最快）。

    顺序是契约的一部分：格的下标要能反推出参数，客户端点热力图某一格时靠它对回去。
    """
    combos: list[dict[str, float | int]] = [{}]
    for name, values in axes:
        combos = [{**combo, name: value} for combo in combos for value in values]
    return combos


#: 「校验并归一」：吃一组参数，返回 (可直接交给策略的完整参数, 错误列表)。
#: 内置策略与用户策略的差别只有一处——**用户策略还要把 `PARAMS` 的缺省值填满**，
#: 内置的缺省由引擎的 `from_params` 在跑的时候补。两种行为都收在这个签名后面。
Normalizer = Callable[[Mapping[str, float | int]], tuple[dict[str, float | int], list[str]]]


def grid_cells(
    *,
    base: Mapping[str, float | int],
    axes: Sequence[tuple[str, Sequence[float | int]]],
    known: frozenset[str] | set[str],
    normalize: Normalizer,
) -> list[dict[str, float | int]]:
    """展开网格并**逐格**校验归一，返回每格的完整参数（基座 + 轴值）。

    校验失败**整单拒绝**并带上是第几格、哪一组参数、为什么——这是 SPEC §6 M5b 写死的那条：
    静默跳过会在热力图上留一个按不出原因的空洞。
    """
    if not axes:
        raise BatchRequestError("网格至少要有 1 条参数轴")
    if len(axes) > MAX_AXES:
        raise BatchRequestError(
            f"参数轴最多 {MAX_AXES} 条（当前 {len(axes)} 条）；"
            "多于两条时请固定其余参数，本端点不做隐式降维"
        )

    seen: set[str] = set()
    for name, values in axes:
        if name in base:
            raise BatchRequestError(f"参数 {name} 同时出现在基座参数与参数轴里，二者只能取一处")
        if name in seen:
            raise BatchRequestError(f"参数轴 {name} 出现了不止一次")
        seen.add(name)
        if name not in known:
            allowed = "、".join(sorted(known)) or "（无）"
            raise BatchRequestError(f"策略不接受参数 {name}；可用：{allowed}")
        if len(values) < 2:
            raise BatchRequestError(f"参数轴 {name} 至少要有 2 个取值（当前 {len(values)} 个）")
        if len(set(values)) != len(values):
            raise BatchRequestError(f"参数轴 {name} 的取值有重复：{list(values)}")

    total = 1
    for _, values in axes:
        total *= len(values)
    if total > MAX_CELLS:
        raise BatchRequestError(f"网格共 {total} 格，超过单次上限 {MAX_CELLS} 格")

    cells: list[dict[str, float | int]] = []
    for combo in cartesian(axes):
        params, errors = normalize({**base, **combo})
        if errors:
            shown = "、".join(f"{k}={v}" for k, v in combo.items())
            raise BatchRequestError(
                f"第 {len(cells) + 1} 格（{shown}）参数不合法：{'；'.join(errors)}"
            )
        cells.append(dict(params))
    return cells


# ── 执行 ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CellSpec:
    """一格要跑什么。用户策略把**已过闸门的源码**带进来——闸门在开跑前做一次，不逐格。"""

    symbol: str
    strategy: str
    params: Mapping[str, float | int]
    strategy_id: str | None = None
    strategy_name: str | None = None
    source: str | None = None
    uses_events: bool = False


@dataclass(frozen=True, slots=True)
class RunOptions:
    """一次批处理共用的口径（与 `BacktestRequest` 同源，由端点转过来）。"""

    costs: CostModel
    pit_mode: Mode
    start: date | None = None
    end: date | None = None
    adjust: str = "qfq"
    initial_cash: float = 1_000_000.0
    data_dir: Path | None = None


@dataclass
class _WindowCache:
    """`resolve_window` 的结果按 `(标的, 策略)` 复用。

    **必须复用**：它内部走 `dc.bars(symbol)`（不带区间，一次全史扫描 ~154ms），
    批量里逐格解析就是纯开销（SPEC §6 M5b）。并发下两个协程可能对同一个键各算一次——
    结果确定且只读，重复计算只损失一点时间，不会算错，故不加锁。
    """

    options: RunOptions
    cache: dict[tuple[str, str], tuple[date, date]] = field(default_factory=dict)

    def resolve(self, spec: CellSpec) -> tuple[date, date]:
        key = (spec.symbol, spec.strategy)
        if key not in self.cache:
            self.cache[key] = resolve_window(
                spec.symbol,
                spec.strategy,
                self.options.start,
                self.options.end,
                uses_events=spec.uses_events,
                data_dir=self.options.data_dir,
            )
        return self.cache[key]


def _config(spec: CellSpec, options: RunOptions, start: date, end: date) -> BacktestConfig:
    return BacktestConfig(
        symbol=spec.symbol,
        strategy=spec.strategy,
        start=start,
        end=end,
        adjust=options.adjust,
        initial_cash=options.initial_cash,
        pit_mode=options.pit_mode,
        costs=options.costs,
        params=dict(spec.params),
        data_dir=options.data_dir,
    )


async def _execute_cell(spec: CellSpec, options: RunOptions, windows: _WindowCache) -> dict[str, Any]:
    """跑一格，返回落进 `summary.cells[]` 的那一块。**失败也是一块**，不抛给上层。"""
    started = time.perf_counter()
    cell: dict[str, Any] = {
        "symbol": spec.symbol,
        "strategy": spec.strategy,
        "strategy_id": spec.strategy_id,
        "strategy_name": spec.strategy_name,
        "params": dict(spec.params),
    }
    try:
        start, end = windows.resolve(spec)
        config = _config(spec, options, start, end)
        if spec.source is not None:
            # 用户策略：与 M4c 的单次回测同一条沙箱路径（`compare_pit` 恒 False——
            # 网格里跑两遍等于把工作量翻倍，而 PIT 对比在单格重跑时照样能看）
            outcome = await run_user_strategy(
                spec.source,
                config=config,
                strategy_name=spec.strategy_name,
                uses_events=spec.uses_events,
                compare_pit=False,
                limits=default_limits(),
            )
            report = outcome.report
        else:
            report = await asyncio.to_thread(build_report, config)

        equity = [point["equity"] for point in report["equity_curve"]]
        returns = daily_returns(equity)
        skew, kurt = moments(returns)
        cell.update(
            ok=True,
            metrics=report["metrics"],
            moments={"skew": skew, "kurt": kurt, "n": len(returns)},
            window={
                "start": report["meta"]["start"],
                "end": report["meta"]["end"],
                "bars": report["meta"]["bars"],
            },
        )
    except Exception as exc:  # noqa: BLE001 —— **单格隔离**：任何异常都只影响这一格
        log.warning("批处理单格失败 %s/%s %s：%r", spec.symbol, spec.strategy, spec.params, exc)
        cell.update(ok=False, error=_error_of(exc))
    cell["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return cell


_gate_loop: asyncio.AbstractEventLoop | None = None
_gate: asyncio.Semaphore | None = None


def concurrency_gate() -> asyncio.Semaphore:
    """进程级并发闸门。**按事件循环取**：`asyncio.Semaphore` 在首次使用时会绑定循环，
    跨循环复用会抛「bound to a different event loop」——生产只有一个循环（即真正的全局闸门），
    测试里每个 `asyncio.run` 各拿一个（各自独立，不影响断言语义）。
    """
    global _gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate = asyncio.Semaphore(CONCURRENCY)
        _gate_loop = loop
    return _gate


def pick_best(cells: Sequence[Mapping[str, Any]]) -> int | None:
    """最优格：**夏普最大**；`None` 排最后；并列按总收益破平（SPEC §6 M5b 固定此判据）。

    判据必须与 DSR 的「被选中者」同源，故写死在这里、由 `run_cells` 与 `grid_overfit` 共用。
    只有 `ok` 的格参与；全体无有效夏普时返 `None`（如实，不挑一个收益最高的充数）。
    """
    def rank(index: int) -> tuple[bool, float, float]:
        metrics = cells[index].get("metrics") or {}
        sharpe = metrics.get("sharpe")
        return (
            sharpe is not None,
            float(sharpe) if sharpe is not None else 0.0,
            float(metrics.get("total_return") or 0.0),
        )

    candidates = [i for i, cell in enumerate(cells) if cell.get("ok") and cell.get("metrics")]
    if not candidates:
        return None
    best = max(candidates, key=rank)
    return best if rank(best)[0] else None


CellCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def run_cells(
    specs: Sequence[CellSpec],
    options: RunOptions,
    *,
    kind: str,
    on_cell: CellCallback | None = None,
    stop: asyncio.Event | None = None,
) -> dict[str, Any]:
    """逐格执行并返回可落库的 `summary`。

    `on_cell` 每跑完一格就被 await 一次（端点在里面推 SSE 帧）；`stop` 置位后
    **不再派发新格**，已在跑的最多 `CONCURRENCY` 格自然跑完（结果照常回调，由调用方决定丢不丢）。
    """
    started = time.perf_counter()
    windows = _WindowCache(options)
    gate = concurrency_gate()
    cells: list[dict[str, Any] | None] = [None] * len(specs)

    async def worker(index: int, spec: CellSpec) -> None:
        async with gate:
            if stop is not None and stop.is_set():
                return  # 中止后不再真跑——闸门是排队点，也是最后一道检查点
            cell = await _execute_cell(spec, options, windows)
            cell["index"] = index
            cells[index] = cell
            if on_cell is not None:
                await on_cell(cell)

    tasks = [asyncio.create_task(worker(i, spec)) for i, spec in enumerate(specs)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                # 不 await（在跑的格取消不了），但**留着引用并取一次异常**——
                # 与 `api/chat.py::_detach` 同一个理由：asyncio 只持弱引用，不留会被 GC 掉
                task.add_done_callback(_swallow)

    done = [cell for cell in cells if cell is not None]
    best_index = pick_best(done)
    summary: dict[str, Any] = {
        "kind": kind,
        "cells": done,
        # 成败计数单列而不是让列表页去数数组：列表查询走 `summary - 'cells'`（把最大的
        # 那一块整个去掉），没有它就只能把每格矩阵拉回来再数一遍
        "cells_total": len(specs),
        "cells_ok": sum(1 for cell in done if cell.get("ok")),
        "best_index": best_index,
        "overfit": _overfit_of(done, best_index),
        "window": _shared_window(done),
        "costs": options.costs.describe(),
        "pit_mode": options.pit_mode.value,
        "adjust": options.adjust,
        "duration_s": round(time.perf_counter() - started, 2),
    }
    return summary


def _swallow(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:  # pragma: no cover - 正常路径不会走到（异常已在格内兜住）
        log.warning("被中止的批处理任务出错：%r", error)


def _overfit_of(cells: Sequence[Mapping[str, Any]], best_index: int | None) -> dict[str, Any]:
    """Deflated Sharpe 及其全部输入（含退化分支的原因码）。"""
    if best_index is None:
        return grid_overfit(
            sharpes=[_sharpe(cell) for cell in cells],
            best_index=None,
            best_skew=None,
            best_kurt=None,
            observations=None,
        )
    best = cells[best_index]
    moments_of_best = best.get("moments") or {}
    return grid_overfit(
        sharpes=[_sharpe(cell) for cell in cells],
        best_index=best_index,
        best_skew=moments_of_best.get("skew"),
        best_kurt=moments_of_best.get("kurt"),
        observations=moments_of_best.get("n"),
    )


def _sharpe(cell: Mapping[str, Any]) -> float | None:
    return (cell.get("metrics") or {}).get("sharpe")


def _shared_window(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """全体格共用的窗口；逐格不同（批量）时返 `None`——**不挑一格冒充全体**。

    全失败时每格都没有 `window`，集合里只有一个空元组：那也返 `None`，
    空字典是「一个什么都没有的窗口」，与「没有共享窗口」是两回事。
    """
    windows = {tuple(sorted((cell.get("window") or {}).items())) for cell in cells}
    if len(windows) != 1:
        return None
    return dict(next(iter(windows))) or None


__all__ = [
    "CELL_ERROR_KINDS",
    "CONCURRENCY",
    "MAX_AXES",
    "MAX_CELLS",
    "MAX_STRATEGIES",
    "MAX_SYMBOLS",
    "BatchRequestError",
    "CellSpec",
    "Normalizer",
    "RunOptions",
    "cartesian",
    "concurrency_gate",
    "grid_cells",
    "pick_best",
    "run_cells",
]
