"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import type { PaperAccountDetail, PaperAccountSummary } from "@/lib/types";

/**
 * 模拟盘的数据出口：会话列表、单个会话（含三个动作），以及标的名称的按需解析。
 *
 * 三个动作的共性：**它们都会把整个详情换回来**（服务端算完就把新状态给全），
 * 所以前端不做乐观更新——账本是钱，宁可慢半拍也不要先改界面再回滚。
 * 动作失败（409 并发 / 422 校验）时把后端 `detail` 原样显示：那些话术是给人看的。
 */

export function usePaperSessions() {
  const [sessions, setSessions] = useState<PaperAccountSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSessions(await api.paperAccounts());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "会话列表加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { sessions, error, reload: load };
}

export interface PaperActions {
  detail: PaperAccountDetail | null;
  loading: boolean;
  error: string | null;
  /** 有动作在跑（推进 / 批准 / 跑到结束）——按钮据此禁用，防连点 */
  busy: boolean;
  reload: () => Promise<void>;
  step: () => Promise<void>;
  decide: (decisionId: string, action: "approve" | "reject") => Promise<void>;
  runToEnd: (approve: "all" | "none") => Promise<void>;
}

export function usePaperAccount(accountId: string | null): PaperActions {
  const [detail, setDetail] = useState<PaperAccountDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 请求竞态：切换会话时旧请求可能后到，用序号丢弃过期响应（M4c 深链踩过同一类）
  const ticket = useRef(0);

  const reload = useCallback(async () => {
    if (!accountId) {
      setDetail(null);
      return;
    }
    const mine = ++ticket.current;
    setLoading(true);
    try {
      const next = await api.paperAccount(accountId);
      if (ticket.current === mine) {
        setDetail(next);
        setError(null);
      }
    } catch (cause) {
      if (ticket.current === mine) {
        setDetail(null);
        setError(describeError(cause, "会话加载失败：后端未启动或网络不通。"));
      }
    } finally {
      if (ticket.current === mine) setLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  /** 动作的统一外壳：置忙 → 拿回新详情 → 出错显示后端话术。 */
  const act = useCallback(
    async (work: () => Promise<PaperAccountDetail>) => {
      setBusy(true);
      try {
        setDetail(await work());
        setError(null);
      } catch (cause) {
        setError(describeError(cause, "操作失败：后端未启动或网络不通。"));
        // 失败多半是「状态已经变了」（409），重新拉一次让界面回到真实状态
        await reload();
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const step = useCallback(async () => {
    if (!accountId) return;
    await act(() => api.stepPaperAccount(accountId));
  }, [accountId, act]);

  const runToEnd = useCallback(
    async (approve: "all" | "none") => {
      if (!accountId) return;
      await act(() => api.runPaperAccount(accountId, approve));
    },
    [accountId, act],
  );

  const decide = useCallback(
    async (decisionId: string, action: "approve" | "reject") => {
      if (!accountId) return;
      setBusy(true);
      try {
        await api.decidePaper(decisionId, action);
        setDetail(await api.paperAccount(accountId));
        setError(null);
      } catch (cause) {
        setError(describeError(cause, "审批失败：后端未启动或网络不通。"));
        await reload();
      } finally {
        setBusy(false);
      }
    },
    [accountId, reload],
  );

  return { detail, loading, error, busy, reload, step, decide, runToEnd };
}

/** 标的名称的缓存：代码 → 名称。一次会话里同一只标的只查一次。 */
const nameCache = new Map<string, string>();

/**
 * 按需解析标的名称（决策卡与持仓表用）。
 *
 * 为什么不为它单开端点：名称在**名称字典**里（`GET /market/symbols?q=` 按代码能精确命中），
 * 一次会话的池子最多 20 只，第一帧并发查完就进缓存；查不到就显示代码——
 * 名称是增强，不是关键路径（`naming` 层的既有口径）。
 */
export function useSymbolNames(symbols: string[]): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>(() =>
    Object.fromEntries(symbols.filter((s) => nameCache.has(s)).map((s) => [s, nameCache.get(s)!])),
  );
  const key = symbols.join(",");

  useEffect(() => {
    let alive = true;
    const missing = symbols.filter((symbol) => !nameCache.has(symbol));
    if (!missing.length) {
      setNames(Object.fromEntries(symbols.map((s) => [s, nameCache.get(s) ?? s])));
      return;
    }
    void Promise.all(
      missing.map(async (symbol) => {
        try {
          const found = await api.symbols(symbol, 5);
          const hit = found.items.find((item) => item.symbol === symbol);
          if (hit) nameCache.set(symbol, hit.name);
        } catch {
          // 名称查不到就算了：显示代码，不打扰用户（字典缺失是已知的 96.7% 覆盖）
        }
      }),
    ).then(() => {
      if (alive) setNames(Object.fromEntries(symbols.map((s) => [s, nameCache.get(s) ?? s])));
    });
    return () => {
      alive = false;
    };
    // 依赖的是「池子里有哪些代码」，不是数组身份
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return names;
}
