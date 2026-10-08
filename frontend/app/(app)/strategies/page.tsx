"use client";

import dynamic from "next/dynamic";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import type { CodeEditorHandle } from "@/components/strategies/code-editor";
import type { Finding } from "@/lib/types";

/**
 * 策略工作台（M4c）——**当前是 Monaco 集成 spike 的壳**。
 *
 * M4c-1 只做「编辑器能不能离线跑起来、标注能不能显示」这两件事的验证；左列表、参数表单、
 * 检查面板与「保存并运行」随 M4c-2 补齐（届时本页整体替换）。spike 用固定样例而不是真端点，
 * 是为了在后端 `/strategies/check` 落地**之前**就把编辑器这条路证完或证伪。
 *
 * Monaco 必须动态引入且关掉 SSR：loader 是浏览器脚本，服务端没有 `window`。
 */
const CodeEditor = dynamic(
  () => import("@/components/strategies/code-editor").then((mod) => mod.CodeEditor),
  {
    ssr: false,
    loading: () => (
      <div className="h-[420px] animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
    ),
  },
);

const SAMPLE = `# 双均线交叉（spike 样例，非最终模板）
PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}}
USES_EVENTS = False


def on_bar(ctx, p):
    fast = p["fast"]
    closes = [bar.close for bar in ctx.history]
    if len(closes) < fast + 1:
        return []
    # 下面这两行故意写错，用来演示行号标注
    future = ctx.history[ctx.index + 1].close
    return [] if future else [Signal(Side.BUY, reason="金叉")]
`;

const SAMPLE_FINDINGS: Finding[] = [
  {
    rule: "R3",
    severity: "error",
    line: 12,
    message: "ctx.history 在物理上没有下一根：可用的是 bars[:index+1]，取 index + 1 拿到的是未来。",
    snippet: "    future = ctx.history[ctx.index + 1].close",
  },
  {
    rule: "R4",
    severity: "warning",
    line: 3,
    message: "声明了 USES_EVENTS = False，但代码里没有读 ctx.events；缺省窗口与 PIT 对比按声明的来。",
    snippet: "USES_EVENTS = False",
  },
];

export default function StrategiesPage() {
  const [code, setCode] = useState(SAMPLE);
  const [marked, setMarked] = useState(false);
  const editorRef = useRef<CodeEditorHandle>(null);

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <h1 className="font-heading text-xl font-semibold">策略工作台</h1>
      <p className="mt-1 text-sm text-ink-2">
        Monaco 集成 spike：验证本地资源可用、行号标注可用。左列表与参数表单随 M4c-2 补齐。
      </p>

      <div className="mt-4 flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => setMarked((prev) => !prev)}>
          {marked ? "清空标注" : "演示标注"}
        </Button>
        <span className="text-xs text-ink-3">
          {marked ? "已注入 1 个 error + 1 个 warning（点右侧按钮可清空）" : "编辑器内暂无标注"}
        </span>
      </div>

      <div className="mt-3">
        <CodeEditor
          ref={editorRef}
          value={code}
          onChange={setCode}
          findings={marked ? SAMPLE_FINDINGS : []}
        />
      </div>
    </main>
  );
}
