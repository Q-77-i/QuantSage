"use client";

/**
 * 登录 / 注册两页共用的排版件。
 *
 * 控件与 `components/backtest/backtest-form.tsx` 同口径：原生元素 + token 样式，
 * 不引新的 shadcn 组件（省掉 CLI 联网与 Base UI 弹层同本项目圆角/配色的对抗）。
 */

import Link from "next/link";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/** 居中卡片：两页只有一张表单，不铺满屏宽（宽度上限与正文列宽一致）。 */
export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4 py-10">
      <h1 className="font-heading text-2xl tracking-tight">{title}</h1>
      <p className="mt-1 text-sm text-ink-2">{subtitle}</p>

      <div className="mt-6 rounded-[var(--radius)] border border-border bg-card p-5">
        {children}
      </div>

      <p className="mt-4 text-center text-xs text-ink-3">{footer}</p>
    </main>
  );
}

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

export function TextInput({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      {...props}
      className={cn(
        "h-8 w-full rounded-[var(--radius)] border border-border bg-card px-2 text-sm text-foreground",
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
    <p role="alert" className="rounded-[var(--radius)] bg-destructive/10 px-3 py-2 text-xs text-destructive">
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
