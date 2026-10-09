"""M5c 因子统计：RankIC / 分层 / 多空 / 换手与费用 / tear sheet 组装。

口径全部可手算：IC 用已知排序（含并列名次）钉，费用用「进出名单 → 费率」钉。
池子那道门槛（缺价剔除 / |收益|>30% 剔除 / 样本不足跳过）各一条。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.costs import CostModel
from app.factor.analysis import (
    MIN_POOL,
    build_pools,
    build_report,
    curve_from_returns,
    curve_metrics,
    ic_series,
    long_short,
    quantile_portfolios,
    summarize_ic,
)

DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)
DAY3 = date(2026, 8, 5)
DAYS = (DAY1, DAY2, DAY3)


def _values(count: int, day: date = DAY1, scale: float = 1.0) -> dict[str, float]:
    return {f"s{i:02d}": i * scale for i in range(count)}


def _forward(count: int, day: date = DAY1) -> dict[str, float]:
    return {f"s{i:02d}": (i - count / 2) / 100 for i in range(count)}


# ── RankIC ──────────────────────────────────────────────────


def test_rank_ic_is_one_for_a_perfect_ordering() -> None:
    pools = build_pools({DAY1: _values(10)}, {DAY1: _forward(10)}, min_pool=3)
    (point,) = ic_series(pools)
    assert point.day == DAY1
    assert point.ic == pytest.approx(1.0)
    assert point.n == 10


def test_rank_ic_handles_tied_ranks() -> None:
    """并列名次取平均秩——手算一遍：rx=[1.5,1.5,3,4]、ry=[4,1,2,3] ⇒ ρ≈0.1054。"""
    values = {DAY1: {"a": 1.0, "b": 1.0, "c": 2.0, "d": 4.0}}
    forward = {DAY1: {"a": 0.04, "b": 0.01, "c": 0.02, "d": 0.03}}

    (point,) = ic_series(build_pools(values, forward, min_pool=3))

    assert point.ic == pytest.approx(0.105409255338946)


def test_ic_summary_reports_icir_and_t() -> None:
    points = ic_series(
        build_pools(
            {DAY1: _values(10), DAY2: _values(10), DAY3: _values(10)},
            {DAY1: _forward(10), DAY2: _forward(10), DAY3: _forward(10)},
            min_pool=3,
        )
    )

    stats = summarize_ic(points)

    assert stats.days == 3
    assert stats.mean == pytest.approx(1.0)
    assert stats.positive_days == 3
    # 三天 IC 恒为 1 ⇒ 标准差 0 ⇒ ICIR/t 无定义（**返 None 而不是除零编个数**）
    assert stats.std == 0.0
    assert stats.icir is None
    assert stats.t_stat is None


def test_ic_summary_without_days_is_all_none() -> None:
    stats = summarize_ic(())
    assert (stats.mean, stats.std, stats.icir, stats.t_stat) == (None, None, None, None)
    assert stats.days == 0


# ── 池子门槛 ────────────────────────────────────────────────


def test_pool_drops_missing_prices_and_untradeable_returns() -> None:
    values = {DAY1: {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}}
    # c 缺价（前向收益缺席）；d 是 +50% 的新股/复牌样本
    forward = {DAY1: {"a": 0.01, "b": 0.02, "d": 0.5}}

    pools = build_pools(values, forward, min_pool=1)

    assert [symbol for symbol, _, _ in pools.days[0].pairs] == ["a", "b"]
    assert pools.dropped_no_price == 1
    assert pools.dropped_untradeable == 1


def test_exactly_thirty_percent_is_kept() -> None:
    """剔的是 `> 30`，与 M5a 基准同规则同边界（北交所 30% 涨停是真实成交）。"""
    values = {DAY1: {"a": 1.0, "b": 2.0, "c": 3.0}}
    forward = {DAY1: {"a": 0.30, "b": 0.0, "c": -0.30}}

    pools = build_pools(values, forward, min_pool=1)

    assert len(pools.days[0].pairs) == 3
    assert pools.dropped_untradeable == 0


def test_thin_days_are_skipped_and_counted() -> None:
    values = {DAY1: _values(MIN_POOL - 1), DAY2: _values(MIN_POOL)}
    forward = {DAY1: _forward(MIN_POOL - 1), DAY2: _forward(MIN_POOL)}

    pools = build_pools(values, forward)

    assert [pool.day for pool in pools.days] == [DAY2]
    assert pools.skipped_thin == 1


def test_pool_pairs_are_sorted_by_factor_value() -> None:
    pools = build_pools({DAY1: _values(5, scale=-1.0)}, {DAY1: _forward(5)}, min_pool=1)
    assert [value for _, value, _ in pools.days[0].pairs] == sorted(
        value for _, value, _ in pools.days[0].pairs
    )


# ── 分层与多空 ──────────────────────────────────────────────


def test_quantiles_split_by_rank_and_spread_is_top_minus_bottom() -> None:
    values = {DAY1: _values(10)}
    forward = {DAY1: _forward(10)}  # 因子值越大收益越高

    pools = build_pools(values, forward, min_pool=10)
    portfolios = quantile_portfolios(pools, quantiles=5, costs=CostModel.disabled())

    assert [p.quantile for p in portfolios] == [1, 2, 3, 4, 5]
    # 因子值 = 收益排名（s00 最低收益 −5%、s09 最高 +4%），故最低组均值 −4.5%、最高组 +3.5%
    assert portfolios[0].gross[0] == pytest.approx(-0.045)
    assert portfolios[4].gross[0] == pytest.approx(0.035)

    spread = long_short(portfolios, pools, costs=CostModel.disabled())
    assert spread.gross[0] == pytest.approx(0.08)
    assert spread.net == pytest.approx(spread.gross)  # 费用全关 ⇒ 毛净相等


def test_turnover_and_costs_are_hand_computable() -> None:
    """Q1（最低两只是 s00/s01）第二天换掉一只 ⇒ 换手 0.5 买 + 0.5 卖。

    费率取自 `CostModel`：买 = 万 2.5 + 滑点 5bps = 0.00075；卖 = 再 + 印花税 0.05% = 0.00125。
    首日全新建仓（买 100%），只付买方费率。
    """
    values = {
        DAY1: _values(10),
        DAY2: {"s00": 0.0, "t01": 0.5, **{f"s{i:02d}": float(i) for i in range(2, 10)}},
    }
    forward = {DAY1: _forward(10), DAY2: {**_forward(10), "t01": 0.02}}

    pools = build_pools(values, forward, min_pool=10)
    portfolios = quantile_portfolios(pools, quantiles=5, costs=CostModel())
    bottom = portfolios[0]

    assert bottom.members[0] == ("s00", "s01")
    assert bottom.members[1] == ("s00", "t01")
    assert bottom.turnover_in[0] == pytest.approx(1.0)  # 首日建仓
    assert bottom.turnover_out[0] == pytest.approx(0.0)
    assert bottom.turnover_in[1] == pytest.approx(0.5)
    assert bottom.turnover_out[1] == pytest.approx(0.5)
    assert bottom.gross[1] - bottom.net[1] == pytest.approx(0.5 * 0.00075 + 0.5 * 0.00125)
    assert bottom.gross[0] - bottom.net[0] == pytest.approx(0.00075)


# ── 曲线与指标 ──────────────────────────────────────────────


def test_curve_compounds_and_metrics_reuse_the_shared_module() -> None:
    levels = curve_from_returns([0.01, -0.02, 0.03])

    assert levels == pytest.approx((1.0, 1.01, 0.9898, 1.019494))
    metrics = curve_metrics(levels)
    assert metrics["total_return"] == pytest.approx(0.019494)
    assert metrics["max_drawdown"] == pytest.approx(0.02)
    assert metrics["sharpe"] is not None


def test_metrics_of_an_empty_curve_do_not_raise() -> None:
    metrics = curve_metrics(())
    assert metrics == {
        "total_return": 0.0,
        "annual_return": 0.0,
        "max_drawdown": 0.0,
        "sharpe": None,
    }


# ── 报告组装 ────────────────────────────────────────────────


#: 报告夹具用 25 只（> MIN_POOL=20，每天分 5 组、每组 5 只）——用 10 只的话每日池子
#: 会被门槛挡掉，报告会「空着也照样出五组」，用例就测不到真东西了
_REPORT_COUNT = 25


def _report(**overrides: object) -> dict:
    values = {day: _values(_REPORT_COUNT) for day in DAYS}
    forward = {day: _forward(_REPORT_COUNT) for day in DAYS}
    kwargs: dict = {
        "source": "event",
        "values": values,
        "forward": forward,
        "all_days": [*DAYS, date(2026, 8, 6)],
        "start": DAY1,
        "end": DAY3,
        "costs": CostModel(),
        "costs_enabled": True,
        "params": {"source": "event", "factor": "factor_value"},
        "universe": {"symbols_seen": 10},
    }
    kwargs.update(overrides)
    return build_report(**kwargs)


def test_report_has_the_contracted_top_level_shape() -> None:
    report = _report()

    assert set(report) == {"params", "window", "universe", "ic", "groups", "long_short", "notes"}
    assert len(report["groups"]) == 5
    assert set(report["groups"][0]) == {
        "quantile", "label", "turnover_avg", "turnover", "gross", "net",
    }
    # 换手率**序列**逐日给出两条腿（SPEC 要求入报告，不是只给均值）
    assert set(report["groups"][0]["turnover"][0]) == {"date", "buy", "sell"}
    # 多空不可交易：这一条不是文案，是响应里的字段
    assert report["long_short"]["tradable"] is False
    assert {"gross", "net", "t_stat"} <= set(report["long_short"])


def test_report_notes_carry_the_mandatory_disclaimers() -> None:
    notes = _report()["notes"]

    assert any("不可做空" in note for note in notes)
    assert any("噪声" in note for note in notes)
    assert any("最低佣金" in note for note in notes)


def test_report_window_filters_days_outside_it() -> None:
    """面板可以带补窗的日子进来（归属日需要），但报告只认窗口内的。"""
    values = {DAY1: _values(_REPORT_COUNT), DAY3: _values(_REPORT_COUNT),
              date(2026, 7, 1): _values(_REPORT_COUNT)}
    forward = {DAY1: _forward(_REPORT_COUNT), DAY3: _forward(_REPORT_COUNT),
               date(2026, 7, 1): _forward(_REPORT_COUNT)}

    report = _report(values=values, forward=forward)

    assert report["window"]["signal_days"] == 2
    assert report["ic"]["days"] == 2


def test_report_marks_costs_off_when_disabled() -> None:
    report = _report(costs=CostModel.disabled(), costs_enabled=False)

    assert report["groups"][0]["net"] is None
    assert report["long_short"]["net"] is None
    assert report["long_short"]["gross"] is not None
