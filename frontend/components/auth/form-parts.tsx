"use client";

/**
 * 登录 / 注册两页共用的表单件。
 *
 * 控件与 `components/backtest/backtest-form.tsx` 同口径：原生元素 + token 样式，
 * 不引新的 shadcn 组件（省掉 CLI 联网与 Base UI 弹层同本项目圆角/配色的对抗）。
 * 与回测表单的唯一差别是行高（40px vs 32px）：登录页一屏只有两个字段，
 * 不必按数据区的密度压。
 */

import Link from "next/link";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export function Field({
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
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-xs text-ink-2">
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

export function TextInput({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      {...props}
      className={cn(
        "h-10 w-full rounded-[var(--radius)] border border-border bg-background px-3 text-sm text-foreground",
        "placeholder:text-ink-3",
        "focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none",
        "aria-invalid:border-destructive",
        className,
      )}
    />
  );
}

/** 服务端错误（401 / 409 / 503）走这里，与字段级错误分开显示。 */
export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-[var(--radius)] border border-destructive/25 bg-destructive/10 px-3 py-2 text-xs text-destructive"
    >
      {message}
    </p>
  );
}

export function AuthLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} className="text-primary underline-offset-4 hover:underline">
      {children}
    </Link>
  );
}
