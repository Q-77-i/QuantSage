"use client";

import { hasBlockingFindings, sortFindings } from "@/lib/strategy-form";
import type { Finding } from "@/lib/types";

/**
 * 检查结果面板（M4c）。
 *
 * 两件事必须一眼看出来：**哪几条会拦住运行**（`error` 档，红）与哪几条只是提醒（`warning` 档，
 * 琥珀）——这正是 SPEC 让 R3 定成双档的产品理由。所以两档用两色两标，不靠文案区分。
 *
 * 点一条 → 编辑器跳到那一行（`onLocate`）。检查是毫秒级的纯函数，面板不做加载骨架：
 * 保持上一次结果即可，闪一下反而更吵。
 */
export function FindingsPanel({
  findings,
  onLocate,
}: {
  findings: Finding[];
  onLocate: (line: number) => void;
}) {
  const sorted = sortFindings(findings);
  const blocking = hasBlockingFindings(findings);
  const errors = sorted.filter((item) => item.severity === "error").length;
  const warnings = sorted.length - errors;

  if (sorted.length === 0) {
    return <p className="text-xs text-ink-3">未发现问题。保存并运行时会再检查一次库里的源码。</p>;
  }

  return (
    <div className="space-y-2">
      <p className={`text-xs ${blocking ? "text-destructive" : "text-warn"}`}>
        {blocking
          ? `${errors} 个 error 会拦住运行`
          : `${warnings} 个 warning：照跑，但建议看一眼`}
        {blocking && warnings > 0 ? `（另有 ${warnings} 个 warning）` : ""}
      </p>
      <ul className="max-h-[200px] space-y-1 overflow-y-auto">
        {sorted.map((finding, index) => (
          <li key={`${finding.rule}-${finding.line}-${index}`}>
            <button
              type="button"
              onClick={() => onLocate(finding.line)}
              className="w-full rounded-[var(--radius)] px-2 py-1 text-left hover:bg-muted"
            >
              <span className="flex items-baseline gap-2 text-xs">
                <span
                  className={`num shrink-0 font-medium ${
                    finding.severity === "error" ? "text-destructive" : "text-warn"
                  }`}
                >
                  {finding.severity === "error" ? "✕" : "!"} {finding.rule}
                </span>
                <span className="num shrink-0 text-ink-3">第 {finding.line} 行</span>
                <span className="text-ink-1">{finding.message}</span>
              </span>
              {finding.snippet ? (
                <span className="mt-0.5 block truncate font-mono text-[11px] text-ink-3">
                  {finding.snippet.trim()}
                </span>
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
