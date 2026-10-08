"use client";

import { useState } from "react";

import type { StrategySummary, StrategyTemplate } from "@/lib/types";

/**
 * 左栏：我的策略 + 模板（M4c）。
 *
 * 「模板」是服务端唯一真源（`app/strategy/templates/`），这里只读预览；要改必须先
 * **另存为我的**——改动落在自己的策略上，模板永远干净。这与内置策略不进模板库是同一条
 * 取舍：同一份东西只留一个真源。
 *
 * 删除是**行内二次确认**，不用浏览器 `confirm`（它会打断页面、且样式不可控）。
 */
export function StrategyList({
  strategies,
  templates,
  selectedId,
  selectedTemplateKey,
  busy,
  onSelect,
  onSelectTemplate,
  onCreate,
  onDelete,
}: {
  strategies: StrategySummary[];
  templates: StrategyTemplate[];
  selectedId: string | null;
  selectedTemplateKey: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onSelectTemplate: (key: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);

  return (
    <div className="space-y-6 text-sm">
      <section>
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-medium tracking-wide text-ink-2">我的策略</h2>
          <button
            type="button"
            onClick={onCreate}
            className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-brand hover:bg-muted"
          >
            + 新建
          </button>
        </div>

        {strategies.length === 0 ? (
          <p className="mt-2 text-xs text-ink-3">还没有策略。从下面的模板「另存为我的」开始最快。</p>
        ) : (
          <ul className="mt-2 space-y-0.5">
            {strategies.map((strategy) => (
              <li key={strategy.id}>
                {confirming === strategy.id ? (
                  <span className="flex items-center gap-2 px-2 py-1 text-xs">
                    <span className="text-ink-2">删除「{strategy.name}」？</span>
                    <button
                      type="button"
                      className="text-destructive hover:underline"
                      onClick={() => {
                        setConfirming(null);
                        onDelete(strategy.id);
                      }}
                    >
                      确认
                    </button>
                    <button
                      type="button"
                      className="text-ink-3 hover:underline"
                      onClick={() => setConfirming(null)}
                    >
                      取消
                    </button>
                  </span>
                ) : (
                  <div
                    className={`group flex items-center gap-2 rounded-[var(--radius)] px-2 py-1 ${
                      selectedId === strategy.id ? "bg-muted font-medium" : "hover:bg-muted"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => onSelect(strategy.id)}
                      className="flex-1 truncate text-left"
                      title={strategy.name}
                    >
                      {strategy.name}
                    </button>
                    <button
                      type="button"
                      aria-label={`删除 ${strategy.name}`}
                      disabled={busy}
                      onClick={() => setConfirming(strategy.id)}
                      className="shrink-0 text-xs text-ink-3 opacity-0 hover:text-destructive group-hover:opacity-100"
                    >
                      删除
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="text-xs font-medium tracking-wide text-ink-2">模板</h2>
        <ul className="mt-2 space-y-0.5">
          {templates.map((template) => (
            <li key={template.key}>
              <button
                type="button"
                onClick={() => onSelectTemplate(template.key)}
                title={template.summary}
                className={`w-full truncate rounded-[var(--radius)] px-2 py-1 text-left ${
                  selectedTemplateKey === template.key ? "bg-muted font-medium" : "hover:bg-muted"
                }`}
              >
                {template.title}
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
