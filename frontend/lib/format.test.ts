import { describe, expect, it } from "vitest";

import { EMPTY, amount, count, dayStamp, eventStamp, num, pct, pp, shortHash } from "./format";

describe("pct —— 输入是比例（0.124 = 12.4%）", () => {
  it("比例转百分数", () => {
    expect(pct(0.124)).toBe("12.4%");
    expect(pct(-0.00267)).toBe("-0.27%");
  });

  it("带号与否由调用方指定，不按数值正负猜", () => {
    // 最大回撤后端返回正值幅度，带号就会显示成「+8.1%」这种错话
    expect(pct(0.067)).toBe("6.7%");
    expect(pct(0.067, { signed: true })).toBe("+6.7%");
    expect(pct(-0.00683, { signed: true })).toBe("-0.68%");
  });

  it("零值即使要求带号也不加号", () => {
    expect(pct(0, { signed: true })).toBe("0%");
  });

  it("null 显示占位符，不是 0 —— 夏普/胜率在样本退化时会是 null", () => {
    expect(pct(null)).toBe(EMPTY);
    expect(pct(null, { signed: true })).toBe(EMPTY);
  });
});

describe("pp —— 输入已是百分点（后端 *_pp 字段）", () => {
  it("原样加 pp 后缀，不再乘 100", () => {
    expect(pp(-0.681, { signed: true })).toBe("-0.68pp");
    expect(pp(0)).toBe("0pp");
  });

  it("同一个数走 pct 与 pp 得到相差 100 倍的结果 —— 这正是要分开两条路径的原因", () => {
    expect(pct(0.124)).toBe("12.4%");
    expect(pp(0.124)).toBe("0.12pp");
  });
});

describe("amount / num / count", () => {
  it("金额加千分位、不带小数", () => {
    expect(amount(997329.155)).toBe("997,329");
    expect(amount(31665.6, { signed: true })).toBe("+31,666");
    expect(amount(-6810.88, { signed: true })).toBe("-6,811");
  });

  it("夏普保留两位并带号", () => {
    expect(num(-0.00848, { signed: true })).toBe("-0.01");
    expect(num(0.4213, { signed: true })).toBe("+0.42");
  });

  it("计数是整数", () => {
    expect(count(4)).toBe("4");
    expect(count(null)).toBe(EMPTY);
  });
});

describe("eventStamp", () => {
  it("按北京时间显示到分钟", () => {
    expect(eventStamp("2026-07-15T10:12:58+08:00")).toBe("07-15 10:12");
  });

  it("微秒精度的可用时间可解析（后端 available_at 带 6 位小数）", () => {
    expect(eventStamp("2026-07-15T10:25:37.564112+08:00")).toBe("07-15 10:25");
  });

  it("偏移不是 +08:00 时也换算到北京时间 —— 不能依赖后端给什么偏移", () => {
    // 同一时刻的 UTC 写法，必须得到同一个钟点
    expect(eventStamp("2026-07-15T02:12:58Z")).toBe("07-15 10:12");
  });

  it("null 给占位符，认不出的串原样显示（不吞线索）", () => {
    expect(eventStamp(null)).toBe(EMPTY);
    expect(eventStamp("不是时间")).toBe("不是时间");
  });
});

describe("dayStamp —— 比 eventStamp 多一个年份", () => {
  it("补出年份（会话与回测记录是跨月摆放的，少了年份就得猜）", () => {
    expect(dayStamp("2026-07-15T10:12:58+08:00")).toBe("2026-07-15 10:12");
  });

  it("与 eventStamp 同一套时区口径：UTC 写法也换算成北京时间", () => {
    expect(dayStamp("2026-07-15T02:12:58Z")).toBe("2026-07-15 10:12");
  });

  it("null 给占位符，认不出的串原样显示", () => {
    expect(dayStamp(null)).toBe(EMPTY);
    expect(dayStamp("不是时间")).toBe("不是时间");
  });
});

describe("shortHash", () => {
  it("截到指定位数，短的不补", () => {
    expect(shortHash("5a0fa7182779dbd16583")).toBe("5a0fa7182779");
    expect(shortHash("abc")).toBe("abc");
    expect(shortHash(null)).toBe(EMPTY);
  });
});
