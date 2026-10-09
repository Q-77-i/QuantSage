"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import type { FactorQueryInput } from "@/lib/factor-report";
import type { FactorReport } from "@/lib/types";

/**
 * 因子报告的状态机：一次请求一份报告（同步端点，没有帧要收）。
 *
 * 在途作废用**序号**：连点两次运行、或切因子源时上一发还没回来，晚到的旧结果不能覆盖新的
 * （`use-optimize` 同一套做法——那里是帧，这里是整份报告，道理相同）。
 */
export function useFactor() {
  const [report, setReport] = useState<FactorReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const run = useCallback(async (input: FactorQueryInput) => {
    const ticket = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await api.factorReport(input);
      if (ticket !== seq.current) return;
      setReport(next);
    } catch (cause) {
      if (ticket !== seq.current) return;
      setError(describeError(cause, "取因子报告失败"));
      setReport(null);
    } finally {
      if (ticket === seq.current) setLoading(false);
    }
  }, []);

  // 卸下时作废在途回调，避免对已卸载组件 setState
  useEffect(() => () => void ++seq.current, []);

  return { report, loading, error, run };
}
