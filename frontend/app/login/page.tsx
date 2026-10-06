"use client";

/**
 * 登录页。
 *
 * `next` 回跳地址来自查询串，必须过 `safeNextPath` 白名单——直接 `router.replace` 会变成
 * 开放重定向（`?next=https://evil.example`）。
 *
 * `useSearchParams` 需要 Suspense 边界：静态预渲染阶段它拿不到查询串，Next 会直接报错。
 */

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { AuthShell } from "@/components/auth/auth-shell";
import { AuthLink, Field, FormError, TextInput } from "@/components/auth/form-parts";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { credentials, hasErrors, safeNextPath, validate } from "@/lib/auth-form";
import type { AuthErrors, AuthForm } from "@/lib/auth-form";

/**
 * 外壳与标题留在 Suspense **之外**：生产构建里 `useSearchParams` 会把边界内的整棵子树
 * 降级成纯客户端渲染，包在外面会让品牌面与标题一起白一下（dev 下看不出这个问题）。
 */
export default function LoginPage() {
  return (
    <AuthShell>
      <h2 className="font-heading text-2xl font-semibold tracking-tight">登录</h2>
      <p className="mt-1.5 text-sm text-ink-2">继续你的研究会话与回测记录。</p>

      <Suspense fallback={<FormSkeleton />}>
        <LoginForm />
      </Suspense>

      <p className="mt-5 text-center text-xs text-ink-3">
        还没有账号？<AuthLink href="/register">注册</AuthLink>
      </p>
    </AuthShell>
  );
}

/** 表单骨架：高度贴近真实表单，避免加载完成时布局跳动。 */
function FormSkeleton() {
  return (
    <div aria-hidden className="mt-7 flex flex-col gap-4">
      <div className="h-[74px] animate-pulse rounded-[var(--radius)] bg-muted" />
      <div className="h-[74px] animate-pulse rounded-[var(--radius)] bg-muted" />
      <div className="h-9 animate-pulse rounded-[var(--radius)] bg-muted" />
    </div>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { refresh } = useAuth();

  const [form, setForm] = useState<AuthForm>({ email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<AuthErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const found = validate(form);
    setErrors(found);
    if (hasErrors(found) || busy) return;

    setBusy(true);
    setServerError(null);
    try {
      const body = credentials(form);
      await api.login(body.email, body.password, remember);
      await refresh();
      router.replace(safeNextPath(params.get("next")));
    } catch (cause) {
      setServerError(cause instanceof ApiError ? cause.message : "连接失败，请确认后端已启动");
      setBusy(false);
    }
  }

  return (
    <form className="mt-7 flex flex-col gap-4" onSubmit={handleSubmit}>
      <Field label="邮箱" htmlFor="login-email" error={errors.email}>
        <TextInput
          id="login-email"
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          autoFocus
          value={form.email}
          aria-invalid={Boolean(errors.email)}
          onChange={(event) => setForm({ ...form, email: event.target.value })}
        />
      </Field>

      <Field label="密码" htmlFor="login-password" error={errors.password}>
        <TextInput
          id="login-password"
          type="password"
          autoComplete="current-password"
          placeholder="至少 8 位"
          value={form.password}
          aria-invalid={Boolean(errors.password)}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
        />
      </Field>

      {/* 记住我：只影响 cookie 是持久还是随浏览器关闭失效，不动 token 有效期 */}
      <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-2 select-none">
        <input
          type="checkbox"
          checked={remember}
          onChange={(event) => setRemember(event.target.checked)}
          className="size-3.5 accent-primary"
        />
        记住我
      </label>

      <FormError message={serverError} />

      <Button type="submit" size="lg" className="mt-1 w-full" disabled={busy}>
        {busy ? "登录中…" : "登录"}
      </Button>
    </form>
  );
}
