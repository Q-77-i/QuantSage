"use client";

/**
 * 净值图：账户 vs 全市场等权（**两序列**）+ 表格孪生。
 *
 * 配色**由校验器定**（dataviz skill 的纪律：不眼看）：账户 = `series1` 蓝实线、
 * 全市场等权 = `series2` 橙虚线——蓝紫那对在本项目里 CVD ΔE 只有 1.4、正常视力也只有 12.5
 * （低于 15 是硬失败），蓝橙这对 24.7/33.6 全过。线型是第二重身份（实线 / 虚线）。
 *
 * 宿主常驻（不「数据到了才渲染」）：`useEChart` 要在挂载那一刻拿到宿主，
 * 晚了实例永远不建（M6b 踩过，静默无画布）。
 */

import { useMemo, useState } from "react";
import { useTheme } from "next-themes";

import { CurveChart } from "@/components/factor/curve-chart";
import { Section } from "@/components/backtest/chart-frame";
import { chartTokens } from "@/lib/chart-theme";
import { amount } from "@/lib/format";
import type { ReportEquityPoint } from "@/lib/types";

export function EquityBenchmarkChart({ points }: { points: ReportEquityPoint[] }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const [table, setTable] = useState(false);

  const { dates, series } = useMemo(() => {
    const t = chartTokens(isDark);
    return {
      dates: points.map((point) => point.date),
      series: [
        {
          name: "账户净值",
          values: points.map((point) => point.equity),
          color: t.series1,
        },
        {
          name: "全市场等权",
          values: points.map((point) => point.benchmark),
          color: t.series2,
          dashed: true,
        },
      ],
    };
  }, [points, isDark]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-end">
        <button
          type="button"
          onClick={() => setTable((value) => !value)}
          className="h-7 rounded-[var(--radius)] border border-border px-2 text-xs hover:bg-muted"
        >
          {table ? "看图" : "看表格"}
        </button>
      </div>
      {/* 图与表二选一，但**两个宿主都在**（图宿主切走再切回时靠 useEChart 的回调 ref 重建） */}
      <div className={table ? "hidden" : undefined}>
        <CurveChart
          dates={dates}
          series={series}
          ariaLabel="账户净值与全市场等权基准的逐日曲线"
          height={300}
          formatValue={(value) => amount(value)}
        />
      </div>
      {table ? (
        <Section title="逐日净值" hint="与图同一份数（账户 / 全市场等权）">
          <div className="max-h-[300px] overflow-auto">
            <table className="w-full border-collapse text-sm">
              <thead className="sticky top-0 bg-background">
                <tr className="border-b border-border text-left text-xs text-ink-3">
                  <th className="py-1.5 pr-3 font-normal">日期</th>
                  <th className="py-1.5 pr-3 font-normal">账户净值</th>
                  <th className="py-1.5 pr-3 font-normal">全市场等权</th>
                </tr>
              </thead>
              <tbody>
                {points.map((point) => (
                  <tr key={point.date} className="border-b border-border/60">
                    <td className="py-1 pr-3 font-mono">{point.date}</td>
                    <td className="py-1 pr-3 font-mono tabular-nums">{amount(point.equity)}</td>
                    <td className="py-1 pr-3 font-mono tabular-nums">
                      {amount(point.benchmark)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}
    </div>
  );
}
