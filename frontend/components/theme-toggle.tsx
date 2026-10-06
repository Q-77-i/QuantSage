"use client";

import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";

/** 浅 / 深主题切换。 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // 服务端不知道用户选了什么主题，挂载前不渲染具体文案，避免 hydration 不一致
  useEffect(() => setMounted(true), []);

  const isDark = mounted && resolvedTheme === "dark";

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label="切换浅色与深色主题"
    >
      {isDark ? "浅色" : "深色"}
    </Button>
  );
}
