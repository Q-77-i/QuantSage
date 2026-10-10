"""M7b 决策记忆（`app.memory.decision_store`）：Store 读写与幂等判据。

离线用 `InMemoryStore` 跑（真库那份在集成用例里双跑，同 M1c 口径）。这里钉三件事：
① 存进去的是「结算快照 + 反思」而不是可重算的事实；② **用户隔离靠 namespace 前缀**；
③ 幂等判据 `is_current`——已平仓永久作数、未平仓随账户推进而重结。
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from langgraph.store.memory import InMemoryStore

from app.memory.decision_store import DecisionMemory, is_current, memory_namespace
from app.memory.reflection import Reflection
from app.memory.settle import SettledTrip


def _settled(
    decision_id: str = "d-buy",
    *,
    symbol: str = "600519",
    entry: date = date(2026, 8, 3),
    exit_: date | None = date(2026, 8, 10),
    pnl: float | None = 984.50,
    alpha: float | None = 7.84,
) -> SettledTrip:
    return SettledTrip(
        decision_id=decision_id,
        symbol=symbol,
        entry_date=entry,
        exit_date=exit_,
        settled=exit_ is not None,
        pnl=pnl,
        return_pct=0.0984008 if pnl is not None else None,
        benchmark_pct=0.02,
        alpha_pp=alpha,
        window_days=6,
        entry_reason="MA 金叉",
        exit_reason="持有到期" if exit_ else "",
    )


def _memory() -> DecisionMemory:
    return DecisionMemory(InMemoryStore())


def test_save_and_get_round_trip_carries_identity() -> None:
    memory = _memory()
    reflection = Reflection("吃到主升段。", "deepseek/deepseek-flash")

    asyncio.run(
        memory.save(
            user_id=1,
            account_id="acct-1",
            settled=_settled(),
            reflection=reflection,
            direction="bullish",
            evidence_key="news:1|2026-08-03",
            as_of=date(2026, 9, 30),
            settled_at="2026-10-10T12:00:00+00:00",
        )
    )
    row = asyncio.run(memory.get(user_id=1, account_id="acct-1", decision_id="d-buy"))

    assert row is not None
    assert row["symbol"] == "600519" and row["settled"] is True
    assert row["reflection"]["model"] == "deepseek/deepseek-flash"
    assert row["reflection"]["prompt_version"].startswith("m7-reflection")
    assert row["direction"] == "bullish" and row["evidence_key"] == "news:1|2026-08-03"
    assert row["as_of"] == "2026-09-30"


def test_open_trip_is_stored_without_a_reflection() -> None:
    """未到期不给教训：`reflection=None`，但到期状态与窗口照记（复盘页要如实展示）。"""
    memory = _memory()
    asyncio.run(
        memory.save(
            user_id=1,
            account_id="acct-1",
            settled=_settled("d-open", exit_=None, pnl=None, alpha=None),
            reflection=None,
            direction=None,
            evidence_key=None,
            as_of=date(2026, 9, 30),
            settled_at="2026-10-10T12:00:00+00:00",
        )
    )

    row = asyncio.run(memory.get(user_id=1, account_id="acct-1", decision_id="d-open"))

    assert row is not None and row["settled"] is False and row["reflection"] is None


def test_users_are_isolated_by_namespace() -> None:
    """**用户隔离**：另一个 user_id 读不到、也聚合不到（namespace 前缀不同）。"""
    memory = _memory()
    asyncio.run(
        memory.save(
            user_id=1,
            account_id="acct-1",
            settled=_settled(),
            reflection=Reflection("教训", "m"),
            direction="bullish",
            evidence_key=None,
            as_of=date(2026, 9, 30),
            settled_at="2026-10-10T12:00:00+00:00",
        )
    )

    assert asyncio.run(memory.get(user_id=2, account_id="acct-1", decision_id="d-buy")) is None
    assert asyncio.run(memory.lessons(user_id=2)) == []
    assert asyncio.run(memory.list_for_account(user_id=2, account_id="acct-1")) == []


def test_lessons_aggregate_across_accounts_and_filter() -> None:
    """跨账户聚合：只收**有反思**的已平仓回合，可按标的 / 方向过滤。"""
    memory = _memory()
    rows = [
        ("acct-1", _settled("d1", symbol="600519"), "bullish", "茅台教训"),
        ("acct-1", _settled("d2", symbol="000001"), "bearish", "平安教训"),
        ("acct-2", _settled("d3", symbol="600519"), "bullish", "另一账户的茅台教训"),
        ("acct-2", _settled("d4", exit_=None, pnl=None, alpha=None), None, None),  # 未平仓
    ]
    for account_id, settled, direction, text in rows:
        asyncio.run(
            memory.save(
                user_id=1,
                account_id=account_id,
                settled=settled,
                reflection=Reflection(text, "m") if text else None,
                direction=direction,
                evidence_key=None,
                as_of=date(2026, 9, 30),
                settled_at="2026-10-10T12:00:00+00:00",
            )
        )

    everything = asyncio.run(memory.lessons(user_id=1))
    # 新的在前：三笔平仓日相同 ⇒ 按 id 定序（倒序）
    assert [row["decision_id"] for row in everything] == ["d3", "d2", "d1"]
    assert asyncio.run(memory.lessons(user_id=1, symbol="600519")) != []
    assert {row["symbol"] for row in asyncio.run(memory.lessons(user_id=1, symbol="600519"))} == {
        "600519"
    }
    assert [row["decision_id"] for row in asyncio.run(memory.lessons(user_id=1, direction="bearish"))] == ["d2"]
    assert asyncio.run(memory.symbols(user_id=1)) == ["000001", "600519"]


def test_is_current_decides_whether_to_re_settle() -> None:
    """幂等判据：已平仓永久作数；未平仓随 as_of / 窗口 / 盈亏变化而重结。"""
    closed = _settled()
    stored_closed = {**closed.to_payload(), "settled": True, "as_of": "2026-09-30"}
    assert is_current(stored_closed, closed, as_of=date(2026, 10, 8))  # 平仓事实不变

    open_trip = _settled("d-open", exit_=None, pnl=1995.0, alpha=18.94)
    stored_open = {**open_trip.to_payload(), "settled": False, "as_of": "2026-09-30"}
    assert is_current(stored_open, open_trip, as_of=date(2026, 9, 30))
    assert not is_current(stored_open, open_trip, as_of=date(2026, 10, 8))  # 账户推进了
    drifted = {**stored_open, "pnl": 1994.0}  # 数据被重下、浮盈变了
    assert not is_current(drifted, open_trip, as_of=date(2026, 9, 30))
    assert not is_current(None, open_trip, as_of=date(2026, 9, 30))
    assert not is_current(stored_open, closed, as_of=date(2026, 9, 30))  # 形态对不上


def test_namespace_is_scoped_per_user_and_account() -> None:
    assert memory_namespace(1, "acct") == ("decisions", "1", "acct")
    assert memory_namespace(2, "acct") != memory_namespace(1, "acct")


@pytest.mark.parametrize("user_id", [1, 42])
def test_list_for_account_sorted_newest_first(user_id: int) -> None:
    memory = _memory()
    for index, day in enumerate([date(2026, 8, 3), date(2026, 8, 20)]):
        asyncio.run(
            memory.save(
                user_id=user_id,
                account_id="acct",
                settled=_settled(f"d{index}", entry=day, exit_=day),
                reflection=None,
                direction=None,
                evidence_key=None,
                as_of=date(2026, 9, 30),
                settled_at="2026-10-10T12:00:00+00:00",
            )
        )

    rows = asyncio.run(memory.list_for_account(user_id=user_id, account_id="acct"))
    assert [row["entry_date"] for row in rows] == ["2026-08-20", "2026-08-03"]
