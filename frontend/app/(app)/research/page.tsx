"use client";

/**
 * 深度研究入口（M8c）：`/research`。
 *
 * 上半是**新建**（问题 / 标的 / 数据截止），下半是**我的运行**。提交后**原地看着它跑**——
 * 帧载荷与落库事件同形，所以这一屏和运行页用的是同一套展示组件，跑完给一个跳转入口。
 *
 * `mode=auto` 时服务端会先判快/深路径：判成快路径就只回一帧 `route`，这里如实告诉用户
 * 「这问题走对话更快」并给链接——**不硬跑一次多 Agent**（PRD：常开多 Agent 是 3.7 倍成本）。
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApprovalCard } from "@/components/agent-run/approval-card";
import { EventTimeline, RunView } from "@/components/agent-run/run-view";
import { useRunStream } from "@/components/agent-run/use-run";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/form-controls";
import { api, describeError } from "@/lib/api";
import { pendingApproval, type RunStatus } from "@/lib/agent-run";
import type { ResearchRunSummary } from "@/lib/types";

export default function ResearchIndexPage() {
  const [question, setQuestion] = useState("分析一下贵州茅台现在值不值得买");
  const [symbols, setSymbols] = useState("");
  const [asOf, setAsOf] = useState("");
  const [mode, setMode] = useState<"auto" | "deep">("auto");
  const [runs, setRuns] = useState<ResearchRunSummary[]>([]);
  const [listError, setListError] = useState<string | null>(null);

  const stream = useRunStream();

  const loadRuns = useCallback(async () => {
    try {
      const data = await api.researchRuns();
      setRuns(data.runs);
      setListError(null);
    } catch (cause) {
      setListError(describeError(cause, "运行列表拉不到"));
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const start = () => {
    void stream.start({
      question,
      symbols: symbols
        .split(/[,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean),
      as_of: asOf ? `${asOf}T15:00:00+08:00` : null,
      mode,
    });
  };

  return (
    <main className="mx-auto flex max-w-[1400px] flex-col gap-8 px-4 py-6">
      <header className="flex flex-col gap-1">
        <h1 className="font-heading text-lg font-semibold">深度研究</h1>
        <p className="text-sm text-ink-2">
          多 Agent 并行取证 → 多空对辩（≤3 轮）→ 单点成文 → 代码化风控闸门。全程可见、可回溯，
          结论接研报容器（分享 / 导出 / 重放一致）。
        </p>
      </header>

      <section className="flex flex-col gap-3 rounded-[var(--radius)] border border-border p-3">
        <label className="flex flex-col gap-1 text-sm">
          研究问题
          <Input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            maxLength={500}
            placeholder="例：分析一下贵州茅台现在值不值得买"
          />
        </label>
        <div className="flex flex-wrap gap-3">
          <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-sm">
            标的（可留空，从问题里解析）
            <Input
              value={symbols}
              onChange={(event) => setSymbols(event.target.value)}
              placeholder="600519,300750"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            数据截止
            <Input
              type="date"
              value={asOf}
              onChange={(event) => setAsOf(event.target.value)}
              className="w-40"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            路径
            <Select value={mode} onChange={(event) => setMode(event.target.value as "auto" | "deep")}>
              <option value="auto">自动判定（推荐）</option>
              <option value="deep">强制深度研究</option>
            </Select>
          </label>
        </div>
        <p className="text-xs text-ink-3">
          时点决定这次研究
          <strong className="font-medium">看得到哪些信息</strong>：取证一律按{" "}
          <code className="num">available_at ≤ 时点</code> 过滤，留空取行情末端。
        </p>
        <div className="flex items-center gap-3">
          <Button disabled={stream.running || question.trim().length < 2} onClick={start}>
            {stream.running ? "运行中…" : "开始研究"}
          </Button>
          {stream.runId ? (
            <Link href={`/research/runs/${stream.runId}`} className="text-sm hover:underline">
              打开运行页（可续看 / 审批）
            </Link>
          ) : null}
        </div>
      </section>

      {stream.route && stream.route.path !== "deep" ? (
        <p className="text-sm text-ink-2">
          这问题更适合直接问：{stream.route.reason}
          <Link href="/" className="text-brand hover:underline">
            去对话页
          </Link>
        </p>
      ) : null}
      {stream.error ? <p className="text-sm text-destructive">{stream.error}</p> : null}

      {stream.events.length ? (
        <RunView
          question={question}
          subject={{ symbols: symbols.split(/[,，\s]+/).filter(Boolean), as_of: asOf || undefined }}
          status={stream.status as RunStatus}
          events={stream.events}
          budget={stream.budget}
          gaps={[]}
          aside={
            <div className="flex flex-col gap-3">
              {stream.status === "awaiting_approval" && pendingApproval(stream.events) && stream.runId ? (
                <ApprovalCard
                  payload={pendingApproval(stream.events)!}
                  busy={stream.running}
                  onDecide={(choice) => void stream.resume(stream.runId!, choice)}
                />
              ) : null}
              {stream.reportId ? (
                <Link href={`/research/${stream.reportId}`}>
                  <Button>看这份研报</Button>
                </Link>
              ) : null}
              <EventTimeline events={stream.events} />
            </div>
          }
        />
      ) : null}

      <section className="flex flex-col gap-2">
        <h2 className="font-heading text-base font-semibold">我的运行</h2>
        {listError ? <p className="text-sm text-destructive">{listError}</p> : null}
        {runs.length === 0 ? <p className="text-sm text-ink-3">还没有跑过深度研究。</p> : null}
        <ul className="flex flex-col divide-y divide-border">
          {runs.map((run) => (
            <li key={run.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
              <Link href={`/research/runs/${run.id}`} className="min-w-0 flex-1 truncate hover:underline">
                {run.question}
              </Link>
              <span className="num text-xs text-ink-3">{run.status}</span>
              <span className="num text-xs text-ink-3">
                {run.created_at?.slice(0, 16).replace("T", " ")}
              </span>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="self-start text-xs text-ink-3 hover:underline"
          onClick={() => void loadRuns()}
        >
          刷新
        </button>
      </section>
    </main>
  );
}
