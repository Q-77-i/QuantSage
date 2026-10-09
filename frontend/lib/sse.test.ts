import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { parseFrames, streamPost } from "./sse";

describe("parseFrames", () => {
  it("一个分片里切出多帧", () => {
    const chunk = 'event: token\ndata: {"text":"你"}\n\nevent: token\ndata: {"text":"好"}\n\n';
    const { frames, rest } = parseFrames(chunk);

    expect(frames.map((frame) => JSON.parse(frame.data).text)).toEqual(["你", "好"]);
    expect(rest).toBe("");
  });

  it("半截帧留在 rest —— 一个网络分片不等于一个事件", () => {
    const { frames, rest } = parseFrames('event: token\ndata: {"text":"你"}\n\nevent: tok');

    expect(frames).toHaveLength(1);
    expect(rest).toBe("event: tok");
  });

  it("分两次喂入也能拼回完整帧", () => {
    const first = parseFrames('event: tool_call\ndata: {"id":"run-1","na');
    expect(first.frames).toHaveLength(0);

    const second = parseFrames(`${first.rest}me":"query_events"}\n\n`);
    expect(second.frames[0].event).toBe("tool_call");
    expect(JSON.parse(second.frames[0].data).name).toBe("query_events");
  });

  it("忽略保活注释帧", () => {
    const { frames, rest } = parseFrames(": keepalive\n\n");

    expect(frames).toHaveLength(0);
    expect(rest).toBe("");
  });

  it("没有 data 的帧不产出", () => {
    expect(parseFrames("event: token\n\n").frames).toHaveLength(0);
  });

  it("多行 data 按规范用换行拼接", () => {
    const { frames } = parseFrames("event: token\ndata: 第一行\ndata: 第二行\n\n");
    expect(frames[0].data).toBe("第一行\n第二行");
  });

  it("接受 CRLF 行尾（中间层会改写行尾）", () => {
    const { frames } = parseFrames('event: done\r\ndata: {"thread_id":"t1"}\r\n\r\n');

    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0].data).thread_id).toBe("t1");
  });

  it("字段名后无空格也能解析", () => {
    const { frames } = parseFrames('event:token\ndata:{"text":"x"}\n\n');
    expect(frames[0]).toEqual({ event: "token", data: '{"text":"x"}' });
  });

  it("带空格的值只吃掉一个前导空格", () => {
    const { frames } = parseFrames("event: token\ndata:  两个空格\n\n");
    expect(frames[0].data).toBe(" 两个空格");
  });
});

describe("streamPost（M5b：网格 / 批量的通用 POST 流）", () => {
  // 用例跑在 node 环境（没有 `window`），而 `apiBase()` 默认跟随页面 host——
  // 显式给一个基址走它自己的覆盖分支（真实浏览器里走的是 location 那条）
  const originalBase = process.env.NEXT_PUBLIC_API_BASE;
  beforeEach(() => {
    process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
  });
  afterEach(() => {
    if (originalBase === undefined) delete process.env.NEXT_PUBLIC_API_BASE;
    else process.env.NEXT_PUBLIC_API_BASE = originalBase;
  });

  /** 造一个可以按分片吐正文的响应 */
  function stubFetch(chunks: string[], init: { ok?: boolean; status?: number } = {}) {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    const calls: { url: string; body: unknown }[] = [];
    globalThis.fetch = (async (url: string, options: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(options.body)) });
      return {
        ok: init.ok ?? true,
        status: init.status ?? 200,
        body,
        headers: new Headers(),
      } as unknown as Response;
    }) as typeof fetch;
    return calls;
  }

  const original = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = original;
  });

  it("跨分片切帧并把事件按到达顺序回调", async () => {
    const calls = stubFetch([
      'event: start\ndata: {"total":2}\n\nevent: ce',
      'll\ndata: {"index":0}\n\nevent: cell\ndata: {"index":1}\n\nevent: done\ndata: {"run_id":"x"}\n\n',
    ]);
    const seen: string[] = [];
    await streamPost("/api/v1/optimize/grid", { symbol: "600519" }, {
      onFrame: (frame) => seen.push(frame.event),
    });

    expect(seen).toEqual(["start", "cell", "cell", "done"]);
    expect(calls[0].url).toContain("/api/v1/optimize/grid");
    expect(calls[0].body).toEqual({ symbol: "600519" });
  });

  it("请求级 422 抛 ApiError，不把它当成一帧", async () => {
    stubFetch(['{"detail":"第 3 格参数不合法"}'], { ok: false, status: 422 });
    await expect(
      streamPost("/api/v1/optimize/grid", {}, { onFrame: () => undefined }),
    ).rejects.toMatchObject({ status: 422 });
  });

  it("保活注释帧不进回调", async () => {
    stubFetch([": keepalive\n\nevent: done\ndata: {}\n\n"]);
    const seen: string[] = [];
    await streamPost("/api/v1/optimize/grid", {}, { onFrame: (frame) => seen.push(frame.event) });
    expect(seen).toEqual(["done"]);
  });
});
