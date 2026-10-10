"use client";

import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useMemo } from "react";

import { useEChart } from "@/components/ui/use-echart";
import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import type { ChartTokens } from "@/lib/chart-theme";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

export interface CurveSeries {
  name: string;
  /** 与 `dates` 逐位对应；缺值给 `null`——ECharts 断线，**不当 0 也不跨缺口连** */
  values: (number | null)[];
  color: string;
  /** 毛/净同色时用线型分辨（本项目既有口径：实线=净、虚线=毛） */
  dashed?: boolean;
}

/**
 * 虚线的节奏 `[实线, 空白]`（像素）。**不写 `"dashed"`**：ECharts 的默认节奏在 2px 细线上
 * 是「8 实 2 空」这种密度，画在 20px 的**图例短线**里几乎看不出来——用户 2026-10-10 实机反馈
 * 「两个图例还是一模一样」。拉开到 9/6 后，图例与曲线两侧都读得出是虚线。
 */
const DASH_PATTERN = [9, 6];

/**
 * 净值曲线（分层 / 多空共用）：同一条 y 轴，绝不双轴。
 *
 * 基线画在 **1.0**（初始净值）而不是 0：曲线都在 1 附近波动，从 0 起画会把差异压平；
 * 而 1.0 这条线本身有含义（「投入的钱还在不在」），故画成实线细线并直接标出来。
 *
 * 文字一律墨色，识别靠图例左边的色块（dataviz：文字不染序列色）。
 */
export function CurveChart({
  dates,
  series,
  ariaLabel,
  height = 300,
  formatValue,
}: {
  dates: string[];
  series: CurveSeries[];
  ariaLabel: string;
  height?: number;
  /**
   * 数值格式化（tooltip 与 y 轴共用一处）。默认按「净值」口径（4 位小数）——
   * 研报页喂的是**金额**（元），走 `lib/format` 的 `amount()`，别让 50 万显示成 `500000.0000`。
   */
  formatValue?: (value: number) => string;
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const option = useMemo(
    () =>
      dates.length === 0
        ? null
        : buildOption(dates, series, chartTokens(isDark), formatValue),
    [dates, series, isDark, formatValue],
  );
  const { hostRef } = useEChart(option);

  return (
    <div
      ref={hostRef}
      className="w-full rounded-[var(--radius)] border border-border bg-chart-surface"
      style={{ height }}
      role="img"
      aria-label={ariaLabel}
    />
  );
}

function buildOption(
  dates: string[],
  series: CurveSeries[],
  t: ChartTokens,
  formatValue?: (value: number) => string,
): EChartsCoreOption {
  const show = formatValue ?? ((value: number) => value.toFixed(2));
  return {
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 12, right: 16, top: 30, bottom: 4, containLabel: true },
    legend: {
      top: 2,
      right: 0,
      // **刻意不设 `icon`**：LineSeries 自己的 `getLegendIcon` 会按**该序列的线型**画图例
      // （`icon: "rect"` 会把所有项一律画成实心块——毛/净同色时线型是唯一区分，
      // 图例两项长得一模一样，读者根本对不上哪条是哪条；2026-10-10 用户实机发现）。
      // 不设 icon 时它画的是「20px 细线 + 80% itemHeight 的小圆点」，itemHeight=2 下圆点
      // 只有 1.6px、肉眼不可见，细线图例的观感照旧。
      // 30px 而不是 20px：虚线要至少两个完整周期才看得出「那是虚线」
      itemWidth: 30,
      itemHeight: 2,
      textStyle: { color: t.ink2, fontSize: 12, fontFamily: CHART_FONT },
      data: series.map((item) => item.name),
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: t.axis, width: 1 } },
      backgroundColor: t.surface,
      borderColor: t.grid,
      borderWidth: 1,
      padding: [6, 10],
      textStyle: { color: t.ink, fontSize: 12, fontFamily: CHART_FONT },
      formatter: (params: unknown) => tooltipHtml(params, series, t, formatValue),
    },
    xAxis: {
      type: "category",
      data: dates,
      boundaryGap: false,
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
        formatter: show,
      },
    },
    series: series.map((item) => ({
      name: item.name,
      type: "line" as const,
      data: item.values,
      showSymbol: false,
      // 缺值**断线**（默认行为）：跨缺口连线会暗示一段没有数据的区间是连续的
      connectNulls: false,
      lineStyle: {
        width: 2,
        color: item.color,
        ...(item.dashed ? { type: DASH_PATTERN } : {}),
      },
      itemStyle: { color: item.color },
      // 基线（初始净值 = 1.0）：实线细线 + 直接标注，不做成虚线网格
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { color: t.axis, width: 1, type: "solid" },
        label: { formatter: "1.00", color: t.ink2, fontSize: 10, position: "insideEndTop" },
        data: [{ yAxis: 1 }],
      },
      z: 2,
    })),
  };
}

function tooltipHtml(
  params: unknown,
  series: CurveSeries[],
  t: ChartTokens,
  formatValue?: (value: number) => string,
): string {
  const show = formatValue ?? ((value: number) => value.toFixed(4));
  const list = (Array.isArray(params) ? params : [params]) as { dataIndex?: number; name?: string }[];
  const first = list[0];
  if (!first || first.dataIndex === undefined) return "";
  const rows = series
    .map((item) => {
      const value = item.values[first.dataIndex!];
      if (value === undefined || value === null) return "";
      const dot = `<span style="display:inline-block;width:8px;height:2px;background:${item.color}"></span>`;
      return (
        `<div style="display:flex;align-items:center;gap:6px;margin-top:2px">` +
        `${dot}<span>${item.name}</span>` +
        `<span style="margin-left:auto;padding-left:16px">${show(value)}</span></div>`
      );
    })
    .join("");
  // 日期用次墨色（同 `equity-chart` 的 tooltip），序列文字一律不染序列色
  return `<div style="color:${t.ink2}">${first.name ?? ""}</div>${rows}`;
}
