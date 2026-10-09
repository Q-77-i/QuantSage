/**
 * 净值曲线的纯函数（M5a）。
 *
 * 单列一个模块而不是从 `components/backtest/equity-chart.tsx` 导出：那样测试一 import
 * 就把 ECharts 与 React 整条图拉进用例（本项目「Vitest 只测纯函数」的口径正是为了避开它）。
 */

import type { EquityPoint } from "./types";

/**
 * 这批净值点里有没有「全市场等权」基准。
 *
 * M5a 之前存下的回测记录**没有这个字段**（那时还没有这个基准），后端在当日无样本时
 * 也会给 `null`。两种都按「不画第三条线」处理——不补 0（那会画出一条贴着 0 的假线）、
 * 也不报错。图例项跟着少一个，见 `equity-chart.tsx`。
 */
export function hasMarket(points: EquityPoint[]): boolean {
  return points.some((point) => point.market !== undefined && point.market !== null);
}
