"use client";

import dynamic from "next/dynamic";
import { useEffect, useRef } from "react";

import { FindingsPanel } from "@/components/strategies/findings-panel";
import { ParamsForm } from "@/components/strategies/params-form";
import { RunBar } from "@/components/strategies/run-bar";
import { StrategyList } from "@/components/strategies/strategy-list";
import { useWorkbench } from "@/components/strategies/use-workbench";
import { Button } from "@/components/ui/button";
import type { CodeEditorHandle } from "@/components/strategies/code-editor";
import { dayStamp } from "@/lib/format";
import { sandboxKindLabel } from "@/lib/strategy-form";

/**
 * 策略工作台（M4c）：左列表 ｜ 右编辑器 + 参数 + 检查结果 + 运行条。
 *
 * 排版遵循 T6 的语言（1px 分隔线分区、不套卡片盒、数字等宽、三态就地）；本页新增的只有
 * 「就地态」——未保存 / 已保存 / 检查中 / 运行中 / 被拦，见 design brief §5。
 *
 * Monaco 必须动态引入且关掉 SSR（loader 是浏览器脚本，服务端没有 window）。
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

export function Workbench({ initialId }: { initialId: string | null }) {
  const workbench = useWorkbench();
  const editorRef = useRef<CodeEditorHandle>(null);
  const { current, openStrategy, lastOpenRef } = workbench;

  // 深链 ?id=：只认「还没主动打开过」的那个 id。用「当前载入的 id」去重是不够的——
  // 从列表点选时 URL 才刚开始换，那个空档里旧的 ?id= 会把刚选的策略拽回去（实测踩过）
  const loadedId = current?.kind === "strategy" ? current.id : null;
  useEffect(() => {
    if (initialId && initialId !== lastOpenRef.current) void openStrategy(initialId);
  }, [initialId, lastOpenRef, openStrategy]);

  const isTemplate = current?.kind === "template";

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <h1 className="font-heading text-xl font-semibold">策略工作台</h1>
      <p className="mt-1 text-xs text-ink-3">
        写策略 → 保存 → 运行。回测跑的是**库里保存的那份源码**，所以运行前会先检查一次。
      </p>

      <div className="mt-5 lg:flex lg:gap-6">
        <aside className="lg:w-[260px] lg:shrink-0">
          <StrategyList
            strategies={workbench.strategies ?? []}
            templates={workbench.templates}
            selectedId={loadedId}
            selectedTemplateKey={current?.kind === "template" ? current.key : null}
            busy={workbench.saving || workbench.running}
            onSelect={(id) => void workbench.openStrategy(id)}
            onSelectTemplate={workbench.openTemplate}
            onCreate={workbench.startNew}
            onDelete={(id) => void workbench.remove(id)}
          />
        </aside>

        {/* pb 给底部那条 sticky 的运行条留位：不留的话它会**永久**盖住最后一段内容
            （参数或检查结果），滚到底也看不到 */}
        <section className="mt-6 min-w-0 flex-1 pb-24 lg:mt-0">
          {current === null ? (
            <EmptyState onCreate={workbench.startNew} />
          ) : (
            <>
              <header className="flex flex-wrap items-center gap-3">
                <NameInput
                  value={current.name}
                  placeholder={isTemplate ? current.name : "策略名（保存时用）"}
                  readOnly={isTemplate}
                  onChange={workbench.rename}
                />
                {workbench.dirty && !isTemplate ? (
                  <span className="text-xs text-ink-3">● 未保存</span>
                ) : null}
                {!workbench.dirty && current.kind === "strategy" ? (
                  <span className="text-xs text-ink-3">已保存 · {dayStamp(current.updatedAt)}</span>
                ) : null}
                {isTemplate ? (
                  <span className="text-xs text-ink-3">
                    模板只读，改它请先「另存为我的」（保存时改个名字即可）
                  </span>
                ) : null}
              </header>

              <div className="mt-3">
                <CodeEditor
                  ref={editorRef}
                  value={workbench.code}
                  onChange={workbench.editCode}
                  findings={workbench.findings}
                  readOnly={isTemplate}
                />
              </div>

              <Section title="参数">
                <ParamsForm
                  meta={workbench.meta}
                  values={workbench.values}
                  onChange={workbench.editParams}
                />
              </Section>

              <Section title="检查结果">
                <FindingsPanel
                  findings={workbench.findings}
                  onLocate={(line) => editorRef.current?.revealLine(line)}
                />
              </Section>

              {workbench.error ? (
                <p
                  role="alert"
                  className="mt-4 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                >
                  {workbench.error}
                </p>
              ) : null}

              {workbench.failure ? (
                <p
                  role="alert"
                  className={`mt-4 text-sm ${
                    workbench.failure.findings.length ? "text-destructive" : "text-warn"
                  }`}
                >
                  {workbench.failure.kind
                    ? `${sandboxKindLabel(workbench.failure.kind)}：${workbench.failure.message}`
                    : workbench.failure.message}
                </p>
              ) : null}

              {isTemplate ? (
                <div className="mt-6">
                  <Button type="button" onClick={workbench.saveAsCopy}>
                    另存为我的
                  </Button>
                  <span className="ml-3 text-xs text-ink-3">
                    保留这份源码，改成你的名字再保存（模板本身不动）。
                  </span>
                </div>
              ) : (
                <RunBar
                  form={workbench.runForm}
                  meta={workbench.meta}
                  saving={workbench.saving}
                  running={workbench.running}
                  onFormChange={workbench.setRunForm}
                  onSubmit={() => void workbench.saveAndRun()}
                />
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}

/** 1px 分隔线分区（T6 语言：不套卡片盒） */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4 border-t border-border pt-3">
      <h2 className="mb-2 text-xs font-medium tracking-wide text-ink-2">{title}</h2>
      {children}
    </section>
  );
}

function NameInput({
  value,
  placeholder,
  readOnly,
  onChange,
}: {
  value: string;
  placeholder: string;
  readOnly: boolean;
  onChange: (value: string) => void;
}) {
  if (readOnly) {
    return <span className="font-heading text-base font-medium">{placeholder}</span>;
  }
  return (
    <input
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className="w-64 rounded-[var(--radius)] border border-transparent bg-transparent px-1 py-0.5 font-heading text-base font-medium outline-none hover:border-border focus:border-brand"
    />
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex h-[320px] flex-col items-center justify-center gap-3 rounded-[var(--radius)] border border-border">
      <p className="text-sm text-ink-2">从左边挑一个策略或模板，或者新建一份。</p>
      <Button type="button" variant="ghost" size="sm" onClick={onCreate}>
        新建空白策略
      </Button>
    </div>
  );
}

