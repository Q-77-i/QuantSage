"use client";

import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { Pager } from "@/components/ui/pager";
import { api, describeError } from "@/lib/api";
import { dayStamp } from "@/lib/format";
import {
  durationText,
  groupChecks,
  levelMarker,
  levelTone,
  nextRunText,
  runSummary,
  staleSourceText,
  statusText,
} from "@/lib/health";
import { offsetOf } from "@/lib/paging";
import type { DataHealth, DataHealthHistory } from "@/lib/types";

/** 与另两张表同值：同一页上的列表，翻页手感不该几个样。 */
const PAGE_SIZE = 20;

/**
 * 数据面看板（E3a）：**只读**，把体检结果摆出来。
 *
 * 三件事按「越需要立刻行动越靠前」排：① 告警一览（红灯）② 数据到哪了（含**源侧边界**——
 * 「本地落后」与「源侧没发」在页头上长得一样，这里把后者单独说出来）③ 运行历史
 * （「什么时候开始不对的」唯一可查的地方）。
 *
 * **不重算任何判据**：红灯、源侧停滞交易日数都由服务端给（`statusText`/`staleSourceText`
 * 只做翻译）。前端判一次就是第二套口径。
 */
export function DataPanel({ page, onGo }: { page: number; onGo: (next: number) => void }) {
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [history, setHistory] = useState<DataHealthHistory | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [head, runs] = await Promise.all([
        api.dataHealth(),
        api.dataHealthHistory({ limit: PAGE_SIZE, offset: offsetOf(page, PAGE_SIZE) }),
      ]);
      setHealth(head);
      setHistory(runs);
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "数据体检加载失败：后端未启动或网络不通。"));
    }
  }, [page]);

  useEffect(() => {
    void load();
  }, [load]);

  const status = statusText(health?.last?.check_status);
  const groups = groupChecks(health?.last?.checks ?? []);

  return (
    <section className="mt-8">
      <h2 className="font-heading text-base font-medium">数据体检</h2>
      <p className="mt-1 text-xs text-ink-3">
        定时跑的数据质量体检：{health ? nextRunText(health.scheduler) : "加载中…"}。
        绿灯只说明**已经落盘的数据**守住了不变量，不说明源头还在发——
        源头那半边看旁边那行「源侧止于」。
      </p>
      {error ? (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      ) : null}
      {health === null && error === null ? (
        <p className="mt-3 text-sm text-ink-3">加载中…</p>
      ) : null}

      {health ? (
        <>
          <div data-health-summary className="mt-4 flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm">
            <span className={status.tone}>
              {status.text}
              {health.last ? `（${dayStamp(health.last.at)}，${durationText(health.last.duration_ms)}）` : ""}
            </span>
            <span className="text-ink-2">
              行情截至 {health.freshness?.latest_trade_date ?? "—"}
            </span>
            <span className={health.stale_source.stalled_trading_days ? "text-warn" : "text-ink-2"}>
              {staleSourceText(health.stale_source)}
            </span>
            <span className="text-ink-3">
              语料 {health.freshness?.event_coverage.start ?? "—"} →{" "}
              {health.freshness?.event_coverage.end ?? "—"}
            </span>
          </div>

          {health.last === null ? (
            <p className="mt-4 text-sm text-ink-2">
              还没有体检记录。定时体检跑过一轮之后，这里会显示告警一览、数据截止与运行历史。
            </p>
          ) : groups.length === 0 ? (
            <p className="mt-4 text-sm text-ink-2">这一轮没有任何一项输出。</p>
          ) : (
            groups.map((group) => (
              <div key={group.level} data-health-group={group.level} className="mt-4">
                <h3 className={`text-xs font-medium ${levelTone(group.level)}`}>
                  {levelMarker(group.level)} {group.level}（{group.items.length}）
                </h3>
                <ul className="mt-1 space-y-1">
                  {group.items.map((check) => (
                    <li key={check.id} className="text-sm">
                      <span className="font-mono text-xs text-ink-3">{check.id}</span>{" "}
                      <span className="text-ink-2">{check.title}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))
          )}
        </>
      ) : null}

      {history ? (
        history.runs.length === 0 ? (
          <p className="mt-6 text-sm text-ink-2">还没有运行记录。</p>
        ) : (
          <div data-health-history className="mt-6">
            <TableShell head={["时间", "触发", "读数", "退出码"]}>
              {history.runs.map((run) => (
                <Row key={`${run.at}-${run.source}`}>
                  <Cell className="text-ink-3">{dayStamp(run.at)}</Cell>
                  <Cell>{run.source}</Cell>
                  <Cell className={run.check_status ? statusText(run.check_status).tone : "text-ink-3"}>
                    {runSummary(run)}
                  </Cell>
                  <Cell numeric>{run.exit_code ?? "—"}</Cell>
                </Row>
              ))}
            </TableShell>
            <Pager total={history.total} limit={PAGE_SIZE} page={page} unit="轮" onGo={onGo} />
          </div>
        )
      ) : null}
    </section>
  );
}
