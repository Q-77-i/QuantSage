"""M7b 结算编排（`app.memory.service`）：惰性结算 + 幂等复用 + 复盘载荷。

真值取自**真实账本语义**（`FakeDatabase` 直接摆行，同 M6 口径），LLM 与行情都注入假件：
该验的是「什么时候花钱调模型、什么时候复用记忆」，不是模型或行情本身。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from app.backtest.types import Fill, Side
from app.data import calendar as cal
from app.data import duckdb_client
from app.memory import service
from app.memory.decision_store import DecisionMemory
from app.memory.reflection import Reflection
from app.paper.store import config_to_payload, decision_to_row, equity_to_row
from app.paper.types import Decision, DecisionStatus, EquityMark, PaperConfig
from tests.conftest import make_backtest_dir, write_bars_parquet
from tests.fakes import FakeDatabase

START = date(2026, 8, 3)
SYMBOL = "600519"


def _days(count: int = 12) -> list[date]:
    days = cal.sessions(START, START + timedelta(days=count * 3 + 10))
    return days[:count]


def _fill(day: date, side: Side, qty: int, price: float, *, did: str) -> Decision:
    commission = 5.0
    stamp = price * qty * 0.0005 if side is Side.SELL else 0.0
    return Decision(
        id=did,
        account_id="acct",
        symbol=SYMBOL,
        trade_date=day,
        side=side,
        est_qty=qty,
        est_price=price,
        reason="MA 金叉" if side is Side.BUY else "持有到期",
        status=DecisionStatus.FILLED,
        fill=Fill(
            trade_date=day,
            side=side,
            qty=qty,
            price=price,
            ref_price=price,
            commission=commission,
            stamp_tax=stamp,
            cash_delta=(qty * price - commission - stamp)
            if side is Side.SELL
            else -(qty * price + commission),
        ),
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """一个账户：两笔已平仓回合 + 一笔未平仓 + 一张过期未成交的决策。"""
    days = _days()
    prices = [100.0 + index for index in range(len(days))]
    bars = [
        {"trade_date": day, "open": price, "close": price, "change_pct": 1.0}
        for day, price in zip(days, prices, strict=True)
    ]
    root = make_backtest_dir(tmp_path, bars, events=[], symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)

    config = PaperConfig(
        initial_cash=200_000.0, symbols=(SYMBOL,), strategy="ma_cross",
        start=days[0], end=days[-1],
    )
    decisions = [
        _fill(days[1], Side.BUY, 100, prices[1], did="d-1"),
        _fill(days[4], Side.SELL, 100, prices[4], did="d-2"),
        _fill(days[6], Side.BUY, 100, prices[6], did="d-3"),
        _fill(days[8], Side.SELL, 100, prices[8], did="d-4"),
        _fill(days[10], Side.BUY, 100, prices[10], did="d-5"),  # 未平仓（期末仍在持仓）
        Decision(
            id="d-6",
            account_id="acct",
            symbol=SYMBOL,
            trade_date=days[9],
            side=Side.BUY,
            est_qty=100,
            est_price=prices[9],
            reason="MA 金叉",
            status=DecisionStatus.EXPIRED,
        ).replace(reject_reason=None),
    ]
    equity = [
        equity_to_row(
            "acct",
            EquityMark(
                trade_date=day, cash=100_000.0, market_value=100_000.0, equity=200_000.0
            ),
        )
        for day in days[:11]
    ]
    db = FakeDatabase()
    db.users[1] = {"id": 1, "email": "t@e.com"}
    asyncio.run(
        db.create_paper_account(
            account_id="acct",
            user_id=1,
            name="结算用例",
            config=config_to_payload(config),  # 用真序列化器，不手抄形状
            cash=100_000.0,
            as_of=days[10],
            rules={},
            positions=[],
            decisions=[decision_to_row(d) for d in decisions],
            equity=equity,
        )
    )
    db.paper_accounts["acct"]["realized_pnl"] = 0.0
    calls: list[dict[str, Any]] = []

    async def fake_reflection(facts: dict[str, Any], **kwargs: Any) -> Reflection:
        calls.append(facts)
        return Reflection(f"教训：{facts['symbol']} 这笔记一笔。", "fake-model")

    monkeypatch.setattr(service, "build_reflection", fake_reflection)
    return {
        "db": db,
        "memory": DecisionMemory(InMemoryStore()),
        "days": days,
        "prices": prices,
        "calls": calls,
    }


def _settle(env: dict[str, Any]):
    return asyncio.run(
        service.settle_account(
            env["db"], env["memory"], user_id=1, account_id="acct", chat=None
        )
    )


def test_first_settlement_reflects_closed_trips_only(env: dict[str, Any]) -> None:
    """已平仓的回合各给一句教训；未平仓的不给（还没有结果可总结）；未成交的只列状态。"""
    review, run = _settle(env)

    assert run.settled == 2 and run.open == 1 and run.unfilled == 1
    assert run.saved == 3 and run.reused == 0  # 两条已平仓 + 一条未平仓都写进记忆
    assert run.lessons == 2  # 只有已平仓的有反思文本
    assert len(env["calls"]) == 2  # 两次 LLM 调用，都是已平仓的
    assert all(call["exit_date"] is not None for call in env["calls"])

    # 复盘列表新的在前（平仓日倒序）
    assert [item["decision_id"] for item in review["settled"]] == ["d-3", "d-1"]
    assert [item["decision_id"] for item in review["open"]] == ["d-5"]
    assert review["open"][0]["reflection"] is None
    assert review["unfilled"][0]["status"] == "expired"
    assert review["unfilled"][0]["status_label"] == "未审批过期（未审批不成交）"


def test_settlement_is_idempotent_and_does_not_pay_twice(env: dict[str, Any]) -> None:
    """二次结算：已平仓的**永久作数**（不再调 LLM），未平仓的也只是重写记录。"""
    _settle(env)
    calls_after_first = len(env["calls"])
    review, run = _settle(env)

    assert len(env["calls"]) == calls_after_first  # 一分钱没再花
    assert run.saved == 0 and run.reused == 3
    assert run.lessons == 2
    assert review["settled"][0]["reflection"]["text"].startswith("教训：")


def test_alpha_uses_the_same_window_benchmark(env: dict[str, Any]) -> None:
    """alpha 与基准**同一把尺子**：窗口 = 回合的进出日，基准取该窗口的全市场等权。"""
    review, _ = _settle(env)
    item = next(entry for entry in review["settled"] if entry["decision_id"] == "d-1")

    # 窗口只含账户走过的交易日（equity 到 days[10]）：建仓 days[1] → 平仓 days[4] 共 4 天
    window = [
        day for day in env["days"]
        if item["entry_date"] <= day.isoformat() <= item["exit_date"]
    ]
    assert item["window_days"] == len(window) == 4
    assert item["benchmark_pct"] is not None
    assert item["alpha_pp"] == pytest.approx(
        (item["return_pct"] - item["benchmark_pct"]) * 100, rel=1e-9
    )


def test_open_trip_alpha_stops_at_the_account_as_of(env: dict[str, Any]) -> None:
    """未平仓的窗口算到 as_of：行情数据到 days[11]，但账户只走到 days[10]。"""
    review, _ = _settle(env)
    item = next(entry for entry in review["open"] if entry["decision_id"] == "d-5")

    assert item["exit_date"] is None
    assert item["window_days"] == 1  # 建仓日当天买入、估值也停在当天之后的一天内
    assert item["pnl"] is not None and item["alpha_pp"] is not None


def test_reflection_failure_still_settles(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """反思挂了照样结算：记忆里有记录、反思位是「不可用 + 原因」，不挡复盘。"""

    async def broken(facts: dict[str, Any], **kwargs: Any) -> Reflection:
        return Reflection(None, "fake-model", note="反思超时（>20s），本次缺席")

    monkeypatch.setattr(service, "build_reflection", broken)
    review, run = _settle(env)

    assert run.saved == 3 and run.lessons == 0
    first = review["settled"][0]
    assert first["reflection"]["text"] is None
    assert "超时" in first["reflection"]["note"]


def test_evidence_and_direction_are_carried_into_the_memory(env: dict[str, Any]) -> None:
    """买入决策带来源时，方向与证据键一起进记忆（跨标的聚合要按它过滤）。"""
    row = asyncio.run(
        env["db"].paper_decision(1, "d-1")
    )
    row["sources"] = {
        "event_id": "news:1",
        "event_time": "2026-08-04 09:00:00+08:00",
        "title": "合同公告",
        "content_hash": "hash-a",
    }
    review, _ = _settle(env)
    item = next(entry for entry in review["settled"] if entry["decision_id"] == "d-1")

    assert item["evidence_key"] == "news:1|2026-08-04"
    assert item["sources"]["title"] == "合同公告"
