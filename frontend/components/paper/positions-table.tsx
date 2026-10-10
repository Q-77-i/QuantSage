"use client";

import { num } from "@/lib/format";
import type { PaperPosition } from "@/lib/types";

/** 持仓表：代码 / 名称 / 股数 / 成本 / 建仓日 / 建仓理由。 */
export function PositionsTable({
  positions,
  names,
}: {
  positions: PaperPosition[];
  names: Record<string, string>;
}) {
  if (!positions.length) {
    return (
      <p className="mt-3 rounded-[var(--radius)] border border-dashed border-border px-4 py-6 text-center text-sm text-ink-2">
        当前空仓。
      </p>
    );
  }
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-ink-2">
            {["标的", "股数", "成本价", "买入费用", "建仓日", "建仓理由"].map((head) => (
              <th key={head} scope="col" className="px-2 py-2 text-left font-normal whitespace-nowrap">
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.symbol} className="h-[38px] border-b border-border">
              <td className="num px-2 whitespace-nowrap">
                {position.symbol}
                {names[position.symbol] && names[position.symbol] !== position.symbol ? (
                  <span className="ml-1.5 text-xs text-ink-3">{names[position.symbol]}</span>
                ) : null}
              </td>
              <td className="num px-2">{num(position.shares, { digits: 0 })}</td>
              <td className="num px-2">{num(position.entry_price)}</td>
              <td className="num px-2">{num(position.entry_fees)}</td>
              <td className="num px-2">{position.entry_date ?? "—"}</td>
              <td className="px-2 text-ink-2">{position.entry_reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
