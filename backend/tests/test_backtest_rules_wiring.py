"""M5a 引擎接线：涨跌停拒单真的落到回测里，且**降级路径不误拦**。

这里验的是「规则接进引擎之后的行为」，规则本身的口径在 `test_a_share_rules.py`。
两条硬要求各有对应用例：

  * **拒单要真的发生**——一字涨停日的买单成交不了，且原因码进报告（SPEC 验收第 3 条）；
  * **不误拦**——同一天只要开过板就照常成交；缺 raw 序列时整条判定降级放行。
    单侧的拒单用例证明不了不误伤，故每一处拒单都配一条放行。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.report import build_report
from app.backtest.types import BarContext, Side, Signal
from tests.conftest import make_backtest_dir, write_bars_parquet

SYMBOL = "600519"
DAYS = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]


class BuyOnSecondBar:
    """在第 2 根 bar 收盘发出买单——成交落在第 3 根开盘，正好是我们布置的那天。"""

    name = "buy_on_second_bar"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if ctx.index == 1 and ctx.position.is_flat:
            return [Signal(Side.BUY, reason="test:buy")]
        return []


def limit_up_bars() -> list[dict[str, object]]:
    """前收 10.00 → 涨停价 11.00；第 3 根做成**一字涨停**（开=高=低=收=11.00）。"""
    return [
        {"trade_date": DAYS[0], "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
        {"trade_date": DAYS[1], "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
        {"trade_date": DAYS[2], "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0},
        {"trade_date": DAYS[3], "open": 11.0, "high": 11.2, "low": 10.8, "close": 11.0},
    ]


def opened_up_bars() -> list[dict[str, object]]:
    """同一天**开过板**（开盘没封住）——买得到，不拦。"""
    bars = limit_up_bars()
    bars[2] = {"trade_date": DAYS[2], "open": 10.8, "high": 11.0, "low": 10.5, "close": 11.0}
    return bars


def config(data_dir) -> BacktestConfig:  # noqa: ANN001 - Path
    return BacktestConfig(symbol=SYMBOL, strategy="ma_cross", data_dir=data_dir)


def test_buy_is_rejected_on_a_one_word_limit_up_day(tmp_path) -> None:  # noqa: ANN001
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")

    result = run_backtest(config(root), strategy=BuyOnSecondBar())
    assert result.fills == ()  # 买不到
    assert [dropped.code for dropped in result.dropped_signals] == ["REJECT_LIMIT_UP"]
    assert result.dropped_signals[0].trade_date == DAYS[2]


def test_buy_fills_when_the_limit_opened_up(tmp_path) -> None:  # noqa: ANN001
    """**成对用例**：一字与开板只差 `low` 有没有掉下来，结论必须相反。"""
    bars = opened_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")

    result = run_backtest(config(root), strategy=BuyOnSecondBar())
    assert [fill.trade_date for fill in result.fills] == [DAYS[2]]
    assert result.fills[0].ref_price == pytest.approx(10.8)
    assert result.dropped_signals == ()


def test_missing_raw_series_degrades_to_no_limit_check(tmp_path) -> None:  # noqa: ANN001
    """只有 qfq 分片（既有夹具就是这个形状）→ 涨跌停判定整条降级，成交照旧。

    降级是「放行」不是「拒单」：拒单会让回测凭空少掉一批成交，而少成交在报告里
    看不出来。代价是这一天不做涨跌停约束——如实标在报告的 `a_share_rules` 里。
    """
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)

    result = run_backtest(config(root), strategy=BuyOnSecondBar())
    assert [fill.trade_date for fill in result.fills] == [DAYS[2]]


def test_unknown_board_degrades_to_no_limit_check(tmp_path) -> None:  # noqa: ANN001
    """板别认不出（代码前缀不在 14 个之内）→ 同样降级，不拿主板幅度硬套。"""
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol="999999")
    write_bars_parquet(root / "bars", "999999", bars, adjust="raw")

    result = run_backtest(
        BacktestConfig(symbol="999999", strategy="ma_cross", data_dir=root),
        strategy=BuyOnSecondBar(),
    )
    assert [fill.trade_date for fill in result.fills] == [DAYS[2]]


def test_sell_is_not_blocked_by_a_limit_up_day(tmp_path) -> None:  # noqa: ANN001
    """一字涨停只拦买单——涨停日**卖得掉**（有的是买家）。"""
    bars = limit_up_bars()
    bars[1] = {"trade_date": DAYS[1], "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")

    class BuyThenSell:
        name = "buy_then_sell"

        def on_bar(self, ctx: BarContext) -> list[Signal]:
            if ctx.index == 0 and ctx.position.is_flat:
                return [Signal(Side.BUY, reason="t")]
            if ctx.index == 2 and not ctx.position.is_flat:
                return [Signal(Side.SELL, reason="t")]
            return []

    result = run_backtest(config(root), strategy=BuyThenSell())
    # 买在第 2 根开盘成交；第 4 根开盘卖出时，第 3 根已是一字涨停——不构成拦截
    assert [fill.trade_date for fill in result.fills] == [DAYS[1], DAYS[3]]
    assert all(dropped.code != "REJECT_LIMIT_UP" for dropped in result.dropped_signals)


def test_report_surfaces_reject_codes(tmp_path) -> None:  # noqa: ANN001
    """报告要能看到拒单**原因码**，否则 SPEC 验收第 3 条的「原因可见」无从谈起。"""
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")

    report = build_report(config(root))
    assert set(report["rejects"]) >= {"count", "by_code", "items"}
    assert report["rejects"]["by_code"] == {}
    assert report["meta"]["a_share_rules"]["limit_check"] == "on"


def test_report_marks_the_degraded_limit_check(tmp_path) -> None:  # noqa: ANN001
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)

    report = build_report(config(root))
    assert report["meta"]["a_share_rules"] == {
        "limit_check": "skipped",
        "reason": "no_raw_series",
        "is_st": False,
        "limit_pct": 0.10,
    }


def test_report_reject_items_carry_the_code(tmp_path) -> None:  # noqa: ANN001
    bars = limit_up_bars()
    root = make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")

    report = build_report(config(root), strategy_factory=BuyOnSecondBar)
    assert report["rejects"]["count"] == 1
    assert report["rejects"]["by_code"] == {"REJECT_LIMIT_UP": 1}
    (item,) = report["rejects"]["items"]
    assert item["date"] == DAYS[2].isoformat()
    assert item["side"] == "buy"
    assert item["code"] == "REJECT_LIMIT_UP"
    assert item["reason"]
