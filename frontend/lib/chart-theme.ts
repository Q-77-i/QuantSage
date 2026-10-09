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
  /**
   * 网格热力图的 diverging 色阶：**负 → 0 → 正**，7 档（M5b）。
   *
   * 为什么不是 `up`/`down` 那对红绿：跑 CVD 模拟实测它们的 ΔE 只有 **7.6**（deutan），
   * 落在 6–8 的 floor 带（配次级编码才合法）；蓝↔红是 **20.9 / 13.9**，远高于目标线 8。
   * 推导与四个臂各自的 `validateOrdinal` 读数见 `P2-M5b-design-brief.md` §二。
   */
  diverge: string[];
  /**
   * 因子分层曲线的 5 档蓝阶：**有序**类别（Q1 最低 → Q5 最高）用 ordinal ramp，
   * 不是五个任意色相。深色不是浅色的翻转——暗底上「越亮 = 越高」。
   * 两套各自跑过 `validate_palette.js --ordinal` 全 PASS（读数见 `P2-M5c-design-brief.md` §二）。
   */
  group: string[];
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
    diverge: ["#a00011", "#c2635b", "#dda7a1", "#f0efec", "#a1b6d3", "#5e82b5", "#184f95"],
    group: ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"],
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
    // 深色不是浅色的自动翻转：极色更亮（暗底上「越亮越大」），中点是另一个灰
    diverge: ["#f69b95", "#ba7e79", "#80615f", "#4a4b52", "#5c6b80", "#7492ba", "#8dbaf7"],
    group: ["#184f95", "#256abf", "#5598e7", "#86b6ef", "#b7d3f6"],
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
