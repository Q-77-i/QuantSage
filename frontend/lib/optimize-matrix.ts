/**
 * 网格结果 → 图表的**数据**（纯函数）。
 *
 * 选项（option）在组件里拼，这里只产出「画什么」——理由与 `lib/markers.ts` /
 * `lib/equity-curve.ts` 相同：**图表组件一 import 就把 ECharts 拉进用例**，
 * 而这些映射恰恰是最容易算错、又最难在浏览器里逐点核对的部分（Vitest 只测纯函数）。
 *
 * 三块数据的形态按 dataviz 的判据选（见 design brief §一）：
 *   · 二维参数格 → **热力图**，色阶是 diverging（夏普有正负，0 有含义）
 *   · 全网格分布 → **强调**（一片中性灰点 + 一个红点），不是分类配色
 *   · 批量 → **表格**，不是图
 */

import type { OptimizeAxis, OptimizeCell, OptimizeSummary } from "./types";

/**
 * 运行中的 `cells` 数组**带空洞**（逐格填充，未到达的槽位是 `undefined`）。
 *
 * 这不是「脏数据」而是这一页的常态：热力图要的就是边跑边长出来。故本模块所有
 * 遍历入口都先过 `present()` —— 未到达的格按「还没有值」处理，与**失败格**同一条路。
 * （第一版没做这件事，结果是运行中整页白屏：`sharpOf(undefined)` 直接抛。）
 */
function present(cells: OptimizeCell[]): OptimizeCell[] {
  return cells.filter(Boolean);
}

/** 热力图的一格。ECharts 的 heatmap 吃 `[x下标, y下标, 值]`，不是坐标值 */
export type HeatmapPoint = [number, number, number];

export interface HeatmapPayload {
  points: HeatmapPoint[];
  /**
   * 与 `points` **逐位对应**的格下标。
   *
   * 点格重跑要用它。第一版是拿「轴值字符串」反查的，那要在组件里再存一份映射表、
   * 还得防着标签重复——直接把下标挂在数据里，点击事件拿到什么就是什么。
   */
  cellIndex: number[];
  /** 轴标签（按请求里轴值的书写顺序，不排序——顺序是契约的一部分） */
  xLabels: string[];
  yLabels: string[];
  xParam: string;
  yParam: string;
  /**
   * 对称色域 `[-max, +max]`。**必须对称**：diverging 的中点要落在 0 上，
   * 拿 min/max 直接当域会让「中灰」跑到数据中点去，色阶就不再表示正负。
   */
  max: number;
  /** 最优格在网格里的下标（`[x, y]`）；无最优或不在网格里时为 null */
  best: [number, number] | null;
  /** 没有值的格（失败 / 该参数组合缺失）——图上留空，**不补 0** */
  missing: number;
}

function axisIndex(labels: string[], value: number): number {
  const text = String(value);
  return labels.indexOf(text);
}

/** 轴值 → 标签。用参数值的字符串形式，与请求里写的那个数一致 */
export function axisLabels(axis: OptimizeAxis): string[] {
  return axis.values.map((value) => String(value));
}

/**
 * 二维网格 → 热力图数据。**一维网格返回 null**——那不是热力图的形态（用 `lineFromGrid`）。
 */
export function heatmapFromGrid(summary: OptimizeSummary, axes: OptimizeAxis[]): HeatmapPayload | null {
  if (axes.length !== 2) return null;
  const [xAxis, yAxis] = axes;
  const xLabels = axisLabels(xAxis);
  const yLabels = axisLabels(yAxis);

  const points: HeatmapPoint[] = [];
  const cellIndex: number[] = [];
  let missing = 0;
  for (const cell of present(summary.cells)) {
    const value = sharpOf(cell);
    const x = axisIndex(xLabels, cell.params[xAxis.param]);
    const y = axisIndex(yLabels, cell.params[yAxis.param]);
    if (x < 0 || y < 0 || value === null) {
      missing += 1;
      continue;
    }
    points.push([x, y, value]);
    cellIndex.push(cell.index);
  }

  const values = points.map((point) => point[2]);
  // 至少留 0.01 的半径：全格同值时 domain 为 0 会让 visualMap 退化
  const max = Math.max(0.01, ...values.map((value) => Math.abs(value)));

  const bestCell = summary.best_index === null ? null : summary.cells[summary.best_index];
  let best: [number, number] | null = null;
  if (bestCell) {
    const x = axisIndex(xLabels, bestCell.params[xAxis.param]);
    const y = axisIndex(yLabels, bestCell.params[yAxis.param]);
    if (x >= 0 && y >= 0) best = [x, y];
  }

  return {
    points,
    cellIndex,
    xLabels,
    yLabels,
    xParam: xAxis.param,
    yParam: yAxis.param,
    max,
    best,
    missing,
  };
}

export function sharpOf(cell: OptimizeCell | undefined | null): number | null {
  return cell?.ok ? (cell.metrics?.sharpe ?? null) : null;
}

/** 一维网格：按轴值顺序取点（y = 夏普），用于折线 */
export interface LinePayload {
  param: string;
  labels: string[];
  values: (number | null)[];
  bestIndex: number | null;
}

export function lineFromGrid(summary: OptimizeSummary, axes: OptimizeAxis[]): LinePayload | null {
  if (axes.length !== 1) return null;
  const [axis] = axes;
  const labels = axisLabels(axis);
  const byValue = new Map<number, OptimizeCell>();
  for (const cell of present(summary.cells)) byValue.set(cell.params[axis.param], cell);

  const values = axis.values.map((value) => {
    const cell = byValue.get(value);
    return cell ? sharpOf(cell) : null;
  });
  const bestCell = summary.best_index === null ? null : summary.cells[summary.best_index];
  return {
    param: axis.param,
    labels,
    values,
    bestIndex: bestCell ? axis.values.indexOf(bestCell.params[axis.param]) : null,
  };
}

// ── 分布（强调形态）────────────────────────────────────────

export interface DistributionPayload {
  /** 每个有效格一个点：[夏普, 该点在同值上的错位层] */
  points: { value: [number, number]; params: Record<string, number>; index: number; best: boolean }[];
  /** 坐标轴范围（留 8% 余量，免得端点贴边） */
  min: number;
  max: number;
  /** 有效格数（= 点数）；失败格不参与 */
  count: number;
}

/**
 * 全网格夏普分布：**一片中性灰点 + 一个红点**（dataviz 的 emphasis 形态）。
 *
 * y 轴只用来把同值的点错开（错位层），不承载信息——故 y 轴刻度全部隐藏。
 */
export function distributionFromGrid(summary: OptimizeSummary): DistributionPayload {
  const rows = present(summary.cells)
    .map((cell) => ({ index: cell.index, value: sharpOf(cell), params: cell.params }))
    .filter((row): row is { index: number; value: number; params: Record<string, number> } => row.value !== null);

  const values = rows.map((row) => row.value);
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const pad = Math.max((max - min) * 0.08, 0.01);

  // 同一个夏普值上的点会完全重叠——按出现次序给它们不同的错位层，
  // 让「有几个格落在这里」看得出来（这也是散点图最常见的假象来源）
  const stacks = new Map<string, number>();
  const points = rows.map((row) => {
    const key = row.value.toFixed(6);
    const layer = stacks.get(key) ?? 0;
    stacks.set(key, layer + 1);
    return {
      value: [row.value, layer] as [number, number],
      params: row.params,
      index: row.index,
      best: row.index === summary.best_index,
    };
  });

  return { points, min: min - pad, max: max + pad, count: rows.length };
}

// ── 批量表 ─────────────────────────────────────────────────

/** 表格的列：只需要一个稳定的 key（React）与展示名 */
export interface BatchColumn {
  key: string;
  label: string;
}

export interface BatchTablePayload {
  symbols: string[];
  strategies: BatchColumn[];
  /** 与 `symbols` × `strategies` 同形的格子；缺失为 null（**留空，不补 0**） */
  grid: (OptimizeCell | null)[][];
  bestIndex: number | null;
}

/**
 * 批量结果 → 表格。行列由**请求**给（不是从 cells 推）：有格失败时从 cells 推会
 * 悄悄少一行/一列，而那正是最该看见的信息。
 *
 * **按下标定位，不按 `(标的, 策略)` 查**——后者会撞：同一个策略带不同参数出现两次
 * 是完全合法的请求（`strategies` 是个列表），而那种键里没有参数，两格会互相覆盖。
 * 格序由后端定死（**标的在外、策略在内**，`width = strategies.length`），
 * 那是接口契约的一部分（`tests/integration/test_optimize_m5b.py` 钉过），照它算下标即可。
 */
export function batchTableFromCells(
  summary: OptimizeSummary,
  symbols: string[],
  strategies: BatchColumn[],
): BatchTablePayload {
  const byIndex = new Map<number, OptimizeCell>();
  for (const cell of present(summary.cells)) byIndex.set(cell.index, cell);

  const width = strategies.length;
  const grid = symbols.map((_, row) =>
    strategies.map((_, column) => byIndex.get(row * width + column) ?? null),
  );
  return { symbols, strategies, grid, bestIndex: summary.best_index };
}

// ── DSR 卡 ─────────────────────────────────────────────────

/** DSR 在 0~1 上的百分比（计量条用）。无定义时返回 null——**不画一根 0 的条** */
export function dsrPercent(dsr: number | null): number | null {
  if (dsr === null || !Number.isFinite(dsr)) return null;
  return Math.min(100, Math.max(0, dsr * 100));
}

/** 输入清单：只列有值的那些（无定义时不该出现一堆 `—`） */
export function overfitInputs(summary: OptimizeSummary): { label: string; value: string }[] {
  const { overfit } = summary;
  const rows: { label: string; value: string }[] = [
    { label: "试验数 N", value: `${overfit.n_trials}（有效 ${overfit.n_valid}）` },
  ];
  if (overfit.sr !== null) rows.push({ label: "最优夏普 SR", value: overfit.sr.toFixed(4) });
  if (overfit.sr0 !== null) rows.push({ label: "期望最大 SR₀", value: overfit.sr0.toFixed(4) });
  if (overfit.sr_variance !== null) {
    rows.push({ label: "试验方差 V", value: overfit.sr_variance.toExponential(2) });
  }
  if (overfit.skew !== null) rows.push({ label: "偏度 γ₃", value: overfit.skew.toFixed(3) });
  if (overfit.kurt !== null) rows.push({ label: "峰度 γ₄", value: overfit.kurt.toFixed(3) });
  if (overfit.observations !== null) rows.push({ label: "观测数 T", value: String(overfit.observations) });
  return rows;
}

/** 每期口径 → 年化（展示用）。公式进的是每期值，界面上给年化才对得上直觉 */
export function annualized(perPeriod: number | null): number | null {
  if (perPeriod === null) return null;
  return perPeriod * Math.sqrt(252);
}
