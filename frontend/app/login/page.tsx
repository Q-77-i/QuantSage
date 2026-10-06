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

import { AuthCard, AuthLink, Field, FormError, TextInput } from "@/components/auth/form-parts";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { credentials, hasErrors, safeNextPath, validate } from "@/lib/auth-form";
import type { AuthErrors, AuthForm } from "@/lib/auth-form";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { refresh } = useAuth();

  const [form, setForm] = useState<AuthForm>({ email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<AuthErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
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
      await api.login(body.email, body.password);
      await refresh();
      router.replace(safeNextPath(params.get("next")));
    } catch (cause) {
      setServerError(cause instanceof ApiError ? cause.message : "连接失败，请确认后端已启动");
      setBusy(false);
    }
  }

  return (
    <AuthCard
      title="登录"
      subtitle="登录后可继续历史会话，并使用回测与自选股。"
      footer={
        <>
          还没有账号？<AuthLink href="/register">注册</AuthLink>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={handleSubmit}>
        <Field label="邮箱" htmlFor="login-email" error={errors.email}>
          <TextInput
            id="login-email"
            type="email"
            autoComplete="email"
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
            value={form.password}
            aria-invalid={Boolean(errors.password)}
            onChange={(event) => setForm({ ...form, password: event.target.value })}
          />
        </Field>

        <FormError message={serverError} />

        <Button type="submit" className="mt-1 w-full" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </Button>
      </form>
    </AuthCard>
  );
}
