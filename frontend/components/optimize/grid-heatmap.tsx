"use client";

import { HeatmapChart, LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useEChart } from "@/components/ui/use-echart";
import { chartTokens } from "@/lib/chart-theme";
import { heatmapFromGrid, lineFromGrid, sharpOf } from "@/lib/optimize-matrix";
import { paramText } from "./dsr-card";
import type { OptimizeAxis, OptimizeCell, OptimizeSummary } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只在控制台留一行 warning，然后图缺一块或空白。
echarts.use([HeatmapChart, LineChart, GridComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

/** 一格的展示值：夏普四位，缺值给「—」（**不补 0**） */
function cellText(cell: { ok: boolean; metrics?: { sharpe: number | null } } | null): string {
  if (!cell || !cell.ok) return "—";
  const sharpe = cell.metrics?.sharpe ?? null;
  return sharpe === null ? "—" : sharpe.toFixed(3);
}

/**
 * 网格结果：**二维出热力图、一维出折线**（SPEC §6 M5b）。
 *
 * 三件事按 dataviz 的规矩来：
 *
 * 1. **色阶是 diverging 且对称于 0**——夏普有正负、0 有含义，中灰必须落在 0 上，
 *    拿 min/max 当域会让「中灰」跑到数据中点去（纯函数里已按 |最大| 取对称半径）；
 * 2. **格上不标数字**——25 个数字糊成一片谁也不读；数值由色标、tooltip 与
 *    **表格视图**承载。只有最优格被圈出来（「标极端值」是允许的直接标注）；
 * 3. **连续色标必须有表格孪生**（dataviz 的反模式清单里唯一一条无障碍硬要求），
 *    所以「表格」不是可选的附加物，是这个组件的另一半。
 *
 * 失败格在图上**留空**（`missing` 计数在提示行里如实说出来），点击它不会触发重跑。
 */
export function GridHeatmap({
  summary,
  axes,
  onPick,
  picking,
}: {
  summary: OptimizeSummary;
  axes: OptimizeAxis[];
  onPick: (cellIndex: number) => void;
  picking: number | null;
}) {
  const [asTable, setAsTable] = useState(false);
  const heatmap = useMemo(() => heatmapFromGrid(summary, axes), [summary, axes]);
  const line = useMemo(() => lineFromGrid(summary, axes), [summary, axes]);

  const hint = (
    <span className="flex items-center gap-3">
      <span>点格可重跑该格</span>
      <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setAsTable((v) => !v)}>
        {asTable ? "看图" : "看表格"}
      </Button>
    </span>
  );

  if (!heatmap && !line) return null;

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-xs text-ink-3">{hint}</p>
        {heatmap && heatmap.missing > 0 ? (
          <p className="text-xs text-ink-3">{heatmap.missing} 格没有值（失败或缺失），图上留空</p>
        ) : null}
      </div>

      {asTable || !heatmap ? (
        <GridView summary={summary} axes={axes} onPick={onPick} picking={picking} />
      ) : (
        <Heatmap summary={summary} axes={axes} onPick={onPick} picking={picking} />
      )}
    </div>
  );
}

// ── 热力图 ─────────────────────────────────────────────────

function Heatmap({
  summary,
  axes,
  onPick,
  picking,
}: {
  summary: OptimizeSummary;
  axes: OptimizeAxis[];
  onPick: (cellIndex: number) => void;
  picking: number | null;
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);
  const onPickRef = useRef(onPick);
  useEffect(() => {
    onPickRef.current = onPick;
  }, [onPick]);

  const payload = heatmapFromGrid(summary, axes);
  const option = useMemo(
    () => (payload ? buildOption(payload, chartTokens(isDark)) : null),
    [payload, isDark],
  );
  // 点击 → 重跑那一格。下标**挂在数据项里**（`data.index`），不做标签串反查
  const onInit = useCallback((chart: EChartsType) => {
    chart.on("click", (params) => {
      const index = (params as { data?: { index?: number } }).data?.index;
      if (typeof index === "number") onPickRef.current(index);
    });
  }, []);
  useEChart(hostRef, option, onInit);

  if (!payload) return null;
  const best = summary.best_index;

  return (
    <>
      <div
        ref={hostRef}
        className="mt-2 h-[380px] w-full rounded-[var(--radius)] border border-border bg-chart-surface"
        role="img"
        aria-label={`参数网格热力图：${payload.xParam} × ${payload.yParam}，共 ${payload.points.length} 格`}
      />
      <p className="mt-1.5 text-xs text-ink-3">
        行 {payload.yParam} · 列 {payload.xParam} · 色阶固定对称于 0（红正蓝负）
        {picking !== null ? " · 正在重跑选中的那一格…" : ""}
        {best !== null && summary.cells[best]
          ? ` · 最优 ${paramText(summary.cells[best].params)}（夏普 ${cellText(summary.cells[best])}）`
          : ""}
      </p>
    </>
  );
}

function buildOption(
  payload: NonNullable<ReturnType<typeof heatmapFromGrid>>,
  tokens: ReturnType<typeof chartTokens>,
): EChartsCoreOption {
  return {
    animation: false,
    grid: { left: 8, right: 16, top: 12, bottom: 56, containLabel: true },
    tooltip: {
      trigger: "item",
      backgroundColor: tokens.surface,
      borderColor: tokens.grid,
      textStyle: { color: tokens.ink, fontSize: 12 },
      formatter: (params: { data: { value: [number, number, number]; params: Record<string, number> } }) =>
        `${paramText(params.data.params)}<br/>夏普 ${params.data.value[2].toFixed(4)}` +
        `<br/><span style="font-size:11px">点击重跑这一格</span>`,
    },
    xAxis: {
      type: "category",
      data: payload.xLabels,
      name: payload.xParam,
      nameLocation: "middle",
      nameGap: 26,
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { lineStyle: { color: tokens.grid } },
      axisTick: { show: false },
      axisLabel: { color: tokens.axis, fontSize: 11 },
      splitArea: { show: false },
    },
    yAxis: {
      type: "category",
      data: payload.yLabels,
      name: payload.yParam,
      nameLocation: "middle",
      nameGap: 30,
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { lineStyle: { color: tokens.grid } },
      axisTick: { show: false },
      axisLabel: { color: tokens.axis, fontSize: 11 },
      splitArea: { show: false },
    },
    // 连续色标：**对称域**（见纯函数），色阶取本模式的 diverging 七档
    visualMap: {
      type: "continuous",
      min: -payload.max,
      max: payload.max,
      calculable: true,
      orient: "horizontal",
      left: "center",
      bottom: 4,
      itemWidth: 12,
      itemHeight: 110,
      precision: 2,
      text: ["正", "负"],
      textStyle: { color: tokens.ink2, fontSize: 11 },
      inRange: { color: tokens.diverge },
    },
    series: [
      {
        type: "heatmap",
        // `index` 是**格在整批里的下标**，点击时直接用它去重跑——不经过标签串反查
        data: payload.points.map(([x, y, value], i) => ({
          value: [x, y, value],
          index: payload.cellIndex[i],
          params: paramsAt(payload, x, y),
        })),
        // 2px 表面色间隙分隔相邻格（不是给每格描边——那是 dataviz 明令的反模式）
        itemStyle: { borderColor: tokens.surface, borderWidth: 2 },
        // 最优格：一圈墨色描边（标极端值）。**不写数字**——见组件 docstring 第 2 条
        emphasis: { itemStyle: { borderColor: tokens.ink, borderWidth: 3 } },
        z: 2,
      },
    ],
  };
}

/** 把 `[x, y]` 还原成该格的参数（tooltip 要显示 `fast=8, slow=15`） */
function paramsAt(
  payload: NonNullable<ReturnType<typeof heatmapFromGrid>>,
  x: number,
  y: number,
): Record<string, number> {
  return {
    [payload.xParam]: Number(payload.xLabels[x]),
    [payload.yParam]: Number(payload.yLabels[y]),
  };
}

// ── 表格视图（连续色标的无障碍孪生）────────────────────────

/**
 * 与热力图**同一份数**的表格。行 = 第二条轴、列 = 第一条轴，与热力图同向。
 *
 * 「看表格」不是附加物：连续色标上颜色是唯一的编码通道，而颜色对色觉障碍、
 * 灰度打印、以及屏幕阅读器都不成立——表格是那个「任何人都读得到」的等价物。
 */
function GridView({
  summary,
  axes,
  onPick,
  picking,
}: {
  summary: OptimizeSummary;
  axes: OptimizeAxis[];
  onPick: (cellIndex: number) => void;
  picking: number | null;
}) {
  const byParams = useMemo(() => {
    const map = new Map<string, { index: number; cell: OptimizeCell }>();
    for (const cell of summary.cells.filter(Boolean)) {
      map.set(JSON.stringify(axisKeys(axes, cell.params)), { index: cell.index, cell });
    }
    return map;
  }, [summary, axes]);

  const xValues = axes[0].values;
  const rows = axes.length === 2 ? axes[1].values : [null];

  return (
    <div className="mt-2 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">
          参数网格的夏普值，行是 {axes.length === 2 ? axes[1].param : "—"}、列是 {axes[0].param}
        </caption>
        <thead>
          <tr>
            <th className="border-b border-border px-2 py-1.5 text-left text-xs font-normal text-ink-3">
              {axes.length === 2 ? `${axes[1].param} ↓ / ${axes[0].param} →` : axes[0].param}
            </th>
            {xValues.map((value) => (
              <th key={value} className="num border-b border-border px-2 py-1.5 text-right text-xs font-normal text-ink-3">
                {value}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((rowValue) => (
            <tr key={String(rowValue)}>
              <th className="num border-b border-border/60 px-2 py-1.5 text-left font-normal text-ink-2">
                {rowValue ?? summary.cells[0]?.symbol ?? "—"}
              </th>
              {xValues.map((xValue) => {
                const key = JSON.stringify(axes.length === 2 ? [xValue, rowValue] : [xValue]);
                const hit = byParams.get(key);
                return (
                  <td key={xValue} className="num border-b border-border/60 px-2 py-1.5 text-right">
                    {hit && hit.cell.ok ? (
                      <button
                        type="button"
                        onClick={() => onPick(hit.index)}
                        disabled={picking !== null}
                        className="rounded-[var(--radius)] px-1.5 py-0.5 hover:bg-muted disabled:opacity-50"
                        title="点击重跑这一格"
                      >
                        {cellText(hit.cell)}
                      </button>
                    ) : (
                      cellText(hit?.cell ?? null)
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 只取轴上的参数键——用来在 cells 里定位某一格 */
function axisKeys(axes: OptimizeAxis[], params: Record<string, number>): number[] {
  return axes.map((axis) => params[axis.param]);
}

// ── 一维网格：折线 ─────────────────────────────────────────

/**
 * 一维参数网格走折线（热力图需要两个维度）。
 *
 * 用折线而不是柱：横轴是**有序的参数值**（周期数一类），折线表达「随参数变化的趋势」，
 * 柱状表达「离散类别的比较」——这里要读的是前者。最优点加一个实心标记 + 直接标注。
 */
export function GridLine({ summary, axes }: { summary: OptimizeSummary; axes: OptimizeAxis[] }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const hostRef = useRef<HTMLDivElement>(null);
  const payload = lineFromGrid(summary, axes);
  const option = useMemo(
    () => (payload ? buildLineOption(payload, chartTokens(isDark)) : null),
    [payload, isDark],
  );
  useEChart(hostRef, option);

  if (!payload) return null;
  return (
    <div
      ref={hostRef}
      className="mt-2 h-[300px] w-full rounded-[var(--radius)] border border-border bg-chart-surface"
      role="img"
      aria-label={`一维参数网格的夏普折线：${payload.param}`}
    />
  );
}

/**
 * 一维参数网格走折线（热力图需要两个维度）。
 *
 * 用折线而不是柱：横轴是**有序的参数值**（周期数一类），折线表达「随参数变化的趋势」，
 * 柱状表达「离散类别的比较」——这里要读的是前者。最优点加一个实心标记 + 直接标注。
 */
function buildLineOption(
  payload: NonNullable<ReturnType<typeof lineFromGrid>>,
  tokens: ReturnType<typeof chartTokens>,
): EChartsCoreOption {
  return {
    animation: false,
    grid: { left: 8, right: 24, top: 24, bottom: 12, containLabel: true },
    tooltip: {
      trigger: "axis",
      backgroundColor: tokens.surface,
      borderColor: tokens.grid,
      textStyle: { color: tokens.ink, fontSize: 12 },
    },
    xAxis: {
      type: "category",
      data: payload.labels,
      name: payload.param,
      nameLocation: "middle",
      nameGap: 24,
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { lineStyle: { color: tokens.grid } },
      axisTick: { show: false },
      axisLabel: { color: tokens.axis, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      name: "夏普",
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { show: false },
      axisLabel: { color: tokens.axis, fontSize: 11 },
      splitLine: { lineStyle: { color: tokens.grid, type: "solid" } },
    },
    series: [
      {
        type: "line",
        data: payload.values,
        connectNulls: false, // 缺值处断开——连起来会画出没有的区间
        lineStyle: { width: 2, color: tokens.series1 },
        itemStyle: { color: tokens.series1, borderColor: tokens.surface, borderWidth: 2 },
        symbolSize: 10,
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: tokens.axis, width: 1, type: "solid" },
          label: { formatter: "0", color: tokens.ink2, fontSize: 10 },
          data: [{ yAxis: 0 }],
        },
        ...(payload.bestIndex !== null && payload.values[payload.bestIndex] !== null
          ? {
              markPoint: {
                symbolSize: 12,
                itemStyle: { color: tokens.diverge[0] }, // 与热力图的正极同色：被选中的那个
                label: {
                  color: tokens.ink,
                  fontSize: 11,
                  position: "top",
                  formatter: `最优 ${payload.labels[payload.bestIndex]}`,
                },
                data: [{ coord: [payload.bestIndex, payload.values[payload.bestIndex]] }],
              },
            }
          : {}),
      },
    ],
  };
}

/** 供页面判断用：这次运行的一格里有没有值（决定要不要渲染结果区） */
export function hasAnySharpe(summary: OptimizeSummary): boolean {
  // `cells` 在运行中带空洞：`sharpOf` 已对 undefined 返 null，这里不必先过滤
  return summary.cells.some((cell) => sharpOf(cell) !== null);
}
