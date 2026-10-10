import { describe, expect, it } from "vitest";

import {
  STRATEGY_PARAM_DEFAULTS,
  canDecide,
  effectiveParams,
  decisionCard,
  equitySeries,
  paperRequest,
  parseSymbols,
  pnlTone,
  progressRatio,
  reasonText,
  ruleDegradations,
  sideLabel,
  sourceRows,
  statusTone,
  stepLines,
  totalReturn,
} from "./paper";
import type { PaperDecision, PaperDecisionStatus, PaperEquityPoint } from "./types";

function decision(overrides: Partial<PaperDecision> = {}): PaperDecision {
  return {
    id: "d1",
    account_id: "a1",
    trade_date: "2026-08-24",
    symbol: "000001",
    side: "buy",
    est_qty: 22_000,
    est_price: 11.3175,
    reason: "ma_cross:golden MA5/MA20",
    event_id: null,
    sources: null,
    status: "pending",
    status_label: "待审批",
    decided_at: null,
    fill: null,
    reject_code: null,
    reject_reason: null,
    ...overrides,
  };
}

describe("statusTone", () => {
  it("把六态收成五种语气", () => {
    const tones = Object.fromEntries(
      (
        [
          "pending",
          "approved",
          "filled",
          "rejected",
          "expired",
          "unfilled",
        ] as PaperDecisionStatus[]
      ).map((status) => [status, statusTone(status)]),
    );
    expect(tones).toEqual({
      pending: "action",
      approved: "waiting",
      filled: "normal",
      rejected: "void",
      expired: "void",
      unfilled: "alert",
    });
  });

  it("只有待审批能批（前端只为禁用按钮，裁决权在服务端）", () => {
    expect(canDecide("pending")).toBe(true);
    for (const status of ["approved", "filled", "rejected", "expired", "unfilled"] as const) {
      expect(canDecide(status)).toBe(false);
    }
  });
});

describe("reasonText", () => {
  it("把策略前缀换成显示名", () => {
    expect(reasonText("ma_cross:golden MA5/MA20")).toBe("双均线 · golden MA5/MA20");
    expect(reasonText("event_driven:hold 5d")).toBe("事件驱动 · hold 5d");
  });

  it("不认识的写法原样返回，不猜", () => {
    expect(reasonText("unknown_thing:x")).toBe("unknown_thing:x");
    expect(reasonText("没有冒号")).toBe("没有冒号");
  });
});

describe("sideLabel", () => {
  it("买卖用中文", () => {
    expect(sideLabel("buy")).toBe("买入");
    expect(sideLabel("sell")).toBe("卖出");
  });
});

describe("sourceRows", () => {
  it("没有来源时返回空数组（卖出如实留空，不编一条）", () => {
    expect(sourceRows(decision())).toEqual([]);
  });

  it("事件时间与可用时间**并列**，且链接给可点的 href", () => {
    const rows = sourceRows(
      decision({
        sources: {
          title: "段永平发帖称买了3万股",
          original_source: "东方财富个股",
          source: "xiaoshi-archive",
          event_time: "2026-09-28 15:48:00+08:00",
          available_at: "2026-09-28 16:23:03+08:00",
          content_hash: "9a4a62b0eec1e5fdc27ac30c56410c567b7ec6ef6333082e1d49ccad63ccfa4d",
          source_url: "https://example.com/a",
        },
      }),
    );
    expect(rows.map((row) => row.label)).toEqual([
      "标题",
      "原始来源",
      "来源",
      "事发",
      "可得",
      "内容哈希",
      "链接",
    ]);
    expect(rows.at(-1)).toEqual({ label: "链接", value: "打开原文", href: "https://example.com/a" });
    expect(rows[5].value).toBe("9a4a62b0eec1…");
  });

  it("缺字段就少一行，不补空值", () => {
    const rows = sourceRows(decision({ sources: { source: "xiaoshi-archive" } }));
    expect(rows).toEqual([{ label: "来源", value: "xiaoshi-archive" }]);
  });
});

describe("decisionCard", () => {
  it("未成交时只有预估，没有成交行", () => {
    const card = decisionCard(decision());
    expect(card.estimate).toContain("预计买入 22,000 股");
    expect(card.actual).toBeNull();
    expect(card.reason).toBe("双均线 · golden MA5/MA20");
  });

  it("成交后**预估与成交两个数都在**（跳空日两者会不同）", () => {
    const card = decisionCard(
      decision({
        status: "filled",
        fill: {
          trade_date: "2026-08-25",
          qty: 21_600,
          price: 11.42,
          ref_price: 11.41,
          commission: 61.67,
          stamp_tax: 0,
          slippage_cost: 216,
          cash_delta: -246_793.67,
        },
      }),
    );
    expect(card.estimate).toContain("预计买入 22,000 股");
    expect(card.actual).toContain("实际成交 21,600 股 @ 11.42");
    expect(card.actual).toContain("2026-08-25");
  });

  it("卖出文案说明是清仓", () => {
    const card = decisionCard(decision({ side: "sell", est_qty: 400, est_price: 1240 }));
    expect(card.sideLabel).toBe("卖出");
    expect(card.estimate).toContain("卖出全部持仓 400 股");
  });

  it("驳回原因原样带出（服务端话术，前端不重写）", () => {
    expect(decisionCard(decision({ reject_reason: "一字涨停：全天无卖盘" })).reject).toBe(
      "一字涨停：全天无卖盘",
    );
  });
});

describe("stepLines", () => {
  it("零条的类别不占位", () => {
    const lines = stepLines({
      trade_date: "2026-08-25",
      filled: [decision({ id: "a" })],
      expired: [],
      unfilled: [],
      generated: [decision({ id: "b" }), decision({ id: "c" })],
    });
    expect(lines).toEqual([
      { tone: "normal", text: "成交 1 笔" },
      { tone: "action", text: "新生成 2 条待审批" },
    ]);
  });

  it("什么都没发生时返回空数组（调用方显示「没有发生任何事」）", () => {
    expect(
      stepLines({
        trade_date: "2026-08-25",
        filled: [],
        expired: [],
        unfilled: [],
        generated: [],
      }),
    ).toEqual([]);
  });

  it("没有推进过（undefined）也不当崩", () => {
    expect(stepLines(undefined)).toEqual([]);
  });
});

describe("pnlTone", () => {
  it("红涨绿跌，**零不染色**（与成交明细的既有判据一致）", () => {
    expect(pnlTone(1.5)).toBe("up");
    expect(pnlTone(-0.01)).toBe("down");
    expect(pnlTone(0)).toBeNull();
  });
});

describe("progressRatio / totalReturn", () => {
  it("比例落在 0–1，总数 0 时返 0", () => {
    expect(
      progressRatio({ start: "a", end: "b", as_of: "a", days_total: 65, days_done: 25 }),
    ).toBeCloseTo(25 / 65);
    expect(
      progressRatio({ start: "a", end: "b", as_of: "a", days_total: 0, days_done: 0 }),
    ).toBe(0);
  });

  it("总收益：初始资金为 0 时返 null，不编一个 0%", () => {
    expect(totalReturn(512_548.2, 500_000)).toBeCloseTo(0.0250964);
    expect(totalReturn(100, 0)).toBeNull();
  });
});

describe("ruleDegradations", () => {
  it("只挑出未生效的标的，并原样带出原因", () => {
    expect(
      ruleDegradations({
        "600519": { limit_check: "on", reason: null },
        "123456": { limit_check: "skipped", reason: "unknown_board" },
      }),
    ).toEqual([{ symbol: "123456", reason: "unknown_board" }]);
  });

  it("没有原因时如实写「未说明」，不编一个", () => {
    expect(ruleDegradations({ "123456": { limit_check: "skipped", reason: null } })).toEqual([
      { symbol: "123456", reason: "未说明" },
    ]);
  });
});

describe("equitySeries", () => {
  const curve: PaperEquityPoint[] = [
    { trade_date: "2026-08-24", cash: 500_000, market_value: 0, equity: 500_000 },
    { trade_date: "2026-08-25", cash: 253_206, market_value: 246_912, equity: 500_118 },
  ];

  it("曲线取净值，标记按成交日排序", () => {
    const series = equitySeries(curve, [
      decision({
        id: "b",
        fill: {
          trade_date: "2026-08-26",
          qty: 100,
          price: 10,
          ref_price: 10,
          commission: 5,
          stamp_tax: 0,
          slippage_cost: 0,
          cash_delta: -1005,
        },
      }),
      decision({
        id: "a",
        side: "sell",
        fill: {
          trade_date: "2026-08-25",
          qty: 200,
          price: 11,
          ref_price: 11,
          commission: 5,
          stamp_tax: 1,
          slippage_cost: 0,
          cash_delta: 2194,
        },
      }),
    ]);
    expect(series.dates).toEqual(["2026-08-24", "2026-08-25"]);
    expect(series.values).toEqual([500_000, 500_118]);
    expect(series.markers.map((m) => m.date)).toEqual(["2026-08-25", "2026-08-26"]);
    expect(series.markers.map((m) => m.side)).toEqual(["sell", "buy"]);
  });

  it("没有决策时标记为空", () => {
    expect(equitySeries(curve).markers).toEqual([]);
  });
});

describe("parseSymbols", () => {
  it("逗号 / 空格 / 换行 / 顿号都能分隔，去重保序", () => {
    expect(parseSymbols("600519, 000001\n600519 300750、601318").symbols).toEqual([
      "600519",
      "000001",
      "300750",
      "601318",
    ]);
  });

  it("坏输入如实回显，不静默吞掉", () => {
    const parsed = parseSymbols("600519, 茅台, 12345");
    expect(parsed.symbols).toEqual(["600519"]);
    expect(parsed.invalid).toEqual(["茅台", "12345"]);
  });

  it("超上限的部分单独列出", () => {
    const text = Array.from({ length: 22 }, (_, i) => String(600_000 + i)).join(" ");
    const parsed = parseSymbols(text);
    expect(parsed.symbols).toHaveLength(20);
    expect(parsed.overflow).toHaveLength(2);
  });
});

describe("paperRequest", () => {
  const form = {
    name: "纸上交易",
    initialCash: "500000",
    symbolsText: "600519 000001",
    strategy: "ma_cross" as const,
    strategyId: "",
    params: { fast: 5, slow: 20 },
    start: "2026-07-01",
    end: "",
    fees: true,
    slippage: true,
  };

  it("end 为空就不传这个键（后端缺省取池子最后一根 bar）", () => {
    const { body, error } = paperRequest(form);
    expect(error).toBeNull();
    expect(body).not.toBeNull();
    expect(Object.keys(body!)).not.toContain("end");
    expect(body!["symbols"]).toEqual(["600519", "000001"]);
  });

  it("填了 end 就带上", () => {
    expect(paperRequest({ ...form, end: "2026-09-30" }).body!["end"]).toBe("2026-09-30");
  });

  it("切了策略就只发该策略的参数（界面上看不见的东西不进请求）", () => {
    // 界面验证逮到的那条：event_driven + 旧策略的 fast/slow → 后端 422
    const eventForm = { ...form, strategy: "event_driven" as const, params: { fast: 5, slow: 20 } };
    expect(paperRequest(eventForm).body!["params"]).toEqual({});
    expect(
      paperRequest({ ...eventForm, params: STRATEGY_PARAM_DEFAULTS.event_driven }).body!["params"],
    ).toEqual({ min_score: 50, hold_days: 5 });
    expect(effectiveParams("ma_cross", { fast: 8, slow: 15, min_score: 50 })).toEqual({
      fast: 8,
      slow: 15,
    });
    // 用户策略的参数 schema 在源码里，这张表单不渲染它 ⇒ 一个都不发（缺省由沙箱补）
    expect(effectiveParams("user", { fast: 5 })).toEqual({});
  });

  it("用户策略必须带 strategy_id", () => {
    const missing = paperRequest({ ...form, strategy: "user" });
    expect(missing.body).toBeNull();
    expect(missing.error).toContain("已保存的策略");
    const ok = paperRequest({ ...form, strategy: "user", strategyId: "sid-1" });
    expect(ok.body!["strategy_id"]).toBe("sid-1");
  });

  it("逐条本地必填校验（后端那套更严，这里只管别发空请求）", () => {
    expect(paperRequest({ ...form, name: " " }).error).toContain("名字");
    expect(paperRequest({ ...form, initialCash: "0" }).error).toContain("正数");
    expect(paperRequest({ ...form, initialCash: "abc" }).error).toContain("正数");
    expect(paperRequest({ ...form, symbolsText: "" }).error).toContain("至少填一只");
    expect(paperRequest({ ...form, symbolsText: "60051" }).error).toContain("不是六位代码");
    expect(paperRequest({ ...form, symbolsText: "600519 茅台" }).error).toContain("茅台");
    expect(paperRequest({ ...form, start: "" }).error).toContain("起点日期");
  });
});
