"use client";

import { Button } from "@/components/ui/button";
import { validateSymbol } from "@/lib/watchlist";
import type { PitMode, StrategyMeta } from "@/lib/types";

/**
 * 运行条（M4c）：工作台的主动作常驻在内容列底部（sticky），不放进页头、也不藏进菜单。
 *
 * 只有三样输入：标的 + 区间（可留空） + PIT 对比开关。费用与滑点在回测页——把它们搬过来
 * 只会让「写策略并跑起来」这条主路径变重。
 *
 * **PIT 开关只在策略消费事件时出现**：`USES_EVENTS = False` 时两种模式必然同结果，
 * 露一个点了没反应的开关比不露更糟。
 */

export interface RunFormState {
  symbol: string;
  start: string;
  end: string;
  pitMode: PitMode;
}

export function RunBar({
  form,
  meta,
  saving,
  running,
  onFormChange,
  onSubmit,
}: {
  form: RunFormState;
  meta: StrategyMeta | null;
  saving: boolean;
  running: boolean;
  onFormChange: (form: RunFormState) => void;
  onSubmit: () => void;
}) {
  const symbolError = form.symbol.trim() ? validateSymbol(form.symbol) : "请填标的代码";
  const busy = saving || running;

  return (
    <div className="sticky bottom-0 -mx-4 border-t border-border bg-plane/95 px-4 py-3 backdrop-blur">
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-2">标的</span>
          <input
            value={form.symbol}
            onChange={(event) => onFormChange({ ...form, symbol: event.target.value })}
            placeholder="600519"
            className="num w-24 rounded-[var(--radius)] border border-border bg-surface px-2 py-1 text-sm outline-none focus:border-brand"
          />
        </label>

        <RangeInput
          label="起"
          value={form.start}
          onChange={(start) => onFormChange({ ...form, start })}
        />
        <RangeInput
          label="止"
          value={form.end}
          onChange={(end) => onFormChange({ ...form, end })}
        />

        {meta?.uses_events ? (
          <label className="flex cursor-pointer items-center gap-2 pb-1 text-sm">
            <input
              type="checkbox"
              checked={form.pitMode === "both"}
              onChange={(event) =>
                onFormChange({ ...form, pitMode: event.target.checked ? "both" : "pit" })
              }
              className="size-4 accent-[var(--brand)]"
            />
            <span title="同一份源码跑两遍：只喂 available_at 之前可见的事件，与不受限的对照">
              PIT / 非 PIT 对比
            </span>
          </label>
        ) : null}

        <div className="ml-auto flex items-center gap-3">
          {symbolError ? <span className="text-xs text-ink-3">{symbolError}</span> : null}
          <Button type="button" disabled={busy || Boolean(symbolError)} onClick={onSubmit}>
            {running ? "运行中…" : saving ? "保存中…" : "保存并运行"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function RangeInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-ink-2">{label}</span>
      <input
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="num rounded-[var(--radius)] border border-border bg-surface px-2 py-1 text-sm outline-none focus:border-brand"
      />
    </label>
  );
}
