/**
 * M8c 运行页纯函数：阶段带 / 甘特瀑布 / 预算计量条 / 待批载荷。
 *
 * 边界取自真读数（`logs/m8/deep.json`）：五条取证并行但不等长、辩论每轮两次发言
 * （同节点多次执行）、被 interrupt 的节点只有 start 没有 end。
 */

import { describe, expect, it } from "vitest";

import {
  budgetMeters,
  buildStages,
  buildWaterfall,
  pendingApproval,
  runGaps,
  shouldPollRun,
  statusTone,
  waterfallSpan,
  type RunNodeEvent,
} from "./agent-run";

/** 真实运行的事件骨架：五条取证并行（news 最长），辩论两轮，闸门收尾。 */
const EVENTS: RunNodeEvent[] = [
  { phase: "start", node: "market", at: 0.0 },
  { phase: "start", node: "news", at: 0.0 },
  { phase: "start", node: "factor", at: 0.0 },
  { phase: "start", node: "fundamental", at: 0.0 },
  { phase: "start", node: "policy", at: 0.0 },
  { phase: "end", node: "factor", at: 3.25, seconds: 3.25, status: "ok" },
  { phase: "end", node: "market", at: 3.25, seconds: 3.25, status: "ok" },
  { phase: "end", node: "fundamental", at: 3.26, seconds: 3.26, status: "ok" },
  { phase: "end", node: "policy", at: 4.78, seconds: 4.78, status: "ok" },
  { phase: "end", node: "news", at: 22.32, seconds: 22.32, status: "ok" },
  { phase: "start", node: "merge_evidence", at: 22.32 },
  { phase: "end", node: "merge_evidence", at: 22.32, seconds: 0.0, status: "ok" },
  { phase: "start", node: "bull", at: 22.4 },
  { phase: "end", node: "bull", at: 24.85, seconds: 2.45, status: "ok" },
  { phase: "start", node: "bear", at: 24.85 },
  { phase: "end", node: "bear", at: 27.48, seconds: 2.63, status: "ok" },
  { phase: "start", node: "bull", at: 27.5 },
  { phase: "end", node: "bull", at: 30.19, seconds: 2.69, status: "ok" },
  { phase: "start", node: "bear", at: 30.19 },
  { phase: "end", node: "bear", at: 32.52, seconds: 2.33, status: "ok" },
  { phase: "start", node: "synthesize", at: 32.6 },
  { phase: "end", node: "synthesize", at: 40.6, seconds: 8.0, status: "ok" },
  { phase: "start", node: "risk_gate", at: 40.6 },
  { phase: "end", node: "risk_gate", at: 40.6, seconds: 0.0, status: "ok" },
];

describe("shouldPollRun", () => {
  it("只有 running 继续轮询；审批是持久态，终态不再拉", () => {
    expect(shouldPollRun("running")).toBe(true);
    for (const status of ["awaiting_approval", "done", "degraded", "failed"] as const) {
      expect(shouldPollRun(status)).toBe(false);
    }
  });
});

describe("buildStages", () => {
  it("按固定顺序给出全部阶段，并带上各自耗时", () => {
    const stages = buildStages(EVENTS, "done");

    expect(stages.map((s) => s.node)).toEqual([
      "market", "news", "fundamental", "policy", "factor",
      "merge_evidence", "bull", "bear", "synthesize", "risk_gate",
    ]);
    expect(stages.every((s) => s.state === "done")).toBe(true);
    expect(stages.find((s) => s.node === "news")?.seconds).toBe(22.32);
    // 辩论每轮各发一次言 ⇒ 阶段上记两次
    expect(stages.find((s) => s.node === "bull")?.runs).toBe(2);
  });

  it("没跑到的阶段是 pending，不是 done", () => {
    const stages = buildStages(EVENTS.slice(0, 8), "running");  // 五个角色都起跑、只落了三个 end

    expect(stages.find((s) => s.node === "synthesize")?.state).toBe("pending");  // 还没轮到
    expect(stages.find((s) => s.node === "policy")?.state).toBe("running");      // 起了还没落
    expect(stages.find((s) => s.node === "market")?.state).toBe("done");
  });

  it("只有 start 的节点在运行中是「进行中」、在待批时是「阻塞」", () => {
    const partial: RunNodeEvent[] = [
      { phase: "start", node: "risk_gate", at: 1 },
      { phase: "end", node: "risk_gate", at: 2, seconds: 1, status: "interrupted" },
    ];

    expect(buildStages(partial, "awaiting_approval").find((s) => s.node === "risk_gate")?.state).toBe("blocked");
    expect(buildStages([{ phase: "start", node: "news", at: 0 }], "running").find((s) => s.node === "news")?.state).toBe("running");
  });

  it("节点降级（非 ok / 非 interrupted）如实标成 degraded", () => {
    const stages = buildStages([{ phase: "start", node: "news", at: 0 }, { phase: "end", node: "news", at: 1, seconds: 1, status: "failed" }], "degraded");

    expect(stages.find((s) => s.node === "news")?.state).toBe("degraded");
  });
});

describe("buildWaterfall", () => {
  it("按起点排序，同节点多次执行**按先后 FIFO 配对**（不能都配到第一次的起点）", () => {
    const rows = buildWaterfall(EVENTS);
    const bulls = rows.filter((r) => r.node === "bull");

    expect(bulls.map((r) => [r.index, r.startAt, r.seconds])).toEqual([
      [1, 22.4, 2.45],
      [2, 27.5, 2.69],
    ]);
  });

  it("并行的那几条起点相同、长度差多少画多少（不做归一）", () => {
    const rows = buildWaterfall(EVENTS);
    const parallel = rows.filter((r) => r.startAt === 0);

    expect(parallel.map((r) => r.node)).toEqual([
      "factor", "fundamental", "market", "news", "policy",
    ]);
    expect(Math.max(...parallel.map((r) => r.seconds))).toBe(22.32);
    expect(Math.min(...parallel.map((r) => r.seconds))).toBe(3.25);
  });

  it("被截断的节点（没有 seconds）记 0，不编时长", () => {
    const rows = buildWaterfall([
      { phase: "start", node: "risk_gate", at: 5 },
      { phase: "end", node: "risk_gate", at: 5, seconds: null, status: "interrupted" },
    ]);

    expect(rows[0].seconds).toBe(0);
    expect(rows[0].status).toBe("interrupted");
  });

  it("时间轴上限取最晚的起点+时长", () => {
    expect(waterfallSpan(buildWaterfall(EVENTS))).toBe(40.6);
    expect(waterfallSpan([])).toBe(1); // 空数据也不除零
  });
});

describe("budgetMeters", () => {
  const budget = {
    llm_calls: 10, tokens: 10257, cost_usd: 0.015557, elapsed_s: 29.3,
    limits: { llm_calls: 40, tokens: 200000, cost_usd: 0.5, seconds: 300 },
    exceeded: null,
  };

  it("四支计量条各自算占比并给可读文本", () => {
    const meters = budgetMeters(budget);

    expect(meters.map((m) => m.key)).toEqual(["llm_calls", "tokens", "cost_usd", "elapsed_s"]);
    expect(meters[0].ratio).toBeCloseTo(0.25);
    expect(meters[0].text).toBe("10 次 / 40 次");
    expect(meters[2].text).toBe("$0.0156 / $0.5000");
  });

  it("服务端点名的超限计数器会标出来", () => {
    const meters = budgetMeters({ ...budget, exceeded: "cost_usd" });

    expect(meters.filter((m) => m.over).map((m) => m.key)).toEqual(["cost_usd"]);
  });

  it("没有上限时只显示用量，不编占比", () => {
    const meters = budgetMeters({ llm_calls: 3 });

    expect(meters[0].ratio).toBe(0);
    expect(meters[0].text).toBe("3 次");
  });
});

describe("待批与缺口", () => {
  it("从事件流里取最后一条待批载荷（断连重连靠它）", () => {
    const events: RunNodeEvent[] = [
      { phase: "interrupt", at: 40.6, payload: { kind: "wording", detail: "建议买入", options: ["continue", "stop"] } },
    ];

    expect(pendingApproval(events)?.kind).toBe("wording");
    expect(pendingApproval(EVENTS)).toBeNull();
  });

  it("缺口文案照抄服务端，前端不自己拼", () => {
    const report = {
      blocks: [{ id: "gaps", text: "- 「政策面」不可用：检索超时\n- 「因子量化」未产出解读要点（数字与证据仍在）" }],
    };

    expect(runGaps(report)).toEqual([
      "「政策面」不可用：检索超时",
      "「因子量化」未产出解读要点（数字与证据仍在）",
    ]);
    expect(runGaps({ blocks: [] })).toEqual([]);
  });

  it("状态色不借涨跌色：完成走品牌色、降级与待批走 warn、失败走 destructive", () => {
    expect(statusTone("done")).toBe("brand");
    expect(statusTone("degraded")).toBe("warn");
    expect(statusTone("awaiting_approval")).toBe("warn");
    expect(statusTone("failed")).toBe("danger");
  });
});
