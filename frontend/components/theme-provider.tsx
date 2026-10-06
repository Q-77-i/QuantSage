"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ComponentProps } from "react";

/**
 * 主题：**浅色为主主题**（design brief §6），不跟随系统偏好；切换结果落在 localStorage。
 *
 * 切换只换 `app/globals.css` 里 `:root` / `.dark` 的颜色值，版式 token 不动。
 */
export function ThemeProvider({ children, ...props }: ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="light"
      enableSystem={false}
      disableTransitionOnChange={false}
      {...props}
    >
      {children}
    </NextThemesProvider>
  );
}
