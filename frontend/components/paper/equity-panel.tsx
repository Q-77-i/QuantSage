"use client";

import { LineChart } from "echarts/charts";
import {
  GridComponent,
  MarkPointComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useEChart } from "@/components/ui/use-echart";
import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import type { ChartTokens } from "@/lib/chart-theme";
import { amount, num } from "@/lib/format";
import { equitySeries } from "@/lib/paper";
import type { PaperDecision, PaperEquityPoint } from "@/lib/types";

// 按需注册：漏一项不抛错，只在控制台留 warning 然后图缺一块（M5c 记过这条）
echarts.use([LineChart, GridComponent, TooltipComponent, MarkPointComponent, CanvasRenderer]);

/**
 * 净值曲线 + **表格孪生**（dataviz 硬要求：图与表同一份数）。
 *
 * 单序列不画图例（一个色就是全部信息）。成交日打标记——买 ▲ 红 / 卖 ▼ 绿，
 * 与 K 线 markers 同一套 `up`/`down` 口径：模拟盘的曲线里「哪天动过手」是最要紧的信息，
 * 去掉标记就只剩一个终值。
 *
 * 表格孪生就是每日结算表本身（页面下方那一节），所以这里不再复制一份列。
 */
export function EquityPanel({
  curve,
  decisions,
}: {
  curve: PaperEquityPoint[];
  decisions: PaperDecision[];
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);
  const [asTable, setAsTable] = useState(false);

  const series = useMemo(() => equitySeries(curve, decisions), [curve, decisions]);
  const option = useMemo(
    () => (curve.length < 2 ? null : buildOption(series, chartTokens(isDark))),
    [series, curve.length, isDark],
  );
  useEChart(hostRef, option);

  const drawable = curve.length >= 2;

  return (
    <div className="mt-1">
      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setAsTable((value) => !value)}
        >
          {asTable ? "看图" : "看表格"}
        </Button>
      </div>
      {/*
        **宿主常驻、且按真实尺寸 init**（同 `ChartFrame` 的写法）。两条都是踩出来的：
        · 宿主「数据到了才渲染」→ `useEChart` 只在组件挂载时 init，那一刻 ref 是 null，
          实例永远不会建出来（ECharts 静默无画布，页面上只少一张图）；
        · 用 `hidden` 把它藏到有数据为止也不行——零尺寸容器会让 ECharts 用兜底的
          100×300 建画布（界面验证的读数里当场露馅），得等 ResizeObserver 补一刀。
        故「还没数据」与「看表格」只在外面叠一层提示 / 换成表格，宿主本身始终可见。
      */}
      <div className="relative h-[300px] w-full">
        <div
          ref={hostRef}
          className={`h-full w-full rounded-[var(--radius)] border border-border bg-chart-surface ${asTable ? "hidden" : ""}`}
          role="img"
          aria-label={`净值曲线，共 ${curve.length} 个交易日，成交标记 ${series.markers.length} 个`}
        />
        {!drawable ? (
          <p className="absolute inset-0 flex items-center justify-center rounded-[var(--radius)] border border-dashed border-border text-sm text-ink-2">
            只有一个交易日的估值点，还画不出曲线——推进一天再看。
          </p>
        ) : null}
      </div>
      {asTable && drawable ? <SettlementTable curve={curve} /> : null}
    </div>
  );
}

/** 每日结算表：账户每天收盘的现金 / 市值 / 净值（也是净值曲线的表格孪生）。 */
export function SettlementTable({ curve }: { curve: PaperEquityPoint[] }) {
  const rows = [...curve].reverse(); // 新的在前（与决策流水一致）
  return (
    <div className="mt-3 max-h-[360px] overflow-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-ink-2">
            {["交易日", "现金", "持仓市值", "净值"].map((head) => (
              <th
                key={head}
                scope="col"
                className="sticky top-0 z-10 bg-background px-2 py-2 text-left font-normal whitespace-nowrap shadow-[inset_0_-1px_0_0_var(--hairline)]"
              >
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((point) => (
            <tr key={point.trade_date} className="h-[34px] border-b border-border">
              <td className="num px-2">{point.trade_date}</td>
              <td className="num px-2">{amount(point.cash)}</td>
              <td className="num px-2">{amount(point.market_value)}</td>
              <td className="num px-2 font-medium">{amount(point.equity)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function buildOption(
  series: ReturnType<typeof equitySeries>,
  t: ChartTokens,
): EChartsCoreOption {
  return {
    animation: false,
    textStyle: { fontFamily: CHART_FONT },
    grid: { left: 68, right: 18, top: 18, bottom: 28 },
    tooltip: {
      trigger: "axis",
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.ink, fontFamily: CHART_FONT, fontSize: 12 },
      valueFormatter: (value: unknown) => amount(Number(value)),
    },
    xAxis: {
      type: "category",
      data: series.dates,
      axisLine: { lineStyle: { color: t.grid } },
      axisLabel: { color: t.axis, fontSize: 11 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: t.grid } },
      axisLabel: {
        color: t.axis,
        fontSize: 11,
        formatter: (value: number) => num(value / 10_000, { digits: 1 }) + "万",
      },
    },
    series: [
      {
        type: "line",
        name: "净值",
        data: series.values,
        showSymbol: false,
        lineStyle: { width: 2, color: t.series1 },
        itemStyle: { color: t.series1 },
        areaStyle: { color: `${t.series1}1f` }, // 与回测页净值曲线同款
        // 成交标记：买 ▲ 红 / 卖 ▼ 绿（A 股口径，与 K 线 markers 同一套）
        markPoint: series.markers.length
          ? {
              symbolSize: 26,
              // 文案在每一项上给（买/卖各不同），这里只定通用样式
              label: { show: true, fontSize: 10, color: "#ffffff" },
              data: series.markers.map((marker) => ({
                name: marker.side === "buy" ? "买入" : "卖出",
                coord: [marker.date, valueAt(series, marker.date)],
                symbol: marker.side === "buy" ? "triangle" : "pin",
                itemStyle: { color: marker.side === "buy" ? t.up : t.down },
                label: { formatter: marker.side === "buy" ? "买" : "卖" },
                value: marker.qty,
              })),
            }
          : undefined,
      },
    ],
  };
}

/** 标记要落在曲线上：取当天（或之前最近一天）的净值。 */
function valueAt(series: ReturnType<typeof equitySeries>, day: string): number {
  let value = series.values[0] ?? 0;
  for (let index = 0; index < series.dates.length; index += 1) {
    if (series.dates[index] <= day) value = series.values[index];
    else break;
  }
  return value;
}
