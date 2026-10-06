"use client";

import { useCallback, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import type {
  BacktestReport,
  BacktestRequest,
  BacktestRunDetail,
  Bar,
  MarketEvent,
} from "@/lib/types";

/**
 * 运行一次回测：报告 → 行情 → 事件；重开历史回测走同一条装配路径。
 *
 * 三件事是**一条流水线**，共用同一个序号戳：后两步的取数窗口来自报告的 `meta`，
 * 只给 POST 加序号的话，先发的那次 run 会用它的 bars 覆盖后一次的 K 线
 * （图看起来是新的，其实横轴范围是上一次的）。
 *
 * K 线的窗口与复权口径一律取**响应**而非表单：缺省区间是后端 `resolve_window`
 * 定的（事件驱动从事件窗口起点开跑），用表单值或全量取数会让 K 线范围与净值曲线对不上。
 *
 * M1c 起 `POST /backtest` 返回信封 `{run_id, report}`：`runId` 用来把地址栏换成
 * `?run=<id>`，刷新或分享时能直接回到这一份报告。
 */
export function useBacktest() {
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [bars, setBars] = useState<Bar[]>([]);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const seqRef = useRef(0);

  /** 报告落定后补上配套的行情与事件。作废的流水线一律不落状态 */
  const fill = useCallback(async (next: BacktestReport, seq: number) => {
    const window = { start: next.meta.start ?? undefined, end: next.meta.end ?? undefined };
    const [barsResponse, eventsResponse] = await Promise.all([
      api.bars(next.meta.symbol, { ...window, adjust: next.meta.adjust }),
      api.events(next.meta.symbol, window),
    ]);

    if (seq !== seqRef.current) return;
    setReport(next);
    setBars(barsResponse.bars);
    setEvents(eventsResponse.events);
  }, []);

  const run = useCallback(
    (body: BacktestRequest) => {
      const seq = ++seqRef.current;
      setLoading(true);
      setError(null);

      void (async () => {
        try {
          const { run_id, report: next } = await api.backtest(body);
          await fill(next, seq);
          if (seq === seqRef.current) setRunId(run_id);
        } catch (cause) {
          if (seq !== seqRef.current) return;
          setError(describeError(cause, "回测请求失败：后端未启动或网络不通。"));
        } finally {
          // 只有仍是最新一次运行时才收掉 loading——否则会把后来者的加载态提前关掉
          if (seq === seqRef.current) setLoading(false);
        }
      })();
    },
    [fill],
  );

  /**
   * 重开一条历史回测：报告直接用存下来的那份，**不再跑引擎**（结果必须与当初一致）。
   * 返回详情供调用方回填表单；失败返回 null。
   */
  const loadRun = useCallback(
    (id: string): Promise<BacktestRunDetail | null> => {
      const seq = ++seqRef.current;
      setLoading(true);
      setError(null);

      return (async () => {
        try {
          const detail = await api.run(id);
          await fill(detail.report, seq);
          if (seq !== seqRef.current) return null;
          setRunId(detail.id);
          return detail;
        } catch (cause) {
          if (seq !== seqRef.current) return null;
          setError(describeError(cause, "回测记录加载失败：后端未启动或网络不通。"));
          return null;
        } finally {
          if (seq === seqRef.current) setLoading(false);
        }
      })();
    },
    [fill],
  );

  return { loading, report, bars, events, error, runId, run, loadRun };
}
