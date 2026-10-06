/**
 * 图表配色：ECharts 与 Lightweight Charts 都在 canvas 上绘制，**读不到 CSS 变量**。
 *
 * 这是主题切换最容易踩的一处：只改 `<html class="dark">` 时页面会变、图表不会变，
 * 必须拿这里的字面值显式重绘。所以同一套 token 在这里有一份字面值副本，
 * `lib/chart-theme.test.ts` 会读 `app/globals.css` 校验两边一致，防止改了一处忘了另一处。
 *
 * 取值依据见 `docs/private/Pn-n/P1-Tn/P1-T6-design-brief.md` §2/§3：
 * 序列、涨跌两套配色都跑过 dataviz 的 validate_palette.js，六项检查通过。
 */

export interface ChartTokens {
  /** 图表容器底色 */
  surface: string;
  /** 网格线 */
  grid: string;
  /** 轴刻度文字 */
  axis: string;
  /** 主要文字（tooltip 正文） */
  ink: string;
  /** 次要文字（图例） */
  ink2: string;
  /** 净值曲线：策略 */
  series1: string;
  /** 净值曲线：基准 */
  series2: string;
  /** 涨（A 股口径红） */
  up: string;
  /** 跌（A 股口径绿） */
  down: string;
}

export const CHART_TOKENS: Record<"light" | "dark", ChartTokens> = {
  light: {
    surface: "#fcfcfb",
    grid: "#e9e9e4",
    axis: "#8a9099",
    ink: "#16181a",
    ink2: "#5a6069",
    series1: "#2a78d6",
    series2: "#eb6834",
    up: "#d03b3b",
    down: "#0e8f6b",
  },
  dark: {
    surface: "#131722",
    grid: "#22262e",
    axis: "#6b7480",
    ink: "#e8eaed",
    ink2: "#9aa4b2",
    series1: "#3987e5",
    series2: "#d95926",
    up: "#ef5350",
    down: "#26a69a",
  },
};

export function chartTokens(isDark: boolean): ChartTokens {
  return isDark ? CHART_TOKENS.dark : CHART_TOKENS.light;
}
