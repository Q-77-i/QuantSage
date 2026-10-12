/**
 * 事件日历 / 快讯流的**纯函数**层（M10b）。
 *
 * 同 `market-home.ts` 的口径：图与表一 import 就把组件树拉进来，而「怎么分档、怎么排序、
 * 缺值怎么显示、翻页怎么去重」与渲染无关，抽出来才测得到。**组件只负责画。**
 *
 * 这一层最重要的一条是 **`lagHours` / `lagText`**：`available_at − event_time` 是这个项目
 * 的护城河在界面上的落点。实测有 8 天后才可得的事件（2026-09-30 的美国 PCE 数据，
 * 10-08 才进语料）——不显示这个差，PIT 就只是一句口号。
 */

import { count, dayOnly, EMPTY, num, weekday } from "./format";
import type { EventCalendar, EventFeed, EventFeedItem } from "./types";

/** 类型的中文名。展示层翻译，与 `events-table.tsx` 的 DIRECTION 表同一个姿态。 */
export const TYPE_LABELS: Record<string, string> = {
  news: "新闻",
  announcement: "公告",
  policy: "政策",
  person: "人物",
  research: "研报",
};

export function typeLabel(type: string | null): string {
  if (!type) return EMPTY;
  return TYPE_LABELS[type] ?? type;
}

// ── 筛选口径 ──────────────────────────────────────────────────────────────

/** 默认看哪几类。**不含公告**——见下面 `TYPE_CHOICES` 的注释。 */
export const ANNOUNCEMENT = "announcement";
export const FEED_TYPES: readonly string[] = ["news", "policy", "person", "research"];

export interface TypeChoice {
  key: string;
  label: string;
  /** `null` = 不筛（全部类型） */
  types: readonly string[] | null;
}

/**
 * 类型筛选的档位。**第一档默认是「快讯（除公告）」**。
 *
 * 为什么默认把公告摘出去：平台的 `importance_score` **不跨类型可比**——实测 2026-09-30
 * 按分数降序的**前 200 名全是公告**（该日公告均分 30.9 却满分 100，新闻均分 48.5、
 * 满分 92），榜首是「验资报告」一类常规披露。不摘就没有「快讯流」，只有一张公告列表。
 * 「公告」单列一档，想看随时切得回来——**这是默认值，不是隐藏**。
 */
export const TYPE_CHOICES: readonly TypeChoice[] = [
  { key: "feed", label: "快讯（除公告）", types: FEED_TYPES },
  { key: "all", label: "全部类型", types: null },
  { key: "announcement", label: "公告", types: [ANNOUNCEMENT] },
  { key: "news", label: "新闻", types: ["news"] },
  { key: "policy", label: "政策", types: ["policy"] },
  { key: "person", label: "人物", types: ["person"] },
  { key: "research", label: "研报", types: ["research"] },
];

export interface ImportanceChoice {
  key: string;
  label: string;
  value: number | null;
}

export const IMPORTANCE_CHOICES: readonly ImportanceChoice[] = [
  { key: "any", label: "不限重要度", value: null },
  { key: "40", label: "重要度 ≥ 40", value: 40 },
  { key: "60", label: "重要度 ≥ 60", value: 60 },
  { key: "80", label: "重要度 ≥ 80", value: 80 },
];

// ── 日期带 ────────────────────────────────────────────────────────────────

export interface StripDay {
  date: string;
  /** `09-30` */
  label: string;
  /** `周三` */
  weekday: string;
  total: number;
  high: number;
  /** 柱高百分比（按窗口内最大值归一，留 4% 下限让「只有一条」的日子也看得见） */
  heightPct: number;
  selected: boolean;
}

export function stripDays(calendar: EventCalendar | null, selected: string | null): StripDay[] {
  const days = calendar?.days ?? [];
  const peak = days.reduce((top, day) => Math.max(top, day.total), 0);
  return days.map((day) => ({
    date: day.date,
    label: dayOnly(day.date),
    weekday: weekday(day.date),
    total: day.total,
    high: day.high,
    heightPct: peak > 0 ? Math.max(4, Math.round((day.total / peak) * 100)) : 0,
    selected: day.date === selected,
  }));
}

/**
 * 缺省选中哪一天：窗口里**最后一个有事件的日子**。
 *
 * 不是「今天」——语料是滚动窗口且滞后于现实（实测最新事件 2026-10-08），指向今天会落空。
 */
export function latestDay(calendar: EventCalendar | null): string | null {
  const days = calendar?.days ?? [];
  return days.length ? days[days.length - 1].date : null;
}

/** 日期带头上那句话：选中日的条数与高分条数。 */
export function daySummary(calendar: EventCalendar | null, selected: string | null): string {
  const day = (calendar?.days ?? []).find((row) => row.date === selected);
  if (!day) return EMPTY;
  const parts = [`共 ${count(day.total)} 条`];
  if (day.high > 0) parts.push(`${count(day.high)} 条重要度 ≥ ${num(calendar?.high_score ?? null, { digits: 0 })}`);
  const types = Object.entries(day.by_type)
    .sort(([, a], [, b]) => b - a)
    .map(([type, n]) => `${typeLabel(type)} ${count(n)}`);
  if (types.length) parts.push(types.join(" · "));
  return parts.join(" · ");
}

// ── 快讯流 ────────────────────────────────────────────────────────────────

/**
 * 列表键 = `event_id@available_at`。
 *
 * **不能只用 `event_id`**：平台对同题事件按月复用 id（`events-table` 实测 84 天里 18 例），
 * 单用 id 会让翻页去重误删真实条目，React 列表也会撞键。
 */
export function feedKey(item: EventFeedItem): string {
  return `${item.event_id}@${item.available_at ?? ""}`;
}

/**
 * 还能不能加载更多。
 *
 * 判据是 `offset < total`：`offset` 始终指向**下一页的服务端位置**（`appendFeed` 维护），
 * 故它与 `total` 的比较一直有效。**不要用 `items.length` 判**——去重会丢掉重复条目，
 * 于是「手里有几条」与「服务端取到第几条」不再相等（正确的是后者）。
 */
export function canLoadMore(feed: EventFeed | null): boolean {
  return feed !== null && feed.offset < feed.total;
}

/** 已加载多少 / 共多少。 */
export function feedProgress(feed: EventFeed | null): string {
  if (!feed) return EMPTY;
  return `${count(feed.items.length)} / ${count(feed.total)}`;
}

/**
 * 追加一页。`date` 变了就**整份换掉**（换了一天还往上叠是错的），同一天才做去重合并。
 *
 * 返回值的 `offset` 被改写成**下一页的服务端位置**（`next.offset + next.count`），
 * 不是「这一份从哪里开始」——`canLoadMore` 与下一次请求都靠它。
 */
export function appendFeed(current: EventFeed | null, next: EventFeed): EventFeed {
  const offset = next.offset + next.count;
  if (!current || current.date !== next.date) return { ...next, offset };
  const seen = new Set(current.items.map(feedKey));
  const items = [...current.items];
  for (const item of next.items) {
    const key = feedKey(item);
    if (!seen.has(key)) {
      seen.add(key);
      items.push(item);
    }
  }
  return { ...next, items, offset };
}

/**
 * 可得滞后（小时）＝ `available_at − event_time`。**这就是护城河**。
 *
 * 为负是**合法**的：源侧的 `available_at` 有时早于它自己标的 `event_time`（实测 97,029 条
 * `<= 0`）。如实显示，不取绝对值也不夹到 0——把负滞后抹平等于篡改这条时间线。
 */
export function lagHours(item: EventFeedItem): number | null {
  if (!item.event_time || !item.available_at) return null;
  const event = new Date(item.event_time).getTime();
  const available = new Date(item.available_at).getTime();
  if (Number.isNaN(event) || Number.isNaN(available)) return null;
  return (available - event) / 3_600_000;
}

/** 滞后的人话。**超过 48 小时就报天数**——那正是最值得看见的那一档（实测最长 8 天）。 */
export function lagText(hours: number | null): string {
  if (hours === null) return EMPTY;
  if (hours < 0) return `${num(-hours, { digits: 1 })} 小时前已可得`;
  if (hours < 1) return "1 小时内可得";
  if (hours < 48) return `${num(hours, { digits: 1 })} 小时后可得`;
  return `${num(hours / 24, { digits: 1 })} 天后可得`;
}

/** 关联标的：优先名字，没名字给代码（**不编**），超过 `max` 个折叠成「等 N 只」。 */
export function symbolText(item: EventFeedItem, max = 4): string {
  const list = item.symbols ?? [];
  if (!list.length) return EMPTY;
  const shown = list.slice(0, max).map((row) => row.name ?? row.code);
  return list.length > max ? `${shown.join("、")} 等 ${count(list.length)} 只` : shown.join("、");
}

/** 条目的展示模型。 */
export interface FeedItemView {
  key: string;
  item: EventFeedItem;
  title: string;
  typeText: string;
  /** 重要度：整数显示（平台给的是浮点，`92.17` 这种小数位没有信息量） */
  score: string | null;
  /** 可得滞后的人话——护城河那一栏，**与两个时间戳并列显示** */
  lag: string;
  symbols: string;
  industries: string;
}

export function feedItemView(item: EventFeedItem): FeedItemView {
  return {
    key: feedKey(item),
    item,
    title: item.title ?? item.event_id,
    typeText: typeLabel(item.event_type),
    score: item.importance_score === null ? null : num(item.importance_score, { digits: 0 }),
    lag: lagText(lagHours(item)),
    symbols: symbolText(item),
    industries: (item.industries ?? []).join("、"),
  };
}

/**
 * 筛选态的**可序列化键 → 实际参数**。
 *
 * 状态存「档位键」而不是数组：数组每次渲染都是新身份，塞进 `useEffect` 依赖会每次重取。
 * 键是纯字符串，依赖比较天然稳定（`useHome` 那边是数字，没这个问题）。
 */
export function typesOf(typeKey: string): readonly string[] | null {
  return TYPE_CHOICES.find((choice) => choice.key === typeKey)?.types ?? FEED_TYPES;
}

export function importanceOf(importanceKey: string): number | null {
  return IMPORTANCE_CHOICES.find((choice) => choice.key === importanceKey)?.value ?? null;
}

/** 每页条数。20 条 ≈ 一屏半，翻页代价比「一次给 200」低。 */
export const PAGE_SIZE = 20;
