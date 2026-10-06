"use client";

import type { ToolStep } from "@/lib/types";

/**
 * 工具调用步骤：默认收成一行，展开看每一步的参数与返回原文。
 *
 * 用原生 `<details>` 而不是 `useState`：零 JS、无 hydration 差异，折叠状态也不必跨渲染保留。
 *
 * 来源标注靠返回原文承载。工具返回是**给模型看的可读文本**（本地行情查询一种格式、
 * 小石四个工具各一种），结构化解析即假设、假设即脆；设计规范只要求「展开后可见」。
 */

function hasArgs(args: unknown): boolean {
  return typeof args === "object" && args !== null && Object.keys(args).length > 0;
}

export function ToolSteps({ tools }: { tools: ToolStep[] }) {
  if (!tools.length) return null;
  const failed = tools.filter((step) => step.is_error).length;

  return (
    <details className="group mb-2 rounded-[var(--radius)] border border-border">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5 text-xs text-ink-2 hover:bg-muted [&::-webkit-details-marker]:hidden">
        <span className="transition-transform group-open:rotate-90">▸</span>
        工具调用 {tools.length} 步
        {failed > 0 && <span className="text-destructive">· {failed} 步失败</span>}
      </summary>

      <ol className="border-t border-border px-2.5 py-2">
        {tools.map((step) => (
          <li key={step.id} className="not-last:border-b not-last:border-border py-2">
            <div className="flex items-center gap-2 text-xs">
              <span className={step.is_error ? "text-destructive" : "text-ink-3"}>
                {step.content === null ? "⋯" : step.is_error ? "✕" : "✔"}
              </span>
              <span className="num">{step.name}</span>
            </div>

            {hasArgs(step.args) && (
              <pre className="mt-1 overflow-x-auto rounded-[var(--radius)] bg-muted px-2 py-1 text-xs text-ink-2">
                {JSON.stringify(step.args, null, 2)}
              </pre>
            )}

            {step.content !== null && (
              <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded-[var(--radius)] border border-border bg-card px-2 py-1.5 text-xs text-ink-2">
                {step.content}
              </pre>
            )}
          </li>
        ))}
      </ol>
    </details>
  );
}
