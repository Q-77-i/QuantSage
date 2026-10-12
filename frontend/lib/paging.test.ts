/**
 * 分页纯函数的用例（`lib/paging.ts`）——标的列表与快讯流**共用这一支**。
 *
 * 分页错了不会报错，只会安静地多一页或少一页，所以三条边界都钉死：
 * 总页数至少 1、页码由 offset 反推、页码 ↔ offset 往返一致。
 */

import { describe, expect, it } from "vitest";

import { clampPage, offsetOf, pageOf, rangeText, totalPages } from "./paging";

describe("totalPages", () => {
  it("向上取整；**一条都没有时是 1 页**（不是 0——「第 1 / 0 页」没法读）", () => {
    expect(totalPages(5561, 50)).toBe(112);
    expect(totalPages(100, 50)).toBe(2);
    expect(totalPages(50, 50)).toBe(1);
    expect(totalPages(0, 50)).toBe(1);
  });

  it("limit 非法时不炸，按 1 页算", () => {
    expect(totalPages(100, 0)).toBe(1);
    expect(totalPages(100, -5)).toBe(1);
    expect(totalPages(Number.NaN, 50)).toBe(1);
  });
});

describe("pageOf / offsetOf", () => {
  it("当前页由服务端回的 offset 反推，不自己记一份", () => {
    expect(pageOf(0, 50)).toBe(1);
    expect(pageOf(50, 50)).toBe(2);
    expect(pageOf(5500, 50)).toBe(111);
  });

  it("**往返一致**——这是「翻页不丢条」的根", () => {
    for (const page of [1, 2, 37, 112, 326]) {
      expect(pageOf(offsetOf(page, 20), 20)).toBe(page);
    }
  });

  it("非法页码按第 1 页，小数向下取整", () => {
    expect(offsetOf(0, 50)).toBe(0);
    expect(offsetOf(-5, 50)).toBe(0);
    expect(offsetOf(Number.NaN, 50)).toBe(0);
    expect(offsetOf(2.7, 50)).toBe(50);
  });
});

describe("clampPage", () => {
  it("超范围落到**最后一页**，不是空白页", () => {
    expect(clampPage(9999, 100, 50)).toBe(2);
    expect(clampPage(0, 100, 50)).toBe(1);
    expect(clampPage(3, 100, 50)).toBe(2);
  });

  it("**总页数未知时只保下界、不猜上界**——猜了会把 `?page=7` 这种深链夹成第 1 页", () => {
    expect(clampPage(7, null, 50)).toBe(7);
    expect(clampPage(0, null, 50)).toBe(1);
  });
});

describe("rangeText", () => {
  it("这一页显示第几条到第几条（1 起、闭区间）", () => {
    expect(rangeText(0, 50, 5561)).toBe("1–50");
    expect(rangeText(5500, 50, 5561)).toBe("5501–5550");
    expect(rangeText(5550, 11, 5561)).toBe("5551–5561"); // 最后一页不满：右端夹到总数
  });

  it("空页给 null，不显示「0–0」", () => {
    expect(rangeText(9999, 0, 100)).toBeNull();
  });
});
