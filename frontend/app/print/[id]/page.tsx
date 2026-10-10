"use client";

/**
 * **打印专用页**：`/print/[id]?t=<一次性导出令牌>`（M7d）。
 *
 * 服务端渲染 PDF 时，渲染器打开的就是这一页——它没有页头、没有按钮、没有守卫，
 * 只有报告正文本身（与研报页**同一份 `ReportView`**）。取数走 `?t=` 令牌，
 * 令牌由后端签发、绑死这份报告、默认 10 分钟过期——渲染器因此不需要会话 cookie。
 *
 * 它也**可以被真人打开**（粘贴地址就是一个干净的、只剩正文的报告），不索引。
 */

import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { ReportView } from "@/components/research/report-view";
import { api, describeError } from "@/lib/api";
import type { PublicReport } from "@/lib/types";

export default function PrintReportPage() {
  return (
    <Suspense fallback={<Skeleton />}>
      <PrintView />
    </Suspense>
  );
}

function PrintView() {
  const params = useParams<{ id: string }>();
  const token = useSearchParams().get("t");
  const id = typeof params.id === "string" ? params.id : null;
  const [report, setReport] = useState<PublicReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id || !token) {
      setError("导出链接无效或已过期");
      return;
    }
    let alive = true;
    api
      .reportForPrint(id, token)
      .then((data) => {
        if (alive) setReport(data);
      })
      .catch((cause) => {
        if (alive) setError(describeError(cause, "导出链接无效或已过期"));
      });
    return () => {
      alive = false;
    };
  }, [id, token]);

  if (error) {
    return (
      <main className="mx-auto max-w-[560px] px-4 py-16 text-center">
        <h1 className="font-heading text-lg font-semibold">{error}</h1>
        <p className="mt-2 text-sm text-ink-3">回到研报页重新导出即可。</p>
      </main>
    );
  }
  if (!report) return <Skeleton />;

  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8 print:px-0 print:py-0">
      <ReportView body={report.report} reportHash={report.report_hash} />
    </main>
  );
}

function Skeleton() {
  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8">
      <div className="h-64 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
    </main>
  );
}
