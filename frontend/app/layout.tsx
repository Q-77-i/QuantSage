import type { Metadata } from "next";

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
      <body className="antialiased">{<ThemeProvider>{children}</ThemeProvider>}</body>
    </html>
  );
}
