"use client";

/**
 * 深度研究运行页（M8c）：`/research/runs/[id]`。
 *
 * 两种进入方式，同一个视图：
 *   * **回看 / 断连重连**：拉详情（`events` 就是过程真源），还在跑就 2s 轮询到终态；
 *   * **待审批**：详情里最后一条 `phase=interrupt` 带着待批载荷，审批后原地续流。
 *
 * 这里**不重跑**任何东西——运行状态在服务端，页面只是它的一面镜子。
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useState } from "react";

import { ApprovalCard } from "@/components/agent-run/approval-card";
import { EventTimeline, RunView } from "@/components/agent-run/run-view";
import { resumeRun, useRunDetail } from "@/components/agent-run/use-run";
import { Button } from "@/components/ui/button";
import { pendingApproval, runGaps, type RunNodeEvent, type RunStatus } from "@/lib/agent-run";

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params.id === "string" ? params.id : null;
  const { detail, error, reload } = useRunDetail(id);
  const [busy, setBusy] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);

  const decide = useCallback(
    async (choice: "continue" | "stop") => {
      if (!id) return;
      setBusy(true);
      setStreamError(null);
      try {
        // 恢复也是一条流：帧与创建时同形，跑完刷新详情即可（详情是唯一真源）
        await resumeRun(id, choice, () => undefined);
      } catch (cause) {
        setStreamError(cause instanceof Error ? cause.message : "恢复失败");
      } finally {
        setBusy(false);
        void reload();
      }
    },
    [id, reload],
  );

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (!detail) return <p className="text-sm text-ink-3">加载中…</p>;

  const events = (detail.events ?? []) as RunNodeEvent[];
  const pending = pendingApproval(events);
  const gaps = runGaps(detail.report ?? null);

  return (
    <main className="mx-auto flex max-w-[1400px] flex-col gap-6 px-4 py-6">
      <RunView
        question={detail.question}
        subject={detail.subject}
        status={detail.status as RunStatus}
        events={events}
        budget={detail.budget ?? {}}
        gaps={gaps}
        aside={
          <div className="flex flex-col gap-4">
            {pending && (detail.status as RunStatus) === "awaiting_approval" ? (
              <ApprovalCard payload={pending} busy={busy} onDecide={decide} />
            ) : null}
            {streamError ? <p className="text-sm text-destructive">{streamError}</p> : null}
            {detail.error ? (
              <p className="text-sm text-destructive">运行失败：{detail.error}</p>
            ) : null}
            {detail.report_id ? (
              <div className="flex flex-wrap items-center gap-3">
                <Link href={`/research/${detail.report_id}`}>
                  <Button>看这份研报</Button>
                </Link>
                <span className="text-xs text-ink-3">
                  报告是<strong className="font-medium">冻结产物</strong>：同一份快照重放，事实层逐字段相等。
                </span>
              </div>
            ) : null}
            <EventTimeline events={events} />
          </div>
        }
      />
    </main>
  );
}
