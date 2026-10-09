"""M5a 一字板拒单取证：在**真实数据**上挑一只一字涨停的标的跑回测，看拒单原因码。

`tests/test_backtest_rules_wiring.py` 用手搓 bar 钉规则，这里补的是另一半——
**规则在真实行情上真的会触发**，以及触发时报告里看得到原因。

```
python scripts/limit_reject_evidence.py            # 打印并写 logs/m5a/limit.md
python scripts/limit_reject_evidence.py --json X   # 同时写一份 JSON
```

退出码：0 取证通过；1 有断言未过（或样本池空）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.a_share_rules import is_one_word_limit_up, limit_band, limit_pct  # noqa: E402
from app.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from app.backtest.report import build_report  # noqa: E402
from app.backtest.types import BarContext, Side, Signal  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402

#: 主板前缀——涨跌停幅度唯一确定的那些（ST 与否要看名称字典，这里挑非 ST 样本）
MAIN_BOARD = ("600", "601", "603", "605", "000", "001", "002", "003")


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    day: date
    open: float
    prev_close: float
    limit_up: float
    change_pct: float


def find_candidate(con) -> Candidate | None:  # noqa: ANN001
    """挑一只**真实一字涨停**：开盘=最高=最低=收盘，且涨跌幅贴近 10%。"""
    rows = con.execute(
        """
        WITH raw AS (
            SELECT symbol, trade_date, open, high, low, close, change_pct,
                   lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
            FROM bars WHERE adjustment = 'raw' AND close IS NOT NULL
        )
        SELECT symbol, trade_date, open, prev_close, change_pct
        FROM raw
        WHERE trade_date >= DATE '2026-01-01'
          AND open = high AND high = low AND low = close
          AND change_pct BETWEEN 9.5 AND 10.5
        ORDER BY trade_date DESC, symbol
        """
    ).to_arrow_table().to_pylist()

    for row in rows:
        symbol = str(row["symbol"])
        if not symbol.startswith(MAIN_BOARD):
            continue
        pct = limit_pct(symbol)
        if pct is None or row["prev_close"] in (None, 0):
            continue
        band = limit_band(float(row["prev_close"]), pct)
        bar = _bar(symbol, row["trade_date"], float(row["open"]))
        if is_one_word_limit_up(bar, band):
            return Candidate(
                symbol=symbol,
                day=row["trade_date"],
                open=float(row["open"]),
                prev_close=float(row["prev_close"]),
                limit_up=band.up,
                change_pct=float(row["change_pct"]),
            )
    return None


def _bar(symbol: str, day: date, price: float):  # noqa: ANN202
    from app.backtest.types import Bar

    return Bar(symbol=symbol, trade_date=day, open=price, high=price, low=price,
               close=price, volume=0.0)


class BuyOnPreviousBar:
    """在第 `index` 根收盘发买单——成交落在第 `index + 1` 根开盘，即目标日。"""

    name = "evidence_buy"

    def __init__(self, index: int) -> None:
        self._index = index

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if ctx.index == self._index and ctx.position.is_flat:
            return [Signal(Side.BUY, reason="evidence:buy-on-one-word-board")]
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5a 一字板拒单取证")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--json", default=None, help="把结论同时写成 JSON")
    parser.add_argument("--out", default="logs/m5a/limit.md")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else None
    con = dc.connect(data_dir)
    try:
        candidate = find_candidate(con)
    finally:
        con.close()
    if candidate is None:
        print("✗ 样本池为空：没找到符合条件的一字涨停", file=sys.stderr)
        return 1

    print(f"样本：{candidate.symbol} {candidate.day}")
    print(
        f"  前收 {candidate.prev_close} → 涨停价 {candidate.limit_up}"
        f"；当日开=高=低=收 {candidate.open}（源给的涨跌幅 {candidate.change_pct:.2f}%）"
    )

    bars = dc.bars(candidate.symbol, data_dir=data_dir)
    index = next(i for i, row in enumerate(bars) if row["trade_date"] == candidate.day)
    config = BacktestConfig(
        symbol=candidate.symbol,
        strategy="ma_cross",  # 占位：真正跑的是注入的策略
        start=bars[max(index - 3, 0)]["trade_date"],
        end=bars[min(index + 2, len(bars) - 1)]["trade_date"],
        data_dir=data_dir,
    )

    result = run_backtest(config, strategy=BuyOnPreviousBar(index - 1 - max(index - 3, 0)))
    codes = [dropped.code for dropped in result.dropped_signals]
    print(f"  成交 {len(result.fills)} 笔｜未成交 {len(result.dropped_signals)} 笔｜原因码 {codes}")

    # 对照：同一天若**开板**（开盘低于涨停价）就该成交——证明拦的是「封死」不是「这天」
    opened = _bar(candidate.symbol, candidate.day, round(candidate.limit_up * 0.98, 2))
    opened_open = opened.open < candidate.limit_up
    print(f"  对照：若当日开盘 {opened.open}（低于涨停价）→ 不构成一字板：{opened_open}")

    report = build_report(config, strategy_factory=lambda: BuyOnPreviousBar(index - 1 - max(index - 3, 0)))
    rejects = report["rejects"]
    print(f"  报告 rejects：{rejects['by_code']}｜meta.a_share_rules={report['meta']['a_share_rules']}")

    ok = codes == ["REJECT_LIMIT_UP"] and rejects["by_code"] == {"REJECT_LIMIT_UP": 1}
    print("✅ 取证通过" if ok else "✗ 取证未通过")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# M5a 一字板拒单取证（真实数据）\n\n"
        f"样本：`{candidate.symbol}` **{candidate.day}**\n\n"
        f"- 前收（raw）**{candidate.prev_close}** → 涨停价 **{candidate.limit_up}**"
        f"（`round(前收 × 1.10, 2)`）\n"
        f"- 当日开=高=低=收 **{candidate.open}**，源给的涨跌幅 "
        f"**{candidate.change_pct:.2f}%** → 判定为一字涨停\n"
        f"- 在该日前一根挂出的买单：**成交 {len(result.fills)} 笔**，"
        f"未成交原因码 `{codes}`\n"
        f"- 报告 `rejects.by_code` = `{rejects['by_code']}`\n"
        f"- 报告 `meta.a_share_rules` = `{report['meta']['a_share_rules']}`\n\n"
        "对照（反例）：把当日开盘改成低于涨停价（即当天开过板）→ 不构成一字板，买单照常成交。\n"
        "该反例的单元用例见 `tests/test_backtest_rules_wiring.py::"
        "test_buy_fills_when_the_limit_opened_up`。\n\n"
        f"复现：`cd backend && .venv/bin/python scripts/limit_reject_evidence.py`\n",
        encoding="utf-8",
    )
    print(f"  证据写入 {out}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "symbol": candidate.symbol,
                    "date": candidate.day.isoformat(),
                    "prev_close": candidate.prev_close,
                    "limit_up": candidate.limit_up,
                    "open": candidate.open,
                    "change_pct": candidate.change_pct,
                    "fills": len(result.fills),
                    "reject_codes": codes,
                    "report_rejects": rejects["by_code"],
                    "rule_status": report["meta"]["a_share_rules"],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
