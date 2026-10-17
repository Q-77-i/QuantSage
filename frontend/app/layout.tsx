import type { Metadata } from "next";
import { headers } from "next/headers";

import { AuthProvider } from "@/components/auth-provider";
import { ThemeProvider } from "@/components/theme-provider";

import "./globals.css";

export const metadata: Metadata = {
  title: "知策 QuantSage",
  description: "前视偏差为零的 AI 投研 Agent",
};

// CSP nonce 只能在动态渲染时注入。根布局强制动态，避免构建期静态页的内联脚本
// 没有 nonce、上线后被自己的 CSP 拦掉。本项目是登录后的本地看板，静态化收益很低。
export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // next-themes 会在页面里注入一段主题脚本；它支持 nonce，但不会自己从 CSP 头里取。
  // 不传的话生产 CSP 会拦掉这段脚本（实测：script-src-elem 拦 inline）。
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    // suppressHydrationWarning：主题脚本在 React 接管前就改了 <html> 的 class，
    // 不加这一条会报 hydration 不一致
    <html lang="zh-CN" suppressHydrationWarning>
      {/* AuthProvider 在根布局：登录页也要用它（登录成功后 refresh 一次） */}
      <body className="antialiased">
        <ThemeProvider nonce={nonce}>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
