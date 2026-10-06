/**
 * 后端契约的 TS 镜像。
 *
 * 以 `docs/specs/P1-Tn.md` §5（报告结构）/ §7（端点）为准，改后端时同步改这里。
 * 只镜像前端真正消费到的字段，不做「全字段照抄」——那只会让改动时两处一起漂。
 */

export type Strategy = "ma_cross" | "event_driven";
export type PitMode = "pit" | "non_pit" | "both";
export type Adjust = "qfq" | "raw";

// ── GET /api/v1/auth/me（M1）────────────────────────────────────────────────

export interface User {
  id: number;
  email: string;
}

// ── GET /api/v1/market/{symbol}/bars ────────────────────────────────────────

export interface Bar {
  /** YYYY-MM-DD：Lightweight Charts 的 business day 口径 */
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_suspended: boolean;
}

export interface BarsResponse {
  symbol: string;
  adjust: Adjust;
  count: number;
  bars: Bar[];
}

// ── GET /api/v1/events ──────────────────────────────────────────────────────

export interface MarketEvent {
  event_id: string;
  title: string;
  summary: string | null;
  event_type: string | null;
  /** 事发时间 */
  event_time: string | null;
  /** 平台首次可用时间——两者之差就是本项目的护城河 */
  available_at: string | null;
  direction_norm: "bullish" | "bearish" | "neutral" | null;
  importance_score: number | null;
  score: number | null;
  source: string | null;
  original_source: string | null;
  content_hash: string | null;
  quality_status: string | null;
}

export interface EventsResponse {
  symbol: string;
  count: number;
  events: MarketEvent[];
}

// ── POST /api/v1/backtest（SPEC §5）─────────────────────────────────────────

export interface Metrics {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  /** 样本不足或标准差为 0 时为 null（不是 0） */
  sharpe: number | null;
  win_rate: number | null;
  trade_count: number;
  final_equity: number;
  benchmark_return: number;
  excess_return: number;
}

export interface EquityPoint {
  date: string;
  equity: number;
  benchmark: number;
}

export interface Trade {
  entry_date: string;
  exit_date: string;
  pnl: number;
  /** **出场**原因（入场原因见 entry_reason） */
  reason: string;
  entry_reason: string;
  return_pct: number;
  hold_bars: number;
  qty: number;
}

export interface OpenPosition {
  shares: number;
  entry_date: string | null;
  entry_price: number;
  entry_reason: string;
  last_close: number;
  unrealized_pnl: number;
  unrealized_return: number;
}

export interface PitDelta {
  final_equity_abs: number;
  /** 核心量化值：期末权益差异比例（分母恒为正） */
  final_equity_pct: number | null;
  total_return_pp: number;
  annual_return_pp: number;
  max_drawdown_pp: number;
  sharpe_abs: number | null;
  win_rate_pp: number | null;
  trade_count: number;
}

export interface PitComparison {
  pit_metrics: Metrics;
  non_pit_metrics: Metrics;
  delta: PitDelta;
  entry_dates: { pit: string[]; non_pit: string[] };
}

export interface BacktestMeta {
  symbol: string;
  strategy: Strategy;
  mode: "pit" | "non_pit";
  start: string | null;
  end: string | null;
  bars: number;
  initial_cash: number;
  adjust: Adjust;
  costs: string;
  cutoff_field: string;
  params: Record<string, number>;
  /** 样本量提示：必须在 UI 常驻展示，不能只留在 JSON 里 */
  warnings: string[];
}

export interface BacktestReport {
  meta: BacktestMeta;
  metrics: Metrics;
  equity_curve: EquityPoint[];
  trades: Trade[];
  open_position: OpenPosition | null;
  /** null = 本次未做对比（未请求，或该策略不消费事件语料） */
  pit_comparison: PitComparison | null;
}

export interface CostOptions {
  fees: boolean;
  slippage: boolean;
  slippage_bps: number;
}

export interface BacktestRequest {
  strategy: Strategy;
  symbol: string;
  start?: string;
  end?: string;
  costs?: Partial<CostOptions>;
  pit_mode?: PitMode;
  params?: Record<string, number>;
}

// ── 对话（SPEC §6）──────────────────────────────────────────────────────────

export interface ThreadSummary {
  thread_id: string;
  title: string;
  messages: number;
}

export interface TokenData {
  text: string;
}

export interface ToolCallData {
  /** run_id：适配器不给 tool_call_id，用它配对 start / end */
  id: string;
  name: string;
  args: unknown;
}

export interface ToolResultData {
  id: string;
  name: string;
  /** 截断预览：MCP 返回可达上百 KB，不整包塞进事件流 */
  content: string;
  is_error: boolean;
}

export interface DoneData {
  thread_id: string;
  content: string;
  usage: Record<string, number> | null;
}

export interface ChatErrorData {
  code: string;
  message: string;
}

// ── GET /api/v1/chat/threads/{thread_id}/messages（SPEC §6）─────────────────

export interface ToolStep {
  /** 实时是 LangGraph run_id、历史是 tool_call_id；只用于单条消息内配对，不跨源比较 */
  id: string;
  name: string;
  args: unknown;
  /** null = 该步没有结果（回合中断） */
  content: string | null;
  is_error: boolean;
}

export interface ThreadMessage {
  role: "user" | "assistant";
  content: string;
  tools: ToolStep[];
}

export interface ThreadMessagesResponse {
  thread_id: string;
  messages: ThreadMessage[];
}
