/**
 * 深度研究运行页的纯函数层（M8c）：事件流 → 阶段带 / 甘特瀑布 / 预算计量条。
 *
 * 为什么不放在组件里：这一层要做的是「把一串带偏移的起止事件排成不重叠的行」
 * 与「把预算换算成计量条的占比」——**边界比界面多**（缺 end 的节点、被 interrupt
 * 截断的节点、上限为 0 的计数器、只有 start 没有 end 的实时态），单测比手点页面可靠。
 *
 * 三条口径写死在这里：
 *   ① 节点顺序 = 服务端 `STAGE_ORDER`（路由不在图内，故从界面角度它是「已完成的第 0 段」）；
 *   ② **长度即真实耗时，不做归一、不截断**——实测 news 22.3s vs 其余 3.3s，差多少画多少；
 *   ③ 被 interrupt 的节点「起了没落」，收尾由服务端补成 `interrupted`，这里照它的状态画。
 */

export type RunStatus = "running" | "awaiting_approval" | "done" | "degraded" | "failed";

export interface RunNodeEvent {
  phase: "start" | "end" | "interrupt";
  node?: string;
  /** 相对本次运行的偏移秒（服务端写） */
  at?: number;
  /** 仅 end：本节点耗时秒 */
  seconds?: number | null;
  /** 仅 end：ok / interrupted / failed */
  status?: string;
  /** 仅 interrupt：待批载荷 */
  payload?: ApprovalPayload;
}

export interface ApprovalPayload {
  kind: string;
  detail: string;
  options: string[];
}

export interface RunLimits {
  llm_calls: number;
  tokens: number;
  seconds: number;
  cost_usd: number;
}

export interface RunBudget {
  llm_calls?: number;
  tokens?: number;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  elapsed_s?: number;
  limits?: Partial<RunLimits>;
  exceeded?: string | null;
}

export interface RunSubject {
  question?: string;
  symbols?: string[];
  as_of?: string;
  run_id?: string;
  scope?: string;
  notes?: string[];
}

export interface RunDetail {
  id: string;
  question: string;
  subject: RunSubject;
  status: RunStatus;
  events: RunNodeEvent[];
  budget: RunBudget;
  report_id: string | null;
  report?: { blocks?: { id: string; title?: string; text?: string }[] } | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  done?: boolean;
}

/** 阶段带上的节点顺序（与服务端 `graph.STAGE_ORDER` 同序） */
export const STAGE_ORDER = [
  "market",
  "news",
  "fundamental",
  "policy",
  "factor",
  "merge_evidence",
  "bull",
  "bear",
  "synthesize",
  "risk_gate",
] as const;

export const STAGE_LABELS: Record<string, string> = {
  market: "行情技术面",
  news: "新闻事件",
  fundamental: "基本面事件",
  policy: "政策面",
  factor: "因子量化",
  merge_evidence: "证据归并",
  bull: "多方",
  bear: "空方",
  synthesize: "综合撰写",
  risk_gate: "风控闸门",
};

export type StageState = "pending" | "running" | "done" | "degraded" | "blocked";

export interface Stage {
  node: string;
  label: string;
  state: StageState;
  seconds: number | null;
  /** 同节点出现多次（辩论每轮各一次）时的次数 */
  runs: number;
}

function lastEnd(events: RunNodeEvent[], node: string): RunNodeEvent | undefined {
  return [...events].reverse().find((e) => e.phase === "end" && e.node === node);
}

function hasStart(events: RunNodeEvent[], node: string): boolean {
  return events.some((e) => e.phase === "start" && e.node === node);
}

/** 事件流 → 阶段带。状态判据：有 end 看它的 status；只有 start 且运行中 = 进行中。 */
export function buildStages(events: RunNodeEvent[], status: RunStatus): Stage[] {
  const blocked = status === "awaiting_approval";
  return STAGE_ORDER.map((node) => {
    const ends = events.filter((e) => e.phase === "end" && e.node === node);
    const end = lastEnd(events, node);
    const started = hasStart(events, node);
    let state: StageState = "pending";
    if (end) {
      state = end.status === "ok" ? "done" : end.status === "interrupted" ? "blocked" : "degraded";
    } else if (started) {
      state = blocked ? "blocked" : "running";
    }
    return {
      node,
      label: STAGE_LABELS[node] ?? node,
      state,
      seconds: end?.seconds ?? null,
      runs: ends.length,
    };
  });
}

export interface WaterfallRow {
  /** 同一节点跑多次时，这是第几次（1 起） */
  index: number;
  node: string;
  label: string;
  startAt: number;
  seconds: number;
  status: string;
}

/**
 * 事件流 → 甘特行。**按起点排序**（并行的那几条因此会横着叠在相邻行上，一眼看出谁在等谁）。
 * 缺 `seconds`（被 interrupt 截断）的行按 0 画——它确实没跑完，不编一个时长给它。
 */
export function buildWaterfall(events: RunNodeEvent[]): WaterfallRow[] {
  const pending: Record<string, number[]> = {};
  const counters: Record<string, number> = {};
  const rows: WaterfallRow[] = [];
  for (const event of events) {
    if (!event.node) continue;
    if (event.phase === "start") {
      (pending[event.node] ??= []).push(event.at ?? 0);
      continue;
    }
    if (event.phase !== "end") continue;
    // **FIFO 配对**：同一节点会跑多次（辩论每轮各一次），按先后取起点——
    // 用「第一个不晚于本 end 的 start」会把第 2 轮配上第 1 轮的起点（真错法）
    const startAt = (pending[event.node] ?? []).shift() ?? 0;
    counters[event.node] = (counters[event.node] ?? 0) + 1;
    rows.push({
      index: counters[event.node],
      node: event.node,
      label: STAGE_LABELS[event.node] ?? event.node,
      startAt,
      seconds: event.seconds ?? 0,
      status: event.status ?? "ok",
    });
  }
  return rows.sort((a, b) => a.startAt - b.startAt || a.node.localeCompare(b.node));
}

/** 甘特的时间轴上限（秒）：取最晚的 `起点 + 时长`，至少 1 秒（防除零）。 */
export function waterfallSpan(rows: WaterfallRow[]): number {
  return Math.max(1, ...rows.map((row) => row.startAt + row.seconds));
}

export interface Meter {
  key: string;
  label: string;
  used: number;
  limit: number;
  ratio: number;
  text: string;
  /** 服务端点名的那一支（`budget.exceeded`）——界面据此标「已超限」 */
  over: boolean;
}

/** 预算 → 四支计量条。上限缺失时 ratio 记 0（宁可只显示用量也不编一个占比）。 */
export function budgetMeters(budget: RunBudget): Meter[] {
  const limits = budget.limits ?? {};
  const rows: [string, string, number | undefined, number | undefined, (v: number) => string][] = [
    ["llm_calls", "调用次数", budget.llm_calls, limits.llm_calls, (v) => `${v} 次`],
    ["tokens", "token", budget.tokens, limits.tokens, (v) => v.toLocaleString("zh-CN")],
    ["cost_usd", "成本", budget.cost_usd, limits.cost_usd, (v) => `$${v.toFixed(4)}`],
    ["elapsed_s", "用时", budget.elapsed_s, limits.seconds, (v) => `${v.toFixed(1)}s`],
  ];
  return rows.map(([key, label, used, limit, fmt]) => {
    const value = used ?? 0;
    const cap = limit ?? 0;
    return {
      key,
      label,
      used: value,
      limit: cap,
      ratio: cap > 0 ? Math.min(1, value / cap) : 0,
      text: cap > 0 ? `${fmt(value)} / ${fmt(cap)}` : fmt(value),
      over: budget.exceeded === key,
    };
  });
}

/** 事件流里最后一条待批载荷（断连重连的客户端靠它恢复审批卡）。 */
export function pendingApproval(events: RunNodeEvent[]): ApprovalPayload | null {
  const found = [...events].reverse().find((e) => e.phase === "interrupt");
  return found?.payload ?? null;
}

/** 降级与缺口（服务端已经写好文案，前端**照抄**，不自己拼话术）。 */
export function runGaps(report: RunDetail["report"]): string[] {
  const blocks = report?.blocks ?? [];
  const gaps = blocks.find((b) => b.id === "gaps");
  if (!gaps?.text) return [];
  return gaps.text
    .split("\n")
    .map((line) => line.replace(/^-\s*/, "").trim())
    .filter(Boolean);
}

const STATUS_LABELS: Record<RunStatus, string> = {
  running: "进行中",
  awaiting_approval: "待审批",
  done: "已完成",
  degraded: "已完成（有降级）",
  failed: "失败",
};

export function statusLabel(status: RunStatus): string {
  return STATUS_LABELS[status] ?? status;
}

/** 回看页是否还需要按 2s 轮询（P2）：审批是持久状态，等人工动作，不该空转。 */
export function shouldPollRun(status: RunStatus): boolean {
  return status !== "done" && status !== "degraded" && status !== "failed" && status !== "awaiting_approval";
}

export function statusTone(status: RunStatus): "brand" | "warn" | "danger" | "muted" {
  if (status === "done") return "brand";
  if (status === "degraded" || status === "awaiting_approval") return "warn";
  if (status === "failed") return "danger";
  return "muted";
}
