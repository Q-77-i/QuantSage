/**
 * 标的浏览的**纯函数**层（M10c）。
 *
 * 同 `market-home.ts` / `event-feed.ts` 的口径：视图模型与渲染分开，缺值一律 `EMPTY`、
 * **不补 0**，色阶 / 涨跌只给「档位」或「方向」，字面值留给组件。
 *
 * 这一层要守的一条：**列表的三态（涨 / 跌 / 平）与缺失是四件事**——`change_pct` 为 null
 * 的标的是「那天没有涨跌幅这个事实」（源侧实测 4.8% 缺失），不是「平盘」。
 */

import { count, EMPTY, num, rate, yi } from "./format";
// 分页判据住在 `./paging`（与快讯流共用）——本模块不再自己算一遍
export { clampPage, offsetOf, pageOf, totalPages } from "./paging";
import type { CrossRow, CrossSection } from "./types";
import type { Tone } from "./market-home";
import { tone } from "./market-home";

/** 排序键。**不含 `turnover_pct`**：源侧 2026-09 起整列为空，按它排等于随机序。 */
export const SORT_CHOICES = [
  { key: "change_pct", label: "涨跌幅" },
  { key: "amount", label: "成交额" },
  { key: "close", label: "收盘价" },
  { key: "symbol", label: "代码" },
] as const;

export type SortKey = (typeof SORT_CHOICES)[number]["key"];

export function sortLabel(key: string): string {
  return SORT_CHOICES.find((choice) => choice.key === key)?.label ?? key;
}

/** 每页条数。服务端上限 500；50 条约一屏半，翻页代价比「一次给 500」低。 */
export const PAGE_SIZE = 50;

// ── 列表行 ────────────────────────────────────────────────────────────────

export interface StockRowView {
  symbol: string;
  /** 名称缺失时给代码——列表里总得有个能认的东西，但**不编名字** */
  label: string;
  hasName: boolean;
  close: string;
  change: string;
  tone: Tone;
  amount: string;
}

export function stockRowView(row: CrossRow): StockRowView {
  return {
    symbol: row.symbol,
    label: row.name ?? row.symbol,
    hasName: row.name !== null,
    close: num(row.close, { digits: 2 }),
    change: rate(row.change_pct, { signed: true }),
    tone: tone(row.change_pct),
    // **成交额在列表里给 2 位小数**（板块块给 1 位）：全市场成交额跨 5 个数量级，
    // 1 位小数下**有 93 只**会显示成「0」（实测最小的一批在 0.01–0.02 亿）。
    // 2 位后仍显示「0.00」的只剩 11 只，那些是真近乎无成交的（停牌一类）——那是事实，不是缺失。
    amount: yi(row.amount, 2),
  };
}

export function stockRows(section: CrossSection | null): StockRowView[] {
  return (section?.items ?? []).map(stockRowView);
}

/** 页头那句话：这一页是哪个口径的多少只。 */
export function listSummary(section: CrossSection | null): string {
  if (!section) return EMPTY;
  const scope = section.industry ? `${section.industry} · ` : "全市场 · ";
  return `${scope}${section.trade_date ?? EMPTY} · 共 ${count(section.total)} 只`;
}

export function listProgress(section: CrossSection | null): string {
  if (!section) return EMPTY;
  return `${count(section.items.length)} / ${count(section.total)}`;
}

/** 个股页的标的名。取不到名字时**回落到代码**并如实标注。 */
export function symbolTitle(symbol: string, name: string | null): string {
  return name ? `${name} ${symbol}` : symbol;
}

/**
 * 「加自选」按钮的文案与可用性。
 *
 * 轻量版：只看**本地已知的自选列表**判重（列表本来就全量加载，零往返），
 * 不复制 `WatchlistPanel` 的 probe 防抖与名称搜索——那是一次重构，不在本段。
 */
export interface AddState {
  label: string;
  disabled: boolean;
}

export function addButtonState(tracked: boolean, busy: boolean): AddState {
  if (tracked) return { label: "已在自选", disabled: true };
  if (busy) return { label: "加入中…", disabled: true };
  return { label: "加入自选", disabled: false };
}
