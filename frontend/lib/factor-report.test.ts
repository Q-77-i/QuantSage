import { describe, expect, it } from "vitest";

import {
  curveTable,
  factorHeadline,
  factorQuery,
  groupLines,
  groupNotes,
  icBars,
  icTable,
  longShortLines,
  noteParts,
  trackOf,
} from "./factor-report";
import type { FactorGroup, FactorReport } from "./types";

function group(quantile: number, net: FactorGroup["net"] = null): FactorGroup {
  return {
    quantile,
    label: `Q${quantile}`,
    turnover_avg: 0.5,
    turnover: [{ date: "2026-08-03", buy: 0.5, sell: 0.4 }],
    gross: {
      curve: [
        { date: "2026-08-03", level: 1.01 },
        { date: "2026-08-04", level: 1.02 },
      ],
      metrics: { total_return: 0.02, annual_return: 0.1, max_drawdown: 0.03, sharpe: 0.5 },
    },
    net,
  };
}

function report(overrides: Partial<FactorReport> = {}): FactorReport {
  return {
    params: {
      source: "event",
      factor: "factor_value",
      quantiles: 5,
      min_pool: 20,
      aggregation: "mean",
      horizon: "open_t1_to_open_t2",
      adjust: "qfq",
      costs: { fees: true, slippage: true, slippage_bps: 5, note: "费率口径，不含最低佣金" },
    },
    window: {
      start: "2026-08-03",
      end: "2026-08-04",
      first_signal_day: "2026-08-03",
      last_signal_day: "2026-08-04",
      signal_days: 2,
      skipped_no_window: 1,
      skipped_thin_pool: 0,
      corpus: { start: "2026-07-07", end: "2026-10-08", rows: 307704 },
      bars_end: "2026-09-30",
    },
    universe: {
      pool_avg: 254,
      pool_min: 30,
      pool_max: 928,
      dropped_no_price: 994,
      dropped_untradeable: 4,
      symbols_seen: 4686,
    },
    ic: {
      per_day: [
        { date: "2026-08-03", ic: 0.02, n: 240 },
        { date: "2026-08-04", ic: -0.01, n: 250 },
      ],
      mean: 0.005,
      std: 0.02,
      icir: 0.25,
      t_stat: 0.35,
      positive_days: 1,
      days: 2,
    },
    groups: [group(1), group(2), group(3), group(4), group(5)],
    long_short: {
      gross: { curve: [{ date: "2026-08-03", level: 1.0 }], metrics: { total_return: 0, annual_return: 0, max_drawdown: 0, sharpe: null } },
      net: null,
      t_stat: 0.57,
      tradable: false,
    },
    notes: ["因子池是「当日有向且挂了标的」的事件池，**不是全市场**", "多空价差是**统计量**：A 股不可做空"],
    ...overrides,
  };
}

describe("factorQuery", () => {
  it("只带非空参数（缺省窗口与缺省费用不写进查询串）", () => {
    // 与 `api.ts::query()` 同一约定：带前导 `?`，调用方直接拼在路径后面
    expect(factorQuery({ source: "event" })).toBe("?source=event");
    expect(factorQuery({ source: "price", direction: "momentum", costs: false })).toBe(
      "?source=price&direction=momentum&costs=false",
    );
  });

  it("窗口与方向原样带出（价格源才带方向）", () => {
    expect(
      factorQuery({ source: "event", start: "2026-07-10", end: "2026-09-30" }),
    ).toBe("?source=event&start=2026-07-10&end=2026-09-30");
  });
});

describe("icBars", () => {
  it("逐日 IC 原样映射，并数出正负两端", () => {
    const bars = icBars(report());
    expect(bars.points).toEqual([
      { date: "2026-08-03", ic: 0.02, n: 240 },
      { date: "2026-08-04", ic: -0.01, n: 250 },
    ]);
    expect(bars.hasPositive).toBe(true);
    expect(bars.hasNegative).toBe(true);
    expect(bars.empty).toBe(false);
  });

  it("没有有效信号日时给出空态而不是崩", () => {
    const bars = icBars(
      report({
        ic: { per_day: [], mean: null, std: null, icir: null, t_stat: null, positive_days: 0, days: 0 },
      }),
    );
    expect(bars.empty).toBe(true);
    expect(bars.points).toEqual([]);
    expect(bars.hasPositive).toBe(false);
  });
});

describe("trackOf / groupLines", () => {
  it("net 缺失时返回 null（前端据此刻画禁用态，不崩）", () => {
    const body = report();
    expect(trackOf(body.groups[0], "gross")?.curve).toHaveLength(2);
    expect(trackOf(body.groups[0], "net")).toBeNull();
    expect(groupLines(body, "net")).toBeNull();
  });

  it("分层曲线的日期取并集，组号即色阶档位（0 = Q1）", () => {
    const lines = groupLines(report(), "gross");
    expect(lines?.dates).toEqual(["2026-08-03", "2026-08-04"]);
    expect(lines?.series.map((s) => s.rampIndex)).toEqual([0, 1, 2, 3, 4]);
    expect(lines?.series[0].values).toEqual([1.01, 1.02]);
    expect(lines?.series[4].label).toBe("Q5");
  });
});

describe("longShortLines", () => {
  it("毛必有、净可缺；日期取自毛曲线", () => {
    const body = report();
    const lines = longShortLines(body);
    expect(lines.dates).toEqual(["2026-08-03"]);
    expect(lines.gross).toEqual([1.0]);
    expect(lines.net).toBeNull();
  });
});

describe("表格孪生", () => {
  it("IC 表逐日列出日期 / IC / 样本数", () => {
    expect(icTable(report())).toEqual([
      { date: "2026-08-03", ic: 0.02, n: 240 },
      { date: "2026-08-04", ic: -0.01, n: 250 },
    ]);
  });

  it("曲线表 = 日期 × (五个分组 + 多空)，缺净列时该列为 null 而不是 0", () => {
    const table = curveTable(report(), "gross");
    expect(table.dates).toEqual(["2026-08-03", "2026-08-04"]);
    expect(table.columns.map((c) => c.label)).toEqual(["Q1", "Q2", "Q3", "Q4", "Q5", "多空"]);
    expect(table.columns[0].values).toEqual([1.01, 1.02]);
    // 多空毛曲线只有第一天：第二天补 null（**不是 0**，也不把行丢掉）
    expect(table.columns[5].values).toEqual([1.0, null]);
  });
});

describe("factorHeadline", () => {
  it("|t| < 2 判为噪声区间（页面据此出「不显著」标记）", () => {
    const head = factorHeadline(report());
    expect(head.hero).toBe(0.005);
    expect(head.significant).toBe(false);
    expect(head.empty).toBe(false);
    expect(head.poolAvg).toBe(254);
  });

  it("t 恰好 ±2 算显著（阈值取严格小于）", () => {
    const body = report();
    body.ic.t_stat = -2.0;
    expect(factorHeadline(body).significant).toBe(true);
  });

  it("没有有效日时 hero 为 null 且不算显著", () => {
    const body = report({
      ic: { per_day: [], mean: null, std: null, icir: null, t_stat: null, positive_days: 0, days: 0 },
    });
    const head = factorHeadline(body);
    expect(head.hero).toBeNull();
    expect(head.significant).toBe(false);
    expect(head.empty).toBe(true);
  });
});

describe("groupNotes", () => {
  it("按口径 / 成本 / 读数性质分三组，认不出的落到「口径与边界」（不丢）", () => {
    const groups = groupNotes([
      "因子池是「当日有向且挂了标的」的事件池，**不是全市场**——分层只在池内排序",
      "费用按 `CostModel` 的**费率**施加，不含最低佣金 5 元",
      "若 IC 的 t 绝对值很小（|t| < 2），那是**噪声区间内的读数**",
      "一句将来才会出现的全新说明",
    ]);
    expect(groups.map((g) => g.title)).toEqual(["口径与边界", "成本", "读数的性质"]);
    expect(groups[0].items).toHaveLength(2);
    expect(groups[1].items).toHaveLength(1);
    expect(groups[2].items).toHaveLength(1);
  });

  it("空列表不产生空分组", () => {
    expect(groupNotes([])).toEqual([]);
  });
});

describe("noteParts", () => {
  it("按 ** 切成普通/加粗片段，不把星号留给用户看", () => {
    expect(noteParts("多空是**统计量**：不可做空")).toEqual([
      { text: "多空是", strong: false },
      { text: "统计量", strong: true },
      { text: "：不可做空", strong: false },
    ]);
    expect(noteParts("**整句加粗**")).toEqual([{ text: "整句加粗", strong: true }]);
    expect(noteParts("没有强调")).toEqual([{ text: "没有强调", strong: false }]);
  });
});
