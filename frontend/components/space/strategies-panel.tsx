"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { api, describeError } from "@/lib/api";
import { dayStamp } from "@/lib/format";
import type { StrategySummary } from "@/lib/types";

/**
 * 我的策略（M4c）：从占位改成**列表 + 入口**。
 *
 * 这一格刻意只做「看有什么、点进去改」——编辑、检查、运行都在工作台（`/strategies`），
 * 两处各放一套编辑器必然会话成两个真源。点某一条走 `?id=` 深链直达。
 */
export function StrategiesPanel() {
  const [strategies, setStrategies] = useState<StrategySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStrategies(await api.strategies());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "策略列表加载失败：后端未启动或网络不通。"));
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

  if (strategies === null) {
    return (
      <div className="space-y-2">
        {[0, 1].map((index) => (
          <div key={index} className="h-8 animate-pulse rounded-[var(--radius)] bg-muted" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-2">
        写策略、检查前视泄漏、直接跑回测都在工作台。
        <Link href="/strategies" className="ml-1 text-brand hover:underline">
          去工作台
        </Link>
      </p>

      {strategies.length === 0 ? (
        <p className="text-sm text-ink-3">
          还没有策略。工作台里有 5 个模板，挑一个「另存为我的」就能改。
        </p>
      ) : (
        <ul className="divide-y divide-border border-y border-border text-sm">
          {strategies.map((strategy) => (
            <li key={strategy.id}>
              <Link
                href={`/strategies?id=${strategy.id}`}
                className="flex items-center gap-4 px-1 py-2.5 hover:bg-muted"
              >
                <span className="min-w-0 flex-1 truncate">{strategy.name}</span>
                <span className="num shrink-0 text-xs text-ink-3">
                  更新于 {dayStamp(strategy.updated_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
