/**
 * 对话状态机：SSE 帧 → 消息列表。
 *
 * 做成纯 reducer 而不是散在组件里的 setState，是因为流式渲染最容易出错的地方
 * 全是「时序」——迟到的 token、重复的 done、abort 落在 done 之后、工具结果先于
 * 调用到达。这些在浏览器里极难复现，在单测里只是几行输入输出。
 *
 * 一条必须守住的不变式：**未触碰的消息对象保持同一引用**。`React.memo` 靠它
 * 跳过已完成消息的重渲染，否则每个 token 都要重渲整条会话。
 */

import type { SseFrame } from "./sse";
import type { ThreadMessage, ToolStep } from "./types";

export type MessageStatus = "streaming" | "done" | "stopped" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools: ToolStep[];
  status: MessageStatus;
  /** SSE `error` 帧或连接中断的说明：在消息流内以一行错误条呈现 */
  error?: string;
}

export interface ChatState {
  messages: ChatMessage[];
  streamingId: string | null;
  /** 历史加载中：主区显示与最终布局同形的骨架屏 */
  loading: boolean;
  /** 当前轮的 HTTP / 网络错误：显示在输入区上方 */
  transportError: string | null;
  /** 历史加载失败：显示在主区（消息本来该出现的位置） */
  loadError: string | null;
  /** 消息 id 序号：放在 state 里，reducer 才是纯函数、测试才好写断言 */
  seq: number;
  /**
   * 服务端此刻**正在为这个会话跑图**（刷新断流后回答还在路上）。
   *
   * 由 `GET .../messages` 的 `running` 字段带来：前端据此显示「回答中…」并轮询，
   * 跑完自动出现——不必再手动刷一次。也用来压掉「重新生成」（那一轮没死，只是还在跑）。
   */
  running: boolean;
}

export type ChatAction =
  | { type: "sent"; text: string }
  | { type: "frame"; frame: SseFrame }
  | { type: "stopped" }
  | { type: "ended" }
  | { type: "failed"; message: string }
  | { type: "loading" }
  | { type: "loaded"; messages: ThreadMessage[]; running?: boolean }
  | { type: "load_failed"; message: string }
  | { type: "reset" };

export const initialChatState: ChatState = {
  messages: [],
  streamingId: null,
  loading: false,
  transportError: null,
  loadError: null,
  seq: 0,
  running: false,
};

export function messageId(seq: number): string {
  return `m${seq}`;
}

export function historyToMessages(
  rows: ThreadMessage[],
  startSeq: number,
): { messages: ChatMessage[]; seq: number } {
  let seq = startSeq;
  const messages = rows.map((row) => ({
    id: messageId(seq++),
    role: row.role,
    content: row.content,
    tools: row.tools,
    status: "done" as const,
  }));
  return { messages, seq };
}

function parseData(raw: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(raw);
    return typeof value === "object" && value !== null
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    // 服务端不会发坏 JSON；真收到就丢这一帧，不把整个会话带崩
    return null;
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 只改动正在流式的那条消息，其余对象原样保留引用。 */
function patchStreaming(
  state: ChatState,
  patch: (message: ChatMessage) => ChatMessage,
): ChatState {
  const target = state.streamingId;
  if (target === null) return state;
  return {
    ...state,
    messages: state.messages.map((message) =>
      message.id === target ? patch(message) : message,
    ),
  };
}

/** 收尾：置状态并清掉流式标记。没有在流式的消息时是 no-op（abort 落在 done 之后就是这条路径）。 */
function finish(
  state: ChatState,
  status: MessageStatus,
  patch: (message: ChatMessage) => Partial<ChatMessage> = () => ({}),
): ChatState {
  const target = state.streamingId;
  if (target === null) return state;
  return {
    ...state,
    streamingId: null,
    messages: state.messages.map((message) =>
      message.id === target ? { ...message, ...patch(message), status } : message,
    ),
  };
}

function applyFrame(state: ChatState, frame: SseFrame): ChatState {
  const payload = parseData(frame.data);
  if (payload === null) return state;

  switch (frame.event) {
    case "token":
      return patchStreaming(state, (message) => ({
        ...message,
        content: message.content + text(payload.text),
      }));

    case "tool_call":
      return patchStreaming(state, (message) => ({
        ...message,
        tools: [
          ...message.tools,
          {
            id: text(payload.id),
            name: text(payload.name) || "unknown",
            args: payload.args ?? {},
            content: null,
            is_error: false,
          },
        ],
      }));

    case "tool_result":
      return patchStreaming(state, (message) => {
        const id = text(payload.id);
        const index = message.tools.findIndex((step) => step.id === id);
        // 找不到对应的调用就补一步：宁可多显示一步，也不让回答引用了看不见的数据
        if (index === -1) {
          return {
            ...message,
            tools: [
              ...message.tools,
              {
                id,
                name: text(payload.name) || "unknown",
                args: {},
                content: text(payload.content),
                is_error: payload.is_error === true,
              },
            ],
          };
        }
        return {
          ...message,
          tools: message.tools.map((step, position) =>
            position === index
              ? {
                  ...step,
                  content: text(payload.content),
                  is_error: payload.is_error === true,
                }
              : step,
          ),
        };
      });

    case "done":
      // 用 done 的完整正文自愈：中途丢过 token 也能回到正确文本
      return finish(state, "done", (message) => ({
        content: text(payload.content) || message.content,
      }));

    case "error": {
      const detail = text(payload.message) || "内部错误";
      if (state.streamingId === null) {
        return { ...state, transportError: detail };
      }
      // 不断连：错误挂在消息上，用户可以接着问
      return finish(state, "error", () => ({ error: detail }));
    }

    default:
      // 后端将来加事件类型时，旧前端不该炸
      return state;
  }
}

/**
 * 上一次提问是否**没拿到回答**（对话页据此给「重新生成」）。
 *
 * 判据是「列表停在一条用户消息上」：失败时若一个 token 都没吐，占位气泡会被撤掉
 * （见 `failed` 分支），刷新后重新拉历史也是同一种形状（服务端那轮同样没落答案）。
 * 正在流式输出、或末尾是助手消息（哪怕内容不完整）都不给——那两种情况用户直接接着说话即可。
 */
export function canRetry(state: ChatState): boolean {
  const last = state.messages[state.messages.length - 1];
  // `running` 也算「还没死」：刷新断流后那一轮仍在服务端跑，此时给「重新生成」会误导，
  // 而且重发会排在它后面（同会话运行是串行的）
  return state.streamingId === null && !state.running && last?.role === "user";
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "sent": {
      const user: ChatMessage = {
        id: messageId(state.seq),
        role: "user",
        content: action.text,
        tools: [],
        status: "done",
      };
      const assistant: ChatMessage = {
        id: messageId(state.seq + 1),
        role: "assistant",
        content: "",
        tools: [],
        status: "streaming",
      };
      return {
        ...state,
        messages: [...state.messages, user, assistant],
        streamingId: assistant.id,
        transportError: null,
        seq: state.seq + 2,
      };
    }

    case "frame":
      return applyFrame(state, action.frame);

    case "stopped":
      return finish(state, "stopped");

    case "ended":
      // 流正常关闭却没等到 done：连接被中间层掐了，如实说明而不是假装正常结束
      return finish(state, "stopped", () => ({ error: "连接提前结束，回答可能不完整" }));

    case "failed": {
      const target = state.streamingId;
      const blank =
        target !== null &&
        state.messages.some(
          (message) =>
            message.id === target && message.content === "" && message.tools.length === 0,
        );
      // 一个 token 都没吐就失败（如 503），占位气泡撤掉，错误交给输入区上方的提示，
      // 免得留一个空气泡；已经吐了内容则保留并标注
      const messages = blank
        ? state.messages.filter((message) => message.id !== target)
        : state.messages.map((message) =>
            message.id === target
              ? { ...message, status: "error" as const, error: action.message }
              : message,
          );
      return { ...state, messages, streamingId: null, transportError: action.message };
    }

    case "loading":
      return { ...state, messages: [], loading: true, loadError: null, streamingId: null };

    case "loaded": {
      const { messages, seq } = historyToMessages(action.messages, state.seq);
      return {
        ...state,
        messages,
        seq,
        loading: false,
        loadError: null,
        running: action.running ?? false,
      };
    }

    case "load_failed":
      return { ...state, messages: [], loading: false, loadError: action.message };

    case "reset":
      return { ...initialChatState };
  }
}
