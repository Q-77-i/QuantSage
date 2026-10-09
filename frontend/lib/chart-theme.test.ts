import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { CHART_TOKENS } from "./chart-theme";

/**
 * 两个图表库在 canvas 上绘制，读不到 CSS 变量，所以配色在 `chart-theme.ts` 里
 * 有一份字面值副本。这个测试守住两边一致：否则会出现「页面换了色、图表没换」
 * 这种只在肉眼对比时才能发现的漂移。
 */
const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`globals.css 里找不到 ${selector} 块`);
  return css.slice(start, css.indexOf("\n}", start));
}

function valueIn(selector: string, name: string): string {
  const matched = block(selector).match(new RegExp(`--${name}:\\s*([^;]+);`));
  if (!matched) throw new Error(`${selector} 里找不到 --${name}`);
  return matched[1].trim().toLowerCase();
}

const MAPPING = [
  ["surface", "chart-surface"],
  ["grid", "grid"],
  ["axis", "ink-3"],
  ["ink", "ink-1"],
  ["ink2", "ink-2"],
  ["series1", "series-1"],
  ["series2", "series-2"],
  ["series3", "series-3"],
  ["up", "up"],
  ["down", "down"],
] as const;

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
] as const)("图表配色与 CSS 变量一致（%s）", (mode, selector) => {
  for (const [tokenKey, cssVar] of MAPPING) {
    it(`${tokenKey} == --${cssVar}`, () => {
      expect(CHART_TOKENS[mode][tokenKey]).toBe(valueIn(selector, cssVar));
    });
  }
});

/**
 * 分层色阶是**数组**，条数也要对得上：CSS 变量少一档时下面的循环只会少跑一轮、
 * 静默通过，故先钉长度。
 */
describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
] as const)("因子分层色阶与 CSS 变量一致（%s）", (mode, selector) => {
  it("5 档逐一对应 --chart-group-1..5", () => {
    expect(CHART_TOKENS[mode].group).toHaveLength(5);
    CHART_TOKENS[mode].group.forEach((value, index) => {
      expect(value).toBe(valueIn(selector, `chart-group-${index + 1}`));
    });
  });

  it("单色相蓝阶：每一档的 B 分量都高于 R（防手滑换进别的色族）", () => {
    for (const value of CHART_TOKENS[mode].group) {
      expect(parseInt(value.slice(5, 7), 16)).toBeGreaterThan(parseInt(value.slice(1, 3), 16));
    }
  });
});

it("深色分层色阶不是浅色的翻转（各自选步）", () => {
  expect(CHART_TOKENS.dark.group).not.toEqual(CHART_TOKENS.light.group);
});

/**
 * `diverge` 不参与上面的 CSS 一致性校验：它**不是**从 CSS 变量来的，而是按 dataviz
 * 方法现推的一支 diverging 色阶（两臂各自过 `validateOrdinal`，推导与读数见
 * `P2-M5b-design-brief.md` §二）。这里钉住的是它的**结构**——结构错了色阶就不表示正负。
 */
describe("网格热力图的 diverging 色阶", () => {
  it("负 → 0 → 正的 7 档，中点是对称中心", () => {
    for (const mode of ["light", "dark"] as const) {
      const ramp = CHART_TOKENS[mode].diverge;
      expect(ramp).toHaveLength(7);
      expect(ramp[3]).toBe(mode === "light" ? "#f0efec" : "#4a4b52"); // 中灰就在第 4 档
    }
  });

  it("两端的色相是相反的（红端与蓝端不是同一族）", () => {
    const ramp = CHART_TOKENS.light.diverge;
    const red = ramp[0];
    const blue = ramp[6];
    // 红端 R 分量显著高于 B，蓝端相反——写死这条是为了防「有人手滑把一端替换成同族色」
    expect(parseInt(red.slice(1, 3), 16)).toBeGreaterThan(parseInt(red.slice(5, 7), 16));
    expect(parseInt(blue.slice(5, 7), 16)).toBeGreaterThan(parseInt(blue.slice(1, 3), 16));
  });

  it("深色不是浅色的自动翻转（各自选步）", () => {
    expect(CHART_TOKENS.dark.diverge).not.toEqual(CHART_TOKENS.light.diverge);
    expect(CHART_TOKENS.dark.diverge[0]).not.toBe(CHART_TOKENS.light.diverge[0]);
  });
});
