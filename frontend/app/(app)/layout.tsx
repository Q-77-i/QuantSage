"use client";

/**
 * 受保护页的路由组（M1）：对话页与回测页都挂在这里。
 *
 * 守卫只在这一个落点：未登录一律重定向登录页并带上回跳地址。把守卫写进每个页面
 * 会漏（新加页面时最容易忘），写进中间件又不行——会话 cookie 在 8000 端口上，
 * `middleware.ts` 看不见它。
 */

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { AppHeader } from "@/components/app-header";
import { useAuth } from "@/components/auth-provider";

export default function ProtectedLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!loading && !user) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    }
  }, [loading, user, router, pathname]);

  // 探测中与「即将跳走」都渲染骨架：避免受保护内容闪一下再消失
  if (loading || !user) return <Bootstrap />;

  return (
    <>
      <AppHeader />
      {children}
    </>
  );
}

function Bootstrap() {
  return (
    <div className="mx-auto flex h-dvh max-w-[1400px] items-center justify-center">
      <span className="h-4 w-32 animate-pulse rounded-[var(--radius)] bg-muted" />
    </div>
  );
}
