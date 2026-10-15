import { describe, expect, it } from "vitest";

import { loginRedirectTarget } from "./api";

/**
 * 会话中途失效该跳哪（P3-E8a）。
 *
 * 判据单独抽成纯函数就是为了这几条：写宽一点点不是把用户困在环里，就是把「密码错」的
 * 提示换成一次莫名其妙的跳转。
 */
describe("loginRedirectTarget", () => {
  it("会话中途失效 → 跳登录页并带回当前位置（含查询串）", () => {
    expect(loginRedirectTarget(401, "/api/v1/tasks", "/backtest?run=x", true)).toBe(
      "/login?next=%2Fbacktest%3Frun%3Dx",
    );
  });

  it("登录 / 注册自己的 401 不跳——那是「密码错」，不是会话失效", () => {
    expect(loginRedirectTarget(401, "/api/v1/auth/login", "/login", true)).toBeNull();
    expect(loginRedirectTarget(401, "/api/v1/auth/register", "/register", true)).toBeNull();
  });

  it("me 探测不跳：「未登录」那条路已由 AuthProvider + 守卫统一处理，两处都跳会打架", () => {
    expect(loginRedirectTarget(401, "/api/v1/auth/me", "/", true)).toBeNull();
  });

  it("已在登录 / 注册页不跳（否则是重定向环）", () => {
    expect(loginRedirectTarget(401, "/api/v1/tasks", "/login", true)).toBeNull();
    expect(loginRedirectTarget(401, "/api/v1/tasks", "/register", true)).toBeNull();
  });

  it("非 401 一律不跳（403/404/422/500/503 各有各的话术，不该变成跳转）", () => {
    for (const status of [200, 403, 404, 422, 500, 503]) {
      expect(loginRedirectTarget(status, "/api/v1/tasks", "/backtest", true)).toBeNull();
    }
  });

  it("服务端渲染阶段（没有 window）不跳", () => {
    expect(loginRedirectTarget(401, "/api/v1/tasks", "/backtest", false)).toBeNull();
  });
});
