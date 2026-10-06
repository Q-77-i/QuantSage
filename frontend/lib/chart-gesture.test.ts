import { describe, expect, it } from "vitest";

import {
  MIN_VISIBLE_BARS,
  isFullView,
  panByPixels,
  panRange,
  pinchFactor,
  zoomRange,
} from "./chart-gesture";

describe("pinchFactor", () => {
  it("正负对称：放大再缩小回到 1", () => {
    expect(pinchFactor(6) * pinchFactor(-6)).toBeCloseTo(1, 10);
  });

  it("下滑（deltaY > 0）放大跨度 = 缩小", () => {
    expect(pinchFactor(5)).toBeGreaterThan(1);
  });

  it("捏开（deltaY < 0）缩小跨度 = 放大", () => {
    expect(pinchFactor(-5)).toBeLessThan(1);
  });

  it("零增量不改变跨度", () => {
    expect(pinchFactor(0)).toBe(1);
  });

  it("比库内写死的 deltaY/100 灵敏——触控板捏合的增量很小", () => {
    // 触控板捏合常见增量 ±2，旧行为只有 2% 的跨度变化
    expect(1 / pinchFactor(-2)).toBeGreaterThan(1.02);
  });
});

describe("isFullView", () => {
  it("整段可见即全览", () => {
    expect(isFullView({ from: 0, to: 54 }, 55)).toBe(true);
  });

  it("容差内仍算全览（LWC 两端会带小数留白）", () => {
    expect(isFullView({ from: -0.5, to: 54.5 }, 55)).toBe(true);
  });

  it("真缩放了就不是全览", () => {
    expect(isFullView({ from: 10, to: 40 }, 55)).toBe(false);
  });

  it("样本为空时视为全览，不显示重置", () => {
    expect(isFullView({ from: 0, to: 0 }, 1)).toBe(true);
  });
});

describe("zoomRange", () => {
  const full = { from: 0, to: 54 }; // 55 根

  it("锚点不动：指针下的那根 bar 收放前后落在同一相对位置", () => {
    const anchor = 20;
    const next = zoomRange(full, anchor, 0.5, 55);
    // 锚点在原区间的相对位置 = 锚点在新区间的相对位置
    const ratioBefore = (anchor - full.from) / (full.to - full.from);
    const ratioAfter = (anchor - next.from) / (next.to - next.from);
    expect(ratioAfter).toBeCloseTo(ratioBefore, 10);
  });

  it("放大到上限即回到全览", () => {
    expect(zoomRange(full, 27, 100, 55)).toEqual(full);
  });

  it("缩小不会把跨度撑过全览", () => {
    expect(zoomRange(full, 27, 100, 55)).toEqual(full);
  });

  it("夹在左边界内", () => {
    const next = zoomRange({ from: 0, to: 54 }, 0, 0.2, 55);
    expect(next.from).toBeGreaterThanOrEqual(0);
    expect(next.from).toBe(0);
  });

  it("夹在右边界内", () => {
    const next = zoomRange({ from: 0, to: 54 }, 54, 0.2, 55);
    expect(next.to).toBeLessThanOrEqual(54);
    expect(next.to).toBe(54);
  });

  it("跨度不会小于最小值——再少画不出蜡烛", () => {
    let range = { from: 0, to: 54 };
    for (let i = 0; i < 100; i += 1) range = zoomRange(range, 27, 0.5, 55);
    expect(range.to - range.from).toBeCloseTo(MIN_VISIBLE_BARS - 1, 10);
  });

  it("样本只有一根时直接返回全览，不做除法", () => {
    expect(zoomRange({ from: 0, to: 0 }, 0, 0.5, 1)).toEqual({ from: 0, to: 0 });
  });

  it("样本为空时不产生 NaN", () => {
    expect(zoomRange({ from: 0, to: 0 }, 0, 0.5, 0)).toEqual({ from: 0, to: 0 });
  });
});

describe("panRange", () => {
  it("跨度不变，只挪位置", () => {
    const next = panRange({ from: 10, to: 30 }, 5, 55);
    expect(next).toEqual({ from: 15, to: 35 });
  });

  it("向左到头即停", () => {
    expect(panRange({ from: 10, to: 30 }, -100, 55)).toEqual({ from: 0, to: 20 });
  });

  it("向右到头即停（末根 bar 的序号是 total-1）", () => {
    expect(panRange({ from: 10, to: 30 }, 100, 55)).toEqual({ from: 34, to: 54 });
  });

  it("全览时无地可挪", () => {
    expect(panRange({ from: 0, to: 54 }, 10, 55)).toEqual({ from: 0, to: 54 });
  });
});

describe("panByPixels", () => {
  it("按可视跨度与画布宽度换算", () => {
    // 跨度 50 根铺在 1000px 上 → 100px 对应 5 根
    expect(panByPixels(100, 50, 1000)).toBeCloseTo(5, 10);
  });

  it("宽度为 0 时不做除法", () => {
    expect(panByPixels(100, 50, 0)).toBe(0);
  });
});
