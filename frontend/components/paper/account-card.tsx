"use client";

import { pct, amount } from "@/lib/format";
import { pnlTone, progressRatio, ruleDegradations, totalReturn } from "@/lib/paper";
import type { PaperAccountDetail } from "@/lib/types";

/**
 * 账户卡：净值是英雄数字，其余三个数陪读。
 *
 * 「数据截止日」必须常驻——模拟盘只能回放到本地行情末端，界面不假装能到今天。
 * 会话末端与数据末端**是两个数**：会话窗口可能短于数据覆盖（那时只显示窗口末端）。
 */
export function AccountCard({ detail }: { detail: PaperAccountDetail }) {
  const { account, progress, valuation } = detail;
  const equity = valuation?.equity ?? account.cash;
  const initial = account.config.initial_cash;
  const ret = totalReturn(equity, initial);
  const degradations = ruleDegradations(account.rules);

  return (
    <div className="mt-3 rounded-[var(--radius)] border border-border p-4">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div>
          <p className="text-xs text-ink-3">账户净值</p>
          <p className="num font-heading text-3xl font-semibold">{amount(equity)}</p>
          <p className="mt-1 text-xs text-ink-3">
            初始 {amount(initial)} ·{" "}
            <span className={toneClass(ret === null ? null : pnlTone(ret))}>
              总收益 {ret === null ? "—" : pct(ret, { signed: true })}
            </span>
          </p>
        </div>

        <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
          <Stat label="可用现金" value={amount(account.cash)} />
          <Stat label="持仓市值" value={amount(valuation?.market_value ?? 0)} />
          <Stat
            label="已实现盈亏"
            value={amount(account.realized_pnl, { signed: true })}
            tone={pnlTone(account.realized_pnl)}
          />
        </dl>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-ink-3">
        <span>
          第 {progress.days_done} / {progress.days_total} 个交易日 · 当前模拟到{" "}
          <span className="num">{progress.as_of}</span>
        </span>
        <span className="hidden sm:inline">·</span>
        <span>
          可回放区间 <span className="num">{progress.start}</span> →{" "}
          <span className="num">{progress.end}</span>（数据到哪模拟到哪）
        </span>
        <span className="sm:hidden" />
        <div className="h-1 w-full max-w-xs overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-brand"
            style={{ width: `${progressRatio(progress) * 100}%` }}
          />
        </div>
      </div>

      {degradations.length ? (
        <p className="mt-3 text-xs text-warn">
          涨跌停判定本次<strong className="font-medium">未生效</strong>的标的：
          {degradations.map((item) => `${item.symbol}（${item.reason}）`).join("、")}
          ——这些标的的单子不设涨跌停闸门，成交结果要按此读。
        </p>
      ) : null}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | null;
}) {
  return (
    <div>
      <dt className="text-xs text-ink-3">{label}</dt>
      <dd className={`num ${toneClass(tone)}`}>{value}</dd>
    </div>
  );
}

function toneClass(tone: "up" | "down" | null | undefined): string {
  return tone === "up" ? "text-up" : tone === "down" ? "text-down" : "";
}
