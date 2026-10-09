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
