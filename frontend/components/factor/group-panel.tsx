"use client";

import { useTheme } from "next-themes";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { chartTokens } from "@/lib/chart-theme";
import { curveTable, groupLines } from "@/lib/factor-report";
import type { Track } from "@/lib/factor-report";
import { num } from "@/lib/format";
import type { FactorReport } from "@/lib/types";

import { CurveChart } from "./curve-chart";

/**
 * 5 条分层净值曲线：**单色相 ordinal 蓝阶**（Q1 最浅 → Q5 最深）。
 *
 * 分位是**有序**类别，用五个任意色相等于把有序信息浪费掉（dataviz：有序类别用 ordinal ramp）。
 * 毛/净是**视图开关**而不是数据筛选：同一份数的两种呈现（关掉费用时净为 null，
 * 开关随之禁用并说明原因——不静默回落到毛）。
 *
 * 「看表格」是 dataviz 的硬要求：连续色标必须有 WCAG 干净的等价物，故表格不是附加功能。
 */
export function GroupPanel({ report }: { report: FactorReport }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const tokens = chartTokens(isDark);
  const hasNet = report.groups.every((group) => group.net !== null);
  const [track, setTrack] = useState<Track>(hasNet ? "net" : "gross");
  const [asTable, setAsTable] = useState(false);

  const lines = useMemo(() => groupLines(report, track), [report, track]);
  const table = useMemo(() => curveTable(report, track), [report, track]);
  const series = useMemo(
    () =>
      (lines?.series ?? []).map((item) => ({
        name: item.label,
        values: item.values,
        color: tokens.group[item.rampIndex] ?? tokens.series1,
      })),
    [lines, tokens],
  );

  return (
    <div className="mt-1">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <div className="flex items-center gap-1 text-xs text-ink-3">
          <span>口径</span>
          {(["gross", "net"] as const).map((key) => (
            <button
              key={key}
              type="button"
              disabled={key === "net" && !hasNet}
              aria-pressed={track === key}
              onClick={() => setTrack(key)}
              className={
                track === key
                  ? "rounded-[var(--radius)] bg-muted px-2 py-0.5 text-foreground"
                  : "rounded-[var(--radius)] px-2 py-0.5 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              }
            >
              {key === "gross" ? "毛（不含费用）" : "净（扣费用）"}
            </button>
          ))}
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={() => setAsTable((value) => !value)}>
          {asTable ? "看图" : "看表格"}
        </Button>
      </div>
      {!hasNet ? (
        <p className="mt-1 text-right text-xs text-ink-3">
          本次请求关闭了费用，只有毛曲线——净曲线要重跑一次并打开费用。
        </p>
      ) : null}

      {asTable ? (
        <CurveTableView dates={table.dates} columns={table.columns} />
      ) : (
        <div className="mt-2">
          {lines === null ? (
            <p className="rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
              本窗口没有有效信号日，没有曲线可画。
            </p>
          ) : (
            <CurveChart
              dates={lines.dates}
              series={series}
              height={320}
              ariaLabel={`${track === "net" ? "净" : "毛"}净值：五个分位组，共 ${lines.dates.length} 个信号日`}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** 曲线图的表格孪生：日期 × (Q1…Q5 + 多空)，缺值显示 `—`（不是 0）。 */
export function CurveTableView({
  dates,
  columns,
}: {
  dates: string[];
  columns: { key: string; label: string; values: (number | null)[] }[];
}) {
  return (
    <div className="mt-2 max-h-[320px] overflow-auto rounded-[var(--radius)] border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">各分位组与多空组合的逐日净值</caption>
        <thead className="sticky top-0 bg-card">
          <tr className="text-left text-xs text-ink-3">
            <th scope="col" className="px-3 py-1.5 font-normal">日期</th>
            {columns.map((column) => (
              <th key={column.key} scope="col" className="px-3 py-1.5 text-right font-normal">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {dates.map((date, index) => (
            <tr key={date} className="border-t border-border">
              <td className="num px-3 py-1 text-ink-2">{date}</td>
              {columns.map((column) => (
                <td key={column.key} className="num px-3 py-1 text-right">
                  {num(column.values[index] ?? null, { digits: 4 })}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
