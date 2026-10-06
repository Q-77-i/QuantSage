/**
 * 成交记录 → K 线买卖点。
 *
 * lightweight-charts v5 有两条**静默失败**的规则，违反时它不报错，只是把点丢掉：
 *   ① marker 的 `time` 必须与某根 bar 的 `time` **精确相等**；
 *   ② markers 必须按时间**升序**。
 *
 * 「K 线买卖点与交易明细一致」是 T6 的验收项，而这类失败在浏览器里表现为
 * 「图正常、点没了」，极难排查。所以映射做成纯函数并单测。
 *
 * 颜色由调用方传入（canvas 读不到 CSS 变量，见 `chart-theme.ts`）；
 * 形状同时编码方向（▲买 / ▼卖），不让颜色单独承载信息。
 */

import type { Bar, Trade } from "./types";

export interface SeriesMarker {
  time: string;
  position: "belowBar" | "aboveBar";
  color: string;
  shape: "arrowUp" | "arrowDown";
  text: string;
}

export interface MarkerPalette {
  up: string;
  down: string;
}

export function tradesToMarkers(
  trades: Trade[],
  bars: Bar[],
  palette: MarkerPalette,
): SeriesMarker[] {
  const known = new Set(bars.map((bar) => bar.time));
  const markers: SeriesMarker[] = [];

  for (const trade of trades) {
    if (known.has(trade.entry_date)) {
      markers.push({
        time: trade.entry_date,
        position: "belowBar",
        color: palette.up,
        shape: "arrowUp",
        text: `买 ${trade.qty}`,
      });
    }
    if (known.has(trade.exit_date)) {
      markers.push({
        time: trade.exit_date,
        position: "aboveBar",
        color: palette.down,
        shape: "arrowDown",
        text: "卖",
      });
    }
  }

  // v5 要求升序。同日买卖（事件策略换仓时会撞上）按「先买后卖」排——
  // 显式给序号，不靠 position 字符串的字母序（aboveBar 恰好排在 belowBar 前面）
  const order: Record<SeriesMarker["position"], number> = { belowBar: 0, aboveBar: 1 };
  return markers.sort(
    (a, b) => a.time.localeCompare(b.time) || order[a.position] - order[b.position],
  );
}

/** 持仓中的那一笔还没有卖方点，单独补一个买入标记，免得图上「只买不卖」像是 bug。 */
export function openPositionMarker(
  openPosition: { entry_date: string | null; shares: number } | null,
  bars: Bar[],
  palette: MarkerPalette,
): SeriesMarker | null {
  const date = openPosition?.entry_date;
  if (!date || !bars.some((bar) => bar.time === date)) return null;
  return {
    time: date,
    position: "belowBar",
    color: palette.up,
    shape: "arrowUp",
    text: `买 ${openPosition.shares}（持有中）`,
  };
}
