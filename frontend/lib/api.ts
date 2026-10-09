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

import type {
  BacktestRequest,
  BacktestResponse,
  BacktestRunDetail,
  BacktestRunSummary,
  BarsResponse,
  DataFreshness,
  EventsResponse,
  StrategyCheck,
  StrategyDetail,
  StrategySaved,
  StrategySummary,
  StrategyTemplate,
  StrategyWriteBody,
  SymbolProbe,
  SymbolSearchResponse,
  ThreadMessagesResponse,
  ThreadSummary,
  User,
  WatchlistItem,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // credentials 必须带：会话是 httpOnly cookie，JS 读不到也放不进去，只能让浏览器带上。
  // 前后端同 site（127.0.0.1 的 3001 ↔ 8000），SameSite=Lax 不影响这条请求。
  const response = await fetch(`${apiBase()}${path}`, { credentials: "include", ...init });
  if (!response.ok) {
    // 体只读一次：文案与 payload 从同一份解析结果出，别两次 json()（流只能读一次）
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // 非 JSON（网关错误页一类）：留给兜底文案
    }
    throw new ApiError(response.status, messageFromBody(body, response.status), body);
  }
  return (await response.json()) as T;
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  // ── 认证（M1）─────────────────────────────────────────────────────────
  me: () => request<User>("/api/v1/auth/me"),

  /** `remember` 只改 cookie 存活方式：true → 持久（关浏览器仍在），false → 会话 cookie */
  login: (email: string, password: string, remember = true) =>
    request<User>("/api/v1/auth/login", jsonInit("POST", { email, password, remember })),

  register: (email: string, password: string) =>
    request<User>("/api/v1/auth/register", jsonInit("POST", { email, password })),

  logout: () => request<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" }),

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

  events: (symbol: string, params: { start?: string; end?: string } = {}) =>
    request<EventsResponse>(`/api/v1/events${query({ symbol, ...params })}`),

  /** M1c 起返回信封 `{run_id, report}`，且需要登录（跑完会落库） */
  backtest: (body: BacktestRequest) =>
    request<BacktestResponse>("/api/v1/backtest", jsonInit("POST", body)),

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
};
