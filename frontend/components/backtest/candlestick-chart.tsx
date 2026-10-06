"use client";

import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from "lightweight-charts";
import { useTheme } from "next-themes";
import { useEffect, useImperativeHandle, useMemo, useRef } from "react";
import type { Ref } from "react";

import { CHART_FONT, chartTokens } from "@/lib/chart-theme";
import { isFullView, pinchFactor, zoomRange } from "@/lib/chart-gesture";
import type { ChartHandle } from "@/lib/chart-handle";
import { buildMarkers } from "@/lib/markers";
import type { Bar, OpenPosition, Trade } from "@/lib/types";

/**
 * K 线 + 买卖点。
 *
 * 取数窗口由调用方按 `report.meta.start/end` 取，所以「买卖点全挤在右端」不会发生——
 * 图的横轴范围与回测区间本就一致。复权口径也必须与回测同为 qfq，否则价格对不上而
 * marker 仍然照落，属于**不报错但图是另一口径**。
 *
 * v5 的 markers 是独立图元：`createSeriesMarkers` 取一次常驻句柄，之后只 `setMarkers`；
 * 重复调用工厂会叠加图元。marker 的 `time` 必须与某根 bar 精确相等且整组升序，
 * 否则**静默丢点**——映射已在 `lib/markers.ts` 做成纯函数并单测。
 *
 * 手势（T6d）：库默认把 `deltaY` 当缩放（`handleScale.mouseWheel`），于是触控板
 * 双指上下滑会缩放而不是滚页面。这里关掉它，改由**捏合**驱动——见 `onWheel`。
 */
export function CandlestickChart({
  bars,
  trades,
  openPosition,
  onZoomChange,
  ref,
}: {
  bars: Bar[];
  trades: Trade[];
  openPosition: OpenPosition | null;
  onZoomChange?: (zoomed: boolean) => void;
  ref?: Ref<ChartHandle>;
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const tokens = chartTokens(isDark);

  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // 只在布尔**翻转**时上报：拖动期间逐帧 setState 会把下半部的两张表一起重渲染
  const zoomedRef = useRef(false);
  const onZoomChangeRef = useRef(onZoomChange);
  useEffect(() => {
    onZoomChangeRef.current = onZoomChange;
  }, [onZoomChange]);

  useImperativeHandle(ref, () => ({
    resetZoom: () => chartRef.current?.timeScale().fitContent(),
  }), []);

  // 配色是 marker 生成时烘进每个点里的（见下），所以主题一变就得重算整组
  const markers = useMemo(
    () => buildMarkers(trades, openPosition, bars, { up: tokens.up, down: tokens.down }),
    [trades, openPosition, bars, tokens],
  );

  // 实例活到 unmount（理由同净值曲线）。resize 交给 `autoSize`，**不要再挂第二个
  // ResizeObserver**：库内部已经在管，且它会与显式的 width/height 冲突。
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const chart = createChart(host, {
      autoSize: true,
      // deltaY 不再缩放：竖直滚动归还页面。deltaX 的平移仍归 `handleScroll.mouseWheel`
      handleScale: { mouseWheel: false },
    });
    const series = chart.addSeries(CandlestickSeries, {});
    seriesRef.current = series;
    chartRef.current = chart;
    markersRef.current = createSeriesMarkers(series, []);

    return () => {
      markersRef.current = null;
      seriesRef.current = null;
      chartRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    series.setData(
      bars.map((bar) => ({
        time: bar.time as Time, // "YYYY-MM-DD" 即 business day 口径，与后端一致
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );
    // 每次换数据都回到全览：默认视窗若只显示末段，早段的买卖点会「在数据里但看不见」
    chartRef.current?.timeScale().fitContent();
  }, [bars]);

  useEffect(() => {
    markersRef.current?.setMarkers(markers);
  }, [markers]);

  // 捏合缩放 + 缩放态上报。两者都依赖画布尺寸与数据量，故与图表实例分开管理
  useEffect(() => {
    const host = hostRef.current;
    const chart = chartRef.current;
    if (!host || !chart) return;
    const timeScale = chart.timeScale();

    const report = () => {
      const range = timeScale.getVisibleLogicalRange();
      const zoomed = range !== null && !isFullView(range, bars.length);
      if (zoomed === zoomedRef.current) return;
      zoomedRef.current = zoomed;
      onZoomChangeRef.current?.(zoomed);
    };

    // LWC v5 没有 zoomIn/zoomOut，只能自己算区间再 set。捏合在浏览器里就是
    // ctrl + wheel，故只接管带 ctrlKey 的轮子，其余（竖直/水平滚动）一律放行。
    // 捕获阶段 + stopPropagation：库自己的处理器挂在画布上，必须先拦下来。
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      const range = timeScale.getVisibleLogicalRange();
      if (!range) return;
      event.preventDefault();
      event.stopPropagation();

      const anchor =
        timeScale.coordinateToLogical(event.clientX - host.getBoundingClientRect().left) ??
        (range.from + range.to) / 2;
      timeScale.setVisibleLogicalRange(
        zoomRange(range, anchor, pinchFactor(event.deltaY), bars.length),
      );
    };

    host.addEventListener("wheel", onWheel, { passive: false, capture: true });
    timeScale.subscribeVisibleLogicalRangeChange(report);
    report(); // 换数据后补报一次：若新区间恰好仍是全览，订阅不触发会让按钮状态滞留

    return () => {
      host.removeEventListener("wheel", onWheel, { capture: true });
      timeScale.unsubscribeVisibleLogicalRangeChange(report);
    };
  }, [bars.length]);

  // 主题不重建，走 applyOptions 补丁——重建会把用户的缩放平移位置一起清掉
  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: {
        background: { color: tokens.surface },
        textColor: tokens.ink2,
        fontFamily: CHART_FONT,
      },
      grid: {
        vertLines: { color: tokens.grid },
        horzLines: { color: tokens.grid },
      },
      rightPriceScale: { borderColor: tokens.grid },
      timeScale: { borderColor: tokens.grid },
      crosshair: {
        horzLine: { labelBackgroundColor: tokens.ink2 },
        vertLine: { labelBackgroundColor: tokens.ink2 },
      },
    });

    seriesRef.current?.applyOptions({
      // 阳线空心（只描边）、阴线实心：A 股口径，且让形状本身编码方向，
      // 红绿在色盲下不可分（实测 deutan ΔE 11.6），不能独自承载信息
      upColor: "transparent",
      borderUpColor: tokens.up,
      wickUpColor: tokens.up,
      downColor: tokens.down,
      borderDownColor: tokens.down,
      wickDownColor: tokens.down,
      borderVisible: true,
    });
  }, [tokens]);

  return <div ref={hostRef} className="absolute inset-0" />;
}
