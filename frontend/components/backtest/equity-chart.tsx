"use client";

import { LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useEffect, useImperativeHandle, useRef } from "react";
import type { Ref } from "react";

import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import type { ChartTokens } from "@/lib/chart-theme";
import type { ChartHandle } from "@/lib/chart-handle";
import {
  panByPixels,
  panRange,
  percentToRange,
  pinchFactor,
  rangeToPercent,
  zoomRange,
} from "@/lib/chart-gesture";
import type { IndexRange } from "@/lib/chart-gesture";
import { amount } from "@/lib/format";
import type { EquityPoint } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只会在控制台留一行 warning，然后图缺一块或空白，
// 所以集中在这里一次注册齐。加图表类型时记得同步这张表。
echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

/**
 * 净值曲线：策略（实线）与基准（虚线）**同一条 y 轴**。
 *
 * 绝不双轴——基准是同一尺度上的第二条线，不是第二个刻度。基准另用虚线做二次编码，
 * 不让色相单独承载识别。x 轴用 `category`（交易日等距），与 K 线的序数轴同口径：
 * 用时间轴会让周末与节假日被压缩，两张上下堆叠的图对同一天就落到不同的水平位置。
 *
 * 缩放（T6d）：**手势与 K 线同一条路径**——捏合（触控板，即 ctrl + wheel）缩放、
 * 横向滚轮平移、拖拽平移，竖直滚动始终归还页面（`onWheel` 里的理由）。另给一条常驻
 * slider 作为可发现的控件；拖手柄平移/缩放也走同一份序号状态。
 * 两图**各自独立**缩放，故缩放期间上述「同一天同一水平位置」不再成立，回到全览恢复。
 */
export function EquityChart({
  points,
  onZoomChange,
  ref,
}: {
  points: EquityPoint[];
  onZoomChange?: (zoomed: boolean) => void;
  ref?: Ref<ChartHandle>;
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const frameRef = useRef<number | null>(null);

  // 只在布尔**翻转**时上报（理由同 K 线：拖动期间逐帧 setState 会重渲染下半部两张表）
  const zoomedRef = useRef(false);
  // 可视区间的**序号口径**副本。滚轮逐帧都会触发，不能每帧去 getOption()（它会深拷贝整个 option）
  const rangeRef = useRef<IndexRange>({ from: 0, to: 0 });
  const onZoomChangeRef = useRef(onZoomChange);
  useEffect(() => {
    onZoomChangeRef.current = onZoomChange;
  }, [onZoomChange]);

  useImperativeHandle(ref, () => ({
    resetZoom: () =>
      chartRef.current?.dispatchAction({ type: "dataZoom", start: 0, end: 100 }),
  }), []);

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

  /**
   * 缩放与平移**全部自己接管**，与 K 线走同一条路径。
   *
   * 不能交给 ECharts 的 `dataZoom.inside`：它会把落在画布上的滚轮一律 `preventDefault`，
   * 于是竖直滚动滚不动页面（用户实机走查发现）。这里在宿主元素上捕获阶段拦下——
   * 竖直滚动只 `stopPropagation` 不让 ECharts 看见（默认行为不受影响，页面照常滚），
   * 捏合与横向滚动才真正处理。
   */
  useEffect(() => {
    const host = hostRef.current;
    const chart = chartRef.current;
    if (!host || !chart) return;

    const total = points.length;
    const apply = (range: IndexRange) => {
      // 无变化就不派发：dispatchAction 会触发一次重绘，全览态下每个滚轮事件都白闪一下
      const same =
        Math.abs(range.from - rangeRef.current.from) < 1e-6 &&
        Math.abs(range.to - rangeRef.current.to) < 1e-6;
      if (same) return;
      rangeRef.current = range;
      chart.dispatchAction({ type: "dataZoom", ...rangeToPercent(range, total) });
    };

    // 缩放态从事件载荷读，不调 getOption()——拖动时它每帧都会触发且会深拷贝整个 option
    const onDataZoom = (event: unknown) => {
      const payload = event as {
        start?: number;
        end?: number;
        batch?: { start?: number; end?: number }[];
      };
      const next = payload.batch?.[0] ?? payload;
      if (next.start === undefined || next.end === undefined) return;

      // 同步序号口径，让滑块拖动与滚轮走同一份状态
      rangeRef.current = percentToRange(next.start, next.end, total);

      const zoomed = next.start > 0.01 || next.end < 99.99;
      if (zoomed === zoomedRef.current) return;
      zoomedRef.current = zoomed;
      onZoomChangeRef.current?.(zoomed);
    };
    chart.on("datazoom", onDataZoom);

    // 新一轮回测回到全览：沿用上一次的缩放窗口会让人以为图没更新
    rangeRef.current = { from: 0, to: Math.max(0, total - 1) };
    chart.dispatchAction({ type: "dataZoom", start: 0, end: 100 });

    const onWheel = (event: WheelEvent) => {
      const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY);

      // 竖直滚动：既不平移也不缩放，只拦下不让 ECharts 吃掉默认行为
      if (!event.ctrlKey && !horizontal) {
        event.stopPropagation();
        return;
      }
      event.preventDefault();
      event.stopPropagation();

      const range = rangeRef.current;
      const box = host.getBoundingClientRect();
      if (event.ctrlKey) {
        const ratio = box.width > 0 ? (event.clientX - box.left) / box.width : 0.5;
        const anchor = range.from + ratio * (range.to - range.from);
        apply(zoomRange(range, anchor, pinchFactor(event.deltaY), total));
      } else {
        apply(panRange(range, panByPixels(event.deltaX, range.to - range.from, box.width), total));
      }
    };

    host.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      host.removeEventListener("wheel", onWheel, { capture: true });
      chart.off("datazoom", onDataZoom);
    };
  }, [points.length]);

  return <div ref={hostRef} className="absolute inset-0" />;
}

function buildOption(points: EquityPoint[], t: ChartTokens): EChartsCoreOption {
  return {
    animation: false,
    backgroundColor: "transparent",
    // bottom 给 slider 让位；ChartFrame 的高度是固定的，故从绘图区里扣
    grid: { left: 4, right: 14, top: 30, bottom: 30, containLabel: true },
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: 0,
        // 滚轮一律由宿主元素上的捕获阶段监听接管（见组件内的 `onWheel`）——
        // ECharts 的 inside 会把落在画布上的滚轮统统 preventDefault，竖直滚动因此滚不动页面。
        // 这里全关掉，只留 `moveOnMouseMove` 的拖拽平移。
        zoomOnMouseWheel: false,
        moveOnMouseWheel: false,
        moveOnMouseMove: true,
      },
      {
        type: "slider",
        xAxisIndex: 0,
        height: 16,
        bottom: 2,
        showDetail: false,
        brushSelect: false,
        borderColor: t.grid,
        backgroundColor: "transparent",
        // 选区用序列色淡染（8 位 hex 的末两位是 alpha），是唯一带色的 chrome
        fillerColor: `${t.series1}1f`,
        dataBackground: {
          lineStyle: { color: t.grid, width: 1 },
          areaStyle: { color: "transparent" },
        },
        selectedDataBackground: {
          lineStyle: { color: t.axis, width: 1 },
          areaStyle: { color: "transparent" },
        },
        handleStyle: { color: t.surface, borderColor: t.axis, borderWidth: 1 },
        moveHandleStyle: { color: t.grid },
        textStyle: { color: t.axis, fontSize: 10, fontFamily: CHART_FONT },
      },
    ],
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
