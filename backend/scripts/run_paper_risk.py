"""E5 风控取证：在**真实行情**上跑两幕，产出 `logs/e5/risk-smoke.md` / `.json`。

两幕各自回答一件事：

1. **组合风控**（20 只标的，risk 95/30/3/5）：止损触发 → 风控卖出 T+1 成交、
   回撤超限 → breach 审计、回撤期间买入被拦截、放行也有审计；
2. **集中度缩量**（3 只标的，risk 95/30/100/100）：决策日缩量后的股数在成交时
   仍是硬上限（`fill.qty <= est_qty`），不会因为次日价格变化买回上限以上。

另两条控制项：风控关闭时零审计、零风险快照；同一输入重放两次事件与快照逐字段相等。

```
cd backend && uv run python scripts/run_paper_risk.py
```
退出码：0 全部断言通过；1 有断言未过。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import CostModel  # noqa: E402
from app.data import calendar as cal  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402
from app.paper.replay import replay  # noqa: E402
from app.paper.store import risk_state_to_payload  # noqa: E402
from app.paper.types import (  # noqa: E402
    DecisionStatus,
    PaperConfig,
    ReplayResult,
    RiskEvent,
    RiskLimits,
)

OUT = Path(__file__).resolve().parents[2] / "logs" / "e5"

#: 与 `run_paper.py` 同一批真实代码（规划期探针也是这批，读数可对照）
POOL = (
    "600519", "000001", "300750", "601318", "000858",
    "600036", "002594", "600900", "601899", "000333",
    "600030", "601166", "002415", "300059", "600276",
    "601012", "688981", "000651", "600887", "601088",
)
CONCENTRATION_POOL = ("600519", "000001", "300750")

PORTFOLIO_RISK = RiskLimits(95, 30, 3, 5)
CONCENTRATION_RISK = RiskLimits(95, 30, 100, 100)


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def window(days: int = 250) -> tuple[date, date]:
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    sessions = cal.sessions(latest - timedelta(days=days * 2 + 30), latest)
    return sessions[-days], latest


def run(pool: tuple[str, ...], risk: RiskLimits | None, start: date, end: date) -> ReplayResult:
    cfg = PaperConfig(
        initial_cash=1_000_000.0,
        symbols=pool,
        strategy="ma_cross",
        start=start,
        end=end,
        params={"fast": 5, "slow": 20},
        costs=CostModel(),
        risk=risk,
    )
    return replay(cfg, auto=DecisionStatus.APPROVED, account_id="e5-smoke")


def event_counts(events: tuple[RiskEvent, ...]) -> list[dict[str, object]]:
    counts = Counter((event.kind, event.verdict, event.gate) for event in events)
    return [
        {"kind": kind, "verdict": verdict, "gate": gate, "count": count}
        for (kind, verdict, gate), count in sorted(counts.items())
    ]


def samples(events: tuple[RiskEvent, ...], limit: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: Counter[str] = Counter()
    for event in events:
        if seen[event.kind] >= limit:
            continue
        seen[event.kind] += 1
        rows.append(
            {
                "trade_date": event.trade_date.isoformat(),
                "kind": event.kind,
                "verdict": event.verdict,
                "gate": event.gate,
                "symbol": event.symbol,
                "metrics": dict(event.metrics),
                "detail": event.detail,
            }
        )
    return rows


def evaluate(start: date, end: date) -> tuple[list[Check], dict[str, object]]:
    portfolio = run(POOL, PORTFOLIO_RISK, start, end)
    triggers = [e for e in portfolio.risk_events if e.kind == "stop_loss"]
    breaches = [
        e for e in portfolio.risk_events if e.kind == "drawdown_state" and e.verdict == "breach"
    ]
    blocks = [
        e for e in portfolio.risk_events if e.kind == "proposal" and e.verdict == "block"
    ]
    passes = [
        e for e in portfolio.risk_events if e.kind == "proposal" and e.verdict == "pass"
    ]
    exits = [d for d in portfolio.decisions if d.origin == "risk"]
    filled = [d for d in exits if d.status is DecisionStatus.FILLED]

    concentration = run(CONCENTRATION_POOL, CONCENTRATION_RISK, start, end)
    caps = [
        e
        for e in concentration.risk_events
        if e.kind == "proposal" and e.verdict == "cap"
    ]
    by_id = {d.id: d for d in concentration.decisions}
    capped_fills = [
        by_id[event.decision_id]
        for event in caps
        if event.decision_id is not None
        and event.decision_id in by_id
        and by_id[event.decision_id].fill is not None
    ]
    hard_cap = all(
        d.fill is not None and d.fill.qty <= d.est_qty for d in capped_fills
    )

    plain = run(CONCENTRATION_POOL, None, start, end)
    again = run(POOL, PORTFOLIO_RISK, start, end)

    checks = [
        Check(
            "止损触发 → 风控卖出 T+1 成交",
            bool(triggers) and len(filled) == len(exits) and bool(exits),
            f"{len(triggers)} 次触发，{len(exits)} 张卖出，{len(filled)} 张已成交",
        ),
        Check(
            "回撤超限触发 breach 审计",
            bool(breaches),
            f"{len(breaches)} 次 breach",
        ),
        Check(
            "回撤超限拦截新开仓",
            bool(blocks),
            f"{len(blocks)} 条 proposal block",
        ),
        Check(
            "放行也有审计",
            bool(passes),
            f"{len(passes)} 条 proposal pass",
        ),
        Check(
            "集中度缩量在成交时仍是硬上限",
            bool(caps) and bool(capped_fills) and hard_cap,
            f"{len(caps)} 条缩量，{len(capped_fills)} 张成交，全部 fill.qty ≤ est_qty",
        ),
        Check(
            "风控关闭零审计、零快照",
            plain.risk_events == () and plain.risk_state is None,
            "risk_events=0，risk_state=None",
        ),
        Check(
            "重放确定性",
            [e.id for e in again.risk_events] == [e.id for e in portfolio.risk_events]
            and again.risk_state == portfolio.risk_state,
            f"{len(portfolio.risk_events)} 条事件逐 id 相等",
        ),
    ]

    evidence: dict[str, object] = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "portfolio": {
            "symbols": list(POOL),
            "risk": asdict(PORTFOLIO_RISK),
            "decisions": len(portfolio.decisions),
            "events": event_counts(portfolio.risk_events),
            "risk_state": risk_state_to_payload(portfolio.risk_state),
            "samples": samples(portfolio.risk_events),
        },
        "concentration": {
            "symbols": list(CONCENTRATION_POOL),
            "risk": asdict(CONCENTRATION_RISK),
            "decisions": len(concentration.decisions),
            "events": event_counts(concentration.risk_events),
            "capped_sample": [
                {
                    "symbol": d.symbol,
                    "est_qty": d.est_qty,
                    "fill_qty": d.fill.qty if d.fill else None,
                    "trade_date": d.trade_date.isoformat(),
                }
                for d in capped_fills[:5]
            ],
        },
        "risk_off": {
            "events": len(plain.risk_events),
            "risk_state": risk_state_to_payload(plain.risk_state),
        },
    }
    return checks, evidence


def main() -> int:
    start, end = window()
    print(f"窗口：{start} → {end}（{len(POOL)} 标的组合 + {len(CONCENTRATION_POOL)} 标的缩量）")
    checks, evidence = evaluate(start, end)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "risk-smoke.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# E5 风控真机取证",
        "",
        f"窗口：{start} → {end}",
        "",
        "| 断言 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for check in checks:
        lines.append(f"| {check.label} | {'✅' if check.ok else '❌'} | {check.detail} |")
    lines.extend(
        [
            "",
            "## 组合风控事件计数",
            "",
            "| kind | verdict | gate | count |",
            "|---|---|---|---|",
        ]
    )
    for row in evidence["portfolio"]["events"]:  # type: ignore[index]
        lines.append(
            f"| {row['kind']} | {row['verdict']} | {row['gate'] or '—'} | {row['count']} |"
        )
    lines.extend(
        [
            "",
            "## 集中度缩量样本",
            "",
            "| 标的 | est_qty | fill_qty | 决策日 |",
            "|---|---|---|---|",
        ]
    )
    for row in evidence["concentration"]["capped_sample"]:  # type: ignore[index]
        lines.append(
            f"| {row['symbol']} | {row['est_qty']} | {row['fill_qty']} | {row['trade_date']} |"
        )
    lines.extend(
        [
            "",
            "## 事件样本",
            "",
            "```json",
            json.dumps(evidence["portfolio"]["samples"], ensure_ascii=False, indent=2),
            "```",
        ]
    )
    (OUT / "risk-smoke.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for check in checks:
        print(f"{'✅' if check.ok else '❌'} {check.label} —— {check.detail}")
    print(f"证据：{OUT / 'risk-smoke.md'}")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
