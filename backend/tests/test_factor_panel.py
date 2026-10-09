"""M5c 因子面板：PIT 归属、聚合、前向收益。

面板是**纯函数**（输入是落盘行、不是库），所以边界用例喂合成行；而「与回测同一把尺子」
不靠口号——同批事件分别喂 `EventFeed.advance` 与 `bucket_day`，归属日必须**逐条相等**
（本文件第一节）。这也是 SPEC §12 M5c 增量里点名的那条断言。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.events import EventFeed
from app.backtest.types import Bar, EventView, Mode
from app.factor.panel import build_event_panel, build_price_panel, bucket_day
from tests.conftest import ts

#: 一个交易周（周一~周五）+ 下周一，供「周末顺延」用例
WEEK = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]
NEXT_MONDAY = date(2026, 8, 10)
DAYS = [*WEEK, NEXT_MONDAY]


# ── 同尺子：归属日 vs 引擎的事件放行日 ──────────────────────


def test_bucket_day_and_engine_feed_agree_on_every_event() -> None:
    """同批事件、两种实现：面板的归属日 == 引擎首次放行它的那根 bar。

    边界刻意压在闸门上下：**15:00:00 整点算当日、15:00:01 顺延**（引擎用 `<=`）。
    """
    stamps = [
        ts("2026-08-03 09:30:00"),  # 盘中
        ts("2026-08-03 14:59:59"),  # 收盘前一秒
        ts("2026-08-03 15:00:00"),  # 整点：**算当日**
        ts("2026-08-03 15:00:01"),  # 一秒之后：顺延到下一交易日
        ts("2026-08-04 15:00:00"),
        ts("2026-08-05 15:00:01"),
        ts("2026-08-08 11:00:00"),  # 周六
        ts("2026-08-09 23:59:59"),  # 周日深夜：同样落到下周一
        ts("2026-08-10 15:00:00"),
        ts("2026-08-10 15:00:01"),  # 最后一天收盘后：窗口内无日子可归
        ts("2026-08-20 09:00:00"),  # 晚于全部日子
    ]
    events = [
        EventView(
            event_id=f"e{i}",
            symbols=("600519",),
            title="",
            event_time=stamp,
            available_at=stamp,
            direction_norm="bullish",
            score=50.0,
        )
        for i, stamp in enumerate(stamps)
    ]
    feed = EventFeed(events, Mode.PIT)
    released: dict[str, date] = {}
    for day in DAYS:
        bar = Bar(symbol="600519", trade_date=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
        for fresh in feed.advance(bar):
            released[fresh.event_id] = day

    ours = {f"e{i}": bucket_day(stamp, DAYS) for i, stamp in enumerate(stamps)}

    assert ours == {eid: released.get(eid) for eid in ours}
    # 再钉一遍人读得懂的期望，防「两边一起错」
    assert ours["e2"] == date(2026, 8, 3)
    assert ours["e3"] == date(2026, 8, 4)
    assert ours["e6"] == NEXT_MONDAY
    assert ours["e7"] == NEXT_MONDAY
    assert ours["e10"] is None


def test_bucket_day_without_days_is_none() -> None:
    assert bucket_day(ts("2026-08-03 09:30:00"), []) is None


# ── 事件面板 ────────────────────────────────────────────────


def test_event_panel_averages_same_symbol_same_day_and_counts_drops() -> None:
    rows = [
        {"symbol": "600519", "available_at": ts("2026-08-03 09:30:00"), "factor_value": 0.6},
        {"symbol": "600519", "available_at": ts("2026-08-03 14:00:00"), "factor_value": 0.2},
        {"symbol": "000001", "available_at": ts("2026-08-03 14:00:00"), "factor_value": -0.4},
        {"symbol": "600519", "available_at": ts("2026-08-03 09:00:00"), "factor_value": None},
        {"symbol": "000001", "available_at": ts("2026-08-30 09:00:00"), "factor_value": 0.9},
    ]

    panel = build_event_panel(rows, DAYS)

    # 同 (日, 标的) 取 **mean**，不是 sum：避免「新闻条数多」的标的被系统性放大
    assert panel.values[date(2026, 8, 3)] == {"600519": pytest.approx(0.4), "000001": -0.4}
    assert panel.rows_seen == 5
    assert panel.dropped_no_value == 1
    assert panel.dropped_no_day == 1


def test_event_panel_clamps_older_events_to_the_first_day_like_the_feed() -> None:
    """早于全部日子的事件归到**首日**——与 `EventFeed` 在首根 bar 就放行它们同理。

    真正把窗口外的日子裁掉的是报告层（`analysis`）：传进来的 `days` 必须含窗口**之前**
    的交易日（价格查询本就多扫一段补窗），否则窗口首日会把历史事件整堆吸进来。
    """
    rows = [
        {"symbol": "600519", "available_at": ts("2026-07-01 09:00:00"), "factor_value": 0.5},
        {"symbol": "000001", "available_at": ts("2026-08-30 09:00:00"), "factor_value": 0.9},
    ]

    panel = build_event_panel(rows, DAYS)

    assert panel.values == {WEEK[0]: {"600519": 0.5}}
    assert panel.dropped_no_day == 1  # 晚于最后一天的才叫「无日子可归」


# ── 价格面板 ────────────────────────────────────────────────


def _price_rows(lag: float | None = 8.0) -> list[dict]:
    return [
        {
            "symbol": "600519",
            "trade_date": day,
            "open": float(10 + i),
            "close": float(20 + i),
            "close_lag": None if (lag is None or i == 0) else lag,
        }
        for i, day in enumerate(WEEK)
    ]


def test_price_panel_reversal_is_negated_momentum() -> None:
    panel = build_price_panel(_price_rows(), direction="reversal")

    assert panel.days == tuple(WEEK)
    # momentum = close / close_lag - 1 = 21 / 8 - 1；reversal 取反
    assert panel.factor[WEEK[1]]["600519"] == pytest.approx(-(21 / 8 - 1))
    # 没有 lag（首根 / 停牌导致补窗不足）的标的当日不进池
    assert "600519" not in panel.factor.get(WEEK[0], {})

    momentum = build_price_panel(_price_rows(), direction="momentum")
    assert momentum.factor[WEEK[1]]["600519"] == pytest.approx(21 / 8 - 1)


def test_forward_return_is_next_open_to_the_open_after() -> None:
    """`open(t+1) → open(t+2)`：与引擎「信号 bar 收盘生成、next bar 开盘成交」同口径。"""
    panel = build_price_panel(_price_rows())

    assert panel.forward[WEEK[0]]["600519"] == pytest.approx(12 / 11 - 1)
    # 末两天没有 t+2，如实缺席而不是补 0
    assert WEEK[3] not in panel.forward
    assert WEEK[4] not in panel.forward


def test_forward_return_absent_when_a_price_is_missing() -> None:
    """停牌/缺价的日子：该标的当日没有前向收益（缺席），不是 0%。"""
    rows = _price_rows()
    rows[2]["open"] = None  # 周三缺开盘价

    panel = build_price_panel(rows)

    # 周三缺开盘价 ⇒ 周一（进场价要用周二、出场价要用周三）与周二（进场价要用周三）
    # 都缺席；周三自己不受影响——它只用周四、周五的开盘
    assert "600519" not in panel.forward.get(WEEK[0], {})
    assert "600519" not in panel.forward.get(WEEK[1], {})
    assert panel.forward[WEEK[2]]["600519"] == pytest.approx(14 / 13 - 1)


def test_price_panel_rejects_unknown_direction() -> None:
    with pytest.raises(ValueError, match="因子方向"):
        build_price_panel(_price_rows(), direction="sideways")
