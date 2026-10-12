/**
 * 标的浏览视图模型的用例（`lib/stocks.ts`）。
 *
 * 两条最容易错且不报错的：**缺失不补 0**（没有涨跌幅 ≠ 平盘）、**名称为空时回落到代码**
 * 而不是编一个。分页的判据不在这里——它住在 `lib/paging.ts`（两处共用），用例在那边。
 */

import { describe, expect, it } from "vitest";

import {
  addButtonState,
  listSummary,
  PAGE_SIZE,
  SORT_CHOICES,
  sortLabel,
  stockRows,
  symbolTitle,
} from "./stocks";
import { EMPTY } from "./format";
import type { CrossRow, CrossSection } from "./types";

function row(overrides: Partial<CrossRow> = {}): CrossRow {
  return {
    symbol: "600519",
    name: "贵州茅台",
    close: 1258.62,
    change_pct: 1.864711309668321,
    amount: 4_797_246_636,
    turnover_pct: null,
    ...overrides,
  };
}

function section(overrides: Partial<CrossSection> = {}): CrossSection {
  return {
    trade_date: "2026-09-30",
    total: 5561,
    count: 50,
    items: [row()],
    adjust: "qfq",
    sort: "change_pct",
    order: "desc",
    industry: null,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}

describe("排序档位", () => {
  it("**不含换手率**——源侧整列为空，按它排等于随机序", () => {
    expect(SORT_CHOICES.map((choice) => choice.key)).not.toContain("turnover_pct");
  });

  it("认得的翻中文，认不得的原样返回", () => {
    expect(sortLabel("change_pct")).toBe("涨跌幅");
    expect(sortLabel("pe_ratio")).toBe("pe_ratio");
  });

  it("每页 50 条，在服务端上限（500）以内", () => {
    expect(PAGE_SIZE).toBeLessThanOrEqual(500);
  });
});

describe("列表行", () => {
  it("涨跌幅是**百分数**（不再乘 100），带号", () => {
    const [view] = stockRows(section());
    expect(view.change).toBe("+1.86%");
    expect(view.tone).toBe("up");
    expect(view.close).toBe("1,258.62");
    expect(view.amount).toBe("47.97");
  });

  it("成交额给 2 位小数——1 位会把 93 只小成交的显示成「0」", () => {
    const [view] = stockRows(section({ items: [row({ amount: 1_293_810 })] }));
    expect(view.amount).toBe("0.01"); // 1 位小数下这里是「0」
  });

  it("**缺失不补 0**：没有涨跌幅就是「—」且不染涨跌色", () => {
    const [view] = stockRows(section({ items: [row({ change_pct: null, amount: null })] }));
    expect(view.change).toBe(EMPTY);
    expect(view.tone).toBe("flat");
    expect(view.amount).toBe(EMPTY);
  });

  it("名称为空时回落到代码，并标出「没有名字」这件事", () => {
    const [view] = stockRows(section({ items: [row({ name: null })] }));
    expect(view.label).toBe("600519");
    expect(view.hasName).toBe(false);
  });

  it("空截面给空数组，不是一张画了 0 的表", () => {
    expect(stockRows(null)).toEqual([]);
    expect(stockRows(section({ items: [] }))).toEqual([]);
  });
});

describe("页头摘要", () => {
  it("带行业时把行业写进去，不带时说全市场", () => {
    expect(listSummary(section())).toBe("全市场 · 2026-09-30 · 共 5,561 只");
    expect(listSummary(section({ industry: "医药生物" }))).toBe("医药生物 · 2026-09-30 · 共 5,561 只");
    expect(listSummary(null)).toBe(EMPTY);
  });

  it("个股页标题：有名字就「名字 代码」，没名字就只给代码", () => {
    expect(symbolTitle("600519", "贵州茅台")).toBe("贵州茅台 600519");
    expect(symbolTitle("600519", null)).toBe("600519");
  });
});

describe("加自选按钮（轻量版）", () => {
  it("已在自选就禁用并说明，写入中显示进度", () => {
    expect(addButtonState(true, false)).toEqual({ label: "已在自选", disabled: true });
    expect(addButtonState(false, true)).toEqual({ label: "加入中…", disabled: true });
    expect(addButtonState(false, false)).toEqual({ label: "加入自选", disabled: false });
  });
});
