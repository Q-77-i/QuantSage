import { describe, expect, it } from "vitest";

import { SubmissionKey, newIdempotencyKey } from "./idempotency";

describe("newIdempotencyKey", () => {
  it("每次给一个新的 UUID", () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();

    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("SubmissionKey", () => {
  it("同一个动作（还在跑）复用同一个键", () => {
    const key = new SubmissionKey();

    const first = key.current(false);
    const retry = key.current(true);

    expect(retry).toBe(first);
  });

  it("落定之后再提交是新动作，换键", () => {
    const key = new SubmissionKey();

    const first = key.current(false);
    const second = key.current(false);

    expect(second).not.toBe(first);
  });

  it("从没取过键时，即使说「在跑」也先生成一个", () => {
    const key = new SubmissionKey();

    expect(key.current(true)).toMatch(/^[0-9a-f-]{36}$/);
    expect(key.current(true)).toBe(key.current(true));
  });

  it("两个实例各管各的动作", () => {
    const a = new SubmissionKey();
    const b = new SubmissionKey();

    expect(a.current(false)).not.toBe(b.current(false));
  });
});
