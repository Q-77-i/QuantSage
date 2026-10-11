"use client";

/**
 * 「我的深度研究」面板（M8c）：个人空间的一格，指向入口页 `/research`。
 *
 * 面板本身只给最近的几条与一个入口——**不在这里放新建表单**：新建要选时点、要看运行过程，
 * 那件事有自己的页面（`/research`）。
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { RunStatusBadge } from "@/components/agent-run/run-view";
import { Button } from "@/components/ui/button";
import { api, describeError } from "@/lib/api";
import type { RunStatus } from "@/lib/agent-run";
import type { ResearchRunSummary } from "@/lib/types";

export function ResearchPanel() {
  const [runs, setRuns] = useState<ResearchRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .researchRuns()
      .then((data) => alive && setRuns(data.runs))
      .catch((cause) => alive && setError(describeError(cause, "运行列表拉不到")));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-ink-2">
          深度研究：五个角色并行取证 → 多空对辩 → 单点成文 → 代码闸门，
          <strong className="font-medium">过程全程可见</strong>。
        </p>
        <Link href="/research">
          <Button>新建深度研究</Button>
        </Link>
      </div>

      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {runs === null && !error ? <p className="text-sm text-ink-3">加载中…</p> : null}

      {runs?.length === 0 ? (
        <p className="text-sm text-ink-3">还没有跑过深度研究。</p>
      ) : null}

      {runs?.length ? (
        <ul className="flex flex-col divide-y divide-border">
          {runs.map((run) => (
            <li key={run.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
              <Link href={`/research/runs/${run.id}`} className="min-w-0 flex-1 truncate hover:underline">
                {run.question}
              </Link>
              <RunStatusBadge status={run.status as RunStatus} />
              <span className="num text-xs text-ink-3">{run.created_at?.slice(0, 16).replace("T", " ")}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
