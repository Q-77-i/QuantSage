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

/** R 分量 vs B 分量：判「偏蓝」还是「偏红」。够用——这两支色阶的两端本来就分得很开。 */
function blueish(hex: string): boolean {
  return parseInt(hex.slice(5, 7), 16) > parseInt(hex.slice(1, 3), 16);
}

/** 到中性中点的 RGB 距离。「离中点越远颜色越强」这条性质的判据。 */
function distanceFromMid(hex: string, mid: string): number {
  const rgb = (value: string) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
  const [r1, g1, b1] = rgb(hex);
  const [r2, g2, b2] = rgb(mid);
  return Math.hypot(r1 - r2, g1 - g2, b1 - b2);
}

/**
 * 两支 diverging 色阶（`diverge` 夏普 / `divergePct` 涨跌幅）不参与上面的 CSS 一致性校验：
 * 它们**不是**从 CSS 变量来的，而是按 dataviz 方法现推的（两臂各自过 `validateOrdinal`，
 * 推导与读数见 `P2-M5b-design-brief.md` §二 与 `P2-M10a-design-brief.md` §四）。
 *
 * 这里钉住两件事：**结构**（结构错了就不表示正负）与**方向**。
 * 方向那条是 2026-10-12 补的——原测试只断言「两端色相相反」，**两个方向都能过**，
 * 于是 M5b 的热力图（`visualMap.inRange.color[0]` 映射到 **min**）与分布图、最优格标记
 * 各自朝相反的方向理解了好几个月，注释还写着「同一支语义」。
 */
describe.each(["diverge", "divergePct"] as const)("%s diverging 色阶", (key) => {
  it("七档，中灰就在第 4 档", () => {
    for (const mode of ["light", "dark"] as const) {
      const ramp = CHART_TOKENS[mode][key];
      expect(ramp).toHaveLength(7);
      expect(ramp[3]).toBe(mode === "light" ? "#f0efec" : "#4a4b52");
    }
  });

  it("数组按「数据从最小到最大」存：[0] 是冷端（蓝族）、[6] 是暖端（红族）", () => {
    for (const mode of ["light", "dark"] as const) {
      const ramp = CHART_TOKENS[mode][key];
      // 方向写反 = ECharts 把色阶整体反过来画，而「两端像素俱在」的断言看不出来
      expect(blueish(ramp[0])).toBe(true);
      expect(blueish(ramp[6])).toBe(false);
    }
  });

  it("离中点越远颜色越强——**两端是极色，不是最淡的那档**", () => {
    // 这条是 2026-10-12 补的：原测试查不出「两臂拼反」——拼反后两端变成最淡的色，
    // 于是**涨得最猛的板块拿到最淡的颜色**，而色相、档数、中点位置全都照样对。
    // 出图之后肉眼一看才发现（验证脚本读的是像素有无，读不出这个）。
    for (const mode of ["light", "dark"] as const) {
      const ramp = CHART_TOKENS[mode][key];
      const mid = ramp[3];
      // 从外往里：第 0 档离中点最远、第 2 档最近（另一端第 6/5/4 同理）
      for (const side of [[0, 1, 2], [6, 5, 4]]) {
        const [outer, middle, inner] = side.map((i) => distanceFromMid(ramp[i], mid));
        expect(outer).toBeGreaterThan(middle);
        expect(middle).toBeGreaterThan(inner);
      }
    }
  });

  it("深色不是浅色的自动翻转（各自选步）", () => {
    expect(CHART_TOKENS.dark[key]).not.toEqual(CHART_TOKENS.light[key]);
    expect(CHART_TOKENS.dark[key][0]).not.toBe(CHART_TOKENS.light[key][0]);
  });
});

it("divergePct 的跌极就是站点的 --down（浅深两色都是）", () => {
  // 涨跌幅色阶不是另起一套语言：跌极与站点既有绿同值，只有浅色涨极压暗过（见 chart-theme 注释）。
  // **极色在数组两端**（[0] = 最小值 = 跌，[6] = 最大值 = 涨），不是挨着中点那两档。
  expect(CHART_TOKENS.light.divergePct[0]).toBe(CHART_TOKENS.light.down);
  expect(CHART_TOKENS.dark.divergePct[0]).toBe(CHART_TOKENS.dark.down);
  // 涨极另起一档（站点红压暗过），故只钉「不等于 --up」——它**是**刻意不同的
  expect(CHART_TOKENS.light.divergePct[6]).not.toBe(CHART_TOKENS.light.up);
});
