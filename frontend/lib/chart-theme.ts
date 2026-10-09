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
  /** 净值曲线：同标的买入持有 */
  series2: string;
  /** 净值曲线：全市场等权基准（M5a） */
  series3: string;
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
    series3: "#8a5cd6",
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
    series3: "#9b7ce8",
    up: "#ef5350",
    down: "#26a69a",
  },
};

export function chartTokens(isDark: boolean): ChartTokens {
  return isDark ? CHART_TOKENS.dark : CHART_TOKENS.light;
}

/**
 * 画布上的字体栈，`globals.css` 的 `--font-mono` 副本。
 *
 * 图表上的文字几乎全是数字（价格轴、时间轴、标记旁的「买 700」），用等宽栈与表格的
 * `.num` 同一口径。**不随主题变**，故不走上面的 token 表，也不需要两份。
 */
export const CHART_FONT = 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace';
