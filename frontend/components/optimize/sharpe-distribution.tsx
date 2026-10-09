"use client";

import { GridComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import { ScatterChart } from "echarts/charts";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useMemo, useRef } from "react";

import { useEChart } from "@/components/ui/use-echart";
import { chartTokens } from "@/lib/chart-theme";
import { distributionFromGrid } from "@/lib/optimize-matrix";
import { paramText } from "./dsr-card";
import type { OptimizeSummary } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只在控制台留一行 warning，然后图缺一块或空白。
echarts.use([ScatterChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

/**
 * 全网格夏普分布：**一片中性灰点 + 一个红点**（dataviz 的 emphasis 形态）。
 *
 * 不复用热力图的 diverging 色阶给每个点按值上色——那是把「位置已经表达了的东西」
 * 再用颜色说一遍，白花掉唯一的自由通道；这一页要回答的是「我挑的那个是不是特例」，
 * 所以只有被选中者着色，其余全是背景。
 *
 * y 轴只用来把**同值的点错开**（错位层，纯函数里算好），故刻度与轴线全部隐藏——
 * 它不承载任何信息，画出来只会让人以为纵向有意义。
 */
export function SharpeDistribution({ summary }: { summary: OptimizeSummary }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);

  const payload = distributionFromGrid(summary);
  const option = useMemo(() => buildOption(payload, chartTokens(isDark)), [payload, isDark]);
  useEChart(hostRef, option);

  return (
    <div
      ref={hostRef}
      className="mt-3 h-[220px] w-full rounded-[var(--radius)] border border-border bg-chart-surface"
      role="img"
      aria-label={`全网格夏普分布，共 ${payload.count} 个有效格`}
    />
  );
}

function buildOption(
  payload: ReturnType<typeof distributionFromGrid>,
  tokens: ReturnType<typeof chartTokens>,
): EChartsCoreOption {
  const positive = tokens.diverge[0]; // 红端 = 正夏普（与热力图同一支语义）
  const rest = tokens.axis; // 背景点：用轴灰，明确「不是数据色」

  return {
    animation: false,
    grid: { left: 12, right: 16, top: 28, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "item",
      backgroundColor: tokens.surface,
      borderColor: tokens.grid,
      textStyle: { color: tokens.ink, fontSize: 12 },
      formatter: (params: { data: { value: [number, number]; params: Record<string, number> } }) =>
        `${paramText(params.data.params)}<br/>夏普 ${params.data.value[0].toFixed(4)}`,
    },
    xAxis: {
      type: "value",
      min: payload.min,
      max: payload.max,
      name: "夏普",
      nameLocation: "end",
      nameGap: 6,
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { lineStyle: { color: tokens.grid } },
      axisTick: { show: false },
      // 端点会落在数据的极值上（min/max 就是 ±8% 余量算出来的），不格式化会露出
      // `-0.1534396341257613` 这种全精度浮点——截图里看出来的
      axisLabel: { color: tokens.axis, fontSize: 11, formatter: (value: number) => value.toFixed(2) },
      splitLine: { lineStyle: { color: tokens.grid, type: "solid" } },
    },
    // 错位层：不打刻度、不画轴线——纵向没有含义
    yAxis: {
      type: "value",
      show: false,
      min: -0.6,
      max: Math.max(1.4, ...payload.points.map((point) => point.value[1] + 0.6)),
    },
    series: [
      {
        type: "scatter",
        symbolSize: 10, // >= 8px（dataviz 的 marker 下限）
        data: payload.points.map((point) => ({
          value: point.value,
          params: point.params,
          itemStyle: {
            color: point.best ? positive : rest,
            // 2px 表面色描边：点重叠时仍分得开（用留白分隔，不画边框把标记圈起来）
            borderColor: tokens.surface,
            borderWidth: 2,
          },
          ...(point.best
            ? {
                label: {
                  show: true,
                  position: "top",
                  distance: 6,
                  color: tokens.ink,
                  fontSize: 11,
                  formatter: `最优 ${paramText(point.params)}`,
                },
              }
            : {}),
        })),
        // 零线：夏普的分界本身有含义，画成实线细线并直接标「0」（不是虚线网格）
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: tokens.axis, width: 1, type: "solid" },
          label: { formatter: "0", color: tokens.ink2, fontSize: 10, position: "insideEndTop" },
          data: [{ xAxis: 0 }],
        },
        emphasis: { scale: 1.4 },
        z: 2,
      },
      // 命中层：把 hover 的目标做到 ~24px（dataviz：8px 的点不该要求精确命中）。
      // 与点集**同一份数据**，只是画成透明——点本身仍是 10px，看得见的没变大。
      {
        type: "scatter",
        symbolSize: 24,
        itemStyle: { color: "transparent" },
        data: payload.points.map((point) => ({ value: point.value, params: point.params })),
        z: 3,
      },
    ],
  };
}
