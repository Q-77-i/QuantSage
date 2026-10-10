"use client";

import { BarChart } from "echarts/charts";
import { GridComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { useEChart } from "@/components/ui/use-echart";
import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import type { ChartTokens } from "@/lib/chart-theme";
import { icBars, icTable } from "@/lib/factor-report";
import type { ICBarsPayload, ICBarPoint } from "@/lib/factor-report";
import { num } from "@/lib/format";
import type { FactorReport } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只在控制台留一行 warning，然后图缺一块或空白。
echarts.use([BarChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

/**
 * 逐日 RankIC：**柱状 + 双色 + 零线**。
 *
 * 为什么是柱不是线：逐日 IC 没有连续性可言（今天 0.02、明天 −0.01 是两件独立的事），
 * 连线会暗示中间的日子有意义。颜色按**符号**给（红=正 / 蓝=负，与 M5b 热力图同一支语义），
 * 零线画成实线细线并直接标「0」——IC 的 0 是「无预测力」，这条线本身就是结论的参照。
 *
 * 表格孪生是 dataviz 的硬要求（连续色标必须有 WCAG 干净的等价物），故「看表格」不是附加功能。
 */
export function ICChart({ report }: { report: FactorReport }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const [asTable, setAsTable] = useState(false);

  const payload = useMemo(() => icBars(report), [report]);
  const rows = useMemo(() => icTable(report), [report]);
  const option = useMemo(
    () => (payload.empty ? null : buildOption(payload, chartTokens(isDark))),
    [payload, isDark],
  );
  const { hostRef } = useEChart(option);

  return (
    <div className="mt-1">
      <div className="flex justify-end">
        <Button type="button" variant="ghost" size="sm" onClick={() => setAsTable((value) => !value)}>
          {asTable ? "看图" : "看表格"}
        </Button>
      </div>
      {asTable ? (
        <ICTable rows={rows} />
      ) : (
        <div
          ref={hostRef}
          className="h-[240px] w-full rounded-[var(--radius)] border border-border bg-chart-surface"
          role="img"
          aria-label={`逐日 RankIC，共 ${payload.points.length} 个有效信号日`}
        />
      )}
      {payload.empty ? (
        <p className="px-4 py-10 text-center text-sm text-ink-2">
          本窗口没有有效信号日——池子不足或行情末端之后没有前向收益，如实留空。
        </p>
      ) : null}
    </div>
  );
}

function ICTable({ rows }: { rows: ICBarPoint[] }) {
  return (
    <div className="max-h-[240px] overflow-auto rounded-[var(--radius)] border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">逐日 RankIC 与当日池内样本数</caption>
        <thead className="sticky top-0 bg-card">
          <tr className="text-left text-xs text-ink-3">
            <th scope="col" className="px-3 py-1.5 font-normal">日期</th>
            <th scope="col" className="px-3 py-1.5 text-right font-normal">RankIC</th>
            <th scope="col" className="px-3 py-1.5 text-right font-normal">样本数</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.date} className="border-t border-border">
              <td className="num px-3 py-1 text-ink-2">{row.date}</td>
              <td className="num px-3 py-1 text-right">{num(row.ic, { signed: true, digits: 4 })}</td>
              <td className="num px-3 py-1 text-right text-ink-2">{row.n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function buildOption(payload: ICBarsPayload, t: ChartTokens): EChartsCoreOption {
  return {
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 12, right: 16, top: 16, bottom: 4, containLabel: true },
    tooltip: {
      trigger: "item",
      backgroundColor: t.surface,
      borderColor: t.grid,
      borderWidth: 1,
      textStyle: { color: t.ink, fontSize: 12, fontFamily: CHART_FONT },
      formatter: (params: { dataIndex: number }) => {
        const point = payload.points[params.dataIndex];
        if (!point) return "";
        return `${point.date}<br/>RankIC ${point.ic.toFixed(4)}<br/>样本 ${point.n}`;
      },
    },
    xAxis: {
      type: "category",
      data: payload.points.map((point) => point.date),
      axisLine: { lineStyle: { color: t.grid } },
      axisTick: { show: false },
      axisLabel: { color: t.axis, fontSize: 11, fontFamily: CHART_FONT, hideOverlap: true },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: t.grid, type: "solid" } },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: t.axis,
        fontSize: 11,
        fontFamily: CHART_FONT,
        formatter: (value: number) => value.toFixed(2),
      },
    },
    series: [
      {
        type: "bar",
        // 细柱：54 个信号日挤在一条轴上也读得出「逐日」的密度
        barMaxWidth: 9,
        data: payload.points.map((point) => ({
          value: point.ic,
          itemStyle: {
            color: point.ic >= 0 ? t.up : t.series1,
            // 圆角只加在**远离基线的那一端**（数据端），基线端保持方角
            borderRadius: point.ic >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2],
          },
        })),
        // 零线：IC 的 0 = 无预测力，画成实线细线并直接标「0」（不是虚线网格）
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: t.axis, width: 1, type: "solid" },
          label: { formatter: "0", color: t.ink2, fontSize: 10, position: "insideEndTop" },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  };
}
