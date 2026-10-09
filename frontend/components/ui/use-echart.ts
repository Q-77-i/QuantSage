"use client";

import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/**
 * ECharts 实例的生命周期：**init 与 cleanup 严格对称**，数据与主题走增量。
 *
 * 两条都是被踩出来的，写在 `equity-chart.tsx` 里的是同一套（这次上提成 hook，
 * 新的三张图不再各抄一遍）：
 *
 * 1. **实例活到 unmount**。重建会丢掉用户的缩放位置，也会重放 ECharts 的入场动画。
 *    所以 init 只在挂载时做一次，cleanup 里 `dispose()` 并把 ref 清空。
 * 2. **绝不能把 init 与「数据变了要重画」写进同一个 effect**——那是本组件第一版的写法，
 *    结果是：另一个 `[]` effect 在 StrictMode 的「挂载→卸载→再挂载」里 dispose 了实例，
 *    而重画的那个 effect 从 ref 里拿到的是**已销毁的实例**，`setOption` 静默失效
 *    （控制台只有一行 `has been disposed`），画布压根没建出来。
 *    这里用 `isDisposed()` 守卫把那条路堵死。
 *
 * `option` 为 null 时不画（数据还没到）——组件据此渲染空盒子，与图表容器同高，不跳版。
 */
export function useEChart(
  hostRef: RefObject<HTMLDivElement | null>,
  option: EChartsCoreOption | null,
  onInit?: (chart: EChartsType) => void,
): RefObject<EChartsType | null> {
  const chartRef = useRef<EChartsType | null>(null);
  const frameRef = useRef<number | null>(null);
  const onInitRef = useRef(onInit);
  useEffect(() => {
    onInitRef.current = onInit;
  }, [onInit]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const chart = echarts.init(host, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    onInitRef.current?.(chart);

    // ECharts 没有 autoSize，必须自己 resize；一帧内的多次触发合并成一次
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
    // 只在挂载时建、卸载时拆——数据与主题的变化走下面那个 effect
  }, [hostRef]);

  useEffect(() => {
    const chart = chartRef.current;
    // StrictMode 的「挂载→卸载→再挂载」会让这里可能拿到已销毁的实例
    if (!chart || chart.isDisposed() || !option) return;
    chart.setOption(option, true);
  }, [option]);

  return chartRef;
}
