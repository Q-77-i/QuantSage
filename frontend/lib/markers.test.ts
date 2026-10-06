import { describe, expect, it } from "vitest";

import { buildMarkers, openPositionMarker, tradesToMarkers } from "./markers";
import type { Bar, Trade } from "./types";

const PALETTE = { up: "#d03b3b", down: "#0e8f6b" };

function bars(dates: string[]): Bar[] {
  return dates.map((time) => ({
    time,
    open: 1,
    high: 1,
    low: 1,
    close: 1,
    volume: 1,
    is_suspended: false,
  }));
}

function trade(entry: string, exit: string, qty = 100): Trade {
  return {
    entry_date: entry,
    exit_date: exit,
    pnl: 0,
    reason: "信号",
    entry_reason: "信号",
    return_pct: 0,
    hold_bars: 1,
    qty,
  };
}

const BARS = bars(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]);

describe("tradesToMarkers", () => {
  it("每笔成交生成买卖两个点，时间取自成交记录", () => {
    const markers = tradesToMarkers([trade("2026-07-02", "2026-07-06")], BARS, PALETTE);

    expect(markers.map((marker) => [marker.time, marker.shape])).toEqual([
      ["2026-07-02", "arrowUp"],
      ["2026-07-06", "arrowDown"],
    ]);
    expect(markers[0].position).toBe("belowBar");
    expect(markers[1].position).toBe("aboveBar");
    expect(markers[0].text).toContain("100");
  });

  it("成交日不在 bar 序列里时丢弃 —— v5 会静默丢点，所以在源头就挡掉", () => {
    const markers = tradesToMarkers([trade("2026-07-04", "2026-07-05")], BARS, PALETTE);
    expect(markers).toHaveLength(0);
  });

  it("乱序输入也输出升序 —— v5 要求 markers 升序，否则静默丢弃", () => {
    const trades = [trade("2026-07-06", "2026-07-07"), trade("2026-07-01", "2026-07-02")];
    const markers = tradesToMarkers(trades, BARS, PALETTE);

    expect(markers.map((marker) => marker.time)).toEqual([
      "2026-07-01",
      "2026-07-02",
      "2026-07-06",
      "2026-07-07",
    ]);
  });

  it("同日买卖时买入排在卖出前", () => {
    const markers = tradesToMarkers([trade("2026-07-02", "2026-07-02")], BARS, PALETTE);

    expect(markers.map((marker) => marker.shape)).toEqual(["arrowUp", "arrowDown"]);
  });

  it("用调用方给的配色 —— canvas 读不到 CSS 变量", () => {
    const dark = { up: "#ef5350", down: "#26a69a" };
    const markers = tradesToMarkers([trade("2026-07-02", "2026-07-06")], BARS, dark);

    expect(markers[0].color).toBe("#ef5350");
    expect(markers[1].color).toBe("#26a69a");
  });

  it("没有成交时返回空数组", () => {
    expect(tradesToMarkers([], BARS, PALETTE)).toEqual([]);
  });
});

describe("openPositionMarker", () => {
  it("期末持仓补一个买入点，免得图上只买不卖像是 bug", () => {
    const marker = openPositionMarker({ entry_date: "2026-07-03", shares: 200 }, BARS, PALETTE);

    expect(marker?.time).toBe("2026-07-03");
    expect(marker?.shape).toBe("arrowUp");
    expect(marker?.text).toContain("持有中");
  });

  it("无持仓或日期不在 bar 序列里时不给点", () => {
    expect(openPositionMarker(null, BARS, PALETTE)).toBeNull();
    expect(
      openPositionMarker({ entry_date: "2026-07-04", shares: 200 }, BARS, PALETTE),
    ).toBeNull();
  });
});

describe("buildMarkers", () => {
  it("成交与期末持仓合并后整体升序 —— setMarkers 只对整组要求升序", () => {
    const markers = buildMarkers(
      [trade("2026-07-06", "2026-07-07")],
      { entry_date: "2026-07-02", shares: 300 },
      BARS,
      PALETTE,
    );

    expect(markers.map((marker) => marker.time)).toEqual([
      "2026-07-02",
      "2026-07-06",
      "2026-07-07",
    ]);
  });

  it("持仓买入点排在末位时也照样升序", () => {
    const markers = buildMarkers(
      [trade("2026-07-01", "2026-07-02")],
      { entry_date: "2026-07-07", shares: 300 },
      BARS,
      PALETTE,
    );

    expect(markers.map((marker) => marker.time)).toEqual([
      "2026-07-01",
      "2026-07-02",
      "2026-07-07",
    ]);
    expect(markers.at(-1)?.text).toContain("持有中");
  });

  it("窗口外的成交与持仓都不产生幽灵点（K 线只有窗口内的 bar）", () => {
    const markers = buildMarkers(
      [trade("2026-06-01", "2026-06-02")],
      { entry_date: "2026-08-01", shares: 300 },
      BARS,
      PALETTE,
    );

    expect(markers).toEqual([]);
  });

  it("无成交无持仓时返回空数组", () => {
    expect(buildMarkers([], null, BARS, PALETTE)).toEqual([]);
  });
});
