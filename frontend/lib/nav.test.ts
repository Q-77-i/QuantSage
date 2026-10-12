/**
 * 页头「当前项」判据的用例（`lib/nav.ts`）。
 *
 * 两条容易写错、且错了不报错的：根路径的前缀匹配会让全站点亮「首页」；
 * 深链（`?run=<id>`）与子路径（`/research/<id>`）要各自落到正确的项。
 */

import { describe, expect, it } from "vitest";

import { activeHref, isActive, NAV_ITEMS } from "./nav";

describe("activeHref", () => {
  it("根路径只有精确相等才算（前缀匹配会让全站点亮「首页」）", () => {
    expect(activeHref("/")).toBe("/");
    expect(activeHref("/backtest")).toBe("/backtest");
    expect(activeHref("/factor")).toBe("/factor");
  });

  it("对话页在 /chat 上点亮「对话」，而根治路径点亮「首页」（M10a 的搬迁判据）", () => {
    expect(activeHref("/chat")).toBe("/chat");
    expect(activeHref("/chat/")).toBe("/chat");
    // 反过来钉住搬迁：研究首页不该点亮「对话」，对话页也不该点亮「首页」
    expect(isActive("/", "/chat")).toBe(false);
    expect(isActive("/chat", "/")).toBe(false);
    // 前缀相同但不是同一项
    expect(activeHref("/chats")).toBeNull();
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

  it("研报页与深度研究都归「深度研究」（M8 起它是一级入口，不再是模拟盘的附属）", () => {
    expect(activeHref("/research")).toBe("/research");
    expect(activeHref("/research/225eb8a3-9f56")).toBe("/research");
    expect(activeHref("/research/runs/abc")).toBe("/research");
    // 反过来钉住那次错高亮：深度研究的页面**不该**点亮模拟盘
    expect(isActive("/research", "/paper")).toBe(false);
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
