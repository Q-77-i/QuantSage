"use client";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { amount, count, pct } from "@/lib/format";
import type { OpenPosition, Trade } from "@/lib/types";

/**
 * 交易明细。
 *
 * 两个口径必须照实呈现，否则页面上的数字会自相矛盾：
 *   · `reason` 是**出场**原因，入场原因另有一列（SPEC §5 明确区分）
 *   · 期末未平仓的那笔**不在** `trades` 里，也不进胜率与交易次数——必须单列，
 *     否则用户拿「交易次数 4」去数表格里 5 行会以为算错了
 */
export function TradesTable({
  trades,
  openPosition,
}: {
  trades: Trade[];
  openPosition: OpenPosition | null;
}) {
  if (!trades.length && !openPosition) {
    return (
      <p className="mt-3 text-sm text-ink-2">
        该区间内没有成交：信号未触发，或区间太短没走到成交那一步。
      </p>
    );
  }

  return (
    <>
      <TableShell
        head={["入场", "出场", "持有", "数量", "收益", "盈亏", "出场原因", "入场原因"]}
      >
        {trades.map((trade, index) => (
          <Row key={`${trade.entry_date}-${trade.exit_date}-${index}`}>
            <Cell numeric>{trade.entry_date}</Cell>
            <Cell numeric>{trade.exit_date}</Cell>
            <Cell numeric>{count(trade.hold_bars)} 日</Cell>
            <Cell numeric>{count(trade.qty)}</Cell>
            <Cell numeric className={tone(trade.return_pct)}>
              {pct(trade.return_pct, { signed: true })}
            </Cell>
            <Cell numeric className={tone(trade.pnl)}>
              {amount(trade.pnl, { signed: true })}
            </Cell>
            <Cell className="text-ink-2">{trade.reason}</Cell>
            <Cell className="text-ink-2">{trade.entry_reason}</Cell>
          </Row>
        ))}

        {openPosition && (
          <Row>
            <Cell numeric>{openPosition.entry_date}</Cell>
            <Cell className="text-ink-3">持有中</Cell>
            <Cell className="text-ink-3">—</Cell>
            <Cell numeric>{count(openPosition.shares)}</Cell>
            <Cell numeric className={tone(openPosition.unrealized_return)}>
              {pct(openPosition.unrealized_return, { signed: true })}
            </Cell>
            <Cell numeric className={tone(openPosition.unrealized_pnl)}>
              {amount(openPosition.unrealized_pnl, { signed: true })}
            </Cell>
            <Cell className="text-ink-3">未平仓</Cell>
            <Cell className="text-ink-2">{openPosition.entry_reason}</Cell>
          </Row>
        )}
      </TableShell>

      {openPosition && (
        <p className="mt-2 text-xs text-ink-3">
          末行是期末未平仓持仓（
          <span className="num">
            成本 {amount(openPosition.entry_price, { digits: 2 })} · 现价{" "}
            {amount(openPosition.last_close, { digits: 2 })}
          </span>
          ），按期末收盘价估值，不计入上面的交易次数与胜率。
        </p>
      )}
    </>
  );
}

/** 盈亏红涨绿跌，但颜色只是二次编码——数字本身带号，色盲下也读得出来。 */
function tone(value: number): string {
  if (value > 0) return "text-up";
  if (value < 0) return "text-down";
  return "";
}
