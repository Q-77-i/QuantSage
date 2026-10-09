/**
 * SSE 客户端：POST + ReadableStream 手解帧。
 *
 * 不能用 `EventSource`：它只支持 GET，而对话端点是 POST。
 *
 * 两条必须守住的线（SPEC §6/§7）：
 *   ① **一个网络分片 ≠ 一个事件**——必须缓冲到 `\n\n` 才切帧，否则半个 JSON 会被当帧解析；
 *   ② `:` 开头的是注释帧（服务端每 15s 发的保活），要忽略而不是当事件。
 */

import { ApiError, apiBase, errorMessage } from "./api";

export interface SseFrame {
  event: string;
  data: string;
}

/**
 * 从缓冲区里切出完整帧，返回剩余的半截。
 *
 * 纯函数：帧解析是最容易出错、又最难在浏览器里复现的一环，单测比手点页面可靠。
 */
export function parseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  // 接受 \r\n：中间层（代理、部分服务端）会改写行尾
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";

  const frames: SseFrame[] = [];
  for (const block of blocks) {
    const frame = parseFrame(block);
    if (frame) frames.push(frame);
  }
  return { frames, rest };
}

function parseFrame(block: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];

  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue; // 保活注释帧
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const raw = colon === -1 ? "" : line.slice(colon + 1);
    const value = raw.startsWith(" ") ? raw.slice(1) : raw;

    if (field === "event") event = value;
    else if (field === "data") data.push(value); // 多行 data 按规范用 \n 拼接
  }

  if (!data.length) return null; // 没有 data 的帧没有意义
  return { event, data: data.join("\n") };
}

export interface PostStreamOptions {
  signal?: AbortSignal;
  onFrame: (frame: SseFrame) => void;
}

/**
 * 通用的 POST + SSE 逐帧读取（M5b）。
 *
 * 与 `streamChat` 是**同一套帧解析**（都用上面的 `parseFrames`，SPEC §12 要求
 * 「不新写一套」），差别只在请求体与响应头回调。没有把 `streamChat` 改成它的薄壳——
 * 那是已经上线且被 T6b/T3 的用例钉住的路径，M5b 没必要碰它。
 */
export async function streamPost(
  path: string,
  body: unknown,
  options: PostStreamOptions,
): Promise<void> {
  const response = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
    credentials: "include",
  });

  if (!response.ok) {
    // 后端在**开流之前**判死的请求级错误都走这条路（422 / 404），detail 才是可行动的
    throw new ApiError(response.status, await errorMessage(response));
  }
  if (!response.body) throw new ApiError(response.status, "响应没有流式主体");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = parseFrames(buffer);
      buffer = rest;
      for (const frame of frames) options.onFrame(frame);
    }
  } finally {
    reader.releaseLock();
  }
}

export interface ChatStreamOptions {
  message: string;
  threadId?: string | null;
  signal?: AbortSignal;
  onFrame: (frame: SseFrame) => void;
  /** 断连时 `done` 帧拿不到，会话号靠响应头兜底 */
  onThreadId?: (threadId: string) => void;
}

/** 发起一次对话并逐帧回调，直到流结束。 */
export async function streamChat(options: ChatStreamOptions): Promise<void> {
  const response = await fetch(`${apiBase()}/api/v1/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: options.message, thread_id: options.threadId ?? null }),
    signal: options.signal,
    // 会话 cookie（M1）：与 lib/api.ts 同因——httpOnly，只能由浏览器自动带上
    credentials: "include",
  });

  if (!response.ok) {
    // 后端的 detail 才是可行动的（如「Agent 未就绪」），裸状态码只会让用户干瞪眼
    throw new ApiError(response.status, await errorMessage(response));
  }
  if (!response.body) throw new ApiError(response.status, "响应没有流式主体");

  const headerThreadId = response.headers.get("X-Thread-Id");
  if (headerThreadId) options.onThreadId?.(headerThreadId);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = parseFrames(buffer);
      buffer = rest;
      for (const frame of frames) options.onFrame(frame);
    }
  } finally {
    reader.releaseLock();
  }
}
