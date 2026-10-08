"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { strategyLabel, symbolName } from "@/lib/backtest-form";
import { api, describeError } from "@/lib/api";
import { count, dayStamp, num, pct } from "@/lib/format";
import type { BacktestRunSummary } from "@/lib/types";

/**
 * 我的回测：跑过的每一次都在这里，点「重开」回到那一份完整报告（曲线、K 线、明细）。
 *
 * 用的是摘要端点（后端只抽 JSONB 子集），所以这一页不会因为记录变多而变重；
 * 完整报告等点进去再取。
 */
export function RunsPanel() {
  const [runs, setRuns] = useState<BacktestRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRuns(await api.runs());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "回测记录加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    );
  }

  if (runs === null) return <Skeleton />;

  if (runs.length === 0) {
    return (
      <p className="text-sm text-ink-2">
        还没有回测记录。
        <Link href="/backtest" className="ml-1 text-brand hover:underline">
          去跑一次
        </Link>
      </p>
    );
  }

  return (
    <TableShell head={["时间", "策略", "标的", "区间", "收益", "最大回撤", "夏普", "交易", ""]}>
      {runs.map((run) => (
        <Row key={run.id}>
          <Cell numeric className="text-ink-3">
            {dayStamp(run.created_at)}
          </Cell>
          {/* 用户策略显示自己的名字（内置策略没有名字，回落成「双均线」这类标签） */}
          <Cell>
            {run.strategy === "user"
              ? `用户策略 · ${run.strategy_name ?? "已删除"}`
              : strategyLabel(run.strategy)}
          </Cell>
          <Cell numeric>
            {run.symbol}
            <span className="ml-1.5 text-xs text-ink-3">{symbolName(run.symbol)}</span>
          </Cell>
          <Cell numeric className="text-ink-2">
            {run.start ?? "—"} ~ {run.end ?? "—"}
          </Cell>
          <Cell numeric className={tone(run.metrics.total_return)}>
            {pct(run.metrics.total_return, { signed: true })}
          </Cell>
          {/* 回撤后端给的是正值幅度，按语义不带号 */}
          <Cell numeric className="text-ink-2">
            {pct(run.metrics.max_drawdown)}
          </Cell>
          <Cell numeric>{num(run.metrics.sharpe)}</Cell>
          <Cell numeric className="text-ink-3">
            {count(run.metrics.trade_count)}
          </Cell>
          <Cell className="text-right">
            <Link
              href={`/backtest?run=${run.id}`}
              className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-brand hover:bg-muted"
            >
              重开
            </Link>
          </Cell>
        </Row>
      ))}
    </TableShell>
  );
}

function tone(value: number | null): string {
  if (value === null) return "text-ink-3";
  return value >= 0 ? "text-up" : "text-down";
}

function Skeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2].map((index) => (
        <div key={index} className="h-9 animate-pulse rounded-[var(--radius)] bg-muted" />
      ))}
    </div>
  );
}
