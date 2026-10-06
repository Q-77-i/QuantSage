"use client";

import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useEffect, useRef } from "react";

import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import type { ChartTokens } from "@/lib/chart-theme";
import { amount } from "@/lib/format";
import type { EquityPoint } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只会在控制台留一行 warning，然后图缺一块或空白，
// 所以集中在这里一次注册齐。加图表类型时记得同步这张表。
echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

/**
 * 净值曲线：策略（实线）与基准（虚线）**同一条 y 轴**。
 *
 * 绝不双轴——基准是同一尺度上的第二条线，不是第二个刻度。基准另用虚线做二次编码，
 * 不让色相单独承载识别。x 轴用 `category`（交易日等距），与 K 线的序数轴同口径：
 * 用时间轴会让周末与节假日被压缩，两张上下堆叠的图对同一天就落到不同的水平位置。
 */
export function EquityChart({ points }: { points: EquityPoint[] }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const frameRef = useRef<number | null>(null);

  // 实例活到 unmount，数据与主题都走增量。重建会丢掉用户的缩放位置，也会重放
  // ECharts 的入场动画（叠上主题的 150ms 颜色过渡就是闪一下空白）。
  // init 与 cleanup 严格对称，不写 `if (chartRef.current) return` 守卫——
  // 守卫在 effect 双跑时会让第二次挂载拿到一个已被销毁的实例。
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const chart = echarts.init(host);
    chartRef.current = chart;

    // ECharts 没有 autoSize，必须自己 resize。一帧内的多次触发合并成一次，
    // 回调里也不 setState——否则拖窗口会让整页重渲。
    const observer = new ResizeObserver(() => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        if (!chart.isDisposed()) chart.resize();
      });
    });
    observer.observe(host);

    return () => {
      observer.disconnect();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    // replaceMerge：重跑之后序列长度会变，默认的 merge 会残留旧点、把 y 轴范围撑大。
    // 颜色一律写在 option 字面值里（不用 init 的注册主题——那个只在 init 时生效一次），
    // 于是切主题就是再补一次 option，实例状态全部保留。
    chart.setOption(buildOption(points, chartTokens(isDark)), { replaceMerge: ["series"] });
  }, [points, isDark]);

  return <div ref={hostRef} className="absolute inset-0" />;
}

function buildOption(points: EquityPoint[], t: ChartTokens): EChartsCoreOption {
  return {
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 4, right: 14, top: 30, bottom: 4, containLabel: true },
    legend: {
      top: 2,
      right: 0,
      icon: "rect",
      itemWidth: 20,
      itemHeight: 2,
      textStyle: { color: t.ink2, fontSize: 12, fontFamily: CHART_FONT },
      data: ["策略", "基准"],
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: t.ink2, fontFamily: CHART_FONT } },
      backgroundColor: t.surface,
      borderColor: t.grid,
      borderWidth: 1,
      padding: [6, 10],
      textStyle: { color: t.ink, fontSize: 12, fontFamily: CHART_FONT },
      formatter: (params: unknown) => tooltipHtml(params, points, t),
    },
    xAxis: {
      type: "category",
      // 逐点对齐：equity_curve 的日期与 bars 的交易日严格一致（后端 zip(strict=True)）
      data: points.map((point) => point.date),
      boundaryGap: false,
      axisLine: { lineStyle: { color: t.grid } },
      axisTick: { show: false },
      axisLabel: { color: t.axis, fontSize: 11, fontFamily: CHART_FONT, hideOverlap: true },
    },
    yAxis: {
      type: "value",
      // 净值只在百万附近小幅波动，从 0 起画会把曲线压成一条直线
      scale: true,
      splitLine: { lineStyle: { color: t.grid } },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: t.axis, fontSize: 11, fontFamily: CHART_FONT, formatter: axisMoney },
    },
    series: [
      {
        name: "策略",
        type: "line",
        data: points.map((point) => point.equity),
        showSymbol: false,
        lineStyle: { width: 2, color: t.series1 },
        itemStyle: { color: t.series1 },
      },
      {
        name: "基准",
        type: "line",
        data: points.map((point) => point.benchmark),
        showSymbol: false,
        lineStyle: { width: 2, color: t.series2, type: "dashed" },
        itemStyle: { color: t.series2 },
      },
    ],
  };
}

function axisMoney(value: number): string {
  if (Math.abs(value) < 10000) return String(value);
  const wan = value / 10000;
  return `${wan.toFixed(Math.abs(value) >= 1000000 ? 0 : 1)}万`;
}

function tooltipHtml(params: unknown, points: EquityPoint[], t: ChartTokens): string {
  const first = (Array.isArray(params) ? params[0] : params) as { dataIndex?: number } | undefined;
  const point = points[first?.dataIndex ?? -1];
  if (!point) return "";

  return [
    `<div style="color:${t.ink2}">${point.date}</div>`,
    row("策略", amount(point.equity), t.series1),
    row("基准", amount(point.benchmark), t.series2),
    row("超额", amount(point.equity - point.benchmark, { signed: true }), null),
  ].join("");
}

/** 数值与序列名一律用墨色，识别靠左边的色块——文字不染序列色。 */
function row(label: string, value: string, swatch: string | null): string {
  const dot = swatch
    ? `<span style="display:inline-block;width:8px;height:2px;background:${swatch}"></span>`
    : `<span style="display:inline-block;width:8px"></span>`;
  return (
    `<div style="display:flex;align-items:center;gap:6px;margin-top:2px">` +
    `${dot}<span>${label}</span>` +
    `<span style="margin-left:auto;padding-left:16px">${value}</span></div>`
  );
}
