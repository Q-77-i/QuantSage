"use client";

import { ChevronDown } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 表单的四件基本控件：标签位 + 原生 select / input / checkbox。
 *
 * 一律用**原生**元素 + token 样式：不引新的 shadcn 组件（省掉 CLI 联网与 Base UI
 * 弹层同本项目的圆角/配色对抗），原生控件在可访问性上还少一类焦点陷阱。
 * 代价是 `<select>` 的弹层由系统绘制、不吃主题 token——这是有意的取舍。
 *
 * 原来长在 `backtest-form.tsx` 里（M5b 之前只有它一个消费方）；网格 / 批量表单要的是
 * 同一套观感与同一组 `htmlFor`/`aria-describedby` 约定，故上提到这里共享——
 * 复制一份的话，两边的错误提示样式迟早长歪。
 */

export function Field({
  label,
  htmlFor,
  error,
  children,
}: {
  label: string;
  htmlFor: string;
  error?: string;
  children: ReactNode;
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
export function Select({ className, children, ...props }: ComponentProps<"select">) {
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

export function Input({ className, ...props }: ComponentProps<"input">) {
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

export function Check({
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
