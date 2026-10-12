/**
 * 研究首页的**纯函数**层：后端 JSON → 视图模型（M10a）。
 *
 * 为什么单独一层（同 `factor-report.ts` / `optimize-matrix.ts` 的口径）：图组件一 import
 * 就把 ECharts 拉进来，而「画什么、怎么排序、缺失怎么显示、口径文案写什么」与渲染无关，
 * 抽出来才测得到。**组件只负责画。**
 *
 * 两条贯穿本模块的纪律：
 *   · **缺值一律 `EMPTY`（`—`）或 `null`，不补 0**——「无法计算」与「等于 0」是两句不同的话；
 *   · **色阶只给「档位」**（`rampIndex` 0–6），字面色值留给组件去查 `chart-theme` 的 token。
 */

import { count, EMPTY, num, rate, yi } from "./format";
import type { MarketIndex, MarketOverview, MarketSectorCoverage, MarketSectors, SectorRow } from "./types";

/** 涨跌语义。**0 与缺失都走 `flat`**——不借用涨跌色去说一件中性的事。 */
export type Tone = "up" | "down" | "flat";

export function tone(value: number | null | undefined): Tone {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

/**
 * 值 → diverging 色阶的**档位**（0 = 最负、3 = 中点、6 = 最正）。
 *
 * 分母取**本批里绝对值最大者**，不是固定阈值：固定阈值下「今天最猛的板块」天天同一个颜色，
 * 色阶就退化成「有没有涨」的两色图，看多少天都一个样。用当批极值归一，每一天的深浅
 * 都相对那一天而言——这也意味着**跨日不能比颜色深浅**，这是刻意的。
 *
 * 条长与条色**冗余编码同一个值**（dataviz：diverging 的量就该配 diverging 的色），
 * 不是重复——条长给精确比较，色给一眼扫过的印象。
 */
export function rampIndex(value: number | null, maxAbs: number): number {
  if (value === null || !Number.isFinite(value) || maxAbs <= 0) return 3;
  const t = Math.max(-1, Math.min(1, value / maxAbs));
  return Math.round(((t + 1) / 2) * 6);
}

/** 一批值里的最大绝对值（色阶归一分母）。空批返回 0，调用方据此退回中性档。 */
export function maxAbs(values: readonly (number | null)[]): number {
  let peak = 0;
  for (const value of values) {
    if (value !== null && Number.isFinite(value)) peak = Math.max(peak, Math.abs(value));
  }
  return peak;
}

// ── 大盘块 ────────────────────────────────────────────────────────────────

export interface IndexCard {
  /** 英雄数字：点位 */
  level: string;
  /** 当日涨跌（带号） */
  change: string;
  tone: Tone;
  /** 口径小字：**必须能读到「不是指数」** */
  caption: string;
}

export function indexCard(index: MarketIndex | null): IndexCard | null {
  if (!index) return null;
  return {
    level: num(index.level, { digits: 2 }),
    change: rate(index.change_pct, { signed: true }),
    tone: tone(index.change_pct),
    caption: `全市场等权代理（${index.kind}），基点 ${count(index.base_level)} @ ${index.base_date ?? EMPTY}，已累积 ${count(index.days)} 个交易日`,
  };
}

export interface StatCard {
  key: string;
  label: string;
  /** 主读数 */
  value: string;
  /** 主读数的语义色（`flat` = 中性，不染） */
  tone: Tone;
  /** 副读数（一行小字） */
  note: string;
}

export function breadthCard(o: MarketOverview | null): StatCard | null {
  const breadth = o?.breadth;
  if (!breadth) return null;
  return {
    key: "breadth",
    label: "涨跌家数",
    value: count(breadth.up),
    tone: "up",
    note: `跌 ${count(breadth.down)} · 平 ${count(breadth.flat)}${breadth.without_pct ? ` · 无涨跌幅 ${count(breadth.without_pct)}` : ""}`,
  };
}

export function limitsCard(o: MarketOverview | null): StatCard | null {
  const limits = o?.limits;
  if (!limits) return null;
  // 「跳过」照登：判不了的那些不在任何一档里，不说出来就成了「它们没涨停」
  const skipped = limits.skipped ? ` · 判不了 ${count(limits.skipped)}` : "";
  return {
    key: "limits",
    label: "涨跌停",
    value: count(limits.limit_up),
    tone: "up",
    note: `跌停 ${count(limits.limit_down)} · 判定 ${count(limits.judged)}${skipped}`,
  };
}

export function amountCard(o: MarketOverview | null): StatCard | null {
  if (!o || o.amount === null) return null;
  return {
    key: "amount",
    label: "成交额（亿元）",
    value: yi(o.amount),
    tone: "flat",
    note: o.breadth ? `有价标的 ${count(o.breadth.total)} 只` : "",
  };
}

/** 两个「截至」。**天然不同步**，并列显示是如实而不是缺陷。 */
export interface AsOf {
  bars: string;
  events: string;
}

export function asOf(o: MarketOverview | null): AsOf | null {
  if (!o?.as_of) return null;
  return { bars: o.as_of.bars ?? EMPTY, events: o.as_of.events ?? EMPTY };
}

// ── 板块块 ────────────────────────────────────────────────────────────────

export interface SectorBar {
  industry: string;
  /** 平均涨跌幅（百分数），用于条长 */
  value: number;
  /** diverging 色阶档位 0–6 */
  rampIndex: number;
  /** 条上的直接标注（带号 + %） */
  label: string;
  /** 条右侧的两个读数：成分数与成交额 */
  meta: string;
  /** 均值与中位数差得远 ⇒ 这一档被少数极端值主导，值得读者多看一眼 */
  skewed: boolean;
}

/** 均值与中位数差多少算「被极端值主导」——超过这个百分点就在表里标出来。 */
const SKEW_PP = 1.0;

export function sectorBars(sectors: MarketSectors | null): SectorBar[] {
  const rows = sectors?.items ?? [];
  const peak = maxAbs(rows.map((row) => row.avg_pct));
  return rows.map((row) => bar(row, peak));
}

function bar(row: SectorRow, peak: number): SectorBar {
  const value = row.avg_pct ?? 0;
  return {
    industry: row.industry,
    value,
    rampIndex: rampIndex(row.avg_pct, peak),
    label: rate(row.avg_pct, { signed: true }),
    meta: `${count(row.symbols)} 只 · ${yi(row.amount)} 亿`,
    skewed:
      row.avg_pct !== null &&
      row.median_pct !== null &&
      Math.abs(row.avg_pct - row.median_pct) > SKEW_PP,
  };
}

/** 表格孪生：与图**同序同值**，连续色标必须有可读数值的孪生（dataviz 的硬要求）。 */
export interface SectorTableRow {
  industry: string;
  symbols: string;
  amount: string;
  avg: string;
  median: string;
  skewed: boolean;
}

export function sectorTable(sectors: MarketSectors | null): SectorTableRow[] {
  return sectorBars(sectors).map((item, index) => {
    const row = (sectors?.items ?? [])[index];
    return {
      industry: item.industry,
      symbols: count(row?.symbols ?? null),
      amount: yi(row?.amount ?? null),
      avg: item.label,
      median: rate(row?.median_pct ?? null, { signed: true }),
      skewed: item.skewed,
    };
  });
}

/**
 * 覆盖率文案。**必须显示**——行业标签来自事件语料，按事件类型覆盖不均
 * （news 44.9% / policy 100% / announcement 0%），画成一张「看起来完整」的图就是编数。
 */
export function coverageText(coverage: MarketSectorCoverage | null): string {
  if (!coverage || !coverage.total) return EMPTY;
  const ratio = coverage.ratio === null ? null : coverage.ratio;
  const percent = ratio === null ? EMPTY : `${num(ratio * 100, { digits: 1 })}%`;
  return `${count(coverage.covered)} / ${count(coverage.total)}（${percent}）`;
}

/** 涨跌停口径的说明：**照抄服务端**，前端不自己拼一份免责话术（M5b 定过的规矩）。 */
export function limitNote(o: MarketOverview | null): string {
  return o?.limits_meta?.note ?? "";
}

/**
 * `Tone` → 文字色 class。**`flat` 不染色**——一件中性的事不该借涨跌色说话。
 *
 * 项目里已有两处同样的映射（`paper/account-card.tsx` 的 `toneClass`、
 * `research/panels.tsx` 的 `up/down` 表）。那两处各带自己的语义（盈亏 / 研报方向），
 * 本文件的语义是「涨跌幅」，故先各留一份；真要合并是一次独立的清理，不塞进 M10。
 */
export function toneClass(value: Tone): string {
  return value === "up" ? "text-up" : value === "down" ? "text-down" : "";
}
