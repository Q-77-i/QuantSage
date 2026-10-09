"use client";

import { batchTableFromCells } from "@/lib/optimize-matrix";
import { pct } from "@/lib/format";
import type { BatchColumn } from "@/lib/optimize-matrix";
import type { OptimizeSummary } from "@/lib/types";

/**
 * 批量结果表：**多标的 × 多策略**。
 *
 * 形态是表不是图（dataviz：类别一多就该用表）——而且这里的每一格本身就是一组指标，
 * 塞进任何坐标系都会丢掉一半信息。
 *
 * 行列由**请求**给（`symbols` × `strategies`），不是从 `cells` 推：有格失败时从 cells 推
 * 会悄悄少一行一列，而那正是最该看见的东西。失败格照常占位，写明失败原因。
 */
export function BatchTable({
  summary,
  symbols,
  columns,
  onPick,
  picking,
}: {
  summary: OptimizeSummary;
  symbols: string[];
  columns: BatchColumn[];
  onPick: (cellIndex: number) => void;
  picking: number | null;
}) {
  const payload = batchTableFromCells(summary, symbols, columns);

  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            <th className="border-b border-border px-2 py-2 text-left text-xs font-normal text-ink-3">标的</th>
            {payload.strategies.map((column) => (
              <th
                key={column.key}
                className="border-b border-border px-2 py-2 text-left text-xs font-normal text-ink-3"
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {payload.symbols.map((symbol, rowIndex) => (
            <tr key={symbol}>
              <th className="num border-b border-border/60 px-2 py-2 text-left font-normal">{symbol}</th>
              {payload.grid[rowIndex].map((cell, columnIndex) => (
                <td key={payload.strategies[columnIndex].key} className="border-b border-border/60 px-2 py-2 align-top">
                  {cell === null ? (
                    <span className="text-ink-3">—</span>
                  ) : cell.ok ? (
                    <button
                      type="button"
                      onClick={() => onPick(cell.index)}
                      disabled={picking !== null}
                      title="点击重跑这一格"
                      className="block rounded-[var(--radius)] px-1.5 py-1 text-left hover:bg-muted disabled:opacity-50"
                    >
                      <span className="num block">
                        夏普 {cell.metrics?.sharpe === null || cell.metrics?.sharpe === undefined
                          ? "—"
                          : cell.metrics.sharpe.toFixed(3)}
                      </span>
                      <span className="num block text-xs text-ink-3">
                        收益 {pct(cell.metrics?.total_return ?? null, { signed: true })} · 回撤{" "}
                        {pct(cell.metrics?.max_drawdown ?? null)}
                      </span>
                    </button>
                  ) : (
                    <span className="block px-1.5 py-1">
                      <span className="block text-xs text-destructive">失败</span>
                      {/* 原因原样带出来（服务端给的话术），不自己翻译成一个笼统的「出错了」 */}
                      <span className="block text-xs text-ink-3">{cell.error?.message}</span>
                    </span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {picking !== null ? <p className="mt-2 text-xs text-ink-3">正在重跑选中的那一格…</p> : null}
    </div>
  );
}
