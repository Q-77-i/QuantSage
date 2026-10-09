"""M5c 因子统计：RankIC / 分层 / 多空 / 换手与费用 / tear sheet 组装。

纯函数，**不 import 数据层**——输入是 `panel.py` 摊出来的两张面（因子值、前向收益）。
指标一律复用 `backtest.metrics`（夏普 / 回撤 / 年化与回测完全同源），
ICIR 的「均值 / 样本标准差」复用 `overfit.per_period_sharpe`（与 M5b 的每期夏普同口径），
剔除阈值复用 `metrics.EXCLUDE_ABS_CHANGE_PCT`（与 M5a 基准同一个数），费率一律取自
`CostModel`——**四样都不另写一套**。上面这几个模块都是无 I/O 的纯口径层。

**费用是费率口径，不含最低佣金 5 元**：最低佣金是「每笔订单 × 资金规模」的函数，本报告
不预设资金规模——与 `metrics.benchmark_curve` 的份额化口径同姿态。规模事实（100 万 ÷
240 只时每笔佣金 1.04 元被抬到 5 元 = 4.8×）写进 `notes`，不藏。

**多空价差是统计量**：A 股不可做空，这个组合不可交易。响应里 `long_short.tradable`
恒为 `false`，不是文案。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import fmean, stdev

from app.backtest.costs import BPS, CostModel
from app.backtest.metrics import EXCLUDE_ABS_CHANGE_PCT, annual_return, max_drawdown, sharpe_ratio
from app.backtest.overfit import per_period_sharpe
from app.factor.panel import EventPanel

#: 分层组数。固定 5——池子日均约 250 只，每组分到约 50 只；组数做成参数只会多一条
#: 「挑组数」的路，SPEC 已定不做。
QUANTILES = 5

#: 日样本下限。实测在「可用窗口归属」口径下一次都不触发（min 池 30 只）——它是**护栏**，
#: 不是过滤器；哪些日子被跳过如实计数。
MIN_POOL = 20

#: 前向收益的剔除阈值，与 M5a 基准**同规则同边界**（`|涨跌幅| > 30%` 的新股首日 / 复牌）：
#: 那些样本在回测里本就不可投资，留着单只就能把组均值带偏。
#: 别名只为在本模块读起来贴切——**取的是同一个常数**，不另写一份会漂移的 30。
EXCLUDE_ABS_RETURN_PCT = EXCLUDE_ABS_CHANGE_PCT

GROUP_LABELS = {1: "Q1（最低）", 2: "Q2", 3: "Q3", 4: "Q4", 5: "Q5（最高）"}


@dataclass(frozen=True, slots=True)
class PoolDay:
    """一个信号日的池子：`(标的, 因子值, 前向收益)`，按因子值升序。"""

    day: date
    pairs: tuple[tuple[str, float, float], ...]


@dataclass(frozen=True, slots=True)
class Pools:
    """窗口内可用的池子，以及被门槛挡掉的计数（如实入报告）。"""

    days: tuple[PoolDay, ...]
    dropped_no_price: int
    dropped_untradeable: int
    skipped_thin: int
    skipped_no_window: int


@dataclass(frozen=True, slots=True)
class ICPoint:
    day: date
    ic: float
    n: int


@dataclass(frozen=True, slots=True)
class ICStats:
    points: tuple[ICPoint, ...]
    mean: float | None
    std: float | None
    icir: float | None
    t_stat: float | None
    positive_days: int
    days: int


@dataclass(frozen=True, slots=True)
class Portfolio:
    """一个分位组的日频组合：成员、毛/净日收益、买卖换手。"""

    quantile: int
    members: tuple[tuple[str, ...], ...]
    gross: tuple[float, ...]
    net: tuple[float, ...]
    turnover_in: tuple[float, ...]
    turnover_out: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Spread:
    """多空价差（Q_top − Q_bottom）：统计量，不可交易。"""

    gross: tuple[float, ...]
    net: tuple[float, ...]
    t_stat: float | None
    days: int


def round_trip_rates(costs: CostModel) -> tuple[float, float]:
    """`(买入费率, 卖出费率)`：佣金 + 滑点；卖出再 + 印花税。

    刻意**不**用 `CostModel.fees()`——那个口径按「每笔订单金额」算，含最低佣金 5 元；
    组合层没有订单金额（收益率空间），见模块 docstring。
    """
    commission = costs.commission_rate if costs.fee_enabled else 0.0
    stamp = costs.stamp_tax_rate if costs.fee_enabled else 0.0
    slippage = costs.slippage_bps / BPS if costs.slippage_enabled else 0.0
    return (commission + slippage, commission + stamp + slippage)


def build_pools(
    values: Mapping[date, Mapping[str, float]],
    forward: Mapping[date, Mapping[str, float]],
    *,
    min_pool: int = MIN_POOL,
) -> Pools:
    """两张面 → 逐日池子。三道门槛**如实计数**：缺价 / 不可交易 / 样本不足。"""
    days: list[PoolDay] = []
    no_price = untradeable = thin = no_window = 0
    for day in sorted(values):
        fwd = forward.get(day)
        if not fwd:
            no_window += 1
            continue
        pairs: list[tuple[str, float, float]] = []
        for symbol, value in values[day].items():
            ret = fwd.get(symbol)
            if ret is None:
                no_price += 1
                continue
            if abs(ret) > EXCLUDE_ABS_RETURN_PCT / 100.0:
                untradeable += 1
                continue
            pairs.append((symbol, float(value), float(ret)))
        if len(pairs) < min_pool:
            thin += 1
            continue
        pairs.sort(key=lambda item: (item[1], item[0]))
        days.append(PoolDay(day=day, pairs=tuple(pairs)))
    return Pools(
        days=tuple(days),
        dropped_no_price=no_price,
        dropped_untradeable=untradeable,
        skipped_thin=thin,
        skipped_no_window=no_window,
    )


def _ranks(values: Sequence[float]) -> list[float]:
    """平均秩（并列取均值）——Spearman 对并列的定义。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman 秩相关；样本 <3 或任一序列无波动时无定义（返 None）。"""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = fmean(rx), fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sqrt(sum((a - mx) ** 2 for a in rx))
    vy = sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def ic_series(pools: Pools) -> tuple[ICPoint, ...]:
    """逐日 RankIC（因子值 vs 前向收益）。"""
    points: list[ICPoint] = []
    for pool in pools.days:
        values = [value for _, value, _ in pool.pairs]
        returns = [ret for _, _, ret in pool.pairs]
        ic = spearman(values, returns)
        if ic is not None:
            points.append(ICPoint(day=pool.day, ic=ic, n=len(pool.pairs)))
    return tuple(points)


def summarize_ic(points: Sequence[ICPoint]) -> ICStats:
    """IC 均值 / 标准差 / ICIR / t 值。

    口径写死：`ICIR = mean / std`（**未年化**，日频），`t = ICIR × √天数`；
    「均值 / 样本标准差」**复用 `overfit.per_period_sharpe`**——与 M5b 判最优格的每期夏普
    是同一段实现，退化分支（样本 <2 / 无波动）也一并同口径返 `None`，绝不硬凑一个数。
    """
    ics = [point.ic for point in points]
    if not ics:
        return ICStats((), None, None, None, None, 0, 0)
    mean = fmean(ics)
    sd = stdev(ics) if len(ics) > 1 else 0.0
    icir = per_period_sharpe(ics)
    t_stat = icir * sqrt(len(ics)) if icir is not None else None
    return ICStats(
        points=tuple(points),
        mean=mean,
        std=sd,
        icir=icir,
        t_stat=t_stat,
        positive_days=sum(1 for ic in ics if ic > 0),
        days=len(ics),
    )


def _cost_series(
    members: Sequence[Sequence[str]], *, buy_rate: float, sell_rate: float
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """按进出名单算 `(每日费用, 买入换手, 卖出换手)`。

    首日按**全新建仓**（买 100%）——不假设组合预先存在；继续持有的成员不调仓（权重漂移），
    故只对进出名单收费。
    """
    costs: list[float] = []
    ins: list[float] = []
    outs: list[float] = []
    previous: set[str] | None = None
    for names in members:
        current = set(names)
        if previous is None:
            buy_turnover, sell_turnover = 1.0, 0.0
        else:
            buy_turnover = len(current - previous) / len(current) if current else 0.0
            sell_turnover = len(previous - current) / len(previous) if previous else 0.0
        costs.append(buy_turnover * buy_rate + sell_turnover * sell_rate)
        ins.append(buy_turnover)
        outs.append(sell_turnover)
        previous = current
    return tuple(costs), tuple(ins), tuple(outs)


def quantile_portfolios(
    pools: Pools, *, quantiles: int = QUANTILES, costs: CostModel
) -> tuple[Portfolio, ...]:
    """每日池内按因子值等分 `quantiles` 组、组内等权；返回每组的毛/净日收益与换手。

    分组取**连续切片**（余数落在最高一组），成员按因子值升序——组号即分位（1 = 最低）。
    """
    if quantiles < 2:
        raise ValueError(f"quantiles 至少为 2，收到 {quantiles!r}")
    buy_rate, sell_rate = round_trip_rates(costs)
    members_by_group: list[list[tuple[str, ...]]] = [[] for _ in range(quantiles)]
    gross_by_group: list[list[float]] = [[] for _ in range(quantiles)]
    for pool in pools.days:
        size = len(pool.pairs) // quantiles
        for group in range(quantiles):
            chunk = pool.pairs[group * size :] if group == quantiles - 1 else pool.pairs[
                group * size : (group + 1) * size
            ]
            members_by_group[group].append(tuple(symbol for symbol, _, _ in chunk))
            gross_by_group[group].append(fmean(ret for _, _, ret in chunk))

    portfolios: list[Portfolio] = []
    for group in range(quantiles):
        fees, ins, outs = _cost_series(
            members_by_group[group], buy_rate=buy_rate, sell_rate=sell_rate
        )
        portfolios.append(
            Portfolio(
                quantile=group + 1,
                members=tuple(members_by_group[group]),
                gross=tuple(gross_by_group[group]),
                net=tuple(g - fee for g, fee in zip(gross_by_group[group], fees, strict=True)),
                turnover_in=ins,
                turnover_out=outs,
            )
        )
    return tuple(portfolios)


def long_short(
    portfolios: Sequence[Portfolio], pools: Pools, *, costs: CostModel
) -> Spread:
    """多空 = 最高组 − 最低组。**统计量，不可交易**（A 股不能做空）。

    净价差按两条腿各自扣费：多头腿买进卖出，空头腿**卖出开仓（含印花税）、买入平仓**——
    费率镜像，不是把多头腿的费用照抄一遍。
    """
    if len(portfolios) < 2:
        return Spread(gross=(), net=(), t_stat=None, days=0)
    buy_rate, sell_rate = round_trip_rates(costs)
    bottom, top = portfolios[0], portfolios[-1]
    gross = tuple(t - b for t, b in zip(top.gross, bottom.gross, strict=True))
    long_fees, _, _ = _cost_series(top.members, buy_rate=buy_rate, sell_rate=sell_rate)
    short_fees, _, _ = _cost_series(bottom.members, buy_rate=sell_rate, sell_rate=buy_rate)
    net = tuple(
        g - long_fee - short_fee
        for g, long_fee, short_fee in zip(gross, long_fees, short_fees, strict=True)
    )
    return Spread(gross=gross, net=net, t_stat=_t_stat(gross), days=len(gross))


def _t_stat(series: Sequence[float]) -> float | None:
    """`均值 / 样本标准差 × √n`——即「每期夏普 × √n」，与 `summarize_ic` 的 t 同口径。"""
    per_period = per_period_sharpe(series)
    return per_period * sqrt(len(series)) if per_period is not None else None


def curve_from_returns(returns: Sequence[float], initial: float = 1.0) -> tuple[float, ...]:
    """日收益序列 → 净值序列（含首点 `initial`，故长度是 `len(returns) + 1`）。"""
    levels = [initial]
    for ret in returns:
        levels.append(levels[-1] * (1.0 + ret))
    return tuple(levels)


def curve_metrics(levels: Sequence[float]) -> dict[str, float | None]:
    """净值序列 → 与回测同源的四个指标（复用 `metrics.py`，口径不另立）。"""
    if not levels:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0, "sharpe": None}
    total = levels[-1] / levels[0] - 1.0 if levels[0] else 0.0
    return {
        "total_return": total,
        "annual_return": annual_return(total, len(levels) - 1),
        "max_drawdown": max_drawdown(levels),
        "sharpe": sharpe_ratio(levels),
    }


def _curve_points(days: Sequence[date], returns: Sequence[float]) -> list[dict[str, object]]:
    """给前端的序列：第 i 个点是「第 i 个信号日那一笔持有期结束后的净值」。"""
    levels = curve_from_returns(returns)
    return [
        {"date": day.isoformat(), "level": level}
        for day, level in zip(days, levels[1:], strict=True)
    ]


def _track(days: Sequence[date], returns: Sequence[float], enabled: bool) -> dict | None:
    if not enabled:
        return None
    levels = curve_from_returns(returns)
    return {"curve": _curve_points(days, returns), "metrics": curve_metrics(levels)}


def factor_label(source: str, *, direction: str | None = None, lookback: int | None = None) -> str:
    """因子标识：事件源固定 `factor_value`；价格源把**方向与回看长度写进标识**——
    口径自述的一部分，端点与取证脚本共用，避免两处各拼一个字符串。"""
    if source == "event":
        return "factor_value"
    return f"price_{direction}_{lookback}"


def report_params(
    *,
    source: str,
    direction: str | None,
    lookback: int | None,
    adjust: str,
    costs: CostModel,
) -> dict[str, object]:
    """报告的 `params` 自述块（端点与取证脚本共用一份，防口径漂移）。"""
    return {
        "source": source,
        "factor": factor_label(source, direction=direction, lookback=lookback),
        "quantiles": QUANTILES,
        "min_pool": MIN_POOL,
        "aggregation": "mean",
        "horizon": "open_t1_to_open_t2",
        "adjust": adjust,
        "costs": {
            "fees": costs.fee_enabled,
            "slippage": costs.slippage_enabled,
            "slippage_bps": costs.slippage_bps,
            "note": "费率口径，不含最低佣金",
        },
    }


def event_universe(panel: EventPanel) -> dict[str, object]:
    """事件源的 `universe` 自述块（**计数来自面板**，不重算）。

    `symbols_seen` 不在这里：它必须与 `pool_avg` 等一样是**窗口内**的口径，
    故由 `build_report` 统一补——面板带着补窗的日子进来，两处口径不能各算各的。
    """
    return {
        "rows_seen": panel.rows_seen,
        "dropped_no_value": panel.dropped_no_value,
        "dropped_no_day": panel.dropped_no_day,
    }


def price_universe(*, lookback: int) -> dict[str, object]:
    """价格源的 `universe` 自述块（键名收在一处，端点与取证脚本共用）。"""
    return {"lookback": lookback}


def _notes(
    *,
    source: str,
    pools: Pools,
    costs_enabled: bool,
    direction: str | None,
    lookback: int | None,
    window_days: Sequence[date],
    cost_drag_bps: float,
    group_size: int,
) -> list[str]:
    """`notes` 是**必填清单**，不是自由文案——每条对应一个已知边界。

    `cost_drag_bps` / `group_size` 是**算出来的**（不是抄来的文案）：拖累取各组「毛 − 净」
    的日频均值；规模事实按「100 万 ÷ 分位组只数」现算每笔佣金，故它在事件源（约 50 只/组）
    与价格源（约 1,000 只/组）下会给出不同结论——**这正是要如实区分的地方**。
    """
    scale_note = (
        "净值为**费率口径**（佣金万 2.5 + 印花税 0.05% 卖出 + 滑点 5bps），**不含最低佣金 5 元**"
        "——它取决于每笔订单金额 × 资金规模，本报告不预设规模。"
    )
    if group_size > 0:
        per_order_commission = 1_000_000.0 / group_size * 0.00025
        scale_note += (
            f"规模事实如实给出：100 万资金下，一个约 {group_size} 只的分位组每笔佣金约 "
            f"{per_order_commission:.2f} 元"
        )
        if per_order_commission < 5.0:
            scale_note += (
                f"，**低于 5 元下限 → 被抬 {5.0 / per_order_commission:.1f}×**"
                "（资金越小、持仓越分散，抬得越多）"
            )
        else:
            scale_note += "，在下限之上（下限不生效）"
    else:
        scale_note += "本窗口没有有效池子，规模事实无从给出"

    notes = [
        f"有效信号日 {len(pools.days)} 天，共 {len(window_days)} 个交易日；"
        "行情末端之后的事件没有前向收益，如实跳过",
        "多空价差是**统计量**：A 股不可做空，该组合不可交易",
        "净值按毛 / 净双轨给出；净值为**费率口径**"
        + ("（已含佣金、印花税与滑点）" if costs_enabled else "（本次请求关闭了费用）"),
    ]
    if source == "event":
        notes.insert(
            0,
            "因子池是「当日有向且挂了标的」的事件池，**不是全市场**——分层只在池内排序",
        )
        notes.append(
            "新闻信号因子与 `factor_scores.score` 同源（|factor_value| = score / 100），"
            "不存在第二个变体可供挑选"
        )
    else:
        notes.insert(0, f"价格因子在全市场池内排序（当日有行情且有 {lookback} 根历史的标的）")
        notes.append(f"价格因子为 {lookback} 日 {direction}；样本期特定，读数不代表长期结论")
    notes.append(
        "换手口径：只对**进出名单**收费（继续持有的不调仓、权重漂移），首日按全新建仓（买 100%）；"
        "换手率序列随报告逐组给出"
    )
    notes.append(
        f"本窗口实测日均费用拖累 **{cost_drag_bps:.1f} bps**（年化 ≈ {cost_drag_bps * 252 / 100:.0f}%），"
        "即毛净值与净净值之差"
    )
    notes.append(scale_note)
    notes.append(
        f"年化指标由 {len(pools.days)} 个交易日外推（与回测同口径的 `metrics.annual_return`），"
        "短样本下量级仅供参考"
    )
    if pools.skipped_thin:
        notes.append(f"有 {pools.skipped_thin} 天因池内样本 < {MIN_POOL} 只被跳过")
    notes.append("若 IC 的 t 绝对值很小（|t| < 2），那是**噪声区间内的读数**，不是因子有效的证据")
    return notes


def build_report(
    *,
    source: str,
    values: Mapping[date, Mapping[str, float]],
    forward: Mapping[date, Mapping[str, float]],
    all_days: Sequence[date],
    start: date,
    end: date,
    costs: CostModel,
    costs_enabled: bool,
    params: Mapping[str, object],
    universe: Mapping[str, object],
    direction: str | None = None,
    lookback: int | None = None,
) -> dict[str, object]:
    """组装 tear sheet。窗口过滤在这里做——面板可以带补窗的日子进来（归属日需要）。"""
    window_days = [day for day in all_days if start <= day <= end]
    in_window = {day: values[day] for day in values if start <= day <= end}
    forward_in_window = {day: forward[day] for day in forward if start <= day <= end}

    pools = build_pools(in_window, forward_in_window)
    stats = summarize_ic(ic_series(pools))
    portfolios = quantile_portfolios(pools, costs=costs)
    spread = long_short(portfolios, pools, costs=costs)
    days = [pool.day for pool in pools.days]

    sizes = [len(pool.pairs) for pool in pools.days]
    groups = [
        {
            "quantile": portfolio.quantile,
            "label": GROUP_LABELS.get(portfolio.quantile, f"Q{portfolio.quantile}"),
            "turnover_avg": fmean(portfolio.turnover_in) if portfolio.turnover_in else 0.0,
            # 换手率**序列**（SPEC 要求）：它是解释净曲线的关键，不是装饰。
            # `buy` = 当日新进名单占组合的比例，`sell` = 当日退出名单占比；首日 buy = 1
            "turnover": [
                {"date": day.isoformat(), "buy": buy, "sell": sell}
                for day, buy, sell in zip(
                    days, portfolio.turnover_in, portfolio.turnover_out, strict=True
                )
            ],
            "gross": _track(days, portfolio.gross, True),
            "net": _track(days, portfolio.net, costs_enabled),
        }
        for portfolio in portfolios
    ]
    drags = [
        fmean(g - n for g, n in zip(portfolio.gross, portfolio.net, strict=True))
        for portfolio in portfolios
        if portfolio.gross
    ]

    return {
        "params": dict(params),
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "first_signal_day": days[0].isoformat() if days else None,
            "last_signal_day": days[-1].isoformat() if days else None,
            "signal_days": len(days),
            "skipped_no_window": pools.skipped_no_window,
            "skipped_thin_pool": pools.skipped_thin,
        },
        "universe": {
            **universe,
            # 覆盖标的数按**窗口内**算（与 pool_* / dropped_* 同口径）——面板含补窗的日子
            "symbols_seen": len({symbol for day in in_window.values() for symbol in day}),
            "pool_avg": round(fmean(sizes)) if sizes else 0,
            "pool_min": min(sizes, default=0),
            "pool_max": max(sizes, default=0),
            "dropped_no_price": pools.dropped_no_price,
            "dropped_untradeable": pools.dropped_untradeable,
        },
        "ic": {
            "per_day": [
                {"date": point.day.isoformat(), "ic": point.ic, "n": point.n}
                for point in stats.points
            ],
            "mean": stats.mean,
            "std": stats.std,
            "icir": stats.icir,
            "t_stat": stats.t_stat,
            "positive_days": stats.positive_days,
            "days": stats.days,
        },
        "groups": groups,
        "long_short": {
            "gross": _track(days, spread.gross, True),
            "net": _track(days, spread.net, costs_enabled),
            "t_stat": spread.t_stat,
            "tradable": False,
        },
        "notes": _notes(
            source=source,
            pools=pools,
            costs_enabled=costs_enabled,
            direction=direction,
            lookback=lookback,
            window_days=window_days,
            cost_drag_bps=fmean(drags) * 1e4 if drags else 0.0,
            group_size=sizes[len(sizes) // 2] // QUANTILES if sizes else 0,
        ),
    }
