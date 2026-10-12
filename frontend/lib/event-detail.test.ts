import { describe, expect, it } from "vitest";

import { DIRECTION, detailGroups, factorText, hoursBetween, stampRows, stockLine } from "./event-detail";
import { EMPTY } from "./format";
import type { EventDetail } from "./types";

function detail(overrides: Partial<EventDetail> = {}): EventDetail {
  return {
    event_id: "news:1",
    date: "2026-10-08",
    event_type: "news",
    title: "美国核心PCE物价指数公布",
    summary: "全文摘要，应当只在详情里出现。",
    event_time: "2026-09-30T20:30:00+08:00",
    available_at: "2026-09-30T20:45:00+08:00",
    reported_available_at: "2026-09-30T20:40:00+08:00",
    observed_at: "2026-10-08T21:12:00+08:00",
    direction: "利多",
    direction_norm: "bullish",
    confidence: 0.4,
    importance_score: 92.17,
    factor_value: 0.55,
    factor_scores: { score: 55, version: "factor-v2" },
    industries: ["宏观政策"],
    symbols: ["600519"],
    stocks: [
      { code: "600519", name: "贵州茅台", source_name: "贵州茅台", reason: "标题明确提及其定向增发" },
      { code: "000016", name: null, source_name: "*ST康佳A", reason: null },
    ],
    person: [],
    source: "xiaoshi-archive",
    original_source: "财联社",
    source_url: "https://example.invalid/a.pdf",
    content_hash: "a1b2c3d4e5f60718",
    quality_status: "stable",
    source_time_quality: "exact",
    dedup_key: "news:1",
    record_version: 1,
    revision_id: "b9b0c3acd4a4e0abd45fc0e6",
    revision_time: null,
    is_corrected: false,
    correction_count: 0,
    ...overrides,
  };
}

function group(key: string, overrides: Partial<EventDetail> = {}) {
  const found = detailGroups(detail(overrides)).find((item) => item.key === key);
  if (!found) throw new Error(`没有这一组：${key}`);
  return found.rows;
}

function valueOf(key: string, label: string, overrides: Partial<EventDetail> = {}): string {
  const row = group(key, overrides).find((item) => item.label === label);
  if (!row) throw new Error(`${key} 组里没有 ${label}`);
  return row.value;
}

describe("stockLine", () => {
  it("代码 + 名称 + 理由", () => {
    expect(
      stockLine({ code: "600519", name: "贵州茅台", source_name: "贵州茅台", reason: "中标主体" }),
    ).toBe("600519 贵州茅台 — 中标主体");
  });

  it("没有理由时给 —（不是不显示这一项）", () => {
    expect(stockLine({ code: "000016", name: null, source_name: "*ST康佳A", reason: null })).toBe(
      "000016 *ST康佳A — —",
    );
  });

  it("字典缺名时退回平台名；两个都没有就只出代码", () => {
    expect(stockLine({ code: "600519", name: null, source_name: "贵州茅台", reason: null })).toContain(
      "贵州茅台",
    );
    expect(stockLine({ code: "600519", name: null, source_name: null, reason: null })).toMatch(
      /^600519 — /,
    );
  });
});

describe("factorText", () => {
  it("键值对展开", () => {
    expect(factorText({ score: 55, version: "factor-v2" })).toBe("score=55 · version=factor-v2");
  });

  it("空对象给 —（person / policy 类实测就是空的）", () => {
    expect(factorText({})).toBe(EMPTY);
  });
});

describe("hoursBetween", () => {
  it("两个时刻之差（小时）", () => {
    expect(hoursBetween("2026-09-30T20:00:00+08:00", "2026-09-30T22:30:00+08:00")).toBe(2.5);
  });

  it("任一为空就没有差可言", () => {
    expect(hoursBetween(null, "2026-09-30T22:30:00+08:00")).toBeNull();
  });
});

describe("detailGroups", () => {
  it("七组齐全（走查按 data-detail 断言这七个 key）", () => {
    const keys = detailGroups(detail()).map((group) => group.key);
    expect(keys).toEqual(["source", "revision", "stocks", "person", "factor"]);
  });

  it("原文有值才给 href —— 空时不给一个点不动的链接", () => {
    const withUrl = group("source").find((row) => row.label === "原文");
    expect(withUrl?.href).toBe("https://example.invalid/a.pdf");

    const without = group("source", { source_url: null }).find((row) => row.label === "原文");
    expect(without?.value).toBe(EMPTY);
    expect(without?.href).toBeUndefined();
  });

  it("修订四件照原样列，不加工成「已修订 N 次」", () => {
    expect(valueOf("revision", "记录版本", { record_version: 3 })).toBe("3");
    expect(valueOf("revision", "是否修订过", { is_corrected: true })).toBe("是");
    expect(valueOf("revision", "修正次数", { correction_count: 2 })).toBe("2");
    expect(valueOf("revision", "修订时刻", { revision_time: null })).toBe(EMPTY);
    // 平台没标记时也要显示事实，而不是留空
    expect(valueOf("revision", "是否修订过")).toBe("否");
  });

  it("关联标的逐只出，空理由给 —", () => {
    const rows = group("stocks");
    expect(rows).toHaveLength(2);
    expect(rows[0].value).toContain("标题明确提及其定向增发");
    expect(rows[1].value.endsWith("—")).toBe(true);
  });

  it("脏提及（空代码 / 「数据缺失」）不进关联标的，但明说丢了几条", () => {
    const rows = group("stocks", {
      stocks: [
        { code: "600519", name: "贵州茅台", source_name: "贵州茅台", reason: "中标主体" },
        { code: "", name: null, source_name: null, reason: null },
        { code: "数据缺失", name: null, source_name: null, reason: "行业整体下挫" },
      ],
    });

    expect(rows).toHaveLength(2);
    expect(rows[0].label).toBe("600519");
    expect(rows[1].label).toBe("未带代码");
    expect(rows[1].value).toContain("2 条");
  });

  it("没有标的时不编，给一条 —", () => {
    expect(group("stocks", { stocks: [] })).toEqual([{ label: "标的", value: EMPTY }]);
  });

  it("人物为空给 —", () => {
    expect(valueOf("person", "提及")).toBe(EMPTY);
    expect(valueOf("person", "提及", { person: ["高善文"] })).toBe("高善文");
  });

  it("归一方向取中文标签；归一不出来时给 —（平台原词在上一行，不会丢信息）", () => {
    expect(valueOf("factor", "归一方向")).toBe("利多");
    expect(valueOf("factor", "归一方向", { direction_norm: null })).toBe(EMPTY);
  });
});

describe("stampRows", () => {
  it("四个时刻并列，滞后按 报告可得 → 平台可得 算", () => {
    const rows = stampRows(detail());
    expect(rows.map((row) => row.label)).toEqual(["事发", "报告可得", "平台可得", "观察于", "可得滞后"]);
    // 数值换算由 `hoursBetween` 自己守（见上），这里只验它接上了 `lagText` 的文案
    expect(rows[4].value).toContain("小时");
    // 差得够大时才说得出小时数（5 分钟会被 `lagText` 收成「1 小时内」）
    expect(stampRows(detail({ reported_available_at: "2026-09-30T18:00:00+08:00" }))[4].value).toContain(
      "2.8",
    );
  });
});

describe("DIRECTION", () => {
  it("三档都有一处定义（详情抽屉与事件表共用）", () => {
    expect(Object.keys(DIRECTION).sort()).toEqual(["bearish", "bullish", "neutral"]);
  });
});
