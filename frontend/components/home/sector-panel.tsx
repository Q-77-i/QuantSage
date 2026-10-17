"use client";

import { BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form-controls";
import { useEChart } from "@/components/ui/use-echart";
import { chartTokens } from "@/lib/chart-theme";
import { coverageText, sectorBars, sectorTable } from "@/lib/market-home";
import type { MarketSectors } from "@/lib/types";

// 按需引入。**漏注册任何一项都不抛错**——只在控制台留一行 warning，然后图缺一块或空白。
echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

/** 样本下限的可选档。默认 10：实测当日榜首是 22 只的行业，下限再低就全是小样本霸榜。 */
const MIN_SYMBOL_CHOICES = [10, 30, 100, 300] as const;

/** 每行条占的高度（px）。38 行 ≈ 600px——够密，又不至于把标签挤掉。 */
const ROW_HEIGHT = 17;
const MIN_CHART_HEIGHT = 260;
const MAX_CHART_HEIGHT = 640;

/**
 * 行业 / 概念板块块（M10a）。
 *
 * **为什么不是 treemap**（PRD/SPEC 的「热力图」字面在数据面前让了路）：PIT 累积归属是
 * **多对多**的——一只标的可同时挂「电子」「半导体」「AI算力」，实测 38 个行业的成交额
 * 合计是全市场的 **469%**。树图 / 堆叠条 / 饼图的语义都是「部分-整体」，在这里会画出一个
 * 「加起来是市场四倍多」的假整体。改用**排序的横向 diverging 条**（`choosing-a-form`：
 * above/below a baseline → diverging bar），并配上表格孪生。
 *
 * **覆盖率与样本数都必须看得见**：标签来自事件语料、非权威分类，且覆盖不均；不显示这两个
 * 数，这张图就在假装自己是一张完整的板块图。覆盖率在区块头上，样本数在每条右侧与表里。
 */
export function SectorPanel({
  sectors,
  loading,
  error,
  minSymbols,
  onMinSymbols,
  onPick,
}: {
  sectors: MarketSectors | null;
  loading: boolean;
  error: string | null;
  minSymbols: number;
  onMinSymbols: (next: number) => void;
  /** 点一条进该行业的标的列表（M10c）——成分口径与这里同一支 PIT 归属 */
  onPick: (industry: string) => void;
}) {
  const [asTable, setAsTable] = useState(false);
  const rows = useMemo(() => sectorBars(sectors), [sectors]);
  const table = useMemo(() => sectorTable(sectors), [sectors]);

  return (
    <Section
      title="行业 / 概念板块"
      hint={
        sectors?.trade_date
          ? `${sectors.trade_date} · 归属覆盖率 ${coverageText(sectors.coverage)}（按当日有价标的）`
          : undefined
      }
    >
      {/* 一行筛选管住下面的图（dataviz：filters in one row above the charts） */}
      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm" data-panel="sector-controls">
        <span className="text-xs text-ink-3">点一条看该行业的标的</span>
        <label className="flex items-center gap-2 text-xs text-ink-3">
          样本下限
          <Select
            aria-label="板块成分数下限"
            className="w-24"
            value={String(minSymbols)}
            onChange={(event) => onMinSymbols(Number(event.target.value))}
          >
            {MIN_SYMBOL_CHOICES.map((value) => (
              <option key={value} value={value}>
                ≥ {value} 只
              </option>
            ))}
          </Select>
        </label>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          onClick={() => setAsTable((value) => !value)}
        >
          {asTable ? "看图" : "看表格"}
        </Button>
        {rows.length ? <span className="text-xs text-ink-3">{rows.length} 个标签</span> : null}
      </div>

      {error ? (
        <p
          role="alert"
          className="mt-3 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      ) : loading && !sectors ? (
        <div className="mt-3 h-40 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
      ) : rows.length === 0 ? (
        <p className="mt-3 rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
          这个下限下没有板块——把样本下限调小些，或换一天看。
        </p>
      ) : asTable ? (
        <SectorTable rows={table} onPick={onPick} />
      ) : (
        <SectorChart rows={rows} onPick={onPick} />
      )}

      {sectors?.note ? (
        <details className="mt-3" data-panel="sector-notes">
          <summary className="cursor-pointer text-xs text-ink-3">口径说明</summary>
          <p className="mt-1 text-xs text-ink-3">{sectors.note}</p>
        </details>
      ) : null}
    </Section>
  );
}

// ── 图 ─────────────────────────────────────────────────────

function SectorChart({
  rows,
  onPick,
}: {
  rows: ReturnType<typeof sectorBars>;
  onPick: (industry: string) => void;
}) {
  const isDark = useTheme().resolvedTheme === "dark";
  const option = useMemo(() => buildOption(rows, chartTokens(isDark === true)), [rows, isDark]);
  // `onInit` 只在建图那一刻跑一次，之后换 handler **不会**对着已有实例重注册 ⇒
  // 必须「稳定 handler + ref 读最新值」（同 `grid-heatmap` 的写法）。
  const onPickRef = useRef(onPick);
  useEffect(() => {
    onPickRef.current = onPick;
  }, [onPick]);
  const onInit = useCallback((chart: EChartsType) => {
    chart.on("click", (params: unknown) => {
      const industry = (params as { data?: { industry?: string } }).data?.industry;
      if (typeof industry === "string") onPickRef.current(industry);
    });
  }, []);
  const { hostRef } = useEChart(option, onInit);

  return (
    <div
      ref={hostRef}
      data-chart="sectors"
      className="mt-3 w-full rounded-[var(--radius)] border border-border bg-chart-surface"
      style={{
        height: Math.min(MAX_CHART_HEIGHT, Math.max(MIN_CHART_HEIGHT, rows.length * ROW_HEIGHT + 56)),
      }}
      role="img"
      aria-label={`行业与概念板块按平均涨跌幅排序，共 ${rows.length} 个`}
    />
  );
}

function buildOption(
  rows: ReturnType<typeof sectorBars>,
  tokens: ReturnType<typeof chartTokens>,
): EChartsCoreOption {
  return {
    animation: false,
    // 横向条：类目在 y 轴，`inverse` 让第一名在最上面（否则 ECharts 从下往上排）
    grid: { left: 8, right: 76, top: 8, bottom: 24, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
    tooltip: {
      trigger: "item",
      backgroundColor: tokens.surface,
      borderColor: tokens.grid,
      textStyle: { color: tokens.ink, fontSize: 12 },
      formatter: (params: { dataIndex: number }) => {
        const row = rows[params.dataIndex];
        return `${row.industry}<br/>平均 ${row.label}<br/>${row.meta}`;
      },
    },
    xAxis: {
      type: "value",
      name: "平均涨跌幅（%）",
      nameLocation: "middle",
      nameGap: 22,
      nameTextStyle: { color: tokens.ink2, fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: tokens.axis,
        fontSize: 11,
        // 端点是 ±最大绝对值再放 8% 余量算出来的，不格式化会露出 -2.5400000000000005
        formatter: (value: number) => `${value.toFixed(1)}%`,
      },
      splitLine: { lineStyle: { color: tokens.grid, type: "solid" } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: rows.map((row) => row.industry),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: tokens.axis, fontSize: 11 },
    },
    series: [
      {
        type: "bar",
        // 条高 = 类目带的 60%，留出 2px 级别的表面色间隙（dataviz：相邻填充之间要留缝）
        barCategoryGap: "40%",
        data: rows.map((row) => ({
          value: row.value,
          industry: row.industry, // 点击回传用；ECharts 会把整条 data 项带进 params
          itemStyle: {
            color: tokens.divergePct[row.rampIndex],
            // 圆角只在**远离零线的那一端**（dataviz：圆角数据端锚在基线上）
            borderRadius: row.value >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
          },
        })),
        // 零线单独画：涨跌的分界本身有含义，不是普通网格线
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: tokens.axis, width: 1, type: "solid" },
          label: { show: false },
          data: [{ xAxis: 0 }],
        },
        // 条上**不写数字**（同 grid-heatmap 的口径：写满数字就成了表格的劣化复制），
        // 数值走 tooltip 与表格孪生
        label: { show: false },
      },
    ],
  };
}

// ── 表格孪生 ───────────────────────────────────────────────

function SectorTable({
  rows,
  onPick,
}: {
  rows: ReturnType<typeof sectorTable>;
  onPick: (industry: string) => void;
}) {
  return (
    <div className="mt-3 overflow-x-auto" data-table="sectors">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-ink-3">
            <th className="py-2 pr-3 font-normal">行业 / 概念</th>
            <th className="py-2 pr-3 text-right font-normal">成分数</th>
            <th className="py-2 pr-3 text-right font-normal">成交额（亿）</th>
            <th className="py-2 pr-3 text-right font-normal">平均涨跌幅</th>
            <th className="py-2 text-right font-normal">中位数</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.industry} className="border-b border-border/60 last:border-0">
              <td className="py-1.5 pr-3">
                <button
                  type="button"
                  className="underline-offset-2 hover:underline"
                  onClick={() => onPick(row.industry)}
                >
                  {row.industry}
                </button>
                {/* 均值与中位数差得远 ⇒ 这一档被少数极端值主导，值得多看一眼中位数 */}
                {row.skewed ? (
                  <span
                    className="ml-1.5 text-xs text-ink-3"
                    title="均值与中位数相差超过 1 个百分点：这一档被少数极端值主导"
                  >
                    偏斜
                  </span>
                ) : null}
              </td>
              <td className="num py-1.5 pr-3 text-right text-ink-2">{row.symbols}</td>
              <td className="num py-1.5 pr-3 text-right text-ink-2">{row.amount}</td>
              <td className="num py-1.5 pr-3 text-right">{row.avg}</td>
              <td className="num py-1.5 text-right text-ink-2">{row.median}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
