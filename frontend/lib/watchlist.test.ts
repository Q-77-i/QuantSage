import { describe, expect, it } from "vitest";

import type { WatchlistItem } from "./types";
import {
  DEFAULT_GROUP,
  addFormHint,
  groupItems,
  groupNames,
  isNameQuery,
  validateSymbol,
} from "./watchlist";

function item(symbol: string, group: string, addedAt: string): WatchlistItem {
  return {
    symbol,
    group_name: group,
    added_at: addedAt,
    added_price: 10,
    latest_close: 11,
    latest_trade_date: "2026-07-03",
    change_pct: 0.1,
  };
}

describe("validateSymbol —— 六位数字，前后空白不算错", () => {
  it("合法代码通过", () => {
    expect(validateSymbol("600519")).toBeNull();
    expect(validateSymbol("  600519  ")).toBeNull();
  });

  it("空输入与位数不对各有各的说法", () => {
    expect(validateSymbol("")).toMatch(/请输入/);
    expect(validateSymbol("   ")).toMatch(/请输入/);
    expect(validateSymbol("60051")).toMatch(/6 位/);
    expect(validateSymbol("6005199")).toMatch(/6 位/);
    expect(validateSymbol("abcdef")).toMatch(/6 位/);
  });
});

describe("groupItems —— 默认分组置顶，其余按最早加入时间", () => {
  it("默认分组即使加得最晚也排第一", () => {
    const groups = groupItems([
      item("600519", "长线", "2026-07-01T00:00:00Z"),
      item("300750", DEFAULT_GROUP, "2026-07-09T00:00:00Z"),
      item("600036", "核心", "2026-07-05T00:00:00Z"),
    ]);

    expect(groups.map((group) => group.name)).toEqual([DEFAULT_GROUP, "长线", "核心"]);
  });

  it("组内按加入时间升序，不按涨幅", () => {
    const groups = groupItems([
      item("600519", "核心", "2026-07-05T00:00:00Z"),
      item("300750", "核心", "2026-07-01T00:00:00Z"),
    ]);

    expect(groups[0].items.map((row) => row.symbol)).toEqual(["300750", "600519"]);
  });

  it("空列表就是空分组，不凭空造一个默认分组出来", () => {
    expect(groupItems([])).toEqual([]);
    expect(groupNames([])).toEqual([]);
  });

  it("分组名列表与展示顺序一致", () => {
    expect(
      groupNames([
        item("600519", "长线", "2026-07-01T00:00:00Z"),
        item("300750", DEFAULT_GROUP, "2026-07-02T00:00:00Z"),
      ]),
    ).toEqual([DEFAULT_GROUP, "长线"]);
  });
});

describe("addFormHint —— 加自选表单边输边给的提示", () => {
  const rows = [item("600519", "核心", "2026-07-01T00:00:00Z")];

  it("没输满六位不打扰：正在打字不该挨骂", () => {
    for (const raw of ["", "6", "60051", "abcdef"]) {
      expect(addFormHint(raw, rows, { status: "idle" }).kind).toBe("idle");
    }
  });

  it("前后空白不算输错", () => {
    expect(addFormHint("  600519  ", rows, { status: "idle" }).kind).toBe("duplicate");
  });

  it("已在自选 → 报分组，且压过体检结果", () => {
    const hint = addFormHint("600519", rows, {
      status: "found",
      code: "600519",
      date: "2026-09-30",
      close: 1258.62,
    });
    expect(hint).toEqual({ kind: "duplicate", group: "核心" });
  });

  it("有数据 → 带最近交易日与收盘价", () => {
    expect(
      addFormHint("600036", rows, {
        status: "found",
        code: "600036",
        date: "2026-09-30",
        close: 41.5,
      }),
    ).toEqual({ kind: "found", date: "2026-09-30", close: 41.5 });
  });

  it("本地没有这个代码 → missing（按钮据此禁用）", () => {
    expect(addFormHint("123456", rows, { status: "missing", code: "123456" })).toEqual({
      kind: "missing",
    });
  });

  it("行情层不可用 → unknown，与 missing 分开（前者不禁用）", () => {
    expect(addFormHint("600036", rows, { status: "unknown", code: "600036" })).toEqual({
      kind: "unknown",
    });
  });

  it("结果属于旧代码 → 当作还在查，不贴到新输入上", () => {
    // 输 600519 → 改成 600036，而 600519 的响应刚回来：那是别人的答案
    expect(addFormHint("600036", rows, { status: "missing", code: "600519" })).toEqual({
      kind: "checking",
    });
    expect(
      addFormHint("600036", rows, { status: "checking", code: "600036" }),
    ).toEqual({ kind: "checking" });
  });

  it("列表还没加载完（null）时不判重，照常走体检", () => {
    expect(
      addFormHint("600036", null, { status: "found", code: "600036", date: null, close: null }),
    ).toEqual({ kind: "found", date: null, close: null });
  });
});

describe("isNameQuery（M5a 名称搜索的分岔口）", () => {
  it("代码一律走代码路径", () => {
    expect(isNameQuery("600519")).toBe(false);
    expect(isNameQuery("6005")).toBe(false); // 没输满也是代码路径
    expect(isNameQuery(" 600519 ")).toBe(false);
  });

  it("含非数字就走名称搜索", () => {
    expect(isNameQuery("茅台")).toBe(true);
    expect(isNameQuery("贵州茅台")).toBe(true);
    expect(isNameQuery("600519 ")).toBe(false);
    expect(isNameQuery("60051a")).toBe(true); // 混了字母也算名称——按形状分流，不猜意图
  });

  it("空输入两条路径都不走", () => {
    expect(isNameQuery("")).toBe(false);
    expect(isNameQuery("   ")).toBe(false);
  });
});
