/**
 * 事件详情（M10d）：把一条 `EventDetail` 摊成**七组可渲染的行**。
 *
 * 纯函数、无副作用——喂一条假数据就能逐条断言「空值显示 —」「每只标的都带理由」「修订
 * 字段照原样」。抽屉组件只负责画，不做任何判断（与 `feedItemView` 同一条纪律）。
 *
 * 三条口径：
 *  1. **空值一律 `EMPTY`（—）**，不编、不留空白（`lib/format.ts` 的 `EMPTY`）；
 *  2. **修订四件照原样列**：`record_version` / `is_corrected` / `correction_count` /
 *     `revision_time` 是什么就显示什么——「已修订 N 次」那种加工文案会把
 *     「平台没标记但内容变过」（M2b 实测：`content_hash` 会变而标记不动）说成没有；
 *  3. `source_url` 为空时**不产生链接**（`href` 不给），不给一个点不动的空壳。
 */

import { EMPTY, count, eventStamp, num, shortHash } from "./format";
import type { EventDetail, EventStockDetail } from "./types";
import { lagText } from "./event-feed";

/** 方向标签与配色。**一处定义**：个股页的事件表与详情抽屉共用（原先在 events-table 里） */
export const DIRECTION: Record<string, { label: string; className: string }> = {
  bullish: { label: "利多", className: "text-up" },
  bearish: { label: "利空", className: "text-down" },
  neutral: { label: "中性", className: "text-ink-3" },
};

export interface DetailRow {
  label: string;
  value: string;
  /** 有值才给：`source_url` 为空时不该出现一个点不动的链接 */
  href?: string;
  /** 长文本（理由、人物列表）占整行 */
  wide?: boolean;
}

export interface DetailGroup {
  key: string;
  title: string;
  rows: DetailRow[];
}

/** 两个时刻之差（小时）；任一为空就没有差可言 */
export function hoursBetween(from: string | null, to: string | null): number | null {
  if (!from || !to) return null;
  const delta = (Date.parse(to) - Date.parse(from)) / 3_600_000;
  return Number.isFinite(delta) ? delta : null;
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  return String(value);
}

/** 外部链接只认 http(s)：React 会挡 `javascript:`，但那是框架兜底，不是契约。 */
export function safeHref(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
}

function yesNo(value: boolean | null): string {
  if (value === null) return EMPTY;
  return value ? "是" : "否";
}

/** 一只标的：`代码 名称 — 理由`；名字取字典、缺了退回平台名，两个都没有就只出代码 */
export function stockLine(stock: EventStockDetail): string {
  const name = stock.name ?? stock.source_name;
  const head = name ? `${stock.code} ${name}` : stock.code;
  return `${head} — ${stock.reason ?? EMPTY}`;
}

const CODED = /^\d{6}$/;

/**
 * 关联标的的行。
 *
 * **平台的 `stocks` 原文里混着脏条目**：实测一条新闻的 20 个提及里 19 个 `code` 是空串、
 * 还有 `code` 写成 `"数据缺失"` 的（`reason` 却写着理由）。六位码是「这只股票是谁」的唯一
 * 可用键（`symbols` 那列也是按它归一的），所以这里**只列有六位码的**；被滤掉的**明说条数**，
 * 不静默隐藏——「平台提了 20 个但只有 1 个能落到股票」本身是要让人看见的事实。
 */
export function stockRows(detail: EventDetail): DetailRow[] {
  const coded = detail.stocks.filter((stock) => CODED.test(stock.code));
  const rows: DetailRow[] = coded.map((stock) => ({
    label: stock.code,
    value: stockLine(stock),
    wide: true,
  }));
  const dropped = detail.stocks.length - coded.length;
  if (dropped > 0) {
    rows.push({
      label: "未带代码",
      value: `另有 ${dropped} 条提及没有六位代码（平台原文如此，进不了关联标的）`,
      wide: true,
    });
  }
  return rows.length ? rows : [{ label: "标的", value: EMPTY }];
}

/** `factor_scores` 的键值对：空对象给 `—`（person / policy 类实测就是空的） */
export function factorText(scores: Record<string, unknown>): string {
  const parts = Object.entries(scores)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${String(value)}`);
  return parts.length ? parts.join(" · ") : EMPTY;
}

export function detailGroups(detail: EventDetail): DetailGroup[] {
  const sourceHref = safeHref(detail.source_url);
  return [
    {
      key: "source",
      title: "来源与版本",
      rows: [
        { label: "来源", value: text(detail.source) },
        { label: "原始发布方", value: text(detail.original_source) },
        {
          label: "原文",
          value: detail.source_url ?? EMPTY,
          ...(sourceHref ? { href: sourceHref } : {}),
          wide: true,
        },
        { label: "内容指纹", value: detail.content_hash ? shortHash(detail.content_hash) : EMPTY },
        { label: "质量标注", value: text(detail.quality_status) },
        { label: "时间精度", value: text(detail.source_time_quality) },
      ],
    },
    {
      key: "revision",
      title: "平台修订标记",
      rows: [
        { label: "记录版本", value: detail.record_version === null ? EMPTY : count(detail.record_version) },
        { label: "是否修订过", value: yesNo(detail.is_corrected) },
        { label: "修正次数", value: detail.correction_count === null ? EMPTY : count(detail.correction_count) },
        { label: "修订时刻", value: eventStamp(detail.revision_time) },
        { label: "修订号", value: detail.revision_id ? shortHash(detail.revision_id) : EMPTY },
        { label: "去重键", value: text(detail.dedup_key) },
      ],
    },
    {
      key: "stocks",
      title: "关联标的",
      rows: stockRows(detail),
    },
    { key: "person", title: "人物", rows: [{ label: "提及", value: detail.person.length ? detail.person.join("、") : EMPTY, wide: true }] },
    {
      key: "factor",
      title: "方向与因子",
      rows: [
        { label: "平台原词", value: text(detail.direction) },
        { label: "归一方向", value: DIRECTION[detail.direction_norm ?? ""]?.label ?? text(detail.direction_norm) },
        { label: "置信", value: num(detail.confidence, { digits: 2 }) },
        { label: "重要度", value: detail.importance_score === null ? EMPTY : num(detail.importance_score, { digits: 0 }) },
        { label: "因子值", value: num(detail.factor_value, { digits: 4, signed: true }) },
        { label: "因子分", value: factorText(detail.factor_scores), wide: true },
        { label: "行业", value: detail.industries.length ? detail.industries.join("、") : EMPTY, wide: true },
      ],
    },
  ];
}

/** 头部那一组的三戳与滞后（护城河展示位）：事发 → 报告可得 → 平台可得 → 我们观察到 */
export function stampRows(detail: EventDetail): DetailRow[] {
  return [
    { label: "事发", value: eventStamp(detail.event_time) },
    { label: "报告可得", value: eventStamp(detail.reported_available_at) },
    { label: "平台可得", value: eventStamp(detail.available_at) },
    { label: "观察于", value: eventStamp(detail.observed_at) },
    {
      label: "可得滞后",
      value: lagText(hoursBetween(detail.reported_available_at ?? detail.event_time, detail.available_at)),
    },
  ];
}
