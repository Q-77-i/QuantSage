"use client";

import { useCallback, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { BacktestReport, BacktestRequest, Bar, MarketEvent } from "@/lib/types";

/**
 * 运行一次回测：报告 → 行情 → 事件。
 *
 * 三件事是**一条流水线**，共用同一个序号戳：后两步的取数窗口来自报告的 `meta`，
 * 只给 POST 加序号的话，先发的那次 run 会用它的 bars 覆盖后一次的 K 线
 * （图看起来是新的，其实横轴范围是上一次的）。
 *
 * K 线的窗口与复权口径一律取**响应**而非表单：缺省区间是后端 `resolve_window`
 * 定的（事件驱动从事件窗口起点开跑），用表单值或全量取数会让 K 线范围与净值曲线对不上。
 */
export function useBacktest() {
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [bars, setBars] = useState<Bar[]>([]);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const run = useCallback((body: BacktestRequest) => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);

    void (async () => {
      try {
        const next = await api.backtest(body);
        const window = { start: next.meta.start ?? undefined, end: next.meta.end ?? undefined };
        const [barsResponse, eventsResponse] = await Promise.all([
          api.bars(next.meta.symbol, { ...window, adjust: next.meta.adjust }),
          api.events(next.meta.symbol, window),
        ]);

        if (seq !== seqRef.current) return; // 已有更新的一次运行在跑，整条流水线作废
        setReport(next);
        setBars(barsResponse.bars);
        setEvents(eventsResponse.events);
      } catch (cause) {
        if (seq !== seqRef.current) return;
        setError(
          cause instanceof ApiError
            ? cause.message
            : "回测请求失败：后端未启动或网络不通。",
        );
      } finally {
        // 只有仍是最新一次运行时才收掉 loading——否则会把后来者的加载态提前关掉
        if (seq === seqRef.current) setLoading(false);
      }
    })();
  }, []);

  return { loading, report, bars, events, error, run };
}
