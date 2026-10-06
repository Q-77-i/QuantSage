import { describe, expect, it } from "vitest";

import { parseFrames } from "./sse";

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
