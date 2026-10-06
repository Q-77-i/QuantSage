import { describe, expect, it } from "vitest";

import {
  credentials,
  hasErrors,
  passwordBytes,
  safeNextPath,
  validate,
} from "./auth-form";

const good = { email: "Alice@Example.com", password: "s3cret-passphrase", confirm: "" };

describe("passwordBytes", () => {
  it("按 UTF-8 字节算，不按字符数", () => {
    expect(passwordBytes("abcd")).toBe(4);
    expect(passwordBytes("密码")).toBe(6); // 两个汉字 = 6 字节
  });
});

describe("validate", () => {
  it("合法输入无错误", () => {
    expect(hasErrors(validate(good))).toBe(false);
  });

  it("邮箱格式不对时报错", () => {
    expect(validate({ ...good, email: "not-an-email" }).email).toMatch(/格式/);
    expect(validate({ ...good, email: "  " }).email).toMatch(/请填写/);
  });

  it("密码按字节判上下限", () => {
    expect(validate({ ...good, password: "short" }).password).toMatch(/至少/);
    // 24 个汉字 = 72 字节，压线通过；25 个就越界——按字符数判会漏掉这个边界
    expect(hasErrors(validate({ ...good, password: "密".repeat(24) }))).toBe(false);
    expect(validate({ ...good, password: "密".repeat(25) }).password).toMatch(/不得超/);
  });

  it("只有注册页校验确认框", () => {
    const mismatch = { ...good, confirm: "别的密码" };
    expect(hasErrors(validate(mismatch, false))).toBe(false);
    expect(validate(mismatch, true).confirm).toMatch(/不一致/);
  });
});

describe("credentials", () => {
  it("邮箱去空白并转小写后再交后端", () => {
    expect(credentials({ ...good, email: "  Alice@Example.COM " })).toEqual({
      email: "alice@example.com",
      password: good.password,
    });
  });
});

describe("safeNextPath", () => {
  it("只放行站内路径", () => {
    expect(safeNextPath("/backtest")).toBe("/backtest");
    expect(safeNextPath("/")).toBe("/");
  });

  it("挡开放重定向与协议相对 URL", () => {
    expect(safeNextPath("https://evil.example")).toBe("/");
    expect(safeNextPath("//evil.example")).toBe("/");
    expect(safeNextPath(null)).toBe("/");
    expect(safeNextPath("")).toBe("/");
  });
});
