import { describe, expect, it } from "vitest";

import { hasMarket } from "./equity-curve";

describe("hasMarket（M5a 之前的报告没有这个字段）", () => {
  it("全无 market → 不画第三条线（旧报告）", () => {
    expect(hasMarket([{ date: "2026-08-03", equity: 1, benchmark: 1 }])).toBe(false);
  });

  it("有 market → 画", () => {
    expect(hasMarket([{ date: "2026-08-03", equity: 1, benchmark: 1, market: 1.01 }])).toBe(true);
  });

  it("null 不算有——后端在当日无样本时给 null，不该画一条贴着 0 的假线", () => {
    expect(hasMarket([{ date: "2026-08-03", equity: 1, benchmark: 1, market: null }])).toBe(false);
  });

  it("空序列不画", () => {
    expect(hasMarket([])).toBe(false);
  });
});
