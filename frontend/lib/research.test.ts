/**
 * 研报页纯函数的用例（`lib/research.ts`）。
 *
 * 重点在三处**容易错且不报错**的地方：比例与百分点两条单位路径、空表与「本次没有」的分别、
 * 以及块清单的兜底（M8 加块不能把这一页弄崩）。
 */

import { describe, expect, it } from "vitest";

import {
  attributionTables,
  blockView,
  downloadName,
  evidenceForBlock,
  evidenceIndex,
  evidenceNotice,
  evidenceRows,
  fingerprintLine,
  kindBadge,
  markdownHref,
  metricCells,
  numberLabel,
  reviewCards,
  shareUrl,
} from "./research";
import type { ReportBody, ReportEvidence, ReportMetrics, ReportReview } from "./types";

const METRICS: ReportMetrics = {
  total_return: 0.0251,
  annual_return: 0.1097,
  max_drawdown: 0.0333,
  sharpe: 0.9215,
  volatility: 0.1228,
  win_rate: 0.3333,
  trade_count: 3,
  final_equity: 512_548.2,
  benchmark_return: 0.0074,
  excess_return: 0.0177,
};

function evidence(over: Partial<ReportEvidence> = {}): ReportEvidence {
  return {
    event_id: "news:1",
    day: "2026-08-03",
    title: "重大合同公告",
    summary: "公司与大客户签订三年期合同。",
    event_time: "2026-08-03 09:00:00+08:00",
    available_at: "2026-08-03 09:10:00+08:00",
    source: "xiaoshi-archive",
    original_source: "华尔街见闻",
    content_hash: "abc123def4567890",
    source_url: "https://example.com/1",
    industries: ["银行"],
    direction_norm: "bullish",
    found: true,
    revised: false,
    corpus_hash: "abc123def4567890",
    decision_ids: ["d1"],
    ...over,
  };
}

function body(over: Partial<ReportBody> = {}): ReportBody {
  return {
    version: 1,
    kind: "paper_account_report",
    account: {
      id: "acct",
      name: "银行事件驱动",
      status: "finished",
      strategy: "event_driven",
      strategy_name: null,
      symbols: ["600519"],
      initial_cash: 500_000,
      start: "2026-07-08",
      as_of: "2026-09-30",
      data_end: "2026-09-30",
    },
    metrics: METRICS,
    benchmark: { kind: "market_equal_weight", note: "全市场等权组合代理", total_return: 0.0074 },
    equity_curve: [],
    attribution: { summary: {}, symbols: [], direction: [], industry: [] },
    review: null,
    blocks: [],
    evidence: [],
    snapshot: { bars: { digest: "e8bb6ad5e378aaaa" }, market_end: "2026-09-30" },
    warnings: [],
    ...over,
  };
}

describe("blockView", () => {
  it("认识五个已知块", () => {
    for (const id of ["overview", "performance", "attribution", "review", "narrative"]) {
      expect(blockView({ id, kind: "fact", title: id })).toBe(id);
    }
  });

  it("未知块落到兜底（M8 加块不用改这页）", () => {
    expect(blockView({ id: "debate", kind: "inference", title: "多空辩论" })).toBe("unknown");
  });
});

describe("kindBadge", () => {
  it("事实与推断各自一枚徽章", () => {
    expect(kindBadge("fact")).toEqual({ label: "事实", tone: "fact" });
    expect(kindBadge("inference")).toEqual({ label: "推断", tone: "inference" });
  });
});

describe("metricCells", () => {
  it("比例走 pct、金额走 amount、计数走 count", () => {
    const cells = metricCells(METRICS);
    const byKey = Object.fromEntries(cells.map((cell) => [cell.key, cell]));

    expect(byKey.total_return.value).toBe("+2.51%");
    expect(byKey.excess_return.value).toBe("+1.77%");
    expect(byKey.max_drawdown.value).toBe("3.33%"); // 回撤是正值幅度，**不带号**
    expect(byKey.final_equity.value).toBe("512,548");
    expect(byKey.trade_count.value).toBe("3");
    expect(byKey.sharpe.value).toBe("0.92");
  });

  it("首行三张大卡：累计 / 超额 / 回撤", () => {
    const primary = metricCells(METRICS)
      .filter((cell) => cell.primary)
      .map((cell) => cell.key);
    expect(primary).toEqual(["total_return", "excess_return", "max_drawdown"]);
  });

  it("缺失值一律「—」，不显示 0", () => {
    const cells = metricCells({ ...METRICS, sharpe: null, benchmark_return: null, excess_return: null });
    const byKey = Object.fromEntries(cells.map((cell) => [cell.key, cell]));
    expect(byKey.sharpe.value).toBe("—");
    expect(byKey.benchmark_return.value).toBe("—");
    expect(byKey.excess_return.value).toBe("—");
    expect(byKey.excess_return.tone).toBe("plain");
  });

  it("盈亏语气按 A 股口径（正红负绿），零与缺失都不着色", () => {
    const cells = metricCells({ ...METRICS, total_return: -0.01, excess_return: 0 });
    const byKey = Object.fromEntries(cells.map((cell) => [cell.key, cell]));
    expect(byKey.total_return.tone).toBe("down");
    expect(byKey.excess_return.tone).toBe("plain");
  });
});

describe("attributionTables", () => {
  it("三张表各有表头与空态文案（空表不留白）", () => {
    const tables = attributionTables(body());
    expect(tables.map((table) => table.key)).toEqual(["symbols", "direction", "industry"]);
    expect(tables[0].empty).toBe("本次没有可归因的回合");
    expect(tables[0].rows).toEqual([]);
  });

  it("有行时按口径格式化（贡献走 pp、金额带号）", () => {
    const withRows = body({
      attribution: {
        summary: {},
        symbols: [
          {
            symbol: "600519",
            trips: 4,
            closed: 3,
            wins: 1,
            realized_pnl: 5284.14,
            unrealized_pnl: 7264.06,
            unmarked: 0,
            contribution_pp: 2.5096,
          },
        ],
        direction: [{ label: "利多", trips: 4, closed: 3, wins: 1, pnl: 12548.2, unmarked: 0 }],
        industry: [{ label: "消费", trips: 3, closed: 3, wins: 1, pnl: 5284.14, unmarked: 0 }],
      },
    });
    const tables = attributionTables(withRows);
    expect(tables[0].rows[0]).toEqual([
      "600519", "4", "3", "1", "+5,284", "+7,264", "+2.51pp", "—",
    ]);
    expect(tables[1].rows[0][0]).toBe("利多");
    expect(tables[2].rows[0][0]).toBe("消费");
  });

  it("未估值的回合单列计数，不混进金额", () => {
    const withUnmarked = body({
      attribution: {
        summary: {},
        symbols: [
          {
            symbol: "600519", trips: 1, closed: 0, wins: 0, realized_pnl: 0,
            unrealized_pnl: 0, unmarked: 1, contribution_pp: 0,
          },
        ],
        direction: [], industry: [],
      },
    });
    expect(attributionTables(withUnmarked)[0].rows[0][7]).toBe("1");
  });
});

describe("reviewCards", () => {
  const review: ReportReview = {
    summary: { settled: 1, open: 1, unfilled: 1, lessons: 1, as_of: "2026-09-30" },
    settled: [
      {
        decision_id: "d1",
        symbol: "600519",
        entry_date: "2026-08-03",
        exit_date: "2026-08-10",
        settled: true,
        pnl: 984.5,
        return_pct: 0.0984,
        benchmark_pct: 0.02,
        alpha_pp: 7.84,
        window_days: 6,
        entry_reason: "MA 金叉",
        exit_reason: "持有到期",
        direction: "bullish",
        evidence_key: "news:1|2026-08-03",
        reflection: { text: "吃到了主升段。", model: "deepseek/deepseek-flash", prompt_version: "m7-reflection-1", note: null },
        sources: null,
        evidence: null,
      },
    ],
    open: [
      {
        decision_id: "d2",
        symbol: "600519",
        entry_date: "2026-09-30",
        exit_date: null,
        settled: false,
        pnl: 1995,
        return_pct: 0.1994,
        benchmark_pct: 0.01,
        alpha_pp: 18.94,
        window_days: 1,
        entry_reason: "MA 金叉",
        exit_reason: "",
        direction: null,
        evidence_key: null,
        reflection: null,
        sources: null,
        evidence: null,
      },
    ],
    unfilled: [
      {
        decision_id: "d3",
        symbol: "600519",
        trade_date: "2026-09-20",
        side: "buy",
        status: "expired",
        status_label: "未审批过期（未审批不成交）",
        reject_code: null,
        reject_reason: null,
        evidence: null,
      },
    ],
  };

  it("已到期卡带教训与模型署名；金额带号、alpha 走 pp", () => {
    const cards = reviewCards(review, "2026-09-30");
    const card = cards.settled[0];
    expect(card.tone).toBe("settled");
    expect(card.statusLabel).toBe("已到期");
    expect(card.pnl).toBe("+985");
    expect(card.pnlTone).toBe("up");
    expect(card.window).toBe("2026-08-03 → 2026-08-10（6 个交易日）");
    expect(card.alphaText).toBe("+7.84pp");
    expect(card.reflectionText).toBe("吃到了主升段。");
    expect(card.reflectionByline).toBe("模型 deepseek/deepseek-flash · m7-reflection-1");
    expect(card.evidenceKey).toBe("news:1|2026-08-03");
  });

  it("未到期卡写清「数据止于哪一天」且没有教训", () => {
    const card = reviewCards(review, "2026-09-30").open[0];
    expect(card.tone).toBe("open");
    expect(card.statusLabel).toBe("未到期（数据止于 2026-09-30）");
    expect(card.window).toContain("未平仓");
    expect(card.reflectionText).toBeNull();
    expect(card.reflectionByline).toBeNull();
  });

  it("教训缺席时把原因带出来（不是留白）", () => {
    const degraded = {
      ...review,
      settled: [
        {
          ...review.settled[0],
          reflection: { text: null, model: "m", prompt_version: "p", note: "反思超时（>20s）" },
        },
      ],
    };
    const card = reviewCards(degraded, "2026-09-30").settled[0];
    expect(card.reflectionText).toBeNull();
    expect(card.reflectionNote).toBe("反思超时（>20s）");
  });

  it("定了没交易：状态文案原样吃服务端，买入/卖出译成中文", () => {
    const rows = reviewCards(review, "2026-09-30").unfilled;
    expect(rows[0].sideLabel).toBe("买入");
    expect(rows[0].statusLabel).toBe("未审批过期（未审批不成交）");
  });

  it("没有复盘（记忆降级）时三段都是空数组，不当崩", () => {
    expect(reviewCards(null, "2026-09-30")).toEqual({ settled: [], open: [], unfilled: [] });
  });

  it("理由走既有可读化（与 /paper 同一套，不新写一份）", () => {
    const raw = { ...review, settled: [{ ...review.settled[0], entry_reason: "ma_cross:golden MA5/MA20" }] };
    expect(reviewCards(raw, "2026-09-30").settled[0].entryReason).toBe("双均线 · golden MA5/MA20");
  });
});

describe("evidenceRows / evidenceNotice", () => {
  it("事发与可得**并列且相邻**（PIT 语义的展示位）", () => {
    const rows = evidenceRows(evidence());
    const labels = rows.map((row) => row.label);
    expect(labels.indexOf("可得")).toBe(labels.indexOf("事发") + 1);
    expect(rows.find((row) => row.label === "内容哈希")?.value).toBe("abc123def456");
  });

  it("方向译中文、行业顿号相连、原文带链接", () => {
    const rows = evidenceRows(evidence());
    expect(rows.find((row) => row.label === "方向")?.value).toBe("利多");
    expect(rows.find((row) => row.label === "行业")?.value).toBe("银行");
    expect(rows.find((row) => row.label === "原文")?.href).toBe("https://example.com/1");
  });

  it("查无此行 / 被平台修订各给一句告警", () => {
    expect(evidenceNotice(evidence({ found: false }))).toContain("查无此行");
    expect(evidenceNotice(evidence({ revised: true, corpus_hash: "newhash000000" }))).toContain("已被平台修订");
    expect(evidenceNotice(evidence())).toBeNull();
  });

  it("按块取证据：悬空键被丢掉（后端闸门保证不会出现，前端也不当崩）", () => {
    const index = evidenceIndex([evidence()]);
    const block = { id: "attribution", kind: "fact" as const, title: "归因", evidence: ["news:1|2026-08-03", "news:404|2026-01-01"] };
    expect(evidenceForBlock(block, index).map((item) => item.event_id)).toEqual(["news:1"]);
    expect(evidenceForBlock({ id: "x", kind: "fact", title: "x" }, index)).toEqual([]);
  });
});

describe("numberLabel", () => {
  it("认得的路径给中文标签，认不得的回落路径末段（不编名字）", () => {
    expect(numberLabel("account.initial_cash")).toBe("初始资金");
    expect(numberLabel("metrics.final_equity")).toBe("期末权益");
    expect(numberLabel("review.summary.settled")).toBe("settled");
  });
});

describe("分享与导出", () => {
  it("分享 URL 由 origin + share_path 拼（末尾斜杠不重复）", () => {
    expect(shareUrl("http://localhost:3001", "/r/abc")).toBe("http://localhost:3001/r/abc");
    expect(shareUrl("http://localhost:3001/", "/r/abc")).toBe("http://localhost:3001/r/abc");
    expect(shareUrl("http://localhost:3001", null)).toBeNull();
  });

  it("Markdown 端点：有 token 走公开端点，没有走受保护端点（登录页也能导出）", () => {
    expect(markdownHref("http://api", "r1", "tok")).toContain("/public/reports/tok/markdown");
    expect(markdownHref("http://api", "r1", null)).toContain("/reports/r1/markdown");
  });

  it("文件名带指纹短码，且清掉路径分隔符", () => {
    expect(downloadName("银行/事件驱动 会话", "31c1ae554143ffff")).toBe("银行-事件驱动-会话-31c1ae55.md");
    expect(downloadName("", "abcdefgh12345678")).toBe("report-abcdefgh.md");
  });

  it("指纹行两个短码都在", () => {
    const line = fingerprintLine("31c1ae554143ffff", "e8bb6ad5e378aaaa");
    expect(line).toBe("报告 31c1ae554143 ｜ 数据快照 e8bb6ad5e378");
    expect(fingerprintLine("31c1ae554143ffff", undefined)).toContain("—");
  });
});
