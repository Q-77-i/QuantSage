import { describe, expect, it } from "vitest";

import {
  type ChatState,
  canRetry,
  chatReducer,
  historyToMessages,
  initialChatState,
} from "./chat-state";
import type { SseFrame } from "./sse";

function frame(event: string, data: unknown): SseFrame {
  return { event, data: JSON.stringify(data) };
}

/** 一问，助手占位已在流式中 */
function streaming(): ChatState {
  return chatReducer(initialChatState, { type: "sent", text: "问" });
}

describe("chatReducer", () => {
  it("发送后追加用户消息与助手占位并进入流式", () => {
    const state = streaming();
    expect(state.messages.map((message) => message.role)).toEqual(["user", "assistant"]);
    expect(state.messages[0].content).toBe("问");
    expect(state.messages[1].status).toBe("streaming");
    expect(state.streamingId).toBe(state.messages[1].id);
  });

  it("token 逐帧累加，done 收尾", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "你" }) });
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "好" }) });
    expect(state.messages[1].content).toBe("你好");

    state = chatReducer(state, {
      type: "frame",
      frame: frame("done", { thread_id: "t", content: "你好", usage: null }),
    });
    expect(state.messages[1].status).toBe("done");
    expect(state.streamingId).toBeNull();
  });

  it("done 的完整正文能自愈丢过的 token", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "半" }) });
    state = chatReducer(state, {
      type: "frame",
      frame: frame("done", { thread_id: "t", content: "完整回答" }),
    });
    expect(state.messages[1].content).toBe("完整回答");
  });

  it("工具调用与结果按 id 配对，参数留在步骤上", () => {
    let state = streaming();
    state = chatReducer(state, {
      type: "frame",
      frame: frame("tool_call", { id: "r1", name: "query_market_bars", args: { symbol: "600519" } }),
    });
    expect(state.messages[1].tools[0].content).toBeNull();

    state = chatReducer(state, {
      type: "frame",
      frame: frame("tool_result", { id: "r1", name: "query_market_bars", content: "收盘 1500", is_error: false }),
    });
    expect(state.messages[1].tools).toHaveLength(1);
    expect(state.messages[1].tools[0].content).toBe("收盘 1500");
    expect(state.messages[1].tools[0].args).toEqual({ symbol: "600519" });
  });

  it("结果先于调用到达时补一步而不是丢弃", () => {
    let state = streaming();
    state = chatReducer(state, {
      type: "frame",
      frame: frame("tool_result", { id: "ghost", name: "x", content: "数据", is_error: true }),
    });
    expect(state.messages[1].tools).toHaveLength(1);
    expect(state.messages[1].tools[0].is_error).toBe(true);
  });

  it("error 帧挂在消息上，不断连，也不污染输入区的提示", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "半句" }) });
    state = chatReducer(state, {
      type: "frame",
      frame: frame("error", { code: "internal", message: "内部错误，请重试" }),
    });

    expect(state.messages[1].status).toBe("error");
    expect(state.messages[1].error).toBe("内部错误，请重试");
    expect(state.messages[1].content).toBe("半句");
    expect(state.streamingId).toBeNull();
    expect(state.transportError).toBeNull();
  });

  it("一个 token 都没吐就失败时撤掉空占位，错误走输入区上方", () => {
    const state = chatReducer(streaming(), { type: "failed", message: "Agent 未就绪" });
    expect(state.transportError).toBe("Agent 未就绪");
    expect(state.messages.map((message) => message.role)).toEqual(["user"]);
    expect(state.streamingId).toBeNull();
  });

  it("已吐出内容后再失败则保留内容并标注", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "半句" }) });
    state = chatReducer(state, { type: "failed", message: "网络中断" });

    expect(state.messages[1].status).toBe("error");
    expect(state.messages[1].content).toBe("半句");
    expect(state.transportError).toBe("网络中断");
  });

  it("流式中停止保留已生成内容", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "写到一半" }) });
    state = chatReducer(state, { type: "stopped" });

    expect(state.messages[1].status).toBe("stopped");
    expect(state.messages[1].content).toBe("写到一半");
    expect(state.streamingId).toBeNull();
  });

  it("空闲时 stopped 与 ended 都是 no-op（abort 落在 done 之后）", () => {
    let state = streaming();
    state = chatReducer(state, {
      type: "frame",
      frame: frame("done", { thread_id: "t", content: "完" }),
    });
    expect(chatReducer(state, { type: "stopped" })).toBe(state);
    expect(chatReducer(state, { type: "ended" })).toBe(state);
  });

  it("流关闭却没等到 done 时如实标注中断", () => {
    let state = streaming();
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "半" }) });
    state = chatReducer(state, { type: "ended" });

    expect(state.messages[1].status).toBe("stopped");
    expect(state.messages[1].error).toContain("连接提前结束");
  });

  it("坏帧被丢弃：非法 JSON、未知事件名、没有流式目标", () => {
    const state = streaming();
    expect(chatReducer(state, { type: "frame", frame: { event: "token", data: "{半截" } })).toBe(state);
    expect(chatReducer(state, { type: "frame", frame: frame("future_event", { x: 1 }) })).toBe(state);
    expect(chatReducer(initialChatState, { type: "frame", frame: frame("token", { text: "野" }) })).toBe(
      initialChatState,
    );
  });

  it("只有正在流式的那条被改动，其余保持同一引用", () => {
    let state = chatReducer(initialChatState, { type: "sent", text: "第一问" });
    state = chatReducer(state, {
      type: "frame",
      frame: frame("done", { thread_id: "t", content: "第一答" }),
    });
    const [firstUser, firstAssistant] = state.messages;

    state = chatReducer(state, { type: "sent", text: "第二问" });
    state = chatReducer(state, { type: "frame", frame: frame("token", { text: "第二" }) });

    expect(state.messages[0]).toBe(firstUser);
    expect(state.messages[1]).toBe(firstAssistant);
  });

  it("历史加载走加载中、成功、失败三态", () => {
    let state = chatReducer(streaming(), { type: "loading" });
    expect(state.loading).toBe(true);
    expect(state.messages).toEqual([]);

    state = chatReducer(state, {
      type: "loaded",
      messages: [
        { role: "user", content: "旧问", tools: [] },
        {
          role: "assistant",
          content: "旧答",
          tools: [{ id: "c1", name: "echo", args: {}, content: "结果", is_error: false }],
        },
      ],
    });
    expect(state.loading).toBe(false);
    expect(state.messages.map((message) => message.status)).toEqual(["done", "done"]);
    expect(state.messages[1].tools[0].content).toBe("结果");

    state = chatReducer(state, { type: "load_failed", message: "会话不存在" });
    expect(state.loadError).toBe("会话不存在");
    expect(state.messages).toEqual([]);
  });

  it("reset 回到空态", () => {
    let state = chatReducer(streaming(), { type: "reset" });
    expect(state).toEqual(initialChatState);
    state = chatReducer(state, { type: "sent", text: "新会话" });
    expect(state.messages[0].id).toBe("m0");
  });
});

describe("historyToMessages", () => {
  it("接续当前序号，不与实时消息撞 id", () => {
    const { messages, seq } = historyToMessages(
      [{ role: "user", content: "x", tools: [] }],
      7,
    );
    expect(messages[0].id).toBe("m7");
    expect(seq).toBe(8);
  });
});

describe("canRetry —— 上一轮没拿到回答时才给「重新生成」", () => {
  const base = { ...initialChatState };

  it("停在用户消息上 → 可重试", () => {
    const state = chatReducer(base, { type: "sent", text: "茅台行情？" });
    const failed = chatReducer(state, { type: "failed", message: "内部错误，请重试" });
    // 一个 token 都没吐：占位气泡被撤掉，列表停在用户消息上
    expect(canRetry(failed)).toBe(true);
  });

  it("正在流式输出 → 不给（此时该用「停止」）", () => {
    expect(canRetry(chatReducer(base, { type: "sent", text: "x" }))).toBe(false);
  });

  it("末尾是助手消息 → 不给（哪怕内容不完整，接着说话即可）", () => {
    let state = chatReducer(base, { type: "sent", text: "x" });
    state = chatReducer(state, { type: "frame", frame: { event: "token", data: { text: "片段" } } });
    state = chatReducer(state, { type: "stopped" });
    expect(canRetry(state)).toBe(false);
  });

  it("空会话 → 不给", () => {
    expect(canRetry(base)).toBe(false);
  });
});
