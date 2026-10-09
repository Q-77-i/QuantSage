/**
 * 后端契约的 TS 镜像。
 *
 * 以 `docs/specs/P1-Tn.md` §5（报告结构）/ §7（端点）为准，改后端时同步改这里。
 * 只镜像前端真正消费到的字段，不做「全字段照抄」——那只会让改动时两处一起漂。
 */

/** 内置策略名（注册表里那两个） */
export type BuiltinStrategy = "ma_cross" | "event_driven";
/** 提交给 `POST /backtest` 的策略名：内置的，或用户策略（M4c，此时必带 `strategy_id`） */
export type Strategy = BuiltinStrategy | "user";
export type PitMode = "pit" | "non_pit" | "both";
export type Adjust = "qfq" | "raw";

// ── GET /api/v1/market/freshness（M1）──────────────────────────────────────

export interface DataFreshness {
  /** 本地行情最后一根 bar 的交易日；数据未落盘时为 null */
  latest_trade_date: string | null;
  latest_event_available_at: string | null;
  /**
   * 事件语料覆盖区间（M2b，值是查出来的、不是写死的）。
   * 起点由首次回填固化、终点随日增前移——事件驱动策略能回测到哪，看的就是这一段。
   */
  event_coverage: EventCoverage;
}

export interface EventCoverage {
  start: string | null;
  end: string | null;
  rows: number;
}

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
  /** 停牌日源数据不给成交量（**不等于 0**），故可为 null */
  volume: number | null;
  is_suspended: boolean;
}

export interface BarsResponse {
  symbol: string;
  adjust: Adjust;
  count: number;
  bars: Bar[];
}

/**
 * `GET /api/v1/market/{symbol}/probe`（代码体检，2026-10-09）。
 *
 * **「本地没有」是 200 + `has_data: false`，不是 404**——所以这一档走正常返回，
 * 与「行情层整体不可用」（503）落在两条完全不同的路径上，界面反应也不同：
 * 前者禁加，后者不拦。
 */
export interface SymbolProbe {
  symbol: string;
  has_data: boolean;
  latest_trade_date: string | null;
  latest_close: number | null;
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

/** `GET /market/symbols` 的响应（M5a）。字典缺失时 `items` 为空列表，不是错误 */
export interface SymbolSearchResponse {
  query: string;
  count: number;
  items: { symbol: string; name: string }[];
}

export interface EquityPoint {
  date: string;
  equity: number;
  /** 同标的买入持有（首根收盘份额化全额买入、扣一次买入成本） */
  benchmark: number;
  /** 全市场等权基准（M5a）。**旧报告没有这一项**——图与 tooltip 按缺失处理 */
  market?: number | null;
}

/** 基准口径自述（M5a）。`note` 由服务端给出，前端**照抄显示**，不另写一份文案。 */
export interface BenchmarkInfo {
  kind: "market_equal_weight";
  note: string;
  exclude_rule: string;
  /** 被剔除的异常样本条数（新股首日 / 复牌，涨跌幅超 30%） */
  excluded: number;
  sample_days: number;
  avg_samples: number;
  total_return: number;
}

/** A 股规则本次的实际生效情况（M5a）。`skipped` 时报告要自证「没做涨跌停判定」 */
export interface AShareRuleStatus {
  limit_check: "on" | "skipped";
  reason: string | null;
  is_st: boolean;
  limit_pct: number | null;
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
  /** 本次回测所依据的事件语料覆盖区间——让存下来的报告自证「看的是哪一段」 */
  event_coverage: EventCoverage;
  /** 窗口内该标的的事件条数；不消费事件的策略为 null（不是 0——含义不同） */
  events_in_window: number | null;
  /** 内置还是用户策略（M4a 起；M4c 的「我的回测」靠它区分显示） */
  strategy_kind: "builtin" | "user";
  /** 用户策略的名字；内置策略为 null */
  strategy_name: string | null;
  /** 基准口径自述（M5a）。**旧记录没有这一项**——重开时按缺失处理，不崩页 */
  benchmark?: BenchmarkInfo;
  /** A 股规则生效情况（M5a）。同上，旧记录缺失 */
  a_share_rules?: AShareRuleStatus;
}

/** 被拒 / 未能成交的信号（M5a）。`code` 为 null 的是非 A 股规则原因（已持仓等） */
export interface RejectedSignal {
  date: string | null;
  side: "buy" | "sell";
  code: string | null;
  reason: string;
  signal_reason: string;
}

export interface RejectSummary {
  count: number;
  /** 原因码 → 条数 */
  by_code: Record<string, number>;
  items: RejectedSignal[];
}

export interface BacktestReport {
  meta: BacktestMeta;
  metrics: Metrics;
  equity_curve: EquityPoint[];
  trades: Trade[];
  /** 拒单与未成交信号，带原因码（M5a）。同上，旧记录缺失 */
  rejects?: RejectSummary;
  open_position: OpenPosition | null;
  /** null = 本次未做对比（未请求，或该策略不消费事件语料） */
  pit_comparison: PitComparison | null;
}

export interface CostOptions {
  fees: boolean;
  slippage: boolean;
  slippage_bps: number;
}

// ── 我的回测（M1c）─────────────────────────────────────────────────────────

/** `POST /api/v1/backtest` 的响应信封：报告外面多一层归属标识（M1c 起） */
export interface BacktestResponse {
  run_id: string;
  report: BacktestReport;
}

/** 落库的 `request` = **解析后的 config**（区间已填充，不是原始请求体） */
export interface StoredBacktestRequest {
  strategy: Strategy;
  symbol: string;
  start: string;
  end: string;
  adjust: Adjust;
  pit_mode: PitMode;
  costs: CostOptions;
  params: Record<string, number>;
}

/** 摘要列表项：**只有 metrics，没有曲线与明细**（列表不该把整份报告拉回来） */
export interface BacktestRunSummary {
  id: string;
  created_at: string;
  symbol: string;
  strategy: Strategy;
  start: string | null;
  end: string | null;
  pit_mode: PitMode;
  metrics: Metrics;
  /** 用户策略的名字（M4c）；内置策略为 null */
  strategy_name: string | null;
}

export interface BacktestRunDetail {
  id: string;
  created_at: string;
  request: StoredBacktestRequest;
  report: BacktestReport;
  /** 当次运行的用户策略 id（内置策略为 null）：与策略**当前**的 hash 比对得出「已非当次代码」 */
  strategy_id: string | null;
  /** 当次运行的源码 sha256（内置策略为 null） */
  code_sha256: string | null;
}

// ── 自选股（M1c）───────────────────────────────────────────────────────────

export interface WatchlistItem {
  symbol: string;
  group_name: string;
  added_at: string;
  /** 加入时最近可得收盘价；样例数据没有这个标的时为 null */
  added_price: number | null;
  latest_close: number | null;
  latest_trade_date: string | null;
  /** 加自选以来涨幅（**比例**，走 `pct()`）；任一价缺失时为 null */
  change_pct: number | null;
}

export interface WatchlistGroup {
  name: string;
  items: WatchlistItem[];
}

export interface BacktestRequest {
  strategy: Strategy;
  /** `strategy: "user"` 时必带（M4c：跑的是**库里那条**源码，不是请求里现传的） */
  strategy_id?: string;
  symbol: string;
  start?: string;
  end?: string;
  costs?: Partial<CostOptions>;
  pit_mode?: PitMode;
  params?: Record<string, number | boolean>;
}

// ── 策略静态检查（M4b 产出，M4c 消费）─────────────────────────────────────

/**
 * 一条检查结果。`severity` 只有两值：`error` 命中即拒绝执行（回测提交前强制为 0），
 * `warning` 照跑但显示。`line` 是 1-based，编辑器标注直接吃。
 */
export interface Finding {
  rule: string;
  severity: "error" | "warning";
  line: number;
  message: string;
  /** 命中那一行的原文，面板里跟在话术后面显示 */
  snippet: string;
}

// ── 策略工作台（M4c）───────────────────────────────────────────────────────

/** `PARAMS` 里一条参数的 schema（后端 `ParamSpec` 的 JSON 形态） */
export interface ParamSpec {
  type: "int" | "float" | "bool";
  default: number | boolean;
  min: number | null;
  max: number | null;
  label: string;
}

/** 静态解析出的元信息：参数表单与「是否消费事件」都从它来 */
export interface StrategyMeta {
  /** 键即参数名，顺序即声明顺序（参数名必是标识符，不会是纯数字键） */
  params: Record<string, ParamSpec>;
  uses_events: boolean;
}

/** `POST /strategies/check`：标注与表单**出自同一次解析** */
export interface StrategyCheck {
  findings: Finding[];
  /** `PARAMS` 解析失败时为 null（问题由 R4 finding 承载） */
  meta: StrategyMeta | null;
}

/** 列表摘要：**不带 code**（列表不为每行拖一份源码） */
export interface StrategySummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

export interface StrategyDetail extends StrategySummary {
  code: string;
  params: Record<string, number | boolean>;
  /** **当前**代码的 sha256（与回测记录里那次运行的 hash 比对，得出「已非当次代码」） */
  code_sha256: string;
}

/** 建 / 改的响应：策略本体 + 那次检查的 findings（**草稿也存得下**） */
export interface StrategySaved extends StrategyDetail {
  findings: Finding[];
}

/** 模板（服务端为唯一真源）：`builtin` 非空即「有内置等价物」 */
export interface StrategyTemplate {
  key: string;
  title: string;
  summary: string;
  builtin: string | null;
  source: string;
}

export interface StrategyWriteBody {
  name?: string;
  code?: string;
  params?: Record<string, number | boolean>;
}

// ── 对话（SPEC §6）──────────────────────────────────────────────────────────

export interface ThreadSummary {
  thread_id: string;
  title: string;
  messages: number;
  /** 最近活动时间（就是列表的排序依据）；个人空间的会话历史用它显示「最近活动」 */
  last_active_at: string;
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
  /** 服务端此刻正在为这个会话跑图（刷新断流后回答还在路上）——前端据此轮询 */
  running: boolean;
}

// ── 批量 / 网格 / 过拟合检验（M5b）─────────────────────────────────────────

/** 一条参数轴。`values` 的顺序即展开顺序，热力图的行列跟着它走 */
export interface OptimizeAxis {
  param: string;
  values: number[];
}

/** 一格：跑的是「这个标的 + 这套参数」。失败格**也在数组里**，以 `error` 说明 */
export interface OptimizeCell {
  /** 在整批里的下标；网格按参数展开序，批量按「标的在外、策略在内」 */
  index: number;
  symbol: string;
  strategy: string;
  /** 用户策略才有（内置为 null） */
  strategy_id: string | null;
  strategy_name: string | null;
  params: Record<string, number>;
  ok: boolean;
  duration_ms: number;
  /** 仅 `ok` 时存在 */
  metrics?: Metrics;
  /** 最优格做 DSR 要用的收益矩；仅 `ok` 时存在 */
  moments?: { skew: number | null; kurt: number | null; n: number };
  /** 该格**实际**跑的区间（批量里逐格不同），仅 `ok` 时存在 */
  window?: { start: string | null; end: string | null; bars: number };
  /** 仅 `!ok` 时存在。`kind` 与沙箱的错误码同源（wall / cpu / memory / output / crash …） */
  error?: { kind: string; message: string };
}

/**
 * 过拟合检验块：DSR **与它的全部输入**（重开时不必回算就能复核）。
 *
 * `reason` 为原因码、`reason_text` 为它的展示文案——两者同行（与 `rejects` 同姿态），
 * 前端直接显示文案，**不自己拼一句**（两处各写一份必然漂移）。
 */
export interface OverfitBlock {
  dsr: number | null;
  reason: string | null;
  reason_text: string | null;
  /** 「N 取全网格格数、未做试验间相关性校正——保守估计」，照抄服务端 */
  note: string;
  n_trials: number;
  n_valid: number;
  best_index: number | null;
  /** 每期口径（未年化）：进公式的是它，年化值已 /√252 */
  sr: number | null;
  sr0: number | null;
  sr_variance: number | null;
  skew: number | null;
  /** **原始**峰度（正态 = 3），不是超额峰度 */
  kurt: number | null;
  observations: number | null;
}

/** 落库的汇总（`summary`）。`cells` 只在重开详情里有，列表页被 SQL 投影掉了 */
export interface OptimizeSummary {
  kind: "grid" | "batch";
  cells: OptimizeCell[];
  cells_total: number;
  cells_ok: number;
  best_index: number | null;
  overfit: OverfitBlock;
  /** 全体格共用的窗口；逐格不同（批量）时为 null——**不挑一格冒充全体** */
  window: { start: string | null; end: string | null; bars: number } | null;
  costs: string;
  pit_mode: PitMode;
  adjust: Adjust;
  duration_s: number;
}

/** `GET /optimize/runs` 的摘要项：`summary` **不含每格矩阵**（SQL 投影 `summary - 'cells'`） */
export interface OptimizeRunSummary {
  id: string;
  created_at: string;
  request: StoredOptimizeRequest;
  summary: Omit<OptimizeSummary, "cells">;
}

/** `GET /optimize/runs/{id}` 的详情 */
export interface OptimizeRunDetail {
  id: string;
  created_at: string;
  request: StoredOptimizeRequest;
  summary: OptimizeSummary;
}

/** 落库的请求（网格与批量共用一套字段，按 `kind` 区分用哪些） */
export interface StoredOptimizeRequest {
  kind: "grid" | "batch";
  strategy?: Strategy;
  strategy_id?: string | null;
  strategy_name?: string | null;
  symbol?: string;
  symbols?: string[];
  strategies?: { strategy: Strategy; strategy_id?: string | null; params?: Record<string, number> }[];
  axes?: OptimizeAxis[];
  params?: Record<string, number>;
  start: string | null;
  end: string | null;
  adjust: Adjust;
  pit_mode: PitMode;
  costs: CostOptions;
}

/** `start` 帧：先把总数与坐标轴给出去，客户端据此画空的热力图与进度 */
export interface OptimizeStartFrame {
  kind: "grid" | "batch";
  total: number;
  symbol?: string;
  strategy?: Strategy;
  strategy_name?: string | null;
  symbols?: string[];
  strategies?: { strategy: Strategy; strategy_name: string | null }[];
  axes?: OptimizeAxis[];
  window?: { start: string; end: string };
  pit_mode: PitMode;
}

/** `done` 帧：落库后的运行号与收尾读数（**每格矩阵不重发**，客户端已经有了） */
export interface OptimizeDoneFrame {
  run_id: string;
  kind: "grid" | "batch";
  cells_total: number;
  cells_ok: number;
  best_index: number | null;
  overfit: OverfitBlock;
  duration_s: number;
}

export interface GridRequest {
  strategy: Strategy;
  strategy_id?: string;
  symbol: string;
  start?: string;
  end?: string;
  costs?: Partial<CostOptions>;
  /** 网格**不收 `both`**：那等于把每格工作量翻倍 */
  pit_mode?: "pit" | "non_pit";
  params?: Record<string, number>;
  axes: OptimizeAxis[];
}

export interface BatchRequest {
  symbols: string[];
  strategies: { strategy: Strategy; strategy_id?: string; params?: Record<string, number> }[];
  start?: string;
  end?: string;
  costs?: Partial<CostOptions>;
  pit_mode?: "pit" | "non_pit";
}

// ── 因子分析（M5c）────────────────────────────────────────────────────────

/** 逐日 RankIC：一天一个读数，`n` 是当日池内样本数 */
export interface FactorICPoint {
  date: string;
  ic: number;
  n: number;
}

export interface FactorIC {
  per_day: FactorICPoint[];
  /** 无有效信号日时为 null（**不是 0**：没有读数与读数为 0 是两回事） */
  mean: number | null;
  std: number | null;
  icir: number | null;
  t_stat: number | null;
  positive_days: number;
  days: number;
}

export interface FactorCurvePoint {
  date: string;
  level: number;
}

/** 与回测同源的四个指标（复用后端 `metrics.py`） */
export interface FactorMetrics {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number | null;
}

export interface FactorTrack {
  curve: FactorCurvePoint[];
  metrics: FactorMetrics;
}

export interface FactorTurnoverPoint {
  date: string;
  buy: number;
  sell: number;
}

export interface FactorGroup {
  quantile: number;
  label: string;
  turnover_avg: number;
  turnover: FactorTurnoverPoint[];
  gross: FactorTrack;
  /** `costs=false` 时为 null（毛/净切换据此禁用） */
  net: FactorTrack | null;
}

export interface FactorLongShort {
  gross: FactorTrack;
  net: FactorTrack | null;
  t_stat: number | null;
  /** **恒为 false**：A 股不可做空，多空价差是统计量不是组合 */
  tradable: boolean;
}

export interface FactorParams {
  source: "event" | "price";
  factor: string;
  quantiles: number;
  min_pool: number;
  aggregation: string;
  horizon: string;
  adjust: string;
  costs: { fees: boolean; slippage: boolean; slippage_bps: number; note: string };
}

export interface FactorWindow {
  start: string;
  end: string;
  first_signal_day: string | null;
  last_signal_day: string | null;
  signal_days: number;
  skipped_no_window: number;
  skipped_thin_pool: number;
  corpus: { start: string | null; end: string | null; rows: number };
  bars_end: string;
}

export interface FactorUniverse {
  pool_avg: number;
  pool_min: number;
  pool_max: number;
  dropped_no_price: number;
  dropped_untradeable: number;
  symbols_seen: number;
  /** 事件源才有 */
  rows_seen?: number;
  dropped_no_value?: number;
  dropped_no_day?: number;
  /** 价格源才有 */
  lookback?: number;
}

export interface FactorReport {
  params: FactorParams;
  window: FactorWindow;
  universe: FactorUniverse;
  ic: FactorIC;
  groups: FactorGroup[];
  long_short: FactorLongShort;
  /** 服务端给的**必填清单**（前端不自己拼免责文案） */
  notes: string[];
}
