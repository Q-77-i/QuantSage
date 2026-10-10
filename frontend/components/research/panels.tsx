"use client";

/**
 * 研报的三个展示件：指标卡、归因三表、证据列表。
 *
 * 都只吃 `lib/research.ts` 的视图模型（那一层决定单位与语气，组件只管画）——
 * 数字格式化不在组件里出现第二遍。
 */

import { useState } from "react";

import { EMPTY, amount } from "@/lib/format";
import {
  evidenceNotice,
  evidenceRows,
  metricCells,
  numberLabel,
  type EvidenceRow,
  type MetricCell,
  type TableView,
} from "@/lib/research";
import type { ReportEvidence, ReportMetrics } from "@/lib/types";

const TONE: Record<MetricCell["tone"], string> = {
  plain: "text-foreground",
  up: "text-up",
  down: "text-down",
};

/** 指标卡：首行三张大卡（累计 / 超额 / 回撤），其余小卡。 */
export function MetricsCards({ metrics }: { metrics: ReportMetrics }) {
  const cells = metricCells(metrics);
  const primary = cells.filter((cell) => cell.primary);
  const rest = cells.filter((cell) => !cell.primary);
  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-3">
        {primary.map((cell) => (
          <div
            key={cell.key}
            data-metric={cell.key}
            className="rounded-[var(--radius)] border border-border bg-card px-4 py-3"
          >
            <div className="text-xs text-ink-3">{cell.label}</div>
            <div className={`mt-1 font-mono text-2xl font-semibold ${TONE[cell.tone]}`}>
              {cell.value}
            </div>
            <div className="mt-1 text-xs text-ink-3">{cell.hint}</div>
          </div>
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {rest.map((cell) => (
          <div
            key={cell.key}
            data-metric={cell.key}
            className="rounded-[var(--radius)] border border-border px-3 py-2"
          >
            <div className="text-xs text-ink-3">{cell.label}</div>
            <div className={`mt-1 font-mono text-lg ${TONE[cell.tone]}`}>{cell.value}</div>
            <div className="mt-0.5 text-[11px] leading-tight text-ink-3">{cell.hint}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 归因三表。空表不留白：写明「本次没有…」。 */
export function AttributionTables({ tables }: { tables: TableView[] }) {
  return (
    <div className="flex flex-col gap-5">
      {tables.map((table) => (
        <div key={table.key} data-attribution={table.key}>
          <div className="flex flex-wrap items-baseline gap-2">
            <h4 className="text-sm font-medium">{table.title}</h4>
            <span className="text-xs text-ink-3">{table.hint}</span>
          </div>
          {table.rows.length ? (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-ink-3">
                    {table.headers.map((header) => (
                      <th key={header} className="py-1.5 pr-3 font-normal">
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((row, index) => (
                    <tr key={`${table.key}-${index}`} className="border-b border-border/60">
                      {row.map((cell, cellIndex) => (
                        <td
                          key={`${table.key}-${index}-${cellIndex}`}
                          className={`py-1.5 pr-3 ${cellIndex === 0 ? "" : "font-mono tabular-nums"}`}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-2 text-sm text-ink-3">{table.empty}</p>
          )}
        </div>
      ))}
    </div>
  );
}

/** 证据项：一行一条，行首「证据」小标 + 可展开的键值表（事发与可得并列在首两行）。 */
export function EvidenceList({
  items,
  emptyText = "本次报告没有可回链的事件证据",
}: {
  items: ReportEvidence[];
  emptyText?: string;
}) {
  if (!items.length) return <p className="text-sm text-ink-3">{emptyText}</p>;
  return (
    <ol className="flex flex-col gap-2">
      {items.map((item) => (
        <EvidenceItem key={`${item.event_id}|${item.day}`} item={item} />
      ))}
    </ol>
  );
}

function EvidenceItem({ item }: { item: ReportEvidence }) {
  const [open, setOpen] = useState(false);
  const rows = evidenceRows(item);
  const notice = evidenceNotice(item);
  return (
    <li className="rounded-[var(--radius)] border border-border" data-evidence={item.event_id}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left text-sm hover:bg-muted/50"
      >
        <span className="mt-0.5 shrink-0 rounded-[2px] border border-border px-1 text-[10px] text-ink-3">
          证据
        </span>
        <span className="flex-1">{item.title ?? item.event_id}</span>
        <span className="shrink-0 text-xs text-ink-3">{open ? "收起" : "展开"}</span>
      </button>
      {/* 折叠时**只是屏幕上藏起来**（`hidden print:block`）：纸上要能看见全部证据 */}
      <dl className={`border-t border-border px-3 py-2 text-xs ${open ? "" : "hidden print:block"}`}>
          {rows.map((row) => (
            <Row key={row.label} row={row} />
          ))}
        {notice ? (
          <p className="mt-2 rounded-[var(--radius)] border border-warn/40 bg-warn/10 px-2 py-1 text-warn">
            {notice}
          </p>
        ) : null}
      </dl>
    </li>
  );
}

function Row({ row }: { row: EvidenceRow }) {
  return (
    <div className="flex gap-2 py-0.5">
      <dt className="w-16 shrink-0 text-ink-3">{row.label}</dt>
      <dd className={`flex-1 break-all ${row.mono ? "font-mono" : ""}`}>
        {row.href ? (
          <a href={row.href} target="_blank" rel="noreferrer" className="underline">
            打开原文
          </a>
        ) : (
          row.value
        )}
      </dd>
    </div>
  );
}

/** 概览块里的数：**每个数都带标签**（两个裸数字是两句没有主语的话）。 */
export function NumberChips({ numbers }: { numbers: Record<string, number | null> | undefined }) {
  const entries = Object.entries(numbers ?? {});
  if (!entries.length) return null;
  return (
    <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-sm">
      {entries.map(([path, value]) => (
        <div key={path} className="flex items-baseline gap-2">
          <dt className="text-ink-3">{numberLabel(path)}</dt>
          <dd className="font-mono tabular-nums">
            {value === null || value === undefined ? EMPTY : amount(value, { digits: 2 })}
          </dd>
        </div>
      ))}
    </dl>
  );
}
