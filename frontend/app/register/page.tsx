"use client";

/**
 * 注册页。注册端点直接签发会话（与登录同一条签发逻辑），所以注册成功即登录态，
 * 不需要再走一次登录。
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { AuthShell } from "@/components/auth/auth-shell";
import { AuthLink, Field, FormError, TextInput } from "@/components/auth/form-parts";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { credentials, hasErrors, validate } from "@/lib/auth-form";
import type { AuthErrors, AuthForm } from "@/lib/auth-form";

export default function RegisterPage() {
  const router = useRouter();
  const { refresh } = useAuth();

  const [form, setForm] = useState<AuthForm>({ email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<AuthErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    const found = validate(form, true);
    setErrors(found);
    if (hasErrors(found) || busy) return;

    setBusy(true);
    setServerError(null);
    try {
      const body = credentials(form);
      await api.register(body.email, body.password);
      await refresh();
      router.replace("/");
    } catch (cause) {
      setServerError(cause instanceof ApiError ? cause.message : "连接失败，请确认后端已启动");
      setBusy(false);
    }
  }

  return (
    <AuthShell>
      <h2 className="font-heading text-2xl font-semibold tracking-tight">注册</h2>
      <p className="mt-1.5 text-sm text-ink-2">创建账号后即可开始研究与回测。</p>

      <form
        className="mt-7 flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field label="邮箱" htmlFor="register-email" error={errors.email}>
          <TextInput
            id="register-email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            autoFocus
            value={form.email}
            aria-invalid={Boolean(errors.email)}
            onChange={(event) => setForm({ ...form, email: event.target.value })}
          />
        </Field>

        <Field label="密码" htmlFor="register-password" error={errors.password}>
          <TextInput
            id="register-password"
            type="password"
            autoComplete="new-password"
            placeholder="至少 8 位"
            value={form.password}
            aria-invalid={Boolean(errors.password)}
            onChange={(event) => setForm({ ...form, password: event.target.value })}
          />
        </Field>

        <Field label="确认密码" htmlFor="register-confirm" error={errors.confirm}>
          <TextInput
            id="register-confirm"
            type="password"
            autoComplete="new-password"
            placeholder="再输一次"
            value={form.confirm}
            aria-invalid={Boolean(errors.confirm)}
            onChange={(event) => setForm({ ...form, confirm: event.target.value })}
          />
        </Field>

        <FormError message={serverError} />

        <Button type="submit" size="lg" className="mt-1 w-full" disabled={busy}>
          {busy ? "注册中…" : "注册并登录"}
        </Button>
      </form>

      <p className="mt-5 text-center text-xs text-ink-3">
        已有账号？<AuthLink href="/login">登录</AuthLink>
      </p>
    </AuthShell>
  );
}
