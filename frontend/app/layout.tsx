import type { Metadata } from "next";

import { AuthProvider } from "@/components/auth-provider";
import { ThemeProvider } from "@/components/theme-provider";

import "./globals.css";

export const metadata: Metadata = {
  title: "知策 QuantSage",
  description: "前视偏差为零的 AI 投研 Agent",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // suppressHydrationWarning：主题脚本在 React 接管前就改了 <html> 的 class，
    // 不加这一条会报 hydration 不一致
    <html lang="zh-CN" suppressHydrationWarning>
      {/* AuthProvider 在根布局：登录页也要用它（登录成功后 refresh 一次） */}
      <body className="antialiased">
        <ThemeProvider>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
