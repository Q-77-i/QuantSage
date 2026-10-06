import { describe, expect, it } from "vitest";

import type { WatchlistItem } from "./types";
import { DEFAULT_GROUP, groupItems, groupNames, validateSymbol } from "./watchlist";

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
