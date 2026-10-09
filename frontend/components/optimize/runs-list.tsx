"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { OptimizeRunSummary } from "@/lib/types";

/**
 * 我的优化（摘要列表）。
 *
 * 两个消费方共用同一份：`/optimize` 页脚「最近的优化」（限 8 条）与
 * `/space?tab=optimizations`（默认 20 条）。抽出来是因为两处各写一遍的话，
 * 「几格 / DSR 怎么显示」迟早长歪——而这两个数正是列表的全部信息。
 *
 * 列表项是**链接**而不是按钮：它跳的是一个可分享的地址（`/optimize?run=<id>`），
 * 中键新开、右键复制都该照常可用。
 */
export function RunsList({
  currentId,
  limit = 20,
  emptyText = "还没有跑过网格或批量。",
}: {
  currentId?: string | null;
  limit?: number;
  emptyText?: string;
}) {
  const [rows, setRows] = useState<OptimizeRunSummary[] | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .optimizeRuns(limit)
      .then((items) => {
        if (alive) setRows(items);
      })
      // 取不到就当作「没有」：列表是附加信息，不该让整页报错
      .catch(() => {
        if (alive) setRows([]);
      });
    return () => {
      alive = false;
    };
  }, [limit, currentId]);

  if (rows === null) {
    return <div className="mt-2 h-16 animate-pulse rounded-[var(--radius)] bg-muted/60" />;
  }
  if (rows.length === 0) {
    return <p className="mt-2 text-sm text-ink-2">{emptyText}</p>;
  }

  return (
    <ul className="mt-2 divide-y divide-border/60 text-sm">
      {rows.map((row) => (
        <li key={row.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-2">
          <Link
            href={`/optimize?mode=${row.summary.kind}&run=${row.id}`}
            className="text-brand hover:underline"
          >
            {row.summary.kind === "grid" ? "网格" : "批量"}
          </Link>
          <span className="num text-xs text-ink-3">
            {row.created_at.slice(0, 16).replace("T", " ")}
          </span>
          <span className="num text-xs text-ink-3">
            {row.summary.cells_ok} / {row.summary.cells_total} 格
          </span>
          <span className="num text-xs text-ink-3">
            DSR {row.summary.overfit.dsr === null ? "不适用" : row.summary.overfit.dsr.toFixed(4)}
          </span>
          {row.summary.overfit.dsr === null && row.summary.overfit.reason_text ? (
            // 不适用时把原因摆出来（一句话，来自服务端）——只写「不适用」等于没说
            <span className="text-xs text-ink-3">（{row.summary.overfit.reason_text}）</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
