"use client";

import { ChevronDown } from "lucide-react";
import type { ComponentProps } from "react";

import { Button } from "@/components/ui/button";
import {
  PARAMS,
  PIT_MODES,
  STRATEGIES,
  SYMBOLS,
  hasErrors,
  switchStrategy,
} from "@/lib/backtest-form";
import type { FormState } from "@/lib/backtest-form";
import { cn } from "@/lib/utils";
import type { PitMode, Strategy } from "@/lib/types";

/**
 * 参数表单。
 *
 * 控件一律用**原生**元素 + token 样式：不引新的 shadcn 组件（省掉 CLI 联网与 Base UI
 * 弹层同本项目的圆角/配色对抗），原生控件在可访问性上还少一类焦点陷阱。
 * 代价是 `<select>` 的弹层由系统绘制、不吃主题 token——这是有意的取舍。
 */
export function BacktestForm({
  value,
  errors,
  running,
  onChange,
  onSubmit,
}: {
  value: FormState;
  errors: Record<string, string>;
  running: boolean;
  onChange: (next: FormState) => void;
  onSubmit: () => void;
}) {
  const patch = (part: Partial<FormState>) => onChange({ ...value, ...part });

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
            className="w-32"
            value={value.strategy}
            onChange={(event) =>
              onChange(switchStrategy(value, event.target.value as Strategy))
            }
          >
            {STRATEGIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="标的" htmlFor="bt-symbol">
          <Select
            id="bt-symbol"
            className="w-36"
            value={value.symbol}
            onChange={(event) => patch({ symbol: event.target.value })}
          >
            {SYMBOLS.map((symbol) => (
              <option key={symbol.code} value={symbol.code}>
                {symbol.code} {symbol.name}
              </option>
            ))}
          </Select>
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

        {PARAMS[value.strategy].map((field) => (
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
              min={field.min}
              max={field.max}
              step={field.step}
              value={value.params[field.key] ?? ""}
              aria-invalid={Boolean(errors[field.key])}
              aria-describedby={errors[field.key] ? `bt-${field.key}-error` : undefined}
              onChange={(event) =>
                patch({ params: { ...value.params, [field.key]: event.target.value } })
              }
            />
          </Field>
        ))}
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

function Field({
  label,
  htmlFor,
  error,
  children,
}: {
  label: string;
  htmlFor: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-xs text-ink-3">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${htmlFor}-error`} className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** 原生 select 的弹层由系统绘制；闭合态得自己补箭头与背景（预检不重置表单底色）。 */
function Select({ className, children, ...props }: ComponentProps<"select">) {
  return (
    <div className={cn("relative", className)}>
      <select
        {...props}
        className={cn(
          "h-8 w-full appearance-none rounded-[var(--radius)] border border-border bg-card pr-7 pl-2 text-sm text-foreground",
          "focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none",
        )}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute top-1/2 right-1.5 size-3.5 -translate-y-1/2 text-ink-3"
      />
    </div>
  );
}

function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      {...props}
      className={cn(
        "h-8 rounded-[var(--radius)] border border-border bg-card px-2 text-sm text-foreground",
        "focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none",
        "aria-invalid:border-destructive",
        className,
      )}
    />
  );
}

function Check({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <span className="flex items-center gap-1.5 text-sm">
      <input
        id={id}
        type="checkbox"
        className="size-3.5 accent-primary"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <label htmlFor={id} className="cursor-pointer">
        {label}
      </label>
    </span>
  );
}
