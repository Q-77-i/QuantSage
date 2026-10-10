"use client";

import { useState } from "react";

import { StatusBadge } from "@/components/paper/status-badge";
import { TableShell, Row, Cell } from "@/components/backtest/table";
import { amount, num, dayStamp } from "@/lib/format";
import { sideLabel } from "@/lib/paper";
import type { PaperDecision } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 决策流水：六态逐条可见。**新的在前**（最近发生的事最要紧）。
 *
 * 两列是刻意的：
 *   * 「预计」与「实际」分开——提案数量按决策日收盘价估、成交股数按次日开盘价重算；
 *   * 未成交的把拒绝原因原样写在行里，不外链到别处（服务端 `reject_reason` 是给人看的话术）。
 */
export function DecisionTable({
  decisions,
  names,
}: {
  decisions: PaperDecision[];
  names: Record<string, string>;
}) {
  const [all, setAll] = useState(false);
  const rows = all ? decisions : decisions.slice(0, 12);

  if (!decisions.length) {
    return (
      <p className="mt-3 rounded-[var(--radius)] border border-dashed border-border px-4 py-6 text-center text-sm text-ink-2">
        还没有任何决策。策略在推进过程中给出信号时，这里会逐条记下来。
      </p>
    );
  }

  return (
    <>
      <TableShell head={["决策日", "标的", "方向", "预计数量", "预计金额", "状态", "实际成交", "理由 / 原因"]}>
        {rows.map((decision) => {
          const fill = decision.fill;
          return (
            <Row key={decision.id}>
              <Cell className="num whitespace-nowrap">{dayStamp(decision.trade_date)}</Cell>
              <Cell className="num whitespace-nowrap">
                {decision.symbol}
                {names[decision.symbol] && names[decision.symbol] !== decision.symbol ? (
                  <span className="ml-1.5 text-xs text-ink-3">{names[decision.symbol]}</span>
                ) : null}
              </Cell>
              <Cell className={cn("whitespace-nowrap", decision.side === "buy" ? "text-up" : "text-down")}>
                {sideLabel(decision.side)}
              </Cell>
              <Cell numeric>{num(decision.est_qty, { digits: 0 })}</Cell>
              <Cell numeric>{amount(decision.est_qty * decision.est_price)}</Cell>
              <Cell>
                {/* 徽章带 `data-decision`：界面验证按**状态属性**断言，不按文案撞
                    （同一条流水里别的行也会出现同样的字） */}
                <span data-decision={decision.id}>
                  <StatusBadge status={decision.status} label={decision.status_label} />
                </span>
              </Cell>
              <Cell className="num whitespace-nowrap">
                {fill ? (
                  <>
                    {num(fill.qty, { digits: 0 })} 股 @ {num(fill.price)}
                    <span className="ml-1 text-xs text-ink-3">
                      费用 {amount(fill.commission + fill.stamp_tax)}
                    </span>
                  </>
                ) : (
                  "—"
                )}
              </Cell>
              <Cell className="text-ink-2">
                {decision.reject_reason ?? decision.reason}
              </Cell>
            </Row>
          );
        })}
      </TableShell>
      {decisions.length > 12 ? (
        <button
          type="button"
          onClick={() => setAll((value) => !value)}
          className="mt-2 text-xs text-brand hover:underline"
        >
          {all ? "只看最近 12 条" : `展开全部 ${decisions.length} 条`}
        </button>
      ) : null}
    </>
  );
}
