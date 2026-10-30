/**
 * 模拟盘的**纯函数**：六态语义、决策卡视图、来源三元组、推进摘要、请求体构造。
 *
 * 分工（同 M5c 的 `lib/factor-report.ts`）：
 *   * 服务端给**事实**（`status_label` 的中文、`est_qty`、`fill`、`reject_reason`）；
 *   * 这里把事实翻成**视图模型**（哪一行显示什么、什么语气、什么单位）；
 *   * 组件只管画。
 *
 * 一条刻意的克制：**六态的中文不从这层出**——`status_label` 由服务端随每条决策一起给
 * （M5b 的 `overfit.reason_text` 同一条规矩：文案只有一处真源，前端不自己拼）。
 * 这层决定的是「**语气**」：要你动手 / 你已决定等市场 / 正常结果 / 已成往事 / 出了意外。
 */

import { safeHref } from "./event-detail";
import { amount, num } from "./format";
import type {
  PaperAccountDetail,
  PaperAccountRequest,
  PaperDecision,
  PaperDecisionStatus,
  PaperEquityPoint,
  PaperProgress,
  PaperRiskEvent,
  PaperRiskGate,
  PaperRiskLimits,
  PaperRiskState,
  PaperRiskVerdict,
  PaperStepOutcome,
} from "./types";

// ── 六态 → 语气 ────────────────────────────────────────────────────────────

/**
 * 语气（不是颜色）：组件把语气映射成具体 token。
 *
 * 为什么不让这层直接给 className：颜色是本项目最容易改坏的东西（红绿是 A 股涨跌的专用色，
 * 六态**不能**借用），把「语义」与「像素」分开，改配色时不必动判据、测配色时不必读组件。
 */
export type StatusTone = "action" | "waiting" | "normal" | "void" | "alert";

export function statusTone(status: PaperDecisionStatus): StatusTone {
  switch (status) {
    case "pending":
      return "action"; // 等你裁决——全页唯一需要动手的状态
    case "approved":
      return "waiting"; // 你已决定，等市场
    case "filled":
      return "normal";
    case "unfilled":
      return "alert"; // 批了但没成，要人看一眼原因
    default:
      return "void"; // rejected / expired：都是「没做」，事实已经过去
  }
}

/** 六态里哪些能批：只有待审批。判据给前端**只为禁用按钮**，裁决权在服务端（409）。 */
export function canDecide(status: PaperDecisionStatus): boolean {
  return status === "pending";
}

// ── 决策卡 ────────────────────────────────────────────────────────────────

const SIDE_LABELS: Record<PaperDecision["side"], string> = { buy: "买入", sell: "卖出" };

/** 策略前缀 → 显示名。理由串是引擎原样带出的（`ma_cross:golden MA5/MA20`），界面只做可读化。 */
const REASON_PREFIXES: Record<string, string> = {
  ma_cross: "双均线",
  event_driven: "事件驱动",
  risk: "风控",
};

export function sideLabel(side: PaperDecision["side"]): string {
  return SIDE_LABELS[side];
}

/** `ma_cross:golden MA5/MA20` → `双均线 · golden MA5/MA20`；不认识的策略原样返回。 */
export function reasonText(reason: string): string {
  const index = reason.indexOf(":");
  if (index <= 0) return reason;
  const label = REASON_PREFIXES[reason.slice(0, index)];
  return label ? `${label} · ${reason.slice(index + 1)}` : reason;
}

export interface SourceRow {
  label: string;
  value: string;
  href?: string;
}

/**
 * 来源三元组与双时间戳的展示顺序（**事件时间与可用时间必须并列**，这是本站的 PIT 语义展示位）。
 *
 * 卖出（持有到期一类）没有来源 ⇒ 返回空数组，界面如实说「无事件来源」，**不编一条**。
 */
export function sourceRows(decision: PaperDecision): SourceRow[] {
  const s = decision.sources;
  if (!s) return [];
  const rows: SourceRow[] = [];
  if (s.title) rows.push({ label: "标题", value: s.title });
  if (s.original_source) rows.push({ label: "原始来源", value: s.original_source });
  if (s.source) rows.push({ label: "来源", value: s.source });
  if (s.event_time) rows.push({ label: "事发", value: s.event_time });
  if (s.available_at) rows.push({ label: "可得", value: s.available_at });
  if (s.content_hash) rows.push({ label: "内容哈希", value: `${s.content_hash.slice(0, 12)}…` });
  const href = safeHref(s.source_url);
  if (href) rows.push({ label: "链接", value: "打开原文", href });
  return rows;
}

/**
 * 决策卡的展示行。
 *
 * 两条口径写在这里而不是组件里：
 *   * **预估与成交是两个数**——提案数量按决策日收盘价估、成交股数按次日开盘价重算，
 *     跳空日两者会不同，界面必须分开说（`est` 永远是「预计」，`fill` 才是既成事实）；
 *   * 卖出没有「预估成交价」以外的东西可看（数量是全部持仓），文案如实写「持仓全部」。
 */
export function decisionCard(decision: PaperDecision) {
  const estimate =
    decision.side === "buy"
      ? `预计买入 ${num(decision.est_qty, { digits: 0 })} 股 · 按决策日收盘价约 ${amount(
          decision.est_qty * decision.est_price,
        )}`
      : `卖出全部持仓 ${num(decision.est_qty, { digits: 0 })} 股 · 按决策日收盘价约 ${amount(
          decision.est_qty * decision.est_price,
        )}`;

  const fill = decision.fill;
  return {
    symbol: decision.symbol,
    side: decision.side,
    sideLabel: sideLabel(decision.side),
    estimate,
    /** 成交之后才有：实际成交了多少股、什么价、哪一天 */
    actual: fill
      ? `实际成交 ${num(fill.qty, { digits: 0 })} 股 @ ${num(fill.price, { digits: 4 })}` +
        `（${fill.trade_date}，含滑点 ${amount(fill.slippage_cost)}、费用 ${amount(
          fill.commission + fill.stamp_tax,
        )}）`
      : null,
    reason: reasonText(decision.reason),
    sources: sourceRows(decision),
    reject: decision.reject_reason,
    /** 风控止损单：系统自动批准；Agent 提案：只到待审批，必须人工裁决 */
    originLabel:
      decision.origin === "risk"
        ? "风控自动"
        : decision.origin === "agent"
          ? "Agent 提案"
          : null,
    autoExit: decision.origin === "risk" && decision.status === "approved",
  };
}

// ── 推进摘要 ──────────────────────────────────────────────────────────────

export interface StepLine {
  tone: StatusTone;
  text: string;
}

/**
 * 「这次推进发生了什么」——四类事件各一行，**零条的不占位**。
 * 全为零时返回空数组，调用方显示「本次推进没有发生任何事」。
 */
export function stepLines(outcome: PaperStepOutcome | undefined): StepLine[] {
  if (!outcome) return [];
  const lines: StepLine[] = [];
  if (outcome.filled.length) lines.push({ tone: "normal", text: `成交 ${outcome.filled.length} 笔` });
  if (outcome.unfilled.length)
    lines.push({
      tone: "alert",
      text: `已批准但没成交 ${outcome.unfilled.length} 笔（原因见流水：一字板 / 停牌 / 资金不足）`,
    });
  if (outcome.expired.length)
    lines.push({
      tone: "void",
      text: `未审批过期 ${outcome.expired.length} 笔（未审批不成交）`,
    });
  if (outcome.generated.length)
    lines.push({ tone: "action", text: `新生成 ${outcome.generated.length} 条待审批` });
  const exits = outcome.risk_exits ?? [];
  if (exits.length)
    lines.push({
      tone: "waiting",
      text: `风控止损 ${exits.length} 笔（自动批准，T+1 开盘成交）`,
    });
  const blocked = (outcome.risk_events ?? []).filter(
    (event) => event.verdict === "block",
  ).length;
  if (blocked)
    lines.push({ tone: "void", text: `风控拦截 ${blocked} 笔买入（见风控日志）` });
  return lines;
}

// ── 账户与进度 ────────────────────────────────────────────

/**
 * 盈亏的正负 → 文字色（A 股口径：红涨绿跌）。
 *
 * **零不染色**——与 `trades-table` 的既有判据一致（那边是 `> 0` / `< 0` 两条）。
 * 把 0 涂成红色等于说「赚了」，而 0 是「没动」。
 */
export function pnlTone(value: number): "up" | "down" | null {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return null;
}

/** 进度比例（0–1）。`days_total` 为 0 时返 0——不为了好看补一个 1。 */
export function progressRatio(progress: PaperProgress): number {
  if (progress.days_total <= 0) return 0;
  return Math.min(Math.max(progress.days_done / progress.days_total, 0), 1);
}

/** 账户相对初始资金的总收益（比例）。初始资金为 0 时返 null（不编一个 0%）。 */
export function totalReturn(equity: number, initialCash: number): number | null {
  if (!initialCash) return null;
  return equity / initialCash - 1;
}

/**
 * 涨跌停判定未生效的标的（如实标注：跑了但没生效，与「压根没跑」必须分得开）。
 * 全部生效时返回空数组。
 */
export function ruleDegradations(
  rules: Record<string, { limit_check: string; reason: string | null }>,
): { symbol: string; reason: string }[] {
  return Object.entries(rules)
    .filter(([, rule]) => rule.limit_check !== "on")
    .map(([symbol, rule]) => ({ symbol, reason: rule.reason ?? "未说明" }));
}

// ── E5 风控 ────────────────────────────────────────────────

export type RiskProfile = "off" | "standard" | "strict" | "custom";

/** 预设只做填充，最终值随会话冻结（API 收的是四个具体数值，不是一个档位名） */
export const RISK_PRESETS: Record<"standard" | "strict", PaperRiskLimits> = {
  standard: {
    max_gross_exposure: 95,
    max_position: 30,
    stop_loss: 6,
    max_drawdown: 10,
  },
  strict: {
    max_gross_exposure: 80,
    max_position: 20,
    stop_loss: 4,
    max_drawdown: 8,
  },
};

export const RISK_PROFILE_LABELS: Record<RiskProfile, string> = {
  off: "关闭",
  standard: "标准",
  strict: "严格",
  custom: "自定义",
};

/** 档位 → 最终四个值；`off` 返回 null（请求里省略 `risk`） */
export function riskProfileLimits(
  profile: RiskProfile,
  custom: PaperRiskLimits,
): PaperRiskLimits | null {
  if (profile === "off") return null;
  if (profile === "custom") return { ...custom };
  return { ...RISK_PRESETS[profile] };
}

export function riskProfileHint(
  profile: RiskProfile,
  custom: PaperRiskLimits,
): string | null {
  const limits = riskProfileLimits(profile, custom);
  if (!limits) return null;
  return (
    `总仓位 ≤${limits.max_gross_exposure}% · 单标的 ≤${limits.max_position}% · ` +
    `止损 -${limits.stop_loss}% · 回撤 ≤${limits.max_drawdown}%`
  );
}

/** 本地只拦「不是 0–100 的正数」；「缩量后买不起一手」由后端 422 指出是哪只 */
export function riskRequestError(limits: PaperRiskLimits): string | null {
  const entries: [string, number][] = [
    ["总仓位上限", limits.max_gross_exposure],
    ["单标的上限", limits.max_position],
    ["止损", limits.stop_loss],
    ["回撤上限", limits.max_drawdown],
  ];
  for (const [label, value] of entries) {
    if (!Number.isFinite(value) || value <= 0 || value > 100) {
      return `${label}要填 0–100 的正数`;
    }
  }
  return null;
}

export type RiskTone = "normal" | "warn";

const RISK_GATE_LABELS: Record<Exclude<PaperRiskGate, null>, string> = {
  gross_exposure: "总仓位",
  concentration: "单标的集中度",
  stop_loss: "止损",
  drawdown: "回撤",
  position_open: "已持仓",
  stop_loss_pending: "止损挂单中",
};

const RISK_VERDICT_LABELS: Record<PaperRiskVerdict, string> = {
  pass: "放行",
  cap: "缩量",
  block: "拦截",
  trigger: "触发",
  breach: "超限",
  clear: "恢复",
};

export function riskGateLabel(gate: PaperRiskGate): string {
  return gate ? RISK_GATE_LABELS[gate] : "—";
}

export function riskVerdictLabel(verdict: PaperRiskVerdict): string {
  return RISK_VERDICT_LABELS[verdict];
}

/** 风控的「需要看一眼」统一用 `--warn`，不借红绿（红绿是涨跌/买卖的专用色） */
export function riskTone(verdict: PaperRiskVerdict): RiskTone {
  return verdict === "pass" || verdict === "clear" ? "normal" : "warn";
}

export function riskEventSymbol(event: PaperRiskEvent): string {
  return event.symbol ?? "全账户";
}

export interface RiskRow {
  metric: "gross_exposure" | "position" | "drawdown";
  label: string;
  /** 百分比；`null` = 无定义（权益 ≤ 0） */
  current: number | null;
  limit: number;
  /** 进度条宽度（0–1）；`null` 时不画条 */
  ratio: number | null;
  alert: boolean;
}

/** `risk_state` 在风控关闭时是 `{}`（旧响应可能缺键）——用 `as_of` 判「有没有快照」 */
export function riskStateOf(
  state: PaperAccountDetail["risk_state"] | undefined,
): PaperRiskState | null {
  if (!state || !("as_of" in state)) return null;
  return state as PaperRiskState;
}

export function riskRows(state: PaperRiskState, limits: PaperRiskLimits): RiskRow[] {
  const row = (
    metric: RiskRow["metric"],
    label: string,
    current: number | null,
    limit: number,
    alert: boolean,
  ): RiskRow => ({
    metric,
    label,
    current,
    limit,
    ratio:
      current === null || limit <= 0
        ? null
        : Math.min(Math.max(current / limit, 0), 1),
    alert,
  });
  return [
    row(
      "gross_exposure",
      "总仓位",
      state.gross_exposure_pct,
      limits.max_gross_exposure,
      state.gross_exposure_pct !== null &&
        state.gross_exposure_pct > limits.max_gross_exposure,
    ),
    row(
      "position",
      "最大单标的",
      state.max_position_pct,
      limits.max_position,
      state.max_position_pct !== null && state.max_position_pct > limits.max_position,
    ),
    row(
      "drawdown",
      "当前回撤",
      state.drawdown_pct,
      limits.max_drawdown,
      state.blocked_by_drawdown,
    ),
  ];
}

// ── 净值曲线 ──────────────────────────────────────────────────────────────

export interface EquitySeries {
  dates: string[];
  values: number[];
  /** 成交日标记：买 ▲ 红 / 卖 ▼ 绿（与 K 线 markers 同一套 up/down 口径） */
  markers: { date: string; side: PaperDecision["side"]; qty: number }[];
}

export function equitySeries(
  curve: PaperEquityPoint[],
  decisions: PaperDecision[] = [],
): EquitySeries {
  const markers = decisions
    .filter((d) => d.fill !== null)
    .map((d) => ({ date: d.fill!.trade_date, side: d.side, qty: d.fill!.qty }))
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  return {
    dates: curve.map((p) => p.trade_date),
    values: curve.map((p) => p.equity),
    markers,
  };
}

// ── 创建会话的输入 ────────────────────────────────────────────────────────

export interface SymbolParse {
  symbols: string[];
  /** 用户敲进来但不是六位数字的片段（如实回显，不静默吞掉） */
  invalid: string[];
  /** 超出池子上限（20）的部分 */
  overflow: string[];
}

/**
 * 解析标的输入：逗号 / 空格 / 换行 / 顿号都能分隔，去重保序，**坏输入如实回显**。
 *
 * 按**分隔符**切而不是按「非数字」切：后者会把用户敲的中文名（「茅台」）直接吞掉——
 * 那样用户只会看到「至少填一只标的」，却不知道自己那半行去哪了（写第一版时踩到，
 * 用例逮住）。
 */
export function parseSymbols(text: string, limit = 20): SymbolParse {
  const tokens = text
    .split(/[\s,，、;；]+/)
    .map((token) => token.trim())
    .filter(Boolean);
  const seen = new Set<string>();
  const symbols: string[] = [];
  const invalid: string[] = [];
  const overflow: string[] = [];
  for (const token of tokens) {
    if (!/^\d{6}$/.test(token)) {
      invalid.push(token);
      continue;
    }
    if (seen.has(token)) continue;
    seen.add(token);
    if (symbols.length >= limit) {
      overflow.push(token);
      continue;
    }
    symbols.push(token);
  }
  return { symbols, invalid, overflow };
}

export interface PaperFormState {
  name: string;
  initialCash: string;
  symbolsText: string;
  strategy: "ma_cross" | "event_driven" | "user";
  strategyId: string;
  params: Record<string, number>;
  start: string;
  end: string;
  fees: boolean;
  slippage: boolean;
  /** E5：档位只做预设填充；`custom` 时用 `risk` 里的四个值 */
  riskProfile: RiskProfile;
  risk: PaperRiskLimits;
}

/**
 * 每个策略在**建会话表单里真正可见**的参数键。
 *
 * 表单与请求体共用这一份，是因为踩过：切了策略之后，state 里旧策略的参数**照发**，
 * 后端按「未知键」判 422（`event_driven 不接受参数 ['fast','slow']`——界面验证逮到）。
 * 这与 M5b 那条「参数被选作轴后输入框收起、默认值照发」是同一个毛病：
 * **界面上看不见的东西不该进请求**。
 */
export const STRATEGY_PARAM_KEYS: Record<PaperFormState["strategy"], string[]> = {
  ma_cross: ["fast", "slow"],
  event_driven: ["min_score", "hold_days"],
  user: [], // 用户策略的 schema 在源码里，这个表单不渲染它（要调参数去策略工作台）
};

/** 各策略的缺省参数——切策略时用它整份换掉，不留上一个策略的残留。 */
export const STRATEGY_PARAM_DEFAULTS: Record<PaperFormState["strategy"], Record<string, number>> = {
  ma_cross: { fast: 5, slow: 20 },
  event_driven: { min_score: 50, hold_days: 5 },
  user: {},
};

/** 只保留该策略真正接受的参数键（缺省值由后端补）。 */
export function effectiveParams(
  strategy: PaperFormState["strategy"],
  params: Record<string, number>,
): Record<string, number> {
  return Object.fromEntries(
    STRATEGY_PARAM_KEYS[strategy]
      .filter((key) => params[key] !== undefined)
      .map((key) => [key, params[key]]),
  );
}

/** 表单 → 请求体。**`end` 为空就不传**（后端缺省取池子最后一根 bar，别传空串）。 */
export function paperRequest(form: PaperFormState): {
  body: PaperAccountRequest | null;
  error: string | null;
} {
  if (!form.name.trim()) return { body: null, error: "给这个会话起个名字" };
  const cash = Number(form.initialCash);
  if (!Number.isFinite(cash) || cash <= 0) return { body: null, error: "初始资金要填一个正数" };
  const parsed = parseSymbols(form.symbolsText);
  // 坏输入先报：用户敲了「60051」时，最有用的话是「这不是六位代码」，
  // 而不是「至少填一只标的」（那会让他以为是没填）
  if (parsed.invalid.length)
    return { body: null, error: `这些不是六位代码：${parsed.invalid.join("、")}` };
  if (parsed.overflow.length)
    return { body: null, error: `标的池最多 20 只，多出来的是：${parsed.overflow.join("、")}` };
  if (!parsed.symbols.length) return { body: null, error: "至少填一只标的（六位代码）" };
  if (!form.start) return { body: null, error: "选一个起点日期" };
  if (form.strategy === "user" && !form.strategyId)
    return { body: null, error: "选一条已保存的策略（模拟盘只认库里的源码）" };
  const risk = riskProfileLimits(form.riskProfile, form.risk);
  if (risk) {
    const riskError = riskRequestError(risk);
    if (riskError) return { body: null, error: riskError };
  }

  return {
    body: {
      name: form.name.trim(),
      initial_cash: cash,
      symbols: parsed.symbols,
      strategy: form.strategy,
      ...(form.strategy === "user" ? { strategy_id: form.strategyId } : {}),
      params: effectiveParams(form.strategy, form.params),
      start: form.start,
      ...(form.end ? { end: form.end } : {}),
      costs: { fees: form.fees, slippage: form.slippage, slippage_bps: 5 },
      ...(risk ? { risk } : {}),
    },
    error: null,
  };
}
