"use client";

/**
 * 登录态的唯一真源。
 *
 * 会话是 httpOnly cookie，JS 侧**读不到**——所以登录态只能靠一次 `GET /auth/me` 探测，
 * 没有别的路子（这是选 httpOnly 防 XSS 的必然代价）。探测期间 `loading` 为真，
 * 守卫据此渲染骨架而不是先闪内容再跳登录。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import type { User } from "@/lib/types";

interface AuthValue {
  user: User | null;
  /** 首次探测未回：调用方应渲染骨架，别急着判「未登录」 */
  loading: boolean;
  refresh: () => Promise<User | null>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const me = await api.me();
      setUser(me);
      return me;
    } catch {
      // 401 = 未登录；网络不通也按未登录处理——守卫会把人送去登录页，
      // 总比停在一个什么都拉不到的页面上强
      setUser(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    // 清 cookie 失败（断网）也要清本地态：留在「已登录」界面只会让后续每步都 401
    await api.logout().catch(() => undefined);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, refresh, logout }),
    [user, loading, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return value;
}
