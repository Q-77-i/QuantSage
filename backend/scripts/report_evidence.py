"""M7a 研报取证：在**真实账户与真实数据**上生成一份报告，并复核两条口径。

`tests/test_reports_api.py` / `tests/integration/test_reports_m7.py` 用的是合成/真实但
受控的输入；这里补的是另一半——**真报告长什么样、真模型的延迟多少、回合重算与账本差多少**：

  * 回合重算合计 vs 账户 `realized_pnl`：真实数据上的舍入差（SPEC §8 记的 0.01 / 0.53 元）；
  * 全流程计时：快照（逐分片 sha256）/ 组装 / **flash 综述**（真调用一次，量延迟）；
  * 证据链：真实决策的来源快照能不能全部回链到语料行。

```
python scripts/report_evidence.py                     # 打印并写 logs/m7/evidence.md
python scripts/report_evidence.py --account-id UUID   # 指定账户
python scripts/report_evidence.py --no-llm            # 不调模型（综述缺席）
```

退出码：0 取证通过；1 有断言未过（券池空 / 回链缺失 / 重算与账本差超容差）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.backtest.benchmark import market_benchmark  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402
from app.memory.settle import mark_open_trips, pair_trips  # noqa: E402
from app.paper.store import (  # noqa: E402
    config_from_payload,
    decision_from_row,
    equity_from_row,
)
from app.report.builder import assemble_report, build_facts, equity_dates  # noqa: E402
from app.report.evidence import resolve_evidence  # noqa: E402
from app.report.markdown import render_markdown  # noqa: E402
from app.report.narrative import build_narrative  # noqa: E402
from app.report.snapshot import (  # noqa: E402
    config_digest,
    data_snapshot,
    decisions_digest,
    report_hash,
    snapshot_hash,
)

#: 重算与账本的容差（元）：落库价格是 NUMERIC(18,4)，每笔成交最多带来 1 分级的舍入
TOLERANCE_PER_FILL = 0.5

OUT_DIR = Path(__file__).resolve().parents[2] / "logs" / "m7"


def pick_account(conn: psycopg.Connection, account_id: str | None) -> dict:
    """挑一个账户：指定优先，否则取**成交决策最多**的那个（有回合才谈得上结算）。"""
    if account_id:
        row = conn.execute(
            "SELECT id, name, config, status, cash, realized_pnl, as_of FROM paper_accounts "
            "WHERE id = %s::uuid",
            (account_id,),
        ).fetchone()
        assert row is not None, f"账户不存在：{account_id}"
        return _account_dict(row)
    row = conn.execute(
        "SELECT a.id, a.name, a.config, a.status, a.cash, a.realized_pnl, a.as_of "
        "FROM paper_accounts a JOIN paper_decisions d ON d.account_id = a.id "
        "WHERE d.status = 'filled' GROUP BY a.id ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "库里没有成交过决策的账户——先跑一个模拟盘会话"
    return _account_dict(row)


def _account_dict(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "name": row[1],
        "config": row[2],
        "status": row[3],
        "cash": row[4],
        "realized_pnl": float(row[5]),
        "as_of": row[6],
    }


def load_account(conn: psycopg.Connection, account: dict) -> dict:
    decisions = [
        decision_from_row(
            {
                "id": r[0],
                "account_id": r[1],
                "trade_date": r[2],
                "symbol": r[3],
                "side": r[4],
                "est_qty": r[5],
                "est_price": r[6],
                "reason": r[7],
                "event_id": r[8],
                "sources": r[9],
                "status": r[10],
                "decided_at": r[11],
                "fill_date": r[12],
                "fill_qty": r[13],
                "fill_price": r[14],
                "fill_ref_price": r[15],
                "commission": r[16],
                "stamp_tax": r[17],
                "cash_delta": r[18],
                "reject_code": r[19],
                "reject_reason": r[20],
            }
        )
        for r in conn.execute(
            "SELECT id, account_id, trade_date, symbol, side, est_qty, est_price, reason, "
            "event_id, sources, status, decided_at, fill_date, fill_qty, fill_price, "
            "fill_ref_price, commission, stamp_tax, cash_delta, reject_code, reject_reason "
            "FROM paper_decisions WHERE account_id = %s::uuid "
            "ORDER BY trade_date, symbol, side",
            (account["id"],),
        ).fetchall()
    ]
    equity = [
        equity_from_row({"trade_date": r[0], "cash": r[1], "market_value": r[2], "equity": r[3]})
        for r in conn.execute(
            "SELECT trade_date, cash, market_value, equity FROM paper_equity "
            "WHERE account_id = %s::uuid ORDER BY trade_date",
            (account["id"],),
        ).fetchall()
    ]
    return {"decisions": decisions, "equity": equity}


def build(account: dict, loaded: dict, *, with_llm: bool, narrative=None) -> dict:
    """组装一份报告。`narrative` 传入即**复用**（重放复核走的就是这条路——
    综述是模型产物，契约里的「同快照重放一致」以复用已存文本为前提）。"""
    config = config_from_payload(account["config"])
    decisions = loaded["decisions"]

    started = time.perf_counter()
    snapshot = data_snapshot()
    snapshot_seconds = time.perf_counter() - started
    snapshot["decisions_hash"] = decisions_digest(decisions)
    snapshot["account"] = {
        "id": account["id"],
        "as_of": account["as_of"].isoformat(),
        "config_hash": config_digest(config),
    }

    closes = dc.closes_through(list(config.symbols), account["as_of"])
    trips = mark_open_trips(pair_trips(decisions), closes)
    dates = equity_dates(loaded["equity"])
    benchmark = market_benchmark(dates, float(config.initial_cash)) if dates else None

    started = time.perf_counter()
    sources = [d.sources if d.sources else None for d in decisions]
    evidence = resolve_evidence(sources, decision_ids=[d.id for d in decisions])
    evidence_seconds = time.perf_counter() - started

    started = time.perf_counter()
    account_row = {**account, "config": None}
    facts = build_facts(
        account=account_row,
        config=config,
        decisions=decisions,
        equity_rows=loaded["equity"],
        trips=trips,
        benchmark=benchmark,
        snapshot=snapshot,
        evidence=evidence,
    )
    facts_seconds = time.perf_counter() - started

    llm_seconds = 0.0
    if narrative is None:
        if with_llm:
            started = time.perf_counter()
            narrative = asyncio.run(build_narrative(_brief(facts)))
            llm_seconds = time.perf_counter() - started
        else:
            from app.report.narrative import Narrative

            narrative = Narrative(None, "-", note="--no-llm：本次不调模型")

    body = assemble_report(facts, narrative)
    return {
        "config": config,
        "decisions": decisions,
        "trips": trips,
        "evidence": evidence,
        "facts": facts,
        "body": body,
        "narrative": narrative,
        "timing": {
            "snapshot_seconds": snapshot_seconds,
            "evidence_seconds": evidence_seconds,
            "facts_seconds": facts_seconds,
            "llm_seconds": llm_seconds,
        },
        "snapshot_hash": snapshot_hash(snapshot),
        "report_hash": report_hash(body),
    }


def _brief(facts: dict) -> dict:
    account = facts["account"]
    attribution = facts["attribution"]
    return {
        "account_name": account.get("name"),
        "strategy_name": account.get("strategy_name") or account.get("strategy"),
        "start": account.get("start"),
        "as_of": account.get("as_of"),
        "market_end": account.get("data_end"),
        "metrics": facts["metrics"],
        "open_trips": sum(row["trips"] - row["closed"] for row in attribution["symbols"]),
        "direction_groups": attribution["direction"],
        "industry_groups": attribution["industry"],
        "notes": facts["warnings"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M7a 研报取证")
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    lines: list[str] = []

    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=5) as conn:
        account = pick_account(conn, args.account_id)
        loaded = load_account(conn, account)
        result = build(account, loaded, with_llm=not args.no_llm)

        # ── 复核 ①：回合重算 vs 账本 ────────────────────────────
        closed = [t for t in result["trips"] if not t.is_open and t.pnl is not None]
        recomputed = sum(t.pnl or 0.0 for t in closed)
        ledger = account["realized_pnl"]
        gap = abs(recomputed - ledger)
        fills = sum(1 for d in result["decisions"] if d.status.value == "filled")
        allowance = TOLERANCE_PER_FILL * max(fills, 1)
        lines.append(
            f"回合重算合计 {recomputed:,.2f} 元 vs 账本 realized_pnl {ledger:,.2f} 元 "
            f"⇒ 差 {gap:.4f} 元（容差 {allowance:.1f} 元 / {fills} 笔成交）"
        )
        if gap > allowance:
            failures.append(f"回合重算与账本差 {gap:.4f} 元，超出容差 {allowance:.1f}")

        # ── 复核 ②：证据链回链 ─────────────────────────────────
        items = list(result["evidence"].values())
        missing = [item for item in items if not item.found]
        lines.append(
            f"证据 {len(items)} 条：可回链 {len(items) - len(missing)}、"
            f"查无 {len(missing)}、被平台修订 {sum(1 for i in items if i.revised)}"
        )

        # ── 读数 ────────────────────────────────────────────────
        timing = result["timing"]
        metrics = result["facts"]["metrics"]
        n_open = sum(1 for t in result["trips"] if t.is_open)
        lines += [
            "",
            f"账户：{account['name']}（{result['config'].strategy} · "
            f"{len(result['config'].symbols)} 只 · 状态 {account['status']}）",
            f"区间：{result['config'].start} → {account['as_of']}（数据止于 "
            f"{result['facts']['account']['data_end']}）",
            f"决策 {len(result['decisions'])} 张（成交 {fills}）⇒ 回合 {len(result['trips'])}"
            f"（已平仓 {len(closed)} / 未平仓 {n_open}）",
            f"指标：累计 {metrics['total_return']:.2%}｜基准 "
            f"{'—' if metrics['benchmark_return'] is None else format(metrics['benchmark_return'], '.2%')}"
            f"｜超额 {'—' if metrics['excess_return'] is None else format(metrics['excess_return'], '.2%')}"
            f"｜回撤 {metrics['max_drawdown']:.2%}｜波动率 "
            f"{'—' if metrics['volatility'] is None else format(metrics['volatility'], '.2%')}"
            f"｜胜率 {'—' if metrics['win_rate'] is None else format(metrics['win_rate'], '.0%')}",
            f"耗时：快照 {timing['snapshot_seconds']:.2f}s｜证据 "
            f"{timing['evidence_seconds']:.2f}s｜组装 {timing['facts_seconds']:.2f}s｜flash 综述 "
            f"{timing['llm_seconds']:.2f}s",
            f"指纹：快照 {result['snapshot_hash'][:12]}｜报告 {result['report_hash'][:12]}",
            f"警告 {len(result['facts']['warnings'])} 条："
            + "；".join(result["facts"]["warnings"][:3]),
        ]
        block = result["body"]["blocks"][-1]
        if block.get("text"):
            lines.append(f"综述（{block['model']}）：{block['text'][:120]}")
        else:
            lines.append(f"综述缺席：{block.get('note')}")

        # 证据 ③：报告 JSON 与 Markdown 都能落盘（分享与导出用的就是它们）
        (out_dir / "report.json").write_text(
            json.dumps(result["body"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "report.md").write_text(
            render_markdown(result["body"], report_hash_value=result["report_hash"]),
            encoding="utf-8",
        )
        lines += ["", f"已写出：{out_dir}/report.json、{out_dir}/report.md"]

        # 复核 ③：冻结产物里没有非 JSON 类型（psycopg 的 Jsonb 会拒收 UUID/Decimal）
        try:
            json.dumps(result["body"], ensure_ascii=False)
        except TypeError as exc:
            failures.append(f"冻结产物含非 JSON 类型：{exc}")
        # 复核 ④：**复用同一段综述**重放 ⇒ 事实层逐字段相等、report_hash 相同
        # （契约的口径就是这条：综述是模型产物，重放一致以复用已存文本为前提）
        again = build(account, loaded, with_llm=False, narrative=result["narrative"])
        if json.dumps(again["facts"], sort_keys=True, ensure_ascii=False) != json.dumps(
            result["facts"], sort_keys=True, ensure_ascii=False
        ):
            failures.append("同一输入两次组装事实层不相等（重放一致被破坏）")
        if again["report_hash"] != result["report_hash"]:
            failures.append("复用同一综述后 report_hash 仍不一致（重放一致被破坏）")

    body_text = "\n".join(lines)
    print(body_text)
    (out_dir / "evidence.md").write_text(
        "# M7a 研报取证（真实账户 / 真实数据）\n\n" + body_text + "\n",
        encoding="utf-8",
    )
    if failures:
        print("\n未过项：\n- " + "\n- ".join(failures))
        return 1
    print("\n取证通过（3 项复核全过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
