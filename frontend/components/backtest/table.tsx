"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 表格外壳：表头样式与横滚在这里定一次，行的内容由调用方全权掌握
 * （事件表要跨列的展开行、PIT 表要强调差异列，都不是统一渲染能表达的）。
 *
 * 行高 38px、hover 整行水洗、**不画单元格竖线**——数据区靠 1px 横线与间距分组。
 */
export function TableShell({
  head,
  children,
  className,
}: {
  head: ReactNode[];
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mt-3 overflow-x-auto", className)}>
      <table className="w-full text-sm">
        <thead>
          {/* 表头吸顶：事件表有 88 行，滚动时不能丢掉列名。分隔线用 inset 阴影而不是
              border——吸顶时 border 会跟着内容一起滚走 */}
          <tr className="text-xs text-ink-2">
            {head.map((cell, index) => (
              <th
                key={index}
                scope="col"
                className="sticky top-0 z-10 bg-background px-2 py-2 text-left font-normal whitespace-nowrap shadow-[inset_0_-1px_0_0_var(--hairline)]"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** 一行。数字单元格自己带 `.num`，与表头一起保证列对齐。 */
export function Row({ children }: { children: ReactNode }) {
  return <tr className="h-[38px] border-b border-border hover:bg-muted">{children}</tr>;
}

export function Cell({
  children,
  className,
  numeric,
}: {
  children: ReactNode;
  className?: string;
  numeric?: boolean;
}) {
  return (
    <td className={cn("px-2 align-middle whitespace-nowrap", numeric && "num", className)}>
      {children}
    </td>
  );
}
