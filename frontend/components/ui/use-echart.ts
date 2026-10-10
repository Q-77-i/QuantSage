"use client";

import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/**
 * ECharts 实例的生命周期：**init 与 cleanup 严格对称**，数据与主题走增量。
 *
 * 两条都是被踩出来的（写在 `equity-chart.tsx` 里的是同一套）：
 *
 * 1. **实例活到宿主卸载为止**。重建会丢掉用户的缩放位置，也会重放 ECharts 的入场动画。
 *    所以只在「宿主出现」时 init 一次，宿主从 DOM 里消失时 dispose。
 * 2. **绝不能把 init 与「数据变了要重画」写进同一个 effect**——那是本组件第一版的写法，
 *    结果是：另一个 `[]` effect 在 StrictMode 的「挂载→卸载→再挂载」里 dispose 了实例，
 *    而重画的那个 effect 从 ref 里拿到的是**已销毁的实例**，`setOption` 静默失效
 *    （控制台只有一行 `has been disposed`），画布压根没建出来。用 `isDisposed()` 守卫堵死。
 *
 * ## 宿主必须「出现就建、消失就拆」，故这里用**回调 ref** 而不是 `RefObject`
 *
 * 早先的签名是 `useEChart(hostRef, option)`，host 是调用方 `useRef` 出来的对象——
 * 而 ref 对象的身份**永不变**，那个 effect 一辈子只跑一次，读到的是挂载那一刻的
 * `hostRef.current`。后果有两类，都真出现过（2026-10-10 界面验证逮到）：
 *
 *   * **宿主晚挂载**（「数据到了才渲染图」）：那一刻 ref 还是 `null`，`if (!host) return`
 *     直接出局，**实例永远不会建出来**——页面上静默少一张图，控制台干干净净；
 *   * **宿主被换掉再换回来**（`看表格 ⇄ 看图` 这类切换）：切走时宿主卸载，切回来时
 *     只挂了个新的空 div，实例同样不会重建——**图再也回不来**（因子页与优化页都中招）。
 *
 * 改成回调 ref（`<div ref={hostRef} />` 的写法**一个字都不用改**，React 会把元素交给它）
 * 之后，宿主出现/消失都变成一次 state 变化，两个 effect 自然跟着重跑。
 *
 * `option` 为 null 时不画（数据还没到）——组件据此渲染空盒子，与图表容器同高，不跳版。
 */
export function useEChart(
  option: EChartsCoreOption | null,
  onInit?: (chart: EChartsType) => void,
): { hostRef: (element: HTMLDivElement | null) => void; chartRef: RefObject<EChartsType | null> } {
  const chartRef = useRef<EChartsType | null>(null);
  const frameRef = useRef<number | null>(null);
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const onInitRef = useRef(onInit);
  useEffect(() => {
    onInitRef.current = onInit;
  }, [onInit]);

  useEffect(() => {
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
  }, [host]);

  useEffect(() => {
    const chart = chartRef.current;
    // StrictMode 的「挂载→卸载→再挂载」会让这里可能拿到已销毁的实例
    if (!chart || chart.isDisposed() || !option) return;
    chart.setOption(option, true);
    // `host` 也进依赖：宿主换回来时建的是**新实例**（空的），而 option 本身没变——
    // 不补这一笔，切回来就是一张空白画布
  }, [option, host]);

  return { hostRef: setHost, chartRef };
}
