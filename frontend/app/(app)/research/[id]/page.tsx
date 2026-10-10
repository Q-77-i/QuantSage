"use client";

/**
 * 研报页（登录态）：`/research/[id]`。
 *
 * 壳在这里（页头由 `(app)` 组布局给），正文走 `ReportView`——**与公开页同一份渲染**。
 * 所有者动作（分享 / 撤销 / 导出）只在这一侧；`/paper` 的「出研报」按钮跳到这里。
 */

import { useParams, useRouter } from "next/navigation";
import { useCallback } from "react";

import { ReportActions } from "@/components/research/report-actions";
import { ReportView } from "@/components/research/report-view";
import { useReport } from "@/components/research/use-report";

export default function ResearchPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = typeof params.id === "string" ? params.id : null;
  const { envelope, setEnvelope, loading, error, reload } = useReport(id);

  const onShareChange = useCallback(
    (token: string | null) => {
      setEnvelope((current) =>
        current
          ? { ...current, share_token: token, share_path: token ? `/r/${token}` : null }
          : current,
      );
    },
    [setEnvelope],
  );

  if (loading) return <Bootstrap />;

  if (error || !envelope) {
    return (
      <main className="mx-auto max-w-[1100px] px-4 py-6">
        <p role="alert" className="rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error ?? "报告不存在"}
          <button type="button" onClick={() => void reload()} className="ml-2 underline">
            重试
          </button>
          <button
            type="button"
            onClick={() => router.push("/paper")}
            className="ml-2 underline"
          >
            回到模拟盘
          </button>
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1100px] px-4 py-6">
      <ReportView
        body={envelope.report}
        reportHash={envelope.report_hash}
        actions={
          <ReportActions
            reportId={envelope.id}
            reportHash={envelope.report_hash}
            accountName={envelope.report.account.name}
            shareToken={envelope.share_token}
            onShareChange={onShareChange}
          />
        }
      />
    </main>
  );
}

function Bootstrap() {
  return (
    <main className="mx-auto max-w-[1100px] px-4 py-6">
      <div className="h-64 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
    </main>
  );
}
