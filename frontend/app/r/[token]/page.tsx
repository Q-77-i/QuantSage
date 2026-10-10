"use client";

/**
 * 公开只读研报：`/r/[token]`。
 *
 * **不在 `(app)` 守卫组里**——这是全站唯一匿名可达的用户数据出口，守卫、页头、所有者动作
 * 一概没有；能看到的只有冻结产物本身（后端响应也不带任何身份字段）。
 * 链接失效（不存在 / 已撤销）时给一个干净的说明页：**不跳登录**——访客没有账号可登。
 */

import { useParams } from "next/navigation";
import { useEffect } from "react";

import { PublicExport } from "@/components/research/report-actions";
import { ReportView } from "@/components/research/report-view";
import { usePublicReport } from "@/components/research/use-report";

export default function PublicReportPage() {
  const params = useParams<{ token: string }>();
  const token = typeof params.token === "string" ? params.token : null;
  const { report, loading, error } = usePublicReport(token);

  // 分享页不索引（`noindex`）：链接是给拿到它的人看的，不进搜索引擎
  useEffect(() => {
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex";
    document.head.appendChild(meta);
    return () => {
      document.head.removeChild(meta);
    };
  }, []);

  if (loading) {
    return (
      <main className="mx-auto max-w-[1100px] px-4 py-8">
        <div className="h-64 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
      </main>
    );
  }

  if (error || !report) {
    return (
      <main className="mx-auto flex min-h-screen max-w-[560px] flex-col items-center justify-center gap-3 px-4 text-center">
        <h1 className="font-heading text-lg font-semibold">分享链接已失效</h1>
        <p className="text-sm text-ink-3">
          这份研报的分享已被撤销，或者链接不完整。请向分享者要一个新链接。
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8">
      <ReportView
        body={report.report}
        reportHash={report.report_hash}
        actions={
          <PublicExport
            token={token ?? ""}
            accountName={report.report.account.name}
            reportHash={report.report_hash}
          />
        }
      />
    </main>
  );
}
