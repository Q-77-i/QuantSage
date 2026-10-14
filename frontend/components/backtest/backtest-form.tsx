"use client";

import { Button } from "@/components/ui/button";
import { Check, Field, Input, Select } from "@/components/ui/form-controls";
import {
  PIT_MODES,
  SAMPLE_SYMBOLS,
  hasErrors,
  parseUserOption,
  strategySelection,
} from "@/lib/backtest-form";
import type { FormState, StrategyOption } from "@/lib/backtest-form";
import type { ParamDescriptor } from "@/lib/strategy-params";
import type { BuiltinStrategy, PitMode } from "@/lib/types";

/**
 * 参数表单。
 *
 * 控件一律用**原生**元素 + token 样式：不引新的 shadcn 组件（省掉 CLI 联网与 Base UI
 * 弹层同本项目的圆角/配色对抗），原生控件在可访问性上还少一类焦点陷阱。
 * 代价是 `<select>` 的弹层由系统绘制、不吃主题 token——这是有意的取舍。
 *
 * **参数区由 descriptor 渲染**（M4c-3）：内置策略的描述符来自前端常量、用户策略的来自
 * 源码 schema，两者在 `lib/strategy-params.ts` 已统一——所以这里既不需要知道选的是哪一种，
 * 也不需要为 `bool` 参数另写一套控件（用户策略可以有开关，内置的全是数字）。
 */
export function BacktestForm({
  value,
  errors,
  running,
  groups,
  descriptors,
  onChange,
  onSubmit,
}: {
  value: FormState;
  errors: Record<string, string>;
  running: boolean;
  /** 策略下拉的分组：内置两条 + 我的策略（无用户策略时只有一组） */
  groups: { label: string; items: StrategyOption[] }[];
  /** 当前选中策略的参数表（加载中可能是空表，此时不要拿它判错） */
  descriptors: ParamDescriptor[];
  onChange: (next: FormState) => void;
  onSubmit: () => void;
}) {
  const patch = (part: Partial<FormState>) => onChange({ ...value, ...part });

  /** 下拉里选中的是 `user:<id>` 还是内置策略名 */
  const selected = value.strategy === "user" ? `user:${value.strategyId ?? ""}` : value.strategy;

  return (
    <form
      className="rounded-[var(--radius)] border border-border bg-card p-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="flex flex-wrap items-start gap-x-5 gap-y-3">
        <Field label="策略" htmlFor="bt-strategy">
          <Select
            id="bt-strategy"
            className="w-40"
            value={selected}
            onChange={(event) => {
              const raw = event.target.value;
              const userId = parseUserOption(raw);
              onChange(
                strategySelection(value, userId === null ? (raw as BuiltinStrategy) : "user", userId),
              );
            }}
          >
            {groups.map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.items.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>
        </Field>

        <Field label="标的" htmlFor="bt-symbol" error={errors.symbol}>
          <div className="flex h-8 items-center gap-1.5">
            <Input
              id="bt-symbol"
              className="num w-24"
              inputMode="numeric"
              autoComplete="off"
              value={value.symbol}
              aria-invalid={Boolean(errors.symbol)}
              aria-describedby={errors.symbol ? "bt-symbol-error" : undefined}
              onChange={(event) => patch({ symbol: event.target.value })}
            />
            {SAMPLE_SYMBOLS.map((symbol) => (
              <button
                key={symbol.code}
                type="button"
                title={symbol.code}
                onClick={() => patch({ symbol: symbol.code })}
                className="rounded-[var(--radius)] px-1.5 py-1 text-xs text-ink-3 transition-colors hover:bg-muted hover:text-foreground"
              >
                {symbol.name}
              </button>
            ))}
          </div>
        </Field>

        <Field label="起始日" htmlFor="bt-start">
          <Input
            id="bt-start"
            type="date"
            className="w-36"
            value={value.start}
            onChange={(event) => patch({ start: event.target.value })}
          />
        </Field>

        <Field label="结束日" htmlFor="bt-end">
          <Input
            id="bt-end"
            type="date"
            className="w-36"
            value={value.end}
            onChange={(event) => patch({ end: event.target.value })}
          />
        </Field>

        <Field label="模式" htmlFor="bt-mode">
          <Select
            id="bt-mode"
            className="w-44"
            value={value.pitMode}
            onChange={(event) => patch({ pitMode: event.target.value as PitMode })}
          >
            {PIT_MODES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="mt-3 flex flex-wrap items-start gap-x-5 gap-y-3 border-t border-border pt-3">
        <fieldset className="flex flex-col gap-1">
          <legend className="mb-1 text-xs text-ink-3">成本</legend>
          <div className="flex h-8 items-center gap-4">
            <Check
              id="bt-fees"
              label="佣金与印花税"
              checked={value.fees}
              onChange={(checked) => patch({ fees: checked })}
            />
            <Check
              id="bt-slippage"
              label="滑点"
              checked={value.slippage}
              onChange={(checked) => patch({ slippage: checked })}
            />
          </div>
        </fieldset>

        <Field label="滑点档位 (bps)" htmlFor="bt-bps" error={errors.slippageBps}>
          <Input
            id="bt-bps"
            type="number"
            inputMode="decimal"
            className="num w-28"
            min={0}
            max={100}
            step={0.5}
            value={value.slippageBps}
            aria-invalid={Boolean(errors.slippageBps)}
            aria-describedby={errors.slippageBps ? "bt-bps-error" : undefined}
            onChange={(event) => patch({ slippageBps: event.target.value })}
          />
        </Field>

        {descriptors.map((field) =>
          field.type === "bool" ? (
            <fieldset key={field.key} className="flex flex-col gap-1">
              <legend className="mb-1 text-xs text-ink-3">{field.label}</legend>
              <div className="flex h-8 items-center">
                <Check
                  id={`bt-${field.key}`}
                  label={field.label}
                  checked={Boolean(value.params[field.key])}
                  onChange={(checked) =>
                    patch({ params: { ...value.params, [field.key]: checked } })
                  }
                />
              </div>
            </fieldset>
          ) : (
            <Field
              key={field.key}
              label={field.label}
              htmlFor={`bt-${field.key}`}
              error={errors[field.key]}
            >
              <Input
                id={`bt-${field.key}`}
                type="number"
                inputMode="decimal"
                className="num w-24"
                min={field.min ?? undefined}
                max={field.max ?? undefined}
                value={String(value.params[field.key] ?? "")}
                aria-invalid={Boolean(errors[field.key])}
                aria-describedby={errors[field.key] ? `bt-${field.key}-error` : undefined}
                onChange={(event) =>
                  patch({ params: { ...value.params, [field.key]: event.target.value } })
                }
              />
            </Field>
          ),
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-t border-border pt-3">
        <p className="text-xs text-ink-3">
          区间留空 = 后端缺省：事件驱动从事件窗口起点开跑，其余策略从首根 bar 起。
        </p>
        <Button
          type="submit"
          size="lg"
          disabled={running || hasErrors(errors)}
          aria-busy={running}
        >
          {running ? "运行中…" : "运行"}
        </Button>
      </div>
    </form>
  );
}
