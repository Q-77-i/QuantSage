"""M7a 归因（`app.report.attribution`）：标的级与事件级。

标的级：每只标的对资金的贡献（已实现 + 未平仓浮盈）/ 初始资金。
事件级：买入决策**驱动事件**的方向与行业分布，逐组给回合结果。
两个口径都在这里钉死：贡献用 pp（百分点）而非比例；事件级按「一笔回合计入它驱动事件
的每个行业」——**组间之和大于整体是正常的**，报告里要如实写这句。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.report.attribution import signal_attribution, symbol_attribution
from app.report.evidence import EvidenceItem
from tests.test_memory_settle import buy, sell


def _evidence(event_id: str, day: date, *, direction: str | None, industries: tuple[str, ...]):
    return EvidenceItem(
        event_id=event_id,
        day=day,
        direction_norm=direction,
        industries=industries,
        found=True,
    )


def test_symbol_attribution_splits_realized_and_unrealized() -> None:
    """两只标的：一只已实现盈利 + 未平仓浮盈，一只亏损——按贡献降序。

    600519：已实现 984.50、浮盈 1,995.00 → 贡献 (984.5+1995)/100000 = 2.9795 pp，1 胜
    000001：买 100 × 10.00（费 5）= 1,005；卖 100 × 10.00（费 5 + 印花 5.5）= 989.5
            → 已实现 −15.50 → 贡献 −0.0155 pp
    """
    from app.memory.settle import mark_open_trips, pair_trips

    trips = mark_open_trips(
        [
            *pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))]),
            *pair_trips(
                [
                    buy(date(2026, 8, 4), symbol="000001", qty=100, price=10.0, did="b2"),
                    sell(date(2026, 8, 11), symbol="000001", qty=100, price=10.0, did="s2"),
                ]
            ),
            *pair_trips([buy(date(2026, 9, 30), did="b3")]),
        ],
        {"600519": 12.0},
    )

    rows = symbol_attribution(trips, initial_cash=100_000.0)

    assert [row.symbol for row in rows] == ["600519", "000001"]
    first = rows[0]
    assert first.trips == 2
    assert first.closed == 1
    assert first.wins == 1
    assert first.realized_pnl == pytest.approx(984.50)
    assert first.unrealized_pnl == pytest.approx(1995.00)
    assert first.contribution_pp == pytest.approx(2.9795)
    second = rows[1]
    assert second.closed == 1
    assert second.wins == 0
    assert second.realized_pnl == pytest.approx(-15.50)
    assert second.unrealized_pnl == pytest.approx(0.0)
    assert second.contribution_pp == pytest.approx(-0.0155)


def test_unmarked_open_trip_counts_as_unmarked_not_zero_money() -> None:
    """缺价未估值的未平仓回合：浮盈记 0 **但单独计数**——「没估值」不等于「不赚不赔」。"""
    from app.memory.settle import pair_trips

    trips = pair_trips([buy(date(2026, 9, 30), did="b-open")])
    rows = symbol_attribution(trips, initial_cash=100_000.0)

    assert rows[0].unrealized_pnl == pytest.approx(0.0)
    assert rows[0].unmarked == 1
    assert rows[0].trips == 1


def test_signal_attribution_groups_by_direction_and_industry() -> None:
    """事件级：方向组与行业组各算一遍；没有事件来源的买入单列「无事件来源」。"""
    from app.memory.settle import mark_open_trips, pair_trips

    bull = {"event_id": "news:1", "event_time": "2026-08-01 10:00:00+08:00"}
    bull2 = {"event_id": "news:2", "event_time": "2026-08-02 10:00:00+08:00"}
    buys = [
        buy(date(2026, 8, 1), qty=100, price=10.0, did="b1").replace(
            event_id="news:1", sources=bull
        ),
        buy(date(2026, 8, 2), qty=100, price=10.0, did="b2").replace(
            event_id="news:2", sources=bull2
        ),
        buy(date(2026, 9, 30), qty=100, price=10.0, did="b3"),  # 无来源、未平仓
    ]
    trips = mark_open_trips(
        [
            *pair_trips([buys[0], sell(date(2026, 8, 10), qty=100, price=11.0, did="s1")]),
            *pair_trips([buys[1], sell(date(2026, 8, 12), qty=100, price=9.5, did="s2")]),
            *pair_trips([buys[2]]),
        ],
        {},  # 不给价 ⇒ b3 的回合无估值
    )
    evidence = {
        "news:1|2026-08-01": _evidence("news:1", date(2026, 8, 1), direction="bullish",
                                       industries=("银行",)),
        "news:2|2026-08-02": _evidence("news:2", date(2026, 8, 2), direction="bullish",
                                       industries=("银行", "地产")),
    }

    groups = signal_attribution(buys, trips, evidence)

    directions = {row.label: row for row in groups["direction"]}
    assert set(directions) == {"利多", "无事件来源"}
    assert directions["利多"].trips == 2
    assert directions["利多"].closed == 2
    assert directions["利多"].wins == 1
    assert directions["无事件来源"].trips == 1
    assert directions["无事件来源"].unmarked == 1

    industries = {row.label: row for row in groups["industry"]}
    assert set(industries) == {"银行", "地产"}
    assert industries["银行"].trips == 2  # 两个事件都挂银行
    assert industries["地产"].trips == 1
