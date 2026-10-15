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
  /**
   * 源侧行情边界与探测时刻（M2d）。**与 `latest_trade_date` 一起看才有意义**：
   * 落后可能是我们没拉，也可能是源侧还没发——只报本地日期时这两件事分不开。
   * 行情刷新每轮把它写进 `_meta/bars_sync.json`，端点本地读、不联网。
   */
  source_bars_last: string | null;
  source_checked_at: string | null;
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
  /**
   * 数据指纹（E7d）：本次运行读到的**窗口内分片**的 sha256 汇总。
   * 与请求配置一起构成「实验可复现」的两半；**旧记录没有这一项**，按缺失显示「—」。
   */
  data_digest?: string;
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
/** P3-E1a：提交回 202 + 任务号；报告改由 `GET /backtest/runs/{run_id}` 取回 */
export interface BacktestSubmitted {
  task_id: string;
  status: "queued";
}

/** 后端任务状态机的取值（`tasks` 表，SPEC §2.1） */
export type TaskStatus = "queued" | "running" | "done" | "failed" | "cancelled";

/** 任务失败的原因。`line` 只有用户代码的运行期异常才有（与旧 422 响应体同形） */
export interface TaskError {
  kind: string;
  message: string;
  line?: number;
}

export interface TaskProgress {
  done: number;
  total: number;
}

interface TaskBase {
  id: string;
  kind: string;
  status: TaskStatus;
  progress: TaskProgress;
  /** 完成时 `{run_id}`——报告本体不在这里，去 `/backtest/runs/{id}` 取 */
  result: { run_id: string } | null;
  error: TaskError | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** 列表项：不含整份 `request`（用户策略源码在里面），另抽两列供列表显示 */
export interface TaskSummary extends TaskBase {
  symbol: string | null;
  strategy: string | null;
}

export interface TaskDetail extends TaskBase {
  request: StoredBacktestRequest;
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
  /** 用户策略可以有 `bool` 参数（M4c-3）；内置策略的参数都是数字 */
  params: Record<string, number | boolean>;
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
  /** 数据指纹（E7d）；旧记录为 null */
  data_digest: string | null;
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
  /** 当次运行的数据指纹（E7d）；旧记录为 null */
  data_digest: string | null;
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

// ── M6 模拟盘 ──────────────────────────────────────────────────────────────
//
// 与后端的对应关系：`app/paper/store.py` 的 `decision_to_payload` / `account_from_row`
// 就是这些类型。**六态的中文文案由服务端给**（`status_label`），前端不自己拼——
// 两处话术迟早漂移（M5b 的 `overfit.reason_text` 同一条规矩）。

/** 决策的六态（SPEC §7） */
export type PaperDecisionStatus =
  | "pending" // 待审批
  | "approved" // 已批准，等次日开盘成交
  | "filled" // 已成交
  | "rejected" // 已驳回
  | "expired" // 未审批过期（未审批不成交）
  | "unfilled"; // 已批准未成交（一字板 / 停牌顺延超限 / 资金不足一手）

export interface PaperFill {
  trade_date: string;
  qty: number;
  price: number;
  /** 未含滑点的开盘价（`price` 与它的差就是滑点成本） */
  ref_price: number;
  commission: number;
  stamp_tax: number;
  slippage_cost: number;
  cash_delta: number;
}

/** 决策的来源三元组：买入决策能回链到驱动它的那条事件；卖出如实留空（`null`） */
export interface PaperSources {
  event_id?: string;
  title?: string;
  event_time?: string;
  available_at?: string;
  source?: string;
  original_source?: string;
  content_hash?: string;
  source_url?: string;
}

export interface PaperDecision {
  id: string;
  account_id: string;
  trade_date: string;
  symbol: string;
  side: "buy" | "sell";
  /** 按**决策日收盘价**预估的整手数；真正的成交股数见 `fill.qty`（次日开盘价重算） */
  est_qty: number;
  est_price: number;
  reason: string;
  event_id: string | null;
  sources: PaperSources | null;
  status: PaperDecisionStatus;
  /** 服务端给的六态中文文案 */
  status_label: string;
  decided_at: string | null;
  fill: PaperFill | null;
  reject_code: string | null;
  reject_reason: string | null;
}

export interface PaperPosition {
  symbol: string;
  shares: number;
  entry_price: number;
  entry_fees: number;
  entry_date: string | null;
  entry_reason: string;
}

export interface PaperEquityPoint {
  trade_date: string;
  cash: number;
  market_value: number;
  equity: number;
}

export interface PaperAccountConfig {
  initial_cash: number;
  symbols: string[];
  strategy: Strategy;
  strategy_name: string | null;
  params: Record<string, number>;
  start: string;
  end: string;
  costs: {
    commission_rate: number;
    commission_min: number;
    stamp_tax_rate: number;
    slippage_bps: number;
    fee_enabled: boolean;
    slippage_enabled: boolean;
  };
}

/** 每只标的的涨跌停判定生效情况（升格如实标注：`skipped` 时要说出来） */
export interface PaperRuleStatus {
  limit_check: "on" | "skipped";
  reason: string | null;
  is_st: boolean;
  limit_pct: number | null;
}

export interface PaperAccount {
  id: string;
  name: string;
  status: "active" | "finished";
  cash: number;
  realized_pnl: number;
  as_of: string;
  config: PaperAccountConfig;
  rules: Record<string, PaperRuleStatus>;
  created_at: string;
  updated_at: string;
}

export interface PaperProgress {
  start: string;
  end: string;
  as_of: string;
  days_total: number;
  days_done: number;
}

/** `step` 的响应里才有：**这一次推进发生了什么** */
export interface PaperStepOutcome {
  trade_date: string;
  filled: PaperDecision[];
  expired: PaperDecision[];
  unfilled: PaperDecision[];
  generated: PaperDecision[];
}

export interface PaperAccountDetail {
  account: PaperAccount;
  progress: PaperProgress;
  /** 最后一个交易日的收盘估值；会话刚建时是第一天 */
  valuation: PaperEquityPoint | null;
  positions: PaperPosition[];
  decisions: PaperDecision[];
  /** `decisions` 里状态为待审批的那些（省得前端自己过滤） */
  pending: PaperDecision[];
  equity_curve: PaperEquityPoint[];
  this_step?: PaperStepOutcome;
}

/** 会话列表的摘要行（`GET /accounts`） */
export interface PaperAccountSummary {
  id: string;
  name: string;
  status: "active" | "finished";
  cash: number;
  as_of: string;
  strategy: Strategy;
  strategy_name: string | null;
  symbols: string[];
  start: string;
  end: string;
  rules: Record<string, PaperRuleStatus>;
  created_at: string;
}

export interface PaperAccountRequest {
  name: string;
  initial_cash: number;
  symbols: string[];
  strategy: Strategy;
  strategy_id?: string;
  params: Record<string, number>;
  start: string;
  end?: string;
  costs: { fees: boolean; slippage: boolean; slippage_bps: number };
}

// ── M7 绩效研报 ────────────────────────────────────────────────────────────

/** 正文结构版本（服务端 `report.version`）；M8 追加块时递增 */
export type ReportBlockKind = "fact" | "inference";

/** 一个块：`kind` 是 claim 分级，`numbers` 是「可复算」的数字（键路径 → 值），`evidence` 是证据键 */
export interface ReportBlock {
  id: string;
  kind: ReportBlockKind;
  title: string;
  text?: string;
  /** 可复算的数字：键路径 → 值。M7 是数值、M8 的深研报是**已格式化的字符串**（如 `-11.75%`） */
  numbers?: Record<string, number | string | null>;
  evidence?: string[];
  /** 仅推断块（综述）有：模型身份与缺席原因 */
  model?: string;
  prompt_version?: string;
  note?: string | null;
}

/** 一条证据：决策当时的来源快照 + 语料行补充 + 两者的对照结论 */
export interface ReportEvidence {
  event_id: string;
  day: string;
  title: string | null;
  summary: string | null;
  event_time: string | null;
  available_at: string | null;
  source: string | null;
  original_source: string | null;
  content_hash: string | null;
  source_url: string | null;
  industries: string[];
  direction_norm: string | null;
  found: boolean;
  revised: boolean;
  corpus_hash: string | null;
  decision_ids: string[];
}

export interface ReportMetrics {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number | null;
  volatility: number | null;
  win_rate: number | null;
  trade_count: number;
  final_equity: number;
  benchmark_return: number | null;
  excess_return: number | null;
}

export interface ReportAccount {
  id: string;
  name: string;
  status: string;
  strategy: string;
  strategy_name: string | null;
  symbols: string[];
  initial_cash: number;
  start: string;
  as_of: string;
  data_end: string;
}

export interface ReportEquityPoint {
  date: string;
  equity: number;
  benchmark: number | null;
}

export interface ReportSymbolRow {
  symbol: string;
  trips: number;
  closed: number;
  wins: number;
  realized_pnl: number;
  unrealized_pnl: number;
  unmarked: number;
  contribution_pp: number;
}

export interface ReportSignalRow {
  label: string;
  trips: number;
  closed: number;
  wins: number;
  pnl: number;
  unmarked: number;
}

export interface ReportReflection {
  text: string | null;
  model: string;
  prompt_version?: string;
  note: string | null;
}

/** 一条复盘：已到期（settled）或未到期（open）；未到期没有教训 */
export interface ReportReviewItem {
  decision_id: string;
  symbol: string;
  entry_date: string;
  exit_date: string | null;
  settled: boolean;
  pnl: number | null;
  return_pct: number | null;
  benchmark_pct: number | null;
  alpha_pp: number | null;
  window_days: number;
  entry_reason: string;
  exit_reason: string;
  direction: string | null;
  evidence_key: string | null;
  reflection: ReportReflection | null;
  sources: PaperSources | null;
  evidence: ReportEvidence | null;
}

/** 定了但没交易的决策（驳回 / 过期 / 未成交）——只列状态，不做反事实收益 */
export interface ReportUnfilledItem {
  decision_id: string;
  symbol: string;
  trade_date: string;
  side: "buy" | "sell";
  status: string;
  status_label: string;
  reject_code: string | null;
  reject_reason: string | null;
  evidence: ReportEvidence | null;
}

export interface ReportReview {
  summary: {
    settled: number;
    open: number;
    unfilled: number;
    lessons: number;
    saved?: number;
    reused?: number;
    as_of: string;
  };
  settled: ReportReviewItem[];
  open: ReportReviewItem[];
  unfilled: ReportUnfilledItem[];
}

/** 冻结产物的完整正文（`report` 字段） */
export interface ReportBody {
  version: number;
  kind: string;
  /** 账户报告（M7）才有；深研报（M8）为 null，头由 `brief` 渲染 */
  account?: ReportAccount;
  metrics: ReportMetrics;
  benchmark: { kind: string | null; note?: string; total_return?: number | null };
  equity_curve: ReportEquityPoint[];
  attribution: {
    summary: Record<string, number>;
    symbols: ReportSymbolRow[];
    direction: ReportSignalRow[];
    industry: ReportSignalRow[];
  };
  review: ReportReview | null;
  blocks: ReportBlock[];
  evidence: ReportEvidence[];
  snapshot: {
    bars?: { digest?: string };
    events?: { digest?: string; days?: number; rows?: number };
    market_end?: string | null;
    review_hash?: string;
  };
  warnings?: string[];
  /** 深研报（M8）：问题 / 标的 / 时点 */
  brief?: { question?: string; symbols?: string[]; as_of?: string };
  run?: { elapsed_s?: number; cost_usd?: number; llm_calls?: number; tokens?: number };
  gaps?: string[];
}

/** 生成 / 取回报告的回执 */
/** 一次深度研究的摘要（列表用，不含事件） */
export interface ResearchRunSummary {
  id: string;
  question: string;
  subject: Record<string, unknown>;
  status: string;
  report_id: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** 运行详情：`events` 是过程可视化的真源，`report` 是完成后的冻结产物 */
export interface ResearchRunDetail extends ResearchRunSummary {
  events: RunEventPayload[];
  budget: Record<string, unknown>;
  report: ResearchReportBody | null;
  done?: boolean;
}

/** 事件载荷：**与落库事件同形**（服务端刻意如此，实时流与回放才能共用一套视图） */
export interface RunEventPayload {
  phase: "start" | "end" | "interrupt";
  node?: string;
  at?: number;
  seconds?: number | null;
  status?: string;
  payload?: { kind: string; detail: string; options: string[] };
}

/** 深研报正文（M8 的 blocks 与 M7 同一结构） */
export interface ResearchReportBody {
  kind: string;
  brief?: Record<string, unknown>;
  run?: Record<string, unknown>;
  blocks: ReportBlock[];
  evidence: ReportEvidence[];
  gaps?: string[];
  debate?: unknown[];
}

export interface ReportEnvelope {
  id: string;
  created_at: string | null;
  report: ReportBody;
  report_hash: string;
  snapshot_hash: string;
  share_token: string | null;
  share_path: string | null;
  reused: boolean;
}

/** 公开只读（`/public/reports/{token}`）——**不含任何身份字段** */
export interface PublicReport {
  id: string;
  report: ReportBody;
  report_hash: string;
  snapshot_hash: string;
  created_at: string | null;
  shared_at: string | null;
}

export interface ReportSummary {
  id: string;
  created_at: string;
  snapshot_hash: string;
  report_hash: string;
  share_token: string | null;
  share_path: string | null;
}

// ── 研究首页（M10a）─────────────────────────────────────────────────────

/** `GET /api/v1/market/overview`（M10a）。各块可为 null——数据没落盘时端点整体 503， */
/** 但「某一天没有样本」是有意义的空，故形状上留得住。 */
export interface MarketOverview {
  trade_date: string | null;
  adjust: string;
  amount: number | null;
  breadth: MarketBreadth | null;
  limits: MarketLimits | null;
  limits_meta: MarketLimitsMeta | null;
  index: MarketIndex | null;
  /** 两个「截至」：行情与事件语料**天然不同步**，界面并列显示 */
  as_of: { bars: string | null; events: string | null } | null;
}

export interface MarketBreadth {
  total: number;
  up: number;
  down: number;
  flat: number;
  /** 有价但没有涨跌幅的行——**不进涨跌平任何一档**（缺失不是零涨跌） */
  without_pct: number;
}

export interface MarketLimits {
  limit_up: number;
  limit_down: number;
  /** 真正判过的家数 */
  judged: number;
  /** 判不了而跳过的家数（缺前收 / 板别认不出）——如实返回，不当成「没涨停」 */
  skipped: number;
}

export interface MarketLimitsMeta {
  adjust: string;
  st_source: string;
  st_symbols: number;
  /** 口径说明由服务端给，界面照登（前端不自己拼一份免责话术） */
  note: string;
}

/** 全市场等权代理——**不是指数**（`kind` / `note` 照登界面） */
export interface MarketIndex {
  kind: string;
  level: number | null;
  change_pct: number | null;
  base_level: number;
  base_date: string | null;
  days: number;
  excluded: number;
  kept: number;
  note: string;
}

/** `GET /api/v1/market/sectors`（M10a） */
export interface MarketSectors {
  trade_date: string | null;
  adjust: string;
  min_symbols: number;
  count: number;
  coverage: MarketSectorCoverage;
  items: SectorRow[];
  /** 归属口径说明（含「各行业成交额之和 > 全市场」这条），界面照登 */
  note: string;
}

export interface MarketSectorCoverage {
  total: number;
  covered: number;
  ratio: number | null;
}

export interface SectorRow {
  industry: string;
  /** 该档均值的样本数——读者判断这个数可不可信的唯一线索 */
  symbols: number;
  amount: number | null;
  avg_pct: number | null;
  median_pct: number | null;
}

// ── 事件日历 / 快讯流（M10b）─────────────────────────────────────────────

/** `GET /api/v1/events/calendar`（M10b）——日期带 */
export interface EventCalendar {
  start: string | null;
  end: string | null;
  high_score: number;
  /** **只含真有事件的日子**：补零会被读成「那天数据缺失」，而那是另一回事 */
  days: EventCalendarDay[];
}

export interface EventCalendarDay {
  /** `YYYY-MM-DD`（北京时间口径，服务端已在 SQL 里分好组） */
  date: string;
  total: number;
  /** 该日 `importance_score >= high_score` 的条数 */
  high: number;
  by_type: Record<string, number>;
}

/** `GET /api/v1/events/feed`（M10b）——某日的分页快讯流 */
export interface EventFeed {
  date: string | null;
  total: number;
  count: number;
  offset: number;
  limit: number;
  /** 本次的类型白名单（null = 全部类型） */
  types: string[] | null;
  min_importance: number | null;
  items: EventFeedItem[];
}

/**
 * 一条快讯。
 *
 * **字段集刻意覆盖 `EvidenceFields`**（M7 证据行真正读的那几个），故可以直接喂
 * `evidenceRows(item)`——两处展示的是同一件事，行序与文案都该同源。
 *
 * **M10d 起不再带 `summary`**（D1）：正文只从详情抽屉出（`EventDetail`），
 * 每行少传几百字；「正文以哪处为准」也就只有一个答案。
 */
export interface EventFeedItem {
  event_id: string;
  event_type: string | null;
  title: string | null;
  /** 事发 */
  event_time: string | null;
  /** 平台首次可得——与事发之差就是这个项目的护城河展示位 */
  available_at: string | null;
  direction_norm: string | null;
  industries: string[];
  importance_score: number | null;
  source: string | null;
  original_source: string | null;
  source_url: string | null;
  content_hash: string | null;
  /** 关联标的；字典里没有名字的给 `null`，**不编** */
  symbols: { code: string; name: string | null }[];
}

// ── 事件详情（M10d）────────────────────────────────────────────────────

/**
 * `GET /api/v1/events/{id}`：一条事件的完整证据链。
 *
 * 与 `EventFeedItem` 的差别是**多了六类真内容**：正文（`summary`）、人物、每只标的的
 * `reason`、方向与因子分解、三个时刻（报告可得 / 平台可得 / 观察）、以及平台的修订标记。
 * 所有这些在库里本来就有，M10d 之前只是没出库。
 */
export interface EventDetail {
  event_id: string;
  /** 这条**实际落在哪一天**：省略 `day` 查询时按最新取，回显就靠它 */
  date: string | null;
  event_type: string | null;
  title: string | null;
  summary: string | null;
  event_time: string | null;
  /** 平台首次可得（护城河展示位） */
  available_at: string | null;
  /** 平台**报告**的可得时刻：与 `available_at` 不一致时，差的就是平台自己的修正 */
  reported_available_at: string | null;
  /** 我们观察到它的时刻 */
  observed_at: string | null;
  /** 平台原词（中英混用） */
  direction: string | null;
  /** 归一后的方向（后端做） */
  direction_norm: string | null;
  confidence: number | null;
  importance_score: number | null;
  factor_value: number | null;
  /** 落盘的双重编码 JSON，后端已解析成对象（解析失败给 `{}`） */
  factor_scores: Record<string, unknown>;
  industries: string[];
  symbols: string[];
  stocks: EventStockDetail[];
  person: string[];
  source: string | null;
  original_source: string | null;
  source_url: string | null;
  content_hash: string | null;
  quality_status: string | null;
  source_time_quality: string | null;
  dedup_key: string | null;
  /** 修订四件：平台自己的标记。**照原样显示**，不加工成「已修订 N 次」那种文案 */
  record_version: number | null;
  revision_id: string | null;
  revision_time: string | null;
  is_corrected: boolean | null;
  correction_count: number | null;
}

/** 一只被打标的标的：`name` 是本地字典解析结果（缺名给 null），`source_name` 是平台自己的 */
export interface EventStockDetail {
  code: string;
  name: string | null;
  source_name: string | null;
  /** 平台为什么把它挂在这条事件上；没有理由时是 `null`（如实显示「—」，不编） */
  reason: string | null;
}

// ── 标的浏览（M10c）─────────────────────────────────────────────────────

/** `GET /api/v1/market/cross-section`（M5a 起；M10c 起前端才第一次消费它） */
export interface CrossSection {
  trade_date: string | null;
  total: number;
  count: number;
  items: CrossRow[];
  adjust: string;
  sort: string;
  order: string;
  /** 行业过滤（PIT 累积归属）；`null` = 全市场 */
  industry: string | null;
  limit: number;
  /** 请求时的起点；前端把它推进成「下一页的服务端位置」后放回 */
  offset: number;
}

export interface CrossRow {
  symbol: string;
  /** 名称来自事件语料抽出的字典（覆盖 96.7%）；没有的给 `null`，**不编** */
  name: string | null;
  close: number | null;
  /** 百分数（`1.86` = +1.86%），**不是比例** */
  change_pct: number | null;
  amount: number | null;
  /** 源侧 2026-08 起逐步停更、2026-09 起整列为空——界面不列这一栏 */
  turnover_pct: number | null;
}
