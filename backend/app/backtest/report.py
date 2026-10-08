"""T5 报告层：跑回测 → 算指标与基准 → 组装 SPEC §5 的输出结构。

本模块是**唯一**把 `BacktestResult` 变成可交付 JSON 的地方（T6 的 `POST /api/v1/backtest`
直接返回 `build_report()` 的结果）。返回普通 dict 而非 dataclass：结构本身就是契约，
多一层包装只会让 FastAPI 序列化多一道转换。

PIT 对比是 T5 的核心产出——`compare_pit=True` 时同一区间跑两遍，唯一变量是
`Mode`（即 `EventView.stamp()` 取 `available_at` 还是 `event_time`），
差值即「前视偏差两口径差异」（方向不预设，可正可负）。策略不消费事件时（ma_cross）两遍结果必然相同，
故由调用方显式请求，不自动开跑。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from collections.abc import Callable

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from app.backtest.metrics import SHORT_WINDOW_BARS, Metrics, benchmark_curve, compute_metrics
from app.backtest.strategies import EventDriven, Strategy
from app.backtest.types import BacktestError, Mode, NoDataError, Position
from app.data import duckdb_client as dc

#: 策略工厂：每遍回测都新建一个实例（用户策略可能在模块级持状态，复用实例会串味）。
StrategyFactory = Callable[[], Strategy]


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _metrics_row(metrics: Metrics, benchmark_return: float) -> dict[str, Any]:
    """SPEC §5 的 metrics 块：六项指标 + 期末权益 + 基准/超额两项可读性扩展。"""
    return {
        "total_return": metrics.total_return,
        "annual_return": metrics.annual_return,
        "max_drawdown": metrics.max_drawdown,
        "sharpe": metrics.sharpe,
        "win_rate": metrics.win_rate,
        "trade_count": metrics.trade_count,
        "final_equity": metrics.final_equity,
        "benchmark_return": benchmark_return,
        "excess_return": metrics.total_return - benchmark_return,
    }


def _open_position(result: BacktestResult) -> dict[str, Any] | None:
    """期末未平仓持仓的浮动盈亏。不进 `trades`，故不影响胜率——报告必须单列。"""
    position: Position = result.open_position
    if position.is_flat or position.entry_price is None:
        return None
    last_close = result.equity_curve[-1].close if result.equity_curve else position.entry_price
    cost_basis = position.shares * position.entry_price + position.entry_fees
    market_value = position.shares * last_close
    return {
        "shares": position.shares,
        "entry_date": _iso(position.entry_date),
        "entry_price": position.entry_price,
        "entry_reason": position.entry_reason,
        "last_close": last_close,
        "unrealized_pnl": market_value - cost_basis,
        "unrealized_return": (market_value - cost_basis) / cost_basis if cost_basis else 0.0,
    }


def _trades(result: BacktestResult) -> list[dict[str, Any]]:
    """SPEC 的四个字段 + 四个扩展字段。`reason` 是**出场**原因，入场原因单列。"""
    return [
        {
            "entry_date": _iso(trade.entry_date),
            "exit_date": _iso(trade.exit_date),
            "pnl": trade.pnl,
            "reason": trade.exit_reason,
            "entry_reason": trade.entry_reason,
            "return_pct": trade.return_pct,
            "hold_bars": trade.hold_bars,
            "qty": trade.qty,
        }
        for trade in result.trades
    ]


def _warnings(result: BacktestResult) -> list[str]:
    """如实标注样本量：年化与夏普按 252 折算，短窗会放大噪声。"""
    bars = len(result.bars)
    if bars >= SHORT_WINDOW_BARS:
        return []
    return [
        f"样本仅 {bars} 个交易日，年化收益与夏普按 252 日折算会放大噪声"
        f"（约 {((252 / bars) ** 0.5):.1f} 倍），仅供参考"
    ]


def _delta(pit: Metrics, non_pit: Metrics) -> dict[str, Any]:
    """非 PIT 相对 PIT 的差异（方向不预设，可正可负）。

    主量化值取 `final_equity_pct`（期末权益差异比例）——分母是期末权益，恒为正，
    不存在总收益接近 0 时相对差爆炸的问题；其余指标用**百分点差**，直观且不会误导。
    夏普/胜率可能为 None（样本退化），差值一并置 None 而非硬凑 0。
    """

    def diff(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else b - a

    return {
        "final_equity_abs": non_pit.final_equity - pit.final_equity,
        "final_equity_pct": non_pit.final_equity / pit.final_equity - 1.0 if pit.final_equity else None,
        "total_return_pp": (non_pit.total_return - pit.total_return) * 100.0,
        "annual_return_pp": (non_pit.annual_return - pit.annual_return) * 100.0,
        "max_drawdown_pp": (non_pit.max_drawdown - pit.max_drawdown) * 100.0,
        "sharpe_abs": diff(pit.sharpe, non_pit.sharpe),
        "win_rate_pp": (
            (non_pit.win_rate - pit.win_rate) * 100.0
            if pit.win_rate is not None and non_pit.win_rate is not None
            else None
        ),
        "trade_count": non_pit.trade_count - pit.trade_count,
    }


def _run_mode(
    config: BacktestConfig, mode: Mode, factory: StrategyFactory | None = None
) -> BacktestResult:
    cfg = config if config.pit_mode is mode else replace(config, pit_mode=mode)
    return run_backtest(cfg, strategy=factory() if factory else None)


def _pit_comparison(
    config: BacktestConfig,
    primary: BacktestResult,
    factory: StrategyFactory | None = None,
) -> dict[str, Any]:
    """同区间跑 PIT 与非 PIT 各一次，返回两份指标 + 差值 + 入场日序列。

    两遍只有 `pit_mode` 不同（成本、参数、区间、初始资金全部一致），否则对比无意义。
    用户策略每遍走一次工厂——不是性能考虑，是不让模块级状态跨遍残留。
    """
    pit = _run_mode(config, Mode.PIT, factory)
    non_pit = _run_mode(config, Mode.NON_PIT, factory)
    pit_metrics = compute_metrics(pit.equity_curve, pit.trades, config.initial_cash)
    non_pit_metrics = compute_metrics(non_pit.equity_curve, non_pit.trades, config.initial_cash)
    return {
        "pit_metrics": _metrics_row(pit_metrics, _benchmark_return(pit, config)),
        "non_pit_metrics": _metrics_row(non_pit_metrics, _benchmark_return(non_pit, config)),
        "delta": _delta(pit_metrics, non_pit_metrics),
        # 入场日序列是两模式差异最直接的证据（期末权益只是结果）
        "entry_dates": {
            "pit": [_iso(d) for d in pit.entry_dates],
            "non_pit": [_iso(d) for d in non_pit.entry_dates],
        },
    }


def _benchmark_return(result: BacktestResult, config: BacktestConfig) -> float:
    """基准收益按「投入的初始资金 → 期末基准净值」计，含建仓时的一次性成本。"""
    curve = benchmark_curve(result.bars, config.initial_cash, config.costs)
    return curve[-1] / config.initial_cash - 1.0 if curve and config.initial_cash else 0.0


def _first_event_date(symbol: str, data_dir: Path | None) -> date | None:
    rows = dc.events(symbol, data_dir=data_dir)
    return min(row["event_time"].date() for row in rows) if rows else None


def uses_events_of(strategy: str, declared: bool | None = None) -> bool:
    """「该策略是否消费事件」的统一判据。

    内置策略按名字认（只有 `event_driven` 吃事件）；用户策略（M4）由源码里的 `USES_EVENTS`
    声明给出——**不猜**：靠扫源码里有没有 `ctx.events` 会把「声明与代码不一致」这类问题
    藏起来（那正是 M4b 检查器要报的 warning）。
    """
    return strategy == EventDriven.name if declared is None else declared


def _events_in_window(config: BacktestConfig, strategy_uses_events: bool) -> int | None:
    """该标的在回测窗口内的事件条数；不消费事件的策略返回 `None`（不是 0——两者含义不同）。"""
    if not strategy_uses_events or config.start is None or config.end is None:
        return None
    rows = dc.events(
        config.symbol,
        start=config.start.isoformat(),
        end=config.end.isoformat(),
        data_dir=config.data_dir,
    )
    return len(rows)


def resolve_window(
    symbol: str,
    strategy: str,
    start: date | None = None,
    end: date | None = None,
    *,
    uses_events: bool | None = None,
    data_dir: Path | None = None,
) -> tuple[date, date]:
    """把「可缺省的请求区间」解析成确定区间。

    `end` 缺省 = 该标的最后一根 bar；`start` 缺省 = `event_driven` 取事件窗口起点
    （语料只覆盖约 3 个月，从行情起点开跑等于大半程空转），其余策略取第一根 bar。

    **事件驱动的时间收口（M2b）**：语料覆盖区间是 `[首次回填日, 最新可用日]`，起点固化、
    终点随日增前移。请求**显式**把起点放在覆盖起点之前时一律拒绝——那种回测的结论是
    「前六年空转、最后三个月交易」，与其跑出一个静默无意义的报告，不如给一句可执行的出路。
    缺省起点仍自动取语料覆盖起点（缺省代决策、显式不被静默改写，与既有口径一致）。

    API 与 `scripts/run_report.py` **共用这一段**——两处各写一套默认值必然漂移。
    解析结果若落不到任何 bar，抛 `NoDataError`（API 映射 404）。
    """
    rows = dc.bars(symbol, data_dir=data_dir)
    if not rows:
        raise NoDataError(f"{symbol} 无行情数据，先跑 scripts/download_bars.py")

    if uses_events_of(strategy, uses_events):
        coverage = dc.event_coverage(data_dir)
        coverage_start = date.fromisoformat(coverage["start"]) if coverage["start"] else None
        if start is not None and coverage_start is not None and start < coverage_start:
            raise BacktestError(
                f"事件语料只覆盖 {coverage['start']} 起（新闻源保留期 3 个月，更早的事件不可得），"
                f"请求的起点 {start} 早于它——把 start 改到 {coverage['start']} 之后；"
                "若要看更长的历史，请改用不消费事件的 ma_cross。"
            )
        if start is None:
            start = _first_event_date(symbol, data_dir)
            if start is None:
                raise NoDataError(
                    f"{symbol} 在事件语料覆盖区间（{coverage['start']} → {coverage['end']}）内没有事件。"
                    "换一个标的，或把策略换成 ma_cross（双均线不需要事件）。"
                )
    start = start or rows[0]["trade_date"]
    end = end or rows[-1]["trade_date"]

    if not dc.bars(symbol, start=start.isoformat(), end=end.isoformat(), data_dir=data_dir):
        raise NoDataError(f"{symbol} 在 {start} → {end} 无行情数据，放宽区间或换标的")
    return start, end


def build_report(
    config: BacktestConfig,
    *,
    compare_pit: bool = False,
    strategy_factory: StrategyFactory | None = None,
    strategy_name: str | None = None,
    uses_events: bool | None = None,
) -> dict[str, Any]:
    """跑回测并返回 SPEC §5 的报告结构（可直接 `json.dumps`）。

    `strategy_factory` / `strategy_name` 是用户策略（M4）的入口，报告结构与内置策略**完全同构**，
    只多两个标识键（`strategy_kind` / `strategy_name`）——前端不需要第二条渲染路径。
    工厂每遍调用一次（PIT 对比要跑两遍）。
    """
    result = run_backtest(config, strategy=strategy_factory() if strategy_factory else None)
    metrics = compute_metrics(result.equity_curve, result.trades, config.initial_cash)
    benchmark = benchmark_curve(result.bars, config.initial_cash, config.costs)
    strategy_uses_events = uses_events_of(config.strategy, uses_events)

    return {
        "meta": {
            "symbol": config.symbol,
            "strategy": config.strategy,
            # `strategy_kind` 与 `strategy_name` 是 M4 新增的标识键（内置策略为 builtin / None），
            # 其余键一个没动——既有报告的重开、对比、测试全部照旧
            "strategy_kind": "user" if strategy_factory else "builtin",
            "strategy_name": strategy_name,
            "mode": config.pit_mode.value,
            "start": _iso(result.bars[0].trade_date) if result.bars else None,
            "end": _iso(result.bars[-1].trade_date) if result.bars else None,
            "bars": len(result.bars),
            "initial_cash": config.initial_cash,
            "adjust": config.adjust,
            "costs": config.costs.describe(),
            "cutoff_field": result.cutoff_field,
            "params": dict(config.params),
            "warnings": _warnings(result),
            # 语料覆盖区间 + 窗口内事件数：让存下来的报告自证「这次看的是哪一段事件语料」
            "event_coverage": dc.event_coverage(config.data_dir),
            "events_in_window": _events_in_window(config, strategy_uses_events),
        },
        "metrics": _metrics_row(metrics, _benchmark_return(result, config)),
        "equity_curve": [
            {"date": _iso(point.trade_date), "equity": point.equity, "benchmark": base}
            for point, base in zip(result.equity_curve, benchmark, strict=True)
        ],
        "trades": _trades(result),
        "open_position": _open_position(result),
        "pit_comparison": _pit_comparison(config, result, strategy_factory) if compare_pit else None,
    }
