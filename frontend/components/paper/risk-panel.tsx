"use client";

import { useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { Cell, Row, TableShell } from "@/components/backtest/table";
import { dayStamp, num } from "@/lib/format";
import {
  riskEventSymbol,
  riskGateLabel,
  riskRows,
  riskStateOf,
  riskTone,
  riskVerdictLabel,
} from "@/lib/paper";
import type { PaperAccountDetail, PaperRiskEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * E5 风控面板：三读数 + 回撤暂停横幅 + 风控日志。
 *
 * 配色纪律：**风控的「需要看一眼」统一用 `--warn`，不借红绿**——红绿在本项目是涨跌 /
 * 买卖的专用色（`StatusBadge` 的 `alert` 走 destructive 是因为「批了却没成」是意外；
 * 风控拦截是设计内的动作，不是故障）。
 *
 * 「上限是硬顶不是目标」必须写在面板上：单标的上限在 5 只以上池子里通常不触发，
 * 用户不该把它当成会天天响的警报。
 */

const RISK_LOG_PREVIEW = 12;

const TONE_CLASS = {
  normal: "border-border text-ink-3",
  warn: "border-warn/40 bg-warn/10 text-warn",
} as const;

export function RiskPanel({
  detail,
  names,
}: {
  detail: PaperAccountDetail;
  names: Record<string, string>;
}) {
  const limits = detail.account.config.risk ?? null;
  const state = riskStateOf(detail.risk_state);
  const events = detail.risk_events ?? [];

  if (!limits || !state) {
    return (
      <Section title="风控" hint="本会话未启用">
        <p className="mt-3 rounded-[var(--radius)] border border-dashed border-border px-4 py-4 text-sm text-ink-2">
          本会话未启用风控（创建时未设置）。新建会话可选「标准」或「严格」档。
        </p>
      </Section>
    );
  }

  return (
    <Section title="风控" hint={`截至 ${state.as_of} · 上限是硬顶不是目标`}>
      <div className="mt-3 grid gap-3 sm:grid-cols-3" data-risk-panel>
        {riskRows(state, limits).map((row) => (
          <div
            key={row.metric}
            data-risk-row={row.metric}
            className="rounded-[var(--radius)] border border-border p-3"
          >
            <p className="text-xs text-ink-3">{row.label}</p>
            <p className={cn("num mt-1 text-lg font-semibold", row.alert ? "text-warn" : "")}>
              {row.current === null ? "—" : `${num(row.current, { digits: 2 })}%`}
              <span className="ml-1 text-xs font-normal text-ink-3">
                / 上限 {num(row.limit, { digits: 0 })}%
              </span>
            </p>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className={cn("h-full rounded-full", row.alert ? "bg-warn" : "bg-brand")}
                style={{ width: `${(row.ratio ?? 0) * 100}%` }}
              />
            </div>
          </div>
        ))}
      </div>

      {state.blocked_by_drawdown ? (
        <p
          data-risk-blocked
          className="mt-3 rounded-[var(--radius)] border border-warn/50 bg-warn/10 px-3 py-2 text-sm text-warn"
        >
          回撤超限：暂停开新仓（卖出不受限，恢复到阈值下自动解禁）。
        </p>
      ) : null}

      <RiskLog events={events} names={names} />
    </Section>
  );
}

function RiskLog({
  events,
  names,
}: {
  events: PaperRiskEvent[];
  names: Record<string, string>;
}) {
  const [all, setAll] = useState(false);

  if (!events.length) {
    return (
      <p className="mt-3 rounded-[var(--radius)] border border-dashed border-border px-4 py-4 text-sm text-ink-2">
        还没有风控事件。策略信号触发闸门时，这里会逐条记下来（放行、缩量、拦截、触发）。
      </p>
    );
  }

  const rows = all ? events : events.slice(0, RISK_LOG_PREVIEW);

  return (
    <>
      <TableShell head={["日期", "标的", "闸门", "处置", "说明"]}>
        {rows.map((event) => (
          <Row key={event.id}>
            <Cell className="num whitespace-nowrap">{dayStamp(event.trade_date)}</Cell>
            <Cell className="num whitespace-nowrap">
              {riskEventSymbol(event)}
              {event.symbol &&
              names[event.symbol] &&
              names[event.symbol] !== event.symbol ? (
                <span className="ml-1.5 text-xs text-ink-3">{names[event.symbol]}</span>
              ) : null}
            </Cell>
            <Cell className="whitespace-nowrap">{riskGateLabel(event.gate)}</Cell>
            <Cell>
              <span
                data-risk-event={event.verdict}
                className={cn(
                  "inline-flex items-center rounded-[var(--radius)] border px-1.5 py-0.5 text-xs whitespace-nowrap",
                  TONE_CLASS[riskTone(event.verdict)],
                )}
              >
                {riskVerdictLabel(event.verdict)}
              </span>
            </Cell>
            <Cell className="min-w-[18rem] text-ink-2 whitespace-normal">
              {event.detail || "—"}
            </Cell>
          </Row>
        ))}
      </TableShell>
      {events.length > RISK_LOG_PREVIEW ? (
        <button
          type="button"
          onClick={() => setAll((value) => !value)}
          className="mt-2 text-xs text-brand hover:underline"
        >
          {all ? `只看最近 ${RISK_LOG_PREVIEW} 条` : `展开全部 ${events.length} 条`}
        </button>
      ) : null}
    </>
  );
}
