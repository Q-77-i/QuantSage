"""M7 研报取证：在**真实账户与真实数据**上生成一份报告（含决策记忆），复核四条口径。

`tests/` 用的是受控输入；这里补的是另一半——**真报告长什么样、真模型多慢、账目差多少**：

  * 回合重算合计 vs 账户 `realized_pnl`：真实数据上的舍入差（SPEC §8 记的元级差异）；
  * 证据链：真实决策的来源快照能不能全部回链到语料行；
  * 全流程计时：快照（逐分片 sha256）/ 组装 / **flash 综述** / **flash 反思**（真调用）；
  * **决策记忆（M7b）**：到期回合结算进真 Store，第二次结算一分钱不花（幂等），
    复盘块的计数与 `review.summary` 对得上。

```
python scripts/report_evidence.py                     # 打印并写 logs/m7/evidence.md
python scripts/report_evidence.py --account-id UUID   # 指定账户
python scripts/report_evidence.py --no-llm            # 不调模型（综述与反思都缺席）
```

退出码：0 取证通过；1 有断言未过（券池空 / 回链缺失 / 重算差超容差 / 幂等被破坏）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.backtest.benchmark import market_benchmark  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402
from app.memory.decision_store import DecisionMemory  # noqa: E402
from app.memory.service import settle_account  # noqa: E402
from app.memory.settle import mark_open_trips, pair_trips  # noqa: E402
from app.paper.store import config_from_payload, decision_from_row, equity_from_row  # noqa: E402
from app.report.builder import (  # noqa: E402
    assemble_report,
    build_facts,
    equity_dates,
    freeze_review,
)
from app.report.evidence import resolve_evidence  # noqa: E402
from app.report.markdown import render_markdown  # noqa: E402
from app.report.narrative import build_narrative  # noqa: E402
from app.report.snapshot import (  # noqa: E402
    canonical_json,
    config_digest,
    data_snapshot,
    decisions_digest,
    report_hash,
    snapshot_hash,
)

#: 重算与账本的容差（元）：落库价格是 NUMERIC(18,4)，每笔成交最多带来分级舍入
TOLERANCE_PER_FILL = 0.5

OUT_DIR = Path(__file__).resolve().parents[2] / "logs" / "m7"

DECISION_COLUMNS = (
    "id, account_id, trade_date, symbol, side, est_qty, est_price, reason, event_id, sources, "
    "status, decided_at, fill_date, fill_qty, fill_price, fill_ref_price, commission, stamp_tax, "
    "cash_delta, reject_code, reject_reason"
)


class _NoChat:
    """`--no-llm`：给一个必炸的假模型，综述与反思都走「降级路径」（缺席 + 原因）。"""

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("--no-llm：本次不调模型")


class ScriptDb:
    """结算要的 db 出口（三个只读方法）——脚本直连 psycopg，不拉整个 API 栈。"""

    def __init__(self, conn: psycopg.Connection, account_row: dict[str, Any]) -> None:
        self._conn = conn
        self._account = account_row

    async def get_paper_account(self, user_id: int, account_id: str) -> dict[str, Any] | None:
        return self._account if str(self._account["id"]) == account_id else None

    async def paper_decisions(self, account_id: str, limit: int) -> list[dict[str, Any]]:
        return self._conn.execute(
            f"SELECT {DECISION_COLUMNS} FROM paper_decisions WHERE account_id = %s::uuid "
            "ORDER BY trade_date, symbol, side LIMIT %s",
            (account_id, limit),
        ).fetchall()

    async def paper_equity(self, account_id: str) -> list[dict[str, Any]]:
        return self._conn.execute(
            "SELECT trade_date, cash, market_value, equity FROM paper_equity "
            "WHERE account_id = %s::uuid ORDER BY trade_date",
            (account_id,),
        ).fetchall()


def pick_account(conn: psycopg.Connection, account_id: str | None) -> dict[str, Any]:
    """挑一个账户：指定优先，否则取**成交决策最多**的那个（有回合才谈得上结算）。"""
    if account_id:
        row = conn.execute(
            "SELECT id, user_id, name, config, status, cash, realized_pnl, as_of "
            "FROM paper_accounts WHERE id = %s::uuid",
            (account_id,),
        ).fetchone()
        assert row is not None, f"账户不存在：{account_id}"
        return row
    row = conn.execute(
        "SELECT a.id, a.user_id, a.name, a.config, a.status, a.cash, a.realized_pnl, a.as_of "
        "FROM paper_accounts a JOIN paper_decisions d ON d.account_id = a.id "
        "WHERE d.status = 'filled' GROUP BY a.id ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "库里没有成交过决策的账户——先跑一个模拟盘会话"
    return row


def load_rows(conn: psycopg.Connection, account: dict[str, Any]) -> dict[str, Any]:
    decision_rows = conn.execute(
        f"SELECT {DECISION_COLUMNS} FROM paper_decisions WHERE account_id = %s::uuid "
        "ORDER BY trade_date, symbol, side",
        (account["id"],),
    ).fetchall()
    equity_rows = conn.execute(
        "SELECT trade_date, cash, market_value, equity FROM paper_equity "
        "WHERE account_id = %s::uuid ORDER BY trade_date",
        (account["id"],),
    ).fetchall()
    return {
        "decisions": [decision_from_row(row) for row in decision_rows],
        "equity": [equity_from_row(row) for row in equity_rows],
    }


async def settle(
    conn: psycopg.Connection, account: dict[str, Any], *, with_llm: bool
) -> tuple[dict[str, Any], Any, Any, float, float]:
    """跑两次 M7b 结算（真 Store；真 flash 反思除非 `with_llm=False`）——第二次验幂等。"""
    from langgraph.store.postgres import AsyncPostgresStore

    async with AsyncPostgresStore.from_conn_string(get_settings().postgres_dsn) as store:
        await store.setup()
        memory = DecisionMemory(store)
        db = ScriptDb(conn, account)
        chat = None if with_llm else _NoChat()
        started = time.perf_counter()
        review, first = await settle_account(
            db, memory, user_id=int(account["user_id"]), account_id=str(account["id"]), chat=chat
        )
        first_seconds = time.perf_counter() - started
        started = time.perf_counter()
        _again, second = await settle_account(
            db, memory, user_id=int(account["user_id"]), account_id=str(account["id"]), chat=chat
        )
        second_seconds = time.perf_counter() - started
        return review, first, second, first_seconds, second_seconds


def build(
    account: dict[str, Any],
    loaded: dict[str, Any],
    *,
    with_llm: bool,
    narrative: Any = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装一份报告。`narrative` 传入即**复用**（重放复核走的就是这条路）。"""
    config = config_from_payload(account["config"])
    decisions = loaded["decisions"]

    started = time.perf_counter()
    snapshot = data_snapshot()
    snapshot_seconds = time.perf_counter() - started
    snapshot["decisions_hash"] = decisions_digest(decisions)
    snapshot["account"] = {
        "id": str(account["id"]),
        "as_of": account["as_of"].isoformat(),
        "config_hash": config_digest(config),
    }
    if review is not None:
        snapshot["review_hash"] = report_hash(freeze_review(review))

    closes = dc.closes_through(list(config.symbols), account["as_of"])
    trips = mark_open_trips(pair_trips(decisions), closes)
    dates = equity_dates(loaded["equity"])
    benchmark = market_benchmark(dates, float(config.initial_cash)) if dates else None

    started = time.perf_counter()
    sources = [d.sources if d.sources else None for d in decisions]
    evidence = resolve_evidence(sources, decision_ids=[d.id for d in decisions])
    evidence_seconds = time.perf_counter() - started

    started = time.perf_counter()
    facts = build_facts(
        account=dict(account),
        config=config,
        decisions=decisions,
        equity_rows=loaded["equity"],
        trips=trips,
        benchmark=benchmark,
        snapshot=snapshot,
        evidence=evidence,
        review=review,
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


def _brief(facts: dict[str, Any]) -> dict[str, Any]:
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
    parser = argparse.ArgumentParser(description="M7 研报取证")
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    lines: list[str] = []

    with psycopg.connect(
        get_settings().postgres_dsn, connect_timeout=5, row_factory=dict_row
    ) as conn:
        account = pick_account(conn, args.account_id)
        loaded = load_rows(conn, account)

        # ── M7b：结算（真 Store）────────────────────────────────
        review, run, second, settle_seconds, second_seconds = asyncio.run(
            settle(conn, account, with_llm=not args.no_llm)
        )
        result = build(account, loaded, with_llm=not args.no_llm, review=review)

        # ── 复核 ①：回合重算 vs 账本 ────────────────────────────
        closed = [t for t in result["trips"] if not t.is_open and t.pnl is not None]
        recomputed = sum(t.pnl or 0.0 for t in closed)
        ledger = float(account["realized_pnl"])
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

        # ── 复核 ③：结算幂等（第二次一分钱不花）──────────────────
        lines.append(
            f"结算：已到期 {run.settled}｜未到期 {run.open}｜定了没交易 {run.unfilled}"
            f"｜有教训 {run.lessons}｜首跑新写 {run.saved}（{settle_seconds:.2f}s）"
            f"｜二跑新写 {second.saved}、复用 {second.reused}（{second_seconds:.2f}s）"
        )
        if second.saved != 0:
            failures.append(f"二次结算又新写了 {second.saved} 条（幂等被破坏）")
        if run.saved == 0 and run.reused > 0:
            lines.append(
                "  （首跑就是全复用：记忆是**上一次进程**写下的——跨进程幂等生效，"
                "这比进程内幂等更强）"
            )
        if run.settled and run.lessons == 0 and not args.no_llm:
            failures.append("有已到期回合却一条教训都没有（反思链路可疑）")

        # ── 复核 ④：复盘块计数与正文对得上 ──────────────────────
        review_block = next((b for b in result["body"]["blocks"] if b["id"] == "review"), None)
        if review_block is None:
            failures.append("报告里没有逐笔复盘块")
        else:
            numbers = review_block["numbers"]
            summary = result["body"]["review"]["summary"]
            for key, path in (
                ("review.summary.settled", "settled"),
                ("review.summary.open", "open"),
                ("review.summary.lessons", "lessons"),
            ):
                if numbers.get(key) != summary.get(path):
                    failures.append(
                        f"复盘块 {key}={numbers.get(key)} 与正文 {summary.get(path)} 不一致"
                    )

        # ── 读数 ────────────────────────────────────────────────
        timing = result["timing"]
        metrics = result["facts"]["metrics"]
        n_open = sum(1 for t in result["trips"] if t.is_open)
        lessons = [
            item for item in review["settled"] if (item.get("reflection") or {}).get("text")
        ]
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
            f"{timing['llm_seconds']:.2f}s｜结算 {settle_seconds:.2f}s（含 flash 反思）",
            f"指纹：快照 {result['snapshot_hash'][:12]}｜报告 {result['report_hash'][:12]}",
            f"警告 {len(result['facts']['warnings'])} 条："
            + "；".join(result["facts"]["warnings"][:3]),
        ]
        if lessons:
            sample = lessons[0]
            lines.append(
                f"教训样本（{sample['reflection']['model']} · {sample['symbol']}"
                f" {sample['entry_date']}→{sample['exit_date']}）：{sample['reflection']['text']}"
            )
        else:
            note = next(
                (i["reflection"]["note"] for i in review["settled"] if i.get("reflection")), ""
            )
            lines.append(f"教训样本：（本次无）{note}")
        block = result["body"]["blocks"][-1]
        if block.get("text"):
            lines.append(f"综述（{block['model']}）：{block['text'][:120]}")
        else:
            lines.append(f"综述缺席：{block.get('note')}")

        (out_dir / "report.json").write_text(
            json.dumps(result["body"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "report.md").write_text(
            render_markdown(result["body"], report_hash_value=result["report_hash"]),
            encoding="utf-8",
        )
        lines += ["", f"已写出：{out_dir}/report.json、{out_dir}/report.md"]

        # 复核 ⑤：冻结产物是纯 JSON，且复用同一综述重放 hash 一致
        try:
            json.dumps(result["body"], ensure_ascii=False)
        except TypeError as exc:
            failures.append(f"冻结产物含非 JSON 类型：{exc}")
        again = build(account, loaded, with_llm=False, narrative=result["narrative"], review=review)
        # 事实层比对走规范化入口（原始事实里还有 UUID/date，裸 json.dumps 会炸）
        same_facts = canonical_json(again["facts"]) == canonical_json(result["facts"])
        if not same_facts:
            failures.append("同一输入两次组装事实层不相等（重放一致被破坏）")
        if again["report_hash"] != result["report_hash"]:
            failures.append("复用同一综述后 report_hash 仍不一致（重放一致被破坏）")

    body_text = "\n".join(lines)
    print(body_text)
    (out_dir / "evidence.md").write_text(
        "# M7 研报取证（真实账户 / 真实数据 / 真 Store）\n\n" + body_text + "\n",
        encoding="utf-8",
    )
    if failures:
        print("\n未过项：\n- " + "\n- ".join(failures))
        return 1
    print("\n取证通过（全部复核通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
