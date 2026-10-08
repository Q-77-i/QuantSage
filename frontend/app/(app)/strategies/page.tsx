"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { Workbench } from "@/components/strategies/workbench";

/**
 * 策略工作台（M4c）。
 *
 * `?id=<strategy_id>` 是深链入口（个人空间的「我的策略」与跑完回跳都用它）；
 * `useSearchParams` 以「只渲染子组件的 Suspense 边界」包住，生产构建下才降级为 CSR。
 */
export default function StrategiesPage() {
  return (
    <Suspense fallback={<Bootstrap />}>
      <WorkbenchWithParams />
    </Suspense>
  );
}

function WorkbenchWithParams() {
  const params = useSearchParams();
  return <Workbench initialId={params.get("id")} />;
}

function Bootstrap() {
  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="h-6 w-32 animate-pulse rounded-[var(--radius)] bg-muted" />
      <div className="mt-5 h-[420px] animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
    </main>
  );
}
