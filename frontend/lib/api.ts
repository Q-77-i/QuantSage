/**
 * 后端 HTTP 客户端。
 *
 * 基址**默认跟随页面的 host**（只换端口），因为会话 cookie 的 SameSite 按 site 算
 * （忽略端口）：页面在 `localhost` 而 API 写死在 `127.0.0.1` 时，两者算跨站，
 * httpOnly cookie 会被浏览器静默丢弃——表现为「注册/登录成功却立刻又回到登录页」。
 * 跟随 host 后，从哪个 host 打开页面都不会踩这条。
 *
 * 需要指向别的后端（换机器/换端口）时才用 `NEXT_PUBLIC_API_BASE` 显式覆盖。
 * 前端跑 3001 而不是 3000：3000 被 Langfuse 自托管 UI 占用。
 */

import { streamGet } from "./sse";
import type { PostStreamOptions } from "./sse";
import { factorQuery } from "./factor-report";
import type { FactorQueryInput } from "./factor-report";
import { query } from "./query";
import type {
  BacktestRequest,
  BacktestRunDetail,
  BacktestRunSummary,
  BacktestSubmitted,
  BarsResponse,
  CrossSection,
  DataFreshness,
  DataHealth,
  DataHealthHistory,
  EventCalendar,
  EventDetail,
  EventFeed,
  EventsResponse,
  MarketOverview,
  MarketSectors,
  StrategyCheck,
  StrategyDetail,
  StrategySaved,
  StrategySummary,
  StrategyTemplate,
  StrategyVersionDetail,
  StrategyVersionDiff,
  StrategyVersionSummary,
  TaskDetail,
  TaskSummary,
  FactorReport,
  OptimizeRunDetail,
  OptimizeRunSummary,
  PaperAccountDetail,
  PaperAccountRequest,
  PaperAccountSummary,
  PaperDecision,
  StrategyWriteBody,
  SymbolProbe,
  SymbolSearchResponse,
  ThreadMessagesResponse,
  ThreadSummary,
  User,
  WatchlistItem,
  PublicReport,
  ReportEnvelope,
  ResearchRunDetail,
  ResearchRunSummary,
  ReportSummary,
  AdminRejections,
  AdminUsage,
} from "./types";

/** 后端端口。只在本机联调时改（见 .env.local 说明）。 */
const API_PORT = process.env.NEXT_PUBLIC_API_PORT ?? "8000";

/**
 * 取 API 基址。**函数而非常量**：`window` 只在浏览器里存在，
 * 而客户端组件在预渲染时也会在服务端跑一遍模块顶层（模块级引用 `window` 会直接炸）。
 */
export function apiBase(): string {
  const override = process.env.NEXT_PUBLIC_API_BASE;
  if (override) return override;
  const { protocol, hostname } = window.location;
  return `${protocol}//${hostname}:${API_PORT}`;
}

/** 普通 REST 的默认上限（P2）：后端假死/网络黑洞时不再无限悬挂。 */
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
/** 同步长请求（生成研报、因子全市场扫描、模拟盘跑到末端）的上限。 */
export const LONG_REQUEST_TIMEOUT_MS = 120_000;

/** 超时信号与调用方 signal 合并；任一 abort 都让 fetch 结束。 */
export function requestSignal(
  signal?: AbortSignal,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /**
     * 解析后的错误体（拿不到就是 null）。
     *
     * 带上它是因为**有些错误带结构化信息**：策略工作台的 422 里除了 `detail` 还有
     * `findings`（编辑器标注直接吃），只留一句人话会把它们丢掉。
     */
    readonly payload: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 把后端的错误体翻成一句人话：FastAPI 的 422 是数组，直接 toString 只会显示 [object Object]。 */
export function messageFromBody(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as { msg?: string })?.msg)
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length) return messages.join("；");
  }
  return `请求失败（HTTP ${status}）`;
}

/** SSE 那条路已经自己读完流了，只需要文案——保留这个入口不破坏既有调用方。 */
export async function errorMessage(response: Response): Promise<string> {
  try {
    return messageFromBody(await response.json(), response.status);
  } catch {
    // 非 JSON 响应（网关错误页一类）走兜底
    return `请求失败（HTTP ${response.status}）`;
  }
}

/**
 * 把异常翻成一句人话：`ApiError` 带的是后端的 detail，其余（断网、后端没起）用调用方给的兜底。
 * 面板与 hook 都走这一条，免得同一种失败在不同页面说法不一。
 */
export function describeError(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.message : fallback;
}

/** 鉴权端点自己的 401 不是「会话失效」：登录页的 401 是「密码错」（见 `loginRedirectTarget`）。 */
const AUTH_PATH_PREFIX = "/api/v1/auth/";

/**
 * 会话中途失效（401）该跳哪：返回登录页地址，或 `null` 表示「不跳，照常抛错」。

 * **纯函数**：三种不该跳的情形都是踩过的坑，写宽一点点就是重定向环或吞掉错误提示，
 * 所以判据单独拿出来由单测钉住，而不是散在 fetch 调用点里。
 *
 * 不跳的三种：
 *  ① 鉴权端点自己（`/auth/login` 输错密码就是 401——跳过去等于把错误提示换成一次重定向）；
 *  ② 已经在登录/注册页（跳自己 = 环）；
 *  ③ 服务端渲染阶段（`window` 不存在，没得跳）。
 *
 * `/auth/me` 也在 ① 里：**「未登录」那条路有它自己的主人**——`AuthProvider` 探测 +
 * `(app)/layout.tsx` 的守卫（它已经在跳 `/login?next=`）。两处都跳会互相打架，
 * 这里只管「本来登着，中途失效了」。
 */
export function loginRedirectTarget(
  status: number,
  path: string,
  currentPath: string,
  hasWindow: boolean,
): string | null {
  if (status !== 401) return null;
  if (!hasWindow) return null;
  if (path.startsWith(AUTH_PATH_PREFIX)) return null;
  if (currentPath === "/login" || currentPath === "/register") return null;
  return `/login?next=${encodeURIComponent(currentPath)}`;
}

/**
 * 会话失效就把人送去登录页（`next` 带回当前位置，登录后原地返回）。
 *
 * 只做跳转，**不吞异常**——调用点照常抛 `ApiError`，页面上的错误文案与跳转互不干扰。
 */
export function bounceToLogin(status: number, path: string): void {
  if (typeof window === "undefined") return;
  const target = loginRedirectTarget(
    status,
    path,
    window.location.pathname + window.location.search,
    true,
  );
  if (target) window.location.assign(target);
}

async function request<T>(
  path: string,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<T> {
  // credentials 必须带：会话是 httpOnly cookie，JS 读不到也放不进去，只能让浏览器带上。
  // 前后端同 site（127.0.0.1 的 3001 ↔ 8000），SameSite=Lax 不影响这条请求。
  const signal = requestSignal(init?.signal ?? undefined, timeoutMs);
  let response: Response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      credentials: "include",
      ...init,
      signal,
    });
  } catch (cause) {
    // 调用方主动 abort 原样抛出；只有超时才翻成可读错误
    if (cause instanceof DOMException && cause.name === "TimeoutError") {
      throw new ApiError(0, "请求超时，请稍后重试");
    }
    throw cause;
  }
  if (!response.ok) {
    // 体只读一次：文案与 payload 从同一份解析结果出，别两次 json()（流只能读一次）
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // 非 JSON（网关错误页一类）：留给兜底文案
    }
    bounceToLogin(response.status, path);
    throw new ApiError(response.status, messageFromBody(body, response.status), body);
  }
  return (await response.json()) as T;
}

const jsonInit = (method: string, body: unknown, key?: string): RequestInit => ({
  method,
  headers: {
    "Content-Type": "application/json",
    // 幂等键（P3-E4b）：同一次用户动作重试复用同一个，服务端据此只跑一遍
    ...(key ? { "Idempotency-Key": key } : {}),
  },
  body: JSON.stringify(body),
});

export const api = {
  // ── 认证（M1）─────────────────────────────────────────────────────────
  me: () => request<User>("/api/v1/auth/me"),

  /** `remember` 只改 cookie 存活方式：true → 持久（关浏览器仍在），false → 会话 cookie */
  login: (email: string, password: string, remember = true) =>
    request<User>("/api/v1/auth/login", jsonInit("POST", { email, password, remember })),

  register: (email: string, password: string) =>
    request<User>("/api/v1/auth/register", jsonInit("POST", { email, password })),

  logout: () => request<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" }),

  // ── admin 视图（E2b）──────────────────────────────────────────────────
  /** 配额视图**一页**（在飞任务 / 当期调用 / 当期被拒）+ 顶层一份上限。非 admin 会 403 */
  adminUsage: (
    params: {
      limit?: number;
      offset?: number;
      q?: string;
      role?: string;
      active_only?: boolean;
      days?: number;
      sort?: string;
      order?: "asc" | "desc";
    } = {},
  ) =>
    request<AdminUsage>(`/api/v1/admin/usage${query({ ...params })}`),

  /** 拒绝记录**一页**（新的在前）。**审计半边的内容就是它**——只有用量快照的话没东西可看 */
  adminRejections: (params: { limit?: number; offset?: number } = {}) =>
    request<AdminRejections>(`/api/v1/admin/rejections${query({ ...params })}`),

  // ── 数据看板（E3a）───────────────────────────────────────────────────
  /** 体检首屏：最近一次落档 + 现算的新鲜度与源侧边界。**没跑过回 `last: null`，不是 404** */
  dataHealth: () => request<DataHealth>("/api/v1/data/health"),

  /** 体检台账**一页**（新的在前）。分页是必需的——台账只增不删 */
  dataHealthHistory: (params: { limit?: number; offset?: number } = {}) =>
    request<DataHealthHistory>(`/api/v1/data/health/history${query({ ...params })}`),

  // ── 数据 ──────────────────────────────────────────────────────────────
  /** 页头「数据截至 X」的数据源。查出来的，不是写死的 */
  freshness: () => request<DataFreshness>("/api/v1/market/freshness"),

  bars: (symbol: string, params: { start?: string; end?: string; adjust?: string } = {}) =>
    request<BarsResponse>(`/api/v1/market/${symbol}/bars${query(params)}`),

  /** 6 位代码体检（自选股表单边输边查）。无数据是 200 `has_data: false`，不是 404 */
  probe: (symbol: string) => request<SymbolProbe>(`/api/v1/market/${symbol}/probe`),

  /** 按代码前缀或名称子串搜标的（M5a）。名称来自事件语料抽出的字典，覆盖 96.7% */
  symbols: (q: string, limit = 20) =>
    request<SymbolSearchResponse>(`/api/v1/market/symbols${query({ q, limit })}`),

  events: (
    symbol: string,
    params: { start?: string; end?: string; limit?: number; offset?: number } = {},
  ) => request<EventsResponse>(`/api/v1/events${query({ symbol, ...params })}`),

  /**
   * **提交**一次回测（P3-E1a）：202 + `{task_id}`，不等引擎。
   * 报告经 `api.run(run_id)` 取回——`useBacktestTask` 把「提交 → 轮询 → 取 run_id」串起来。
   *
   * 全部闸门仍在提交期判（401/404/422/503 照旧）；**跑起来才知道**的错误（沙箱配额、
   * 用户代码运行期异常）落在任务 `error` 里。
   */
  submitBacktest: (body: BacktestRequest, key?: string) =>
    request<BacktestSubmitted>("/api/v1/backtest", jsonInit("POST", body, key)),

  // ── 任务（P3-E1a）─────────────────────────────────────────────────────
  tasks: (limit = 20) => request<TaskSummary[]>(`/api/v1/tasks${query({ limit })}`),

  /** 轮询与回看都走它：`status` / `progress` / `result.run_id` / `error` */
  task: (taskId: string, signal?: AbortSignal) =>
    request<TaskDetail>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, { signal }),

  /** 取消排队中的任务；已在跑或已结束是 409（后端如实拒绝，不假装能停） */
  cancelTask: (taskId: string) =>
    request<TaskDetail>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
    }),

  // ── 我的回测（M1c）────────────────────────────────────────────────────
  runs: (limit = 20) =>
    request<BacktestRunSummary[]>(`/api/v1/backtest/runs${query({ limit })}`),

  /** 重开某次回测：取回完整报告 + 当次的请求配置 */
  run: (runId: string) =>
    request<BacktestRunDetail>(`/api/v1/backtest/runs/${encodeURIComponent(runId)}`),

  // ── 自选股（M1c）──────────────────────────────────────────────────────
  watchlist: () => request<WatchlistItem[]>("/api/v1/watchlist"),

  addWatchlist: (symbol: string, groupName?: string) =>
    request<WatchlistItem>(
      "/api/v1/watchlist",
      jsonInit("POST", groupName ? { symbol, group_name: groupName } : { symbol }),
    ),

  moveWatchlist: (symbol: string, groupName: string) =>
    request<{ symbol: string; group_name: string }>(
      `/api/v1/watchlist/${encodeURIComponent(symbol)}`,
      jsonInit("PATCH", { group_name: groupName }),
    ),

  removeWatchlist: (symbol: string) =>
    request<{ symbol: string; deleted: boolean }>(
      `/api/v1/watchlist/${encodeURIComponent(symbol)}`,
      { method: "DELETE" },
    ),

  renameWatchlistGroup: (from: string, to: string) =>
    request<{ group_name: string; renamed_from: string }>(
      `/api/v1/watchlist/groups/${encodeURIComponent(from)}`,
      jsonInit("PATCH", { name: to }),
    ),

  deleteWatchlistGroup: (name: string) =>
    request<{ group_name: string; deleted: boolean; fallback_group: string }>(
      `/api/v1/watchlist/groups/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  // ── 策略工作台（M4c）──────────────────────────────────────────────────
  /** 本人全部策略（摘要，不带 code） */
  strategies: () => request<StrategySummary[]>("/api/v1/strategies"),

  strategy: (id: string) =>
    request<StrategyDetail>(`/api/v1/strategies/${encodeURIComponent(id)}`),

  /** 建策略。**草稿也存得下**（findings 随响应返回，闸门在运行前） */
  createStrategy: (body: { name: string; code: string; params?: Record<string, number | boolean> }) =>
    request<StrategySaved>("/api/v1/strategies", jsonInit("POST", body)),

  updateStrategy: (id: string, body: StrategyWriteBody) =>
    request<StrategySaved>(
      `/api/v1/strategies/${encodeURIComponent(id)}`,
      jsonInit("PUT", body),
    ),

  deleteStrategy: (id: string) =>
    request<{ id: string; deleted: boolean }>(
      `/api/v1/strategies/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  /** 源码 → `{findings, meta}`：标注与参数表单同源（纯函数，不落库） */
  checkStrategy: (code: string) =>
    request<StrategyCheck>("/api/v1/strategies/check", jsonInit("POST", { code })),

  strategyTemplates: () => request<StrategyTemplate[]>("/api/v1/strategies/templates"),

  // ── 5d 策略版本 ────────────────────────────────────────────────────────
  strategyVersions: (id: string) =>
    request<StrategyVersionSummary[]>(
      `/api/v1/strategies/${encodeURIComponent(id)}/versions`,
    ),

  strategyVersion: (id: string, versionNo: number) =>
    request<StrategyVersionDetail>(
      `/api/v1/strategies/${encodeURIComponent(id)}/versions/${versionNo}`,
    ),

  strategyVersionDiff: (id: string, versionNo: number, against?: number) =>
    request<StrategyVersionDiff>(
      `/api/v1/strategies/${encodeURIComponent(id)}/versions/${versionNo}/diff${
        against === undefined ? "" : `?against=${against}`
      }`,
    ),

  rollbackStrategy: (id: string, versionNo: number, expectedVersion?: number) =>
    request<StrategySaved>(
      `/api/v1/strategies/${encodeURIComponent(id)}/rollback`,
      jsonInit("POST", {
        version_no: versionNo,
        ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
      }),
    ),

  threads: () => request<ThreadSummary[]>("/api/v1/chat/threads"),

  threadMessages: (threadId: string) =>
    request<ThreadMessagesResponse>(
      `/api/v1/chat/threads/${encodeURIComponent(threadId)}/messages`,
    ),

  deleteThread: (threadId: string) =>
    request<{ thread_id: string; deleted: boolean }>(
      `/api/v1/chat/threads/${encodeURIComponent(threadId)}`,
      { method: "DELETE" },
    ),

  /** 我的优化（摘要，**不含每格矩阵**）：最近在前 */
  optimizeRuns: (limit = 20) =>
    request<OptimizeRunSummary[]>(`/api/v1/optimize/runs?limit=${limit}`),

  /** 重开一次优化：`request` + 完整 `summary`（含每格矩阵） */
  optimizeRun: (runId: string) =>
    request<OptimizeRunDetail>(`/api/v1/optimize/runs/${encodeURIComponent(runId)}`),

  // ── 因子分析（M5c）─────────────────────────────────────────────────────
  /** 因子报告：**同步一次返回**（公开端点、不落库）；`costs=false` 时净曲线为 null */
  factorReport: (params: FactorQueryInput) =>
    request<FactorReport>(
      `/api/v1/factor/report${factorQuery(params)}`,
      undefined,
      LONG_REQUEST_TIMEOUT_MS,
    ),

  // ── 标的浏览（M10c）────────────────────────────────────────────────────
  /**
   * 某交易日的全市场截面（也可按**行业**过滤——走与板块块同一支 PIT 归属）。
   *
   * 分页用 `limit` / `offset`（服务端上限 500）；返回信封的 `total` 是**过滤后**的总数，
   * 「还有没有下一页」看它。
   */
  crossSection: (params: {
    date?: string;
    sort?: string;
    order?: "desc" | "asc";
    q?: string;
    industry?: string;
    limit?: number;
    offset?: number;
  } = {}) =>
    request<CrossSection>(`/api/v1/market/cross-section${query({ ...params })}`),

  // ── 事件日历 / 快讯流（M10b）──────────────────────────────────────────
  /**
   * 日期带：区间内每日的事件计数。`end` 缺省取语料里最后一个有事件的日子。
   */
  eventCalendar: (params: { start?: string; end?: string; highScore?: number } = {}) =>
    request<EventCalendar>(
      `/api/v1/events/calendar${query({
        start: params.start,
        end: params.end,
        high_score: params.highScore,
      })}`,
    ),
  /**
   * 某日的分页快讯流。**默认类型由调用方给**——服务端不替调用方筛（重要度不跨类型可比，
   * 不筛就是一张公告列表），界面把默认口径写在筛选行上。
   */
  eventFeed: (params: {
    date?: string;
    types?: readonly string[];
    minImportance?: number;
    limit?: number;
    offset?: number;
  } = {}) => {
    const search = new URLSearchParams();
    if (params.date) search.set("date", params.date);
    for (const type of params.types ?? []) search.append("types", type);
    if (params.minImportance !== undefined) search.set("min_importance", String(params.minImportance));
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const text = search.toString();
    return request<EventFeed>(`/api/v1/events/feed${text ? `?${text}` : ""}`);
  },
  /**
   * 一条事件的完整证据链（M10d）。`day` 可省——`event_id` 只在日分区内唯一
   * （平台对同题事件复用 id），省略时服务端取最新那条并在响应里回显 `date`。
   */
  eventDetail: (eventId: string, day?: string | null) =>
    request<EventDetail>(
      `/api/v1/events/${encodeURIComponent(eventId)}${day ? `?day=${day}` : ""}`,
    ),

  // ── 研究首页（M10a）────────────────────────────────────────────────────
  /** 大盘总览：涨跌家数 / 涨跌停 / 成交额 / 等权代理 + 两个「截至」。公开端点。 */
  marketOverview: (date?: string) =>
    request<MarketOverview>(`/api/v1/market/overview${query({ date })}`),
  /**
   * 板块块：PIT 累积归属下的行业 / 概念表现。
   *
   * `minSymbols` 是**样本下限**（服务端下限 3）：小样本的均值会霸榜，界面必须能调它。
   * 查询串的键是后端的 `min_symbols`，故显式映射一次（`query()` 只负责丢空值）。
   */
  marketSectors: (params: { date?: string; minSymbols?: number } = {}) =>
    request<MarketSectors>(
      `/api/v1/market/sectors${query({ date: params.date, min_symbols: params.minSymbols })}`,
    ),

  // ── 模拟盘（M6）────────────────────────────────────────────────────────
  /** 我的会话（摘要，最近创建在前） */
  paperAccounts: (limit = 20) =>
    request<PaperAccountSummary[]>(`/api/v1/paper/accounts?limit=${limit}`),

  /** 会话详情：账户卡 + 持仓 + 决策流水 + 待审批 + 净值曲线 */
  paperAccount: (id: string) =>
    request<PaperAccountDetail>(`/api/v1/paper/accounts/${encodeURIComponent(id)}`),

  createPaperAccount: (body: PaperAccountRequest) =>
    request<PaperAccountDetail>("/api/v1/paper/accounts", jsonInit("POST", body)),

  /** 推进**一个**交易日：成交上一日的批准单 → 结算 → 生成本日决策 */
  stepPaperAccount: (id: string) =>
    request<PaperAccountDetail>(
      `/api/v1/paper/accounts/${encodeURIComponent(id)}/step`,
      { method: "POST" },
      LONG_REQUEST_TIMEOUT_MS,
    ),

  /** 跑到区间末端（不可逆）。`approve` 决定这一路上新生成的决策怎么处理 */
  runPaperAccount: (id: string, approve: "all" | "none") =>
    request<PaperAccountDetail>(
      `/api/v1/paper/accounts/${encodeURIComponent(id)}/run`,
      { ...jsonInit("POST", { approve }) },
      LONG_REQUEST_TIMEOUT_MS,
    ),

  /** 批准 / 驳回一张待审批的决策：**只改状态**，推进后才成交 */
  decidePaper: (decisionId: string, action: "approve" | "reject") =>
    request<PaperDecision>(
      `/api/v1/paper/decisions/${encodeURIComponent(decisionId)}/${action}`,
      { method: "POST" },
    ),

  // ── 绩效研报（M7）──────────────────────────────────────

  /** 生成并冻结一份报告（同 (账户, 快照) **幂等复用**：`reused=true` 时没新建、也没再花钱） */
  createReport: (accountId: string) =>
    request<ReportEnvelope>(
      "/api/v1/reports",
      jsonInit("POST", { account_id: accountId }),
      LONG_REQUEST_TIMEOUT_MS,
    ),

  /** 某账户已出的报告摘要（`/paper` 的入口状态用） */
  reports: (accountId: string) =>
    request<{ reports: ReportSummary[] }>(
      `/api/v1/reports?account_id=${encodeURIComponent(accountId)}`,
    ),

  report: (id: string) => request<ReportEnvelope>(`/api/v1/reports/${encodeURIComponent(id)}`),

  // ── 深度研究（M8）────────────────────────────────────────────────────
  /** 我的深度研究运行列表（摘要，不带事件） */
  researchRuns: () => request<{ runs: ResearchRunSummary[] }>("/api/v1/research/runs"),

  /** 一次运行的详情：**事件回放的真源**（断连重连与事后回看都走它） */
  researchRun: (id: string) =>
    request<ResearchRunDetail>(`/api/v1/research/runs/${encodeURIComponent(id)}`),

  /** 生成 / 取回分享链接（幂等：已分享过返回同一个 token） */
  shareReport: (id: string) =>
    request<{ id: string; share_token: string; share_path: string; shared_at: string | null }>(
      `/api/v1/reports/${encodeURIComponent(id)}/share`,
      { method: "POST" },
    ),

  /** 撤销分享：旧链接随即失效（公开端点 404） */
  unshareReport: (id: string) =>
    request<{ id: string; share_token: null; share_path: null }>(
      `/api/v1/reports/${encodeURIComponent(id)}/share`,
      { method: "DELETE" },
    ),

  /** 打印页取数：凭一次性导出令牌读冻结产物（渲染器没有会话 cookie，走的就是这条路） */
  reportForPrint: (id: string, token: string) =>
    request<PublicReport>(
      `/api/v1/reports/${encodeURIComponent(id)}/print?t=${encodeURIComponent(token)}`,
    ),

  /** **匿名只读**：凭分享 token 取冻结产物（未登录的浏览器走的就是这条路） */
  publicReport: (token: string) =>
    request<PublicReport>(`/api/v1/public/reports/${encodeURIComponent(token)}`),
};

/**
 * 发起一次网格 / 批量（SSE 逐格推送）。
 *
 * 独立于 `api.*`：那个对象是「一进一出」的 request/response，而这里是一串帧。
 * 帧解析复用 `lib/sse.ts` 的既有纯函数，**不新写一套**（SPEC §12）。
 */
/**
 * **提交**一次网格/批量（P3-E1b）：202 + `{task_id}`，执行在 worker 里。
 * 进度由 `streamTask(taskId, ...)` 订阅——提交与订阅分开，断线不影响执行。
 */
export function submitOptimize(
  kind: "grid" | "batch",
  body: unknown,
  key?: string,
): Promise<{ task_id: string; status: string }> {
  return request(`/api/v1/optimize/${kind}`, jsonInit("POST", body, key));
}

/** 订阅任务事件流（GET + SSE）。帧与发起时逐字同形，重连与在线共用这一条。 */
export function streamTask(taskId: string, options: PostStreamOptions): Promise<void> {
  return streamGet(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`, options);
}
