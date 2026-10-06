"use client";

import Link from "next/link";

/**
 * 我的策略：**M4 之前的占位**。
 *
 * 策略的归属列（`user_id`）与保存/读取随 M4 的策略工作台一起做——现在写一个只有壳的
 * 列表出来，等于提前定死一份没有编辑器、没有沙箱的存储契约。这一格先如实空着。
 */
export function StrategiesPanel() {
  return (
    <p className="text-sm text-ink-2">
      策略工作台在 M4：在线编辑器、参数配置、前视泄漏静态检查。
      <Link href="/backtest" className="ml-1 text-brand hover:underline">
        先看回测
      </Link>
    </p>
  );
}
