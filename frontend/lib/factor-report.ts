/**
 * 因子报告的纯函数层：请求 → 查询串，报告 → 各图/表的数据。
 *
 * 图组件一 import 就把 ECharts 拉进用例，故「画什么」的映射全部留在这里由 Vitest 测
 * （同 `optimize-matrix.ts`）。**颜色也留在这里之外**——本模块只给「色阶档位」（`rampIndex`），
 * 具体字面值由组件按主题从 `chart-theme` 取。
 *
 * 三条口径写死在这里，别在组件里再判一次：
 *   · `net` 缺失（`costs=false`）→ 相关映射返回 `null`，调用方画禁用态，**不补 0**；
 *   · 「不显著」判据 = `|t| < 2`（阈值取严格小于，t 恰好 ±2 算显著）；
 *   · `notes` 是服务端的必填清单，前端只分组、不增删文案。
 */

import { query } from "./query";
import type { FactorGroup, FactorReport, FactorTrack } from "./types";

export type Track = "gross" | "net";

export interface FactorQueryInput {
  source: "event" | "price";
  /** 仅价格源有意义；事件源传 undefined（服务端会忽略，但查询串里出现只会让人困惑） */
  direction?: "reversal" | "momentum";
  start?: string;
  end?: string;
  /** 缺省 true；**只有关掉时才写进查询串**（true 是服务端缺省） */
  costs?: boolean;
}

/** 请求参数 → 查询串。空值一律不写（`query()` 的既有口径）。 */
export function factorQuery(input: FactorQueryInput): string {
  return query({
    source: input.source,
    direction: input.source === "price" ? input.direction : undefined,
    start: input.start,
    end: input.end,
    costs: input.costs === false ? "false" : undefined,
  });
}

export interface ICBarPoint {
  date: string;
  ic: number;
  n: number;
}

export interface ICBarsPayload {
  points: ICBarPoint[];
  hasPositive: boolean;
  hasNegative: boolean;
  /** 本窗口没有有效信号日（图区改出说明文案，不画空坐标系） */
  empty: boolean;
}

export function icBars(report: FactorReport): ICBarsPayload {
  const points = report.ic.per_day.map((point) => ({
    date: point.date,
    ic: point.ic,
    n: point.n,
  }));
  return {
    points,
    hasPositive: points.some((point) => point.ic > 0),
    hasNegative: points.some((point) => point.ic < 0),
    empty: points.length === 0,
  };
}

/** 取一条轨（毛/净）：缺失返 `null`——调用方据此出禁用态，**不把 null 当 0**。 */
export function trackOf(group: FactorGroup, track: Track): FactorTrack | null {
  return track === "gross" ? group.gross : group.net;
}

export interface GroupLineSeries {
  quantile: number;
  label: string;
  /** 色阶档位（0 = Q1 最浅）。字面值由组件按主题取 `tokens.group[rampIndex]` */
  rampIndex: number;
  values: (number | null)[];
}

export interface GroupLinesPayload {
  dates: string[];
  series: GroupLineSeries[];
}

/** 5 条分层曲线：日期取并集，缺值补 `null`（ECharts 会断线，不会当 0）。 */
export function groupLines(report: FactorReport, track: Track): GroupLinesPayload | null {
  const groups = report.groups.map((group) => ({ group, data: trackOf(group, track) }));
  if (groups.some(({ data }) => data === null)) return null;

  const dates = sortedDates(groups.flatMap(({ data }) => data!.curve.map((p) => p.date)));
  return {
    dates,
    series: groups.map(({ group, data }, index) => {
      const byDate = new Map(data!.curve.map((point) => [point.date, point.level]));
      return {
        quantile: group.quantile,
        label: group.label,
        rampIndex: index,
        values: dates.map((date) => byDate.get(date) ?? null),
      };
    }),
  };
}

export interface LongShortPayload {
  dates: string[];
  gross: (number | null)[];
  net: (number | null)[] | null;
}

export function longShortLines(report: FactorReport): LongShortPayload {
  const grossCurve = report.long_short.gross.curve;
  const dates = grossCurve.map((point) => point.date);
  const grossMap = new Map(grossCurve.map((point) => [point.date, point.level]));
  const netCurve = report.long_short.net?.curve ?? null;
  const netMap = netCurve ? new Map(netCurve.map((point) => [point.date, point.level])) : null;
  return {
    dates,
    gross: dates.map((date) => grossMap.get(date) ?? null),
    net: netMap ? dates.map((date) => netMap.get(date) ?? null) : null,
  };
}

export interface CurveTableColumn {
  key: string;
  label: string;
  values: (number | null)[];
}

export interface CurveTable {
  dates: string[];
  columns: CurveTableColumn[];
}

/** 曲线图的表格孪生：日期 × (五个分组 + 多空)，同一份数。 */
export function curveTable(report: FactorReport, track: Track): CurveTable {
  const dates = sortedDates([
    ...report.groups.flatMap((group) => trackOf(group, track)?.curve.map((p) => p.date) ?? []),
    ...(track === "gross" ? report.long_short.gross.curve.map((p) => p.date) : []),
    ...(report.long_short.net?.curve.map((p) => p.date) ?? []),
  ]);

  const columnOf = (key: string, label: string, points: { date: string; level: number }[]) => {
    const byDate = new Map(points.map((point) => [point.date, point.level]));
    return { key, label, values: dates.map((date) => byDate.get(date) ?? null) };
  };

  const spreadTrack = track === "gross" ? report.long_short.gross : report.long_short.net;
  return {
    dates,
    columns: [
      ...report.groups.map((group) =>
        columnOf(`q${group.quantile}`, group.label, trackOf(group, track)?.curve ?? []),
      ),
      columnOf("spread", "多空", spreadTrack?.curve ?? []),
    ],
  };
}

/** IC 图的表格孪生：逐日 IC 与样本数。 */
export function icTable(report: FactorReport): ICBarPoint[] {
  return report.ic.per_day.map((point) => ({ date: point.date, ic: point.ic, n: point.n }));
}

export interface FactorHeadline {
  /** 英雄数字：RankIC 均值 */
  hero: number | null;
  icir: number | null;
  tStat: number | null;
  days: number;
  positiveDays: number;
  poolAvg: number;
  /** `|t| < 2` ⇒ false（**噪声区间**，页面据此出「不显著」标记） */
  significant: boolean;
  empty: boolean;
}

export function factorHeadline(report: FactorReport): FactorHeadline {
  const t = report.ic.t_stat;
  const days = report.ic.days;
  return {
    hero: report.ic.mean,
    icir: report.ic.icir,
    tStat: t,
    days,
    positiveDays: report.ic.positive_days,
    poolAvg: report.universe.pool_avg,
    significant: t !== null && Math.abs(t) >= 2,
    empty: days === 0,
  };
}

export interface NoteGroup {
  title: string;
  items: string[];
}

/**
 * 把服务端的 notes 分三组显示（十来条平铺是一堵墙）。
 *
 * **只分组不改写**：认不出的落进「口径与边界」，绝不丢——新增的说明最多位置不理想，
 * 不会消失。判定按关键词，顺序有意义（同时含「费用」与「年化」的句子归成本组）。
 */
const NOTE_BUCKETS: { title: string; keywords: string[] }[] = [
  { title: "成本", keywords: ["费用", "佣金", "换手", "收费", "拖累"] },
  { title: "读数的性质", keywords: ["噪声", "样本期", "年化", "外推", "显著"] },
];
const DEFAULT_BUCKET = "口径与边界";

export function groupNotes(notes: string[]): NoteGroup[] {
  const buckets = new Map<string, string[]>();
  for (const note of notes) {
    const title =
      NOTE_BUCKETS.find((bucket) => bucket.keywords.some((word) => note.includes(word)))?.title ??
      DEFAULT_BUCKET;
    const items = buckets.get(title) ?? [];
    items.push(note);
    buckets.set(title, items);
  }
  const order = [DEFAULT_BUCKET, ...NOTE_BUCKETS.map((bucket) => bucket.title)];
  return order
    .filter((title) => buckets.has(title))
    .map((title) => ({ title, items: buckets.get(title)! }));
}

export interface NotePart {
  text: string;
  strong: boolean;
}

/** `**加粗**` 的行内渲染：服务端文案按 markdown 强调写，直接显示星号是噪声。 */
export function noteParts(note: string): NotePart[] {
  const parts: NotePart[] = [];
  note.split("**").forEach((text, index) => {
    if (text) parts.push({ text, strong: index % 2 === 1 });
  });
  return parts;
}

function sortedDates(dates: string[]): string[] {
  return [...new Set(dates)].sort();
}
