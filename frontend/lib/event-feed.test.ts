/**
 * 事件日历 / 快讯流视图模型的用例（`lib/event-feed.ts`）。
 *
 * 重点钉四条容易错且不报错的：
 *   · **翻页去重按 `event_id@available_at`**——同题事件按月复用 id，只用 id 会误删真实条目；
 *   · **换了一天要整份换掉**，不能往上一页上叠；
 *   · **滞后可以为负**（源侧 `available_at` 早于自己标的 `event_time`，实测 97,029 条）——
 *     抹平它等于篡改这条时间线；
 *   · **缺值不补 0**：没有重要度、没有标的、没有名字，各自如实留空。
 */

import { describe, expect, it } from "vitest";

import {
  daySummary,
  feedKey,
  FEED_TYPES,
  IMPORTANCE_CHOICES,
  lagHours,
  lagText,
  latestDay,
  stripDays,
  symbolText,
  typeLabel,
  TYPE_CHOICES,
} from "./event-feed";
import { EMPTY } from "./format";
import type { EventCalendar, EventFeedItem } from "./types";

function item(overrides: Partial<EventFeedItem> = {}): EventFeedItem {
  return {
    event_id: "news:1",
    event_type: "news",
    title: "美国核心PCE物价指数公布",
    event_time: "2026-09-30T20:30:00+08:00",
    available_at: "2026-09-30T20:45:00+08:00",
    direction_norm: "bullish",
    industries: ["宏观政策"],
    importance_score: 92.17,
    source: "xiaoshi-archive",
    original_source: "财联社",
    source_url: null,
    content_hash: "abc",
    symbols: [{ code: "600519", name: "贵州茅台" }],
    ...overrides,
  };
}

function calendar(days: EventCalendar["days"]): EventCalendar {
  return { start: "2026-09-01", end: "2026-09-30", high_score: 60, days };
}

describe("筛选口径", () => {
  it("默认那档是「快讯（除公告）」——公告被摘出去，但单列一档随时切得回来", () => {
    expect(TYPE_CHOICES[0].key).toBe("feed");
    expect(TYPE_CHOICES[0].types).toEqual(FEED_TYPES);
    expect(TYPE_CHOICES[0].types).not.toContain("announcement");
    const back = TYPE_CHOICES.find((choice) => choice.key === "announcement");
    expect(back?.types).toEqual(["announcement"]);
  });

  it("「全部类型」是 null（不筛），不是空数组（空数组在服务端会被当成没传）", () => {
    expect(TYPE_CHOICES.find((choice) => choice.key === "all")?.types).toBeNull();
  });

  it("重要度档位第一档是 null（不限），其余是数字", () => {
    expect(IMPORTANCE_CHOICES[0].value).toBeNull();
    expect(IMPORTANCE_CHOICES.map((choice) => choice.value)).toEqual([null, 40, 60, 80]);
  });

  it("类型中文名：认得出就翻，认不出原样返回（不吞掉线索）", () => {
    expect(typeLabel("announcement")).toBe("公告");
    expect(typeLabel("macro")).toBe("macro");
    expect(typeLabel(null)).toBe(EMPTY);
  });
});

describe("日期带", () => {
  const days = [
    { date: "2026-09-28", total: 3222, high: 497, by_type: { news: 3000, announcement: 222 } },
    { date: "2026-09-29", total: 6512, high: 909, by_type: { news: 4000, announcement: 2512 } },
    { date: "2026-09-30", total: 3256, high: 500, by_type: { news: 3000, announcement: 256 } },
  ];

  it("柱高按窗口内最大值归一，且有 4% 下限（只有一条的日子也看得见）", () => {
    const strip = stripDays(calendar(days), null);
    expect(strip.map((day) => day.heightPct)).toEqual([49, 100, 50]);
    const tiny = stripDays(calendar([{ date: "2026-09-01", total: 1, high: 0, by_type: {} }]), null);
    expect(tiny[0].heightPct).toBe(100); // 单日窗口：它就是峰值
    const mixed = stripDays(
      calendar([
        { date: "2026-09-01", total: 10000, high: 0, by_type: {} },
        { date: "2026-09-02", total: 1, high: 0, by_type: {} },
      ]),
      null,
    );
    expect(mixed[1].heightPct).toBe(4); // 0.01% 会被取整成 0，下限兜住
  });

  it("标签是 MM-DD 与星期（不重复年份，同一天窗口里年份是噪声）", () => {
    const strip = stripDays(calendar(days), null);
    expect(strip[0].label).toBe("09-28");
    expect(strip[0].weekday).toMatch(/^周/);
  });

  it("选中态只落一天", () => {
    const strip = stripDays(calendar(days), "2026-09-29");
    expect(strip.map((day) => day.selected)).toEqual([false, true, false]);
  });

  it("缺省选最后一天**有事件**的日子——不是「今天」（语料滞后于现实）", () => {
    expect(latestDay(calendar(days))).toBe("2026-09-30");
    expect(latestDay(calendar([]))).toBeNull();
    expect(latestDay(null)).toBeNull();
  });

  it("日期带为空时不炸", () => {
    expect(stripDays(null, null)).toEqual([]);
  });

  it("选中日的摘要含总数、高分条数与类型拆分", () => {
    const text = daySummary(calendar(days), "2026-09-29");
    // 开头那枚日期不是装饰：这一块与大盘块看的是不同的日子（事件按日增量、行情整片 release）
    expect(text.startsWith("2026-09-29")).toBe(true);
    expect(text).toContain("共 6,512 条");
    expect(text).toContain("909 条重要度 ≥ 60");
    expect(text).toContain("新闻 4,000");
    expect(daySummary(calendar(days), "2026-01-01")).toBe(EMPTY);
  });
});

describe("列表键", () => {
  it("是 `event_id@available_at`——同题事件按月复用 id，只用 id 会让 React 列表撞键", () => {
    const a = item({ event_id: "news:1", available_at: "2026-09-30T20:45:00+08:00" });
    const b = item({ event_id: "news:1", available_at: "2026-10-08T03:19:00+08:00" });
    expect(feedKey(a)).not.toBe(feedKey(b));
    expect(feedKey(a)).toBe("news:1@2026-09-30T20:45:00+08:00");
  });
});

describe("可得滞后（护城河那一栏）", () => {
  it("按小时算差值", () => {
    expect(lagHours(item())).toBeCloseTo(0.25, 5);
  });

  it("**实测过的那个例子**：事发 9-30 20:30、可得 10-08 03:19 → 约 7.3 天", () => {
    const hours = lagHours(
      item({ event_time: "2026-09-30T20:30:00+08:00", available_at: "2026-10-08T03:19:15+08:00" }),
    );
    expect(hours).toBeCloseTo(174.8, 1);
    expect(lagText(hours)).toBe("7.3 天后可得");
  });

  it("**负滞后如实显示**，不取绝对值也不夹到 0", () => {
    const hours = lagHours(
      item({ event_time: "2026-09-30T10:00:00+08:00", available_at: "2026-09-30T08:00:00+08:00" }),
    );
    expect(hours).toBe(-2);
    expect(lagText(hours)).toBe("2 小时前已可得");
  });

  it("分档：1 小时内 / 小时 / 超过 48 小时改报天", () => {
    expect(lagText(0.5)).toBe("1 小时内可得");
    expect(lagText(3.25)).toBe("3.3 小时后可得");
    expect(lagText(47.9)).toBe("47.9 小时后可得");
    expect(lagText(72)).toBe("3 天后可得");
  });

  it("缺任一时间戳就给「—」，不编一个 0", () => {
    expect(lagHours(item({ available_at: null }))).toBeNull();
    expect(lagText(null)).toBe(EMPTY);
  });
});

describe("标的与缺值", () => {
  it("优先名字，没名字给代码", () => {
    expect(symbolText(item())).toBe("贵州茅台");
    expect(symbolText(item({ symbols: [{ code: "600519", name: null }] }))).toBe("600519");
  });

  it("超过上限折叠成「等 N 只」", () => {
    const many = Array.from({ length: 6 }, (_, i) => ({ code: `60000${i}`, name: `公司${i}` }));
    expect(symbolText(item({ symbols: many }), 3)).toBe("公司0、公司1、公司2 等 6 只");
  });

  it("没有标的就是「—」，不显示一个空字符串", () => {
    expect(symbolText(item({ symbols: [] }))).toBe(EMPTY);
  });
});
