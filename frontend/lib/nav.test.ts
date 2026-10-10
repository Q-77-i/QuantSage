/**
 * 页头「当前项」判据的用例（`lib/nav.ts`）。
 *
 * 两条容易写错、且错了不报错的：`/` 的前缀匹配会让全站都点亮「对话」；
 * 深链（`?run=<id>`）与子路径（`/research/<id>`）要各自落到正确的项。
 */

import { describe, expect, it } from "vitest";

import { activeHref, isActive, NAV_ITEMS } from "./nav";

describe("activeHref", () => {
  it("首页只有精确相等才算（前缀匹配会让全站点亮「对话」）", () => {
    expect(activeHref("/")).toBe("/");
    expect(activeHref("/backtest")).toBe("/backtest");
    expect(activeHref("/factor")).toBe("/factor");
  });

  it("子路径与末尾斜杠都算同一项", () => {
    expect(activeHref("/backtest/")).toBe("/backtest");
    expect(activeHref("/strategies/abc")).toBe("/strategies");
    expect(activeHref("/space/")).toBe("/space");
  });

  it("前缀相同但不是同一项的不点亮（/backtestX 不是回测）", () => {
    expect(activeHref("/backtesting")).toBeNull();
    expect(activeHref("/papers")).toBeNull();
  });

  it("研报页归到「模拟盘」（它是模拟盘账户的产物，入口也在那儿）", () => {
    expect(activeHref("/research/225eb8a3-9f56")).toBe("/paper");
    expect(isActive("/research/225eb8a3-9f56", "/paper")).toBe(true);
  });

  it("不在导航里的页面（公开分享页 / 打印页）不点亮任何一项", () => {
    expect(activeHref("/r/some-token")).toBeNull();
    expect(activeHref("/print/abc")).toBeNull();
    expect(activeHref(null)).toBeNull();
  });

  it("清单自身没有重复 href 或 key（新增项时别撞）", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    const keys = NAV_ITEMS.map((item) => item.key);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("每一项都能被自己的 href 点亮（清单与判据不漂移）", () => {
    for (const item of NAV_ITEMS) {
      expect(activeHref(item.href)).toBe(item.href);
    }
  });
});
