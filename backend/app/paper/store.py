"""形状转换：`Decision` / `PaperConfig` ↔ **DB 行**与 **JSON 信封**。

三个形状各司其职，转换只在这里发生（别处再写一份就会漂移）：

* `Decision`（内存对象，`types.py`）——重放与账本用的；
* **DB 行**（平铺列、Decimal/date）——进 Postgres 的；
* **JSON 信封**（嵌套、全字符串/数字）——**API 响应与沙箱子进程信封共用同一种**，
  这样「前端看到的决策单」与「沙箱子进程读到的决策单」结构上不可能不一致。

金额一律走 `float(...)`：psycopg 给 NUMERIC 是 `Decimal`，`Decimal - float` 会直接 TypeError
（同 `core/db.py::_watchlist_row` 的教训）——让 Decimal 止步在这一层。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from app.backtest.costs import CostModel
from app.backtest.types import Fill, Position, Side
from app.paper.types import (
    STATUS_LABELS,
    Decision,
    DecisionStatus,
    EquityMark,
    PaperConfig,
    ReplayResult,
)


# ── 配置 ────────────────────────────────────────────────────


def config_to_payload(config: PaperConfig) -> dict[str, Any]:
    """配置 → JSON（`paper_accounts.config` 与沙箱信封共用）。`costs` 的键就是 `CostModel` 的字段名。"""
    return {
        "initial_cash": config.initial_cash,
        "symbols": list(config.symbols),
        "strategy": config.strategy,
        "strategy_name": config.strategy_name,
        "params": dict(config.params),
        "start": config.start.isoformat(),
        "end": config.end.isoformat(),
        "costs": {
            "commission_rate": config.costs.commission_rate,
            "commission_min": config.costs.commission_min,
            "stamp_tax_rate": config.costs.stamp_tax_rate,
            "slippage_bps": config.costs.slippage_bps,
            "fee_enabled": config.costs.fee_enabled,
            "slippage_enabled": config.costs.slippage_enabled,
        },
    }


def config_from_payload(data: Mapping[str, Any]) -> PaperConfig:
    """`config_to_payload` 的逆运算（子进程侧与 DB 读回时用）。"""
    return PaperConfig(
        initial_cash=float(data["initial_cash"]),
        symbols=tuple(str(s) for s in data["symbols"]),
        strategy=str(data["strategy"]),
        start=_date(data["start"]),
        end=_date(data["end"]),
        params=dict(data.get("params") or {}),
        costs=CostModel(**data["costs"]),
        strategy_name=data.get("strategy_name"),
    )


# ── 会话（账户行 → API 形状）─────────────────────────────────


def account_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """账户行 → JSON 契约。`Decimal` / `UUID` / `date` / `datetime` 都在这一层翻译掉。"""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "cash": float(row["cash"]),
        "realized_pnl": float(row.get("realized_pnl") or 0.0),
        "as_of": _as_date(row["as_of"]).isoformat(),
        "config": dict(row["config"]) if row.get("config") is not None else None,
        "rules": dict(row.get("rules") or {}),
        "created_at": _text(row.get("created_at")),
        "updated_at": _text(row.get("updated_at")),
    }


def account_summary_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """列表行 → 摘要（只带界面要用的几列，不拖整份 config）。"""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "cash": float(row["cash"]),
        "as_of": _as_date(row["as_of"]).isoformat(),
        "strategy": row.get("strategy"),
        "strategy_name": row.get("strategy_name"),
        "symbols": list(row.get("symbols") or []),
        "start": row.get("start"),
        "end": row.get("end"),
        "rules": {k: dict(v) for k, v in (row.get("rules") or {}).items()},
        "created_at": _text(row.get("created_at")),
    }


# ── 决策 ────────────────────────────────────────────────────


def fill_to_payload(fill: Fill) -> dict[str, Any]:
    return {
        "trade_date": fill.trade_date.isoformat(),
        "qty": fill.qty,
        "price": fill.price,
        "ref_price": fill.ref_price,
        "commission": fill.commission,
        "stamp_tax": fill.stamp_tax,
        "slippage_cost": fill.slippage_cost,
        "cash_delta": fill.cash_delta,
    }


def _fill_from_payload(data: Mapping[str, Any], side: Side) -> Fill:
    """成交块不带 side（它在决策上），读回时补上——`Fill` 需要它来算费用与滑点。"""
    return Fill(
        trade_date=_date(data["trade_date"]),
        side=side,
        qty=int(data["qty"]),
        price=float(data["price"]),
        ref_price=float(data["ref_price"]),
        commission=float(data["commission"]),
        stamp_tax=float(data["stamp_tax"]),
        cash_delta=float(data["cash_delta"]),
    )


def decision_to_payload(decision: Decision) -> dict[str, Any]:
    """决策 → JSON**信封**（API 响应 + 沙箱入参共用）。

    `status_label` 与状态同行带出（M5b 的 `overfit.reason_text` 同一条规矩：
    文案在服务端定，前端不自己拼一句）。
    """
    return {
        "id": decision.id,
        "account_id": decision.account_id,
        "trade_date": decision.trade_date.isoformat(),
        "symbol": decision.symbol,
        "side": decision.side.value,
        "est_qty": decision.est_qty,
        "est_price": decision.est_price,
        "reason": decision.reason,
        "event_id": decision.event_id,
        "sources": dict(decision.sources) if decision.sources else None,
        "status": decision.status.value,
        "status_label": STATUS_LABELS[decision.status],
        "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
        "fill": fill_to_payload(decision.fill) if decision.fill else None,
        "reject_code": decision.reject_code,
        "reject_reason": decision.reject_reason,
    }


def decision_from_payload(data: Mapping[str, Any]) -> Decision:
    """JSON 信封 → 决策（沙箱子进程侧与 DB 读回时用）。"""
    side = Side(data["side"])
    fill = data.get("fill")
    return Decision(
        id=str(data["id"]),
        account_id=str(data.get("account_id") or ""),
        symbol=str(data["symbol"]),
        trade_date=_date(data["trade_date"]),
        side=side,
        est_qty=int(data["est_qty"]),
        est_price=float(data["est_price"]),
        reason=str(data.get("reason") or ""),
        event_id=data.get("event_id"),
        sources=dict(data["sources"]) if data.get("sources") else None,
        status=DecisionStatus(data["status"]),
        decided_at=_datetime(data.get("decided_at")),
        fill=_fill_from_payload(fill, side) if fill else None,
        reject_code=data.get("reject_code"),
        reject_reason=data.get("reject_reason"),
    )


def _fill_from_payload(data: Mapping[str, Any], side: Side) -> Fill:
    """成交块不带 side（它在决策上），读回时补上——`Fill` 需要它来算费用与滑点。"""
    return Fill(
        trade_date=_date(data["trade_date"]),
        side=side,
        qty=int(data["qty"]),
        price=float(data["price"]),
        ref_price=float(data["ref_price"]),
        commission=float(data["commission"]),
        stamp_tax=float(data["stamp_tax"]),
        cash_delta=float(data["cash_delta"]),
    )


def decision_to_row(decision: Decision) -> dict[str, Any]:
    """决策 → `paper_decisions` 的一行（列名与 DDL 同源，见 `core/db.py`）。"""
    fill = decision.fill
    return {
        "id": decision.id,
        "account_id": decision.account_id,
        "trade_date": decision.trade_date,
        "symbol": decision.symbol,
        "side": decision.side.value,
        "est_qty": decision.est_qty,
        "est_price": decision.est_price,
        "reason": decision.reason,
        "event_id": decision.event_id,
        "sources": dict(decision.sources) if decision.sources else None,
        "status": decision.status.value,
        "decided_at": decision.decided_at,
        "fill_date": fill.trade_date if fill else None,
        "fill_qty": fill.qty if fill else None,
        "fill_price": fill.price if fill else None,
        "fill_ref_price": fill.ref_price if fill else None,
        "commission": fill.commission if fill else None,
        "stamp_tax": fill.stamp_tax if fill else None,
        "cash_delta": fill.cash_delta if fill else None,
        "reject_code": decision.reject_code,
        "reject_reason": decision.reject_reason,
    }


def decision_from_row(row: Mapping[str, Any]) -> Decision:
    """`paper_decisions` 一行 → 决策。`fill_*` 全空即「没有成交」。"""
    side = Side(row["side"])
    fill = None
    if row.get("fill_date") is not None:
        fill = Fill(
            trade_date=_as_date(row["fill_date"]),
            side=side,
            qty=int(row["fill_qty"]),
            price=float(row["fill_price"]),
            ref_price=float(row["fill_ref_price"]),
            commission=float(row["commission"]),
            stamp_tax=float(row["stamp_tax"]),
            cash_delta=float(row["cash_delta"]),
            reason=str(row.get("reason") or ""),
            event_id=row.get("event_id"),
        )
    return Decision(
        id=str(row["id"]),
        account_id=str(row["account_id"]),
        symbol=str(row["symbol"]),
        trade_date=_as_date(row["trade_date"]),
        side=side,
        est_qty=int(row["est_qty"]),
        est_price=float(row["est_price"]),
        reason=str(row.get("reason") or ""),
        event_id=row.get("event_id"),
        sources=dict(row["sources"]) if row.get("sources") else None,
        status=DecisionStatus(row["status"]),
        decided_at=row.get("decided_at"),
        fill=fill,
        reject_code=row.get("reject_code"),
        reject_reason=row.get("reject_reason"),
    )


# ── 持仓与净值 ──────────────────────────────────────────────


def position_to_row(account_id: str, symbol: str, position: Position) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "symbol": symbol,
        "shares": position.shares,
        "entry_price": position.entry_price,
        "entry_fees": position.entry_fees,
        "entry_date": position.entry_date,
        "entry_reason": position.entry_reason,
    }


def position_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """持仓行 → **API 形状**（前端直接吃；不还原成 `Position`——界面要的是「浮盈多少」这类数，
    得配当日收盘价算，那是端点的事）。"""
    return {
        "symbol": str(row["symbol"]),
        "shares": int(row["shares"]),
        "entry_price": float(row["entry_price"]),
        "entry_fees": float(row["entry_fees"]),
        "entry_date": _as_date(row["entry_date"]).isoformat() if row.get("entry_date") else None,
        "entry_reason": str(row.get("entry_reason") or ""),
    }


def equity_to_row(account_id: str, mark: EquityMark) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "trade_date": mark.trade_date,
        "cash": mark.cash,
        "market_value": mark.market_value,
        "equity": mark.equity,
    }


def equity_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": _as_date(row["trade_date"]).isoformat(),
        "cash": float(row["cash"]),
        "market_value": float(row["market_value"]),
        "equity": float(row["equity"]),
    }


# ── 结果（沙箱信封）────────────────────────────────────────


def position_to_payload(symbol: str, position: Position) -> dict[str, Any]:
    """持仓 → JSON（与 `position_from_row` 的输出同形，两个方向都能往返）。"""
    return {
        "symbol": symbol,
        "shares": position.shares,
        "entry_price": position.entry_price,
        "entry_fees": position.entry_fees,
        "entry_date": position.entry_date.isoformat() if position.entry_date else None,
        "entry_reason": position.entry_reason,
    }


def position_from_payload(row: Mapping[str, Any]) -> Position:
    entry_date = row.get("entry_date")
    return Position(
        shares=int(row["shares"]),
        entry_price=float(row["entry_price"]),
        entry_fees=float(row["entry_fees"]),
        entry_date=_date(entry_date) if entry_date else None,
        entry_reason=str(row.get("entry_reason") or ""),
    )


def result_to_payload(result: ReplayResult) -> dict[str, Any]:
    """重放结果 → JSON。**沙箱子进程把账户状态回传给父进程**就靠它（D5：整段重放放进沙箱）。"""
    return {
        "state": {
            "cash": result.state.cash,
            "realized_pnl": result.state.realized_pnl,
            "positions": [
                position_to_payload(symbol, position)
                for symbol, position in result.state.positions.items()
            ],
        },
        "decisions": [decision_to_payload(d) for d in result.decisions],
        "equity_curve": [
            {
                "trade_date": m.trade_date.isoformat(),
                "cash": m.cash,
                "market_value": m.market_value,
                "equity": m.equity,
            }
            for m in result.equity_curve
        ],
        "days": [
            {
                "trade_date": day.trade_date.isoformat(),
                "filled": [d.id for d in day.filled],
                "expired": [d.id for d in day.expired],
                "unfilled": [d.id for d in day.unfilled],
                "generated": [d.id for d in day.generated],
            }
            for day in result.days
        ],
        "rules": {symbol: dict(rule) for symbol, rule in result.rules.items()},
    }


def result_from_payload(data: Mapping[str, Any]) -> ReplayResult:
    """`result_to_payload` 的逆运算（父进程侧用）。"""
    from app.paper.types import AccountState, DayOutcome

    decisions = tuple(decision_from_payload(d) for d in data["decisions"])
    by_id = {d.id: d for d in decisions}
    positions = {
        str(row["symbol"]): position_from_payload(row) for row in data["state"]["positions"]
    }
    curve = tuple(
        EquityMark(
            trade_date=_as_date(m["trade_date"]),
            cash=float(m["cash"]),
            market_value=float(m["market_value"]),
            equity=float(m["equity"]),
        )
        for m in data["equity_curve"]
    )
    days = tuple(
        DayOutcome(
            trade_date=_as_date(day["trade_date"]),
            filled=tuple(by_id[i] for i in day["filled"]),
            expired=tuple(by_id[i] for i in day["expired"]),
            unfilled=tuple(by_id[i] for i in day["unfilled"]),
            generated=tuple(by_id[i] for i in day["generated"]),
        )
        for day in data["days"]
    )
    return ReplayResult(
        state=AccountState(
            cash=float(data["state"]["cash"]),
            positions=positions,
            realized_pnl=float(data["state"].get("realized_pnl") or 0.0),
        ),
        decisions=decisions,
        equity_curve=curve,
        days=days,
        rules={str(k): dict(v) for k, v in (data.get("rules") or {}).items()},
    )


# ── 小工具 ─────────────────────────────────────────────────


def _date(value: Any) -> date:
    return date.fromisoformat(str(value))


def _as_date(value: Any) -> date:
    """DB 给的是 `date`，信封给的是字符串——两处都收。"""
    return value if isinstance(value, date) and not isinstance(value, datetime) else _date(value)


def _datetime(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _text(value: Any) -> str | None:
    """时间戳一类原样转文本（`datetime.isoformat()`）；None 就是 None。"""
    return None if value is None else str(value.isoformat() if hasattr(value, "isoformat") else value)
