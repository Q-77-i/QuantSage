"use client";

/**
 * 研报的数据钩子：登录态一份、公开只读一份、账户报告列表一份。
 *
 * 三个都**不自动刷新**——报告是冻结产物，「你看到的就是当时生成的那一份」是它的承诺；
 * 轮询只会让人怀疑手里的数变了。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import type { PublicReport, ReportEnvelope, ReportSummary } from "@/lib/types";

/** 序号戳作废旧流水（同 `use-backtest`：快速切换报告时，后到的旧响应不能覆盖新的） */
function useSeq() {
  const ref = useRef(0);
  return ref;
}

export function useReport(id: string | null) {
  const [envelope, setEnvelope] = useState<ReportEnvelope | null>(null);
  const [loading, setLoading] = useState(Boolean(id));
  const [error, setError] = useState<string | null>(null);
  const seqRef = useSeq();

  const load = useCallback(async () => {
    if (!id) return;
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await api.report(id);
      if (seq !== seqRef.current) return; // 已被更新的请求作废
      setEnvelope(data);
    } catch (cause) {
      if (seq !== seqRef.current) return;
      setError(describeError(cause, "报告打不开，稍后重试"));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [id, seqRef]);

  useEffect(() => {
    void load();
  }, [load]);

  return { envelope, setEnvelope, loading, error, reload: load };
}

export function usePublicReport(token: string | null) {
  const [report, setReport] = useState<PublicReport | null>(null);
  const [loading, setLoading] = useState(Boolean(token));
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      setReport(await api.publicReport(token));
    } catch (cause) {
      setError(describeError(cause, "分享链接打不开"));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  return { report, loading, error, reload: load };
}

export function useAccountReports(accountId: string | null) {
  const [reports, setReports] = useState<ReportSummary[] | null>(null);

  const load = useCallback(async () => {
    if (!accountId) {
      setReports(null);
      return;
    }
    try {
      const data = await api.reports(accountId);
      setReports(data.reports);
    } catch {
      setReports([]); // 列表拿不到不该挡住「出研报」按钮（生成端点自己会报错）
    }
  }, [accountId]);

  useEffect(() => {
    void load();
  }, [load]);

  return { reports, reload: load };
}
