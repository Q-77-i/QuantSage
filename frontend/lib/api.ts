/**
 * 后端 HTTP 客户端。
 *
 * 基址走 `NEXT_PUBLIC_API_BASE`（见 `.env.local`），默认指向本地 8000。
 * 前端跑 3001 而不是 3000：3000 被 Langfuse 自托管 UI 占用。
 */

import type {
  BacktestReport,
  BacktestRequest,
  BarsResponse,
  EventsResponse,
  ThreadSummary,
} from "./types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 把后端的错误体翻成一句人话：FastAPI 的 422 是数组，直接 toString 只会显示 [object Object]。 */
async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    const detail = (body as { detail?: unknown })?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (item as { msg?: string })?.msg)
        .filter((msg): msg is string => Boolean(msg));
      if (messages.length) return messages.join("；");
    }
  } catch {
    // 非 JSON 响应（网关错误页一类）走下面的兜底
  }
  return `请求失败（HTTP ${response.status}）`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  bars: (symbol: string, params: { start?: string; end?: string; adjust?: string } = {}) =>
    request<BarsResponse>(`/api/v1/market/${symbol}/bars${query(params)}`),

  events: (symbol: string, params: { start?: string; end?: string } = {}) =>
    request<EventsResponse>(`/api/v1/events${query({ symbol, ...params })}`),

  backtest: (body: BacktestRequest) =>
    request<BacktestReport>("/api/v1/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  threads: () => request<ThreadSummary[]>("/api/v1/chat/threads"),
};
