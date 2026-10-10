"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { AccountCard } from "@/components/paper/account-card";
import { DecisionCards } from "@/components/paper/decision-cards";
import { DecisionTable } from "@/components/paper/decision-table";
import { EquityPanel, SettlementTable } from "@/components/paper/equity-panel";
import { PaperForm } from "@/components/paper/paper-form";
import { PositionsTable } from "@/components/paper/positions-table";
import { StepControls } from "@/components/paper/step-controls";
import { usePaperAccount, usePaperSessions, useSymbolNames } from "@/components/paper/use-paper";
import { useAccountReports } from "@/components/research/use-report";
import { Select } from "@/components/ui/form-controls";
import { api, describeError } from "@/lib/api";
import type { PaperAccountDetail } from "@/lib/types";

export default function PaperPage() {
  // `useSearchParams` 在生产构建下要求 Suspense 边界（同 factor / optimize 页）
  return (
    <Suspense fallback={<Bootstrap />}>
      <PaperView />
    </Suspense>
  );
}

/**
 * 模拟盘：**纸上交易**——策略出信号、你逐条裁决、按次日开盘价成交、每日按收盘价结算。
 *
 * 页面顺序就是这件事的顺序：账户卡（现在值多少）→ **待审批决策（等你动手的）** →
 * 推进控制 → 净值曲线 → 持仓 → 决策流水 → 每日结算。
 *
 * 与别的页最不同的两点，都在界面上写出来了：
 *   * 批准**不会立刻成交**，要再点一次「推进一天」（成交价在次日开盘才产生）；
 *   * 会话只能回放到**本地行情末端**，界面不假装能模拟到今天。
 */
function PaperView() {
  const params = useSearchParams();
  const router = useRouter();
  const idParam = params.get("id");
  // 已有会话时表单收起来（这一页的主人是账户，不是表单）
  const [showForm, setShowForm] = useState(false);

  const { sessions, reload: reloadSessions } = usePaperSessions();
  // 没指定就打开最近建的那个（列表已按创建时间倒序）；一个都没有才显示创建表单
  const accountId = idParam ?? (sessions?.length ? sessions[0].id : null);
  const { detail, loading, error, busy, step, decide, runToEnd, reload } =
    usePaperAccount(accountId);

  // 闸门区同时给「待审批」与「已批准」：后者不撤走，否则批完卡片当场消失、看不到回执
  const gate = useMemo(
    () =>
      detail
        ? detail.decisions.filter((d) => d.status === "pending" || d.status === "approved")
        : [],
    [detail],
  );
  const waiting = gate.filter((d) => d.status === "approved").length;

  const codes = useMemo(() => {
    if (!detail) return [];
    return [...new Set([...detail.account.config.symbols, ...detail.positions.map((p) => p.symbol)])];
  }, [detail]);
  const names = useSymbolNames(codes);

  const onCreated = useCallback(
    (created: PaperAccountDetail) => {
      void reloadSessions();
      router.replace(`/paper?id=${created.account.id}`, { scroll: false });
    },
    [reloadSessions, router],
  );

  const changeSession = useCallback(
    (id: string) => {
      router.replace(`/paper?id=${id}`, { scroll: false });
    },
    [router],
  );

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <h1 className="font-heading text-xl font-semibold">模拟盘</h1>
      <p className="mt-1 text-sm text-ink-3">
        纸上交易：策略出信号 → 你逐条裁决 → 按<strong className="font-medium">次日开盘价</strong>
        成交 → 每日按收盘价结算。撮合与费用跟回测同一套口径——
        <strong className="font-medium">全部批准的会话，成交逐笔等于一次回测</strong>。
      </p>

      {error ? (
        <p
          role="alert"
          className="mt-4 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
          <button type="button" onClick={() => void reload()} className="ml-2 underline">
            重试
          </button>
        </p>
      ) : null}

      {sessions && sessions.length ? (
        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
          <Select
            aria-label="切换会话"
            value={accountId ?? ""}
            onChange={(event) => changeSession(event.target.value)}
            className="w-[16rem]"
          >
            {sessions.map((session) => (
              <option key={session.id} value={session.id}>
                {session.name}（模拟到 {session.as_of}）
              </option>
            ))}
          </Select>
          {detail ? (
            <span className="text-xs text-ink-3">
              {detail.account.status === "finished" ? "已跑到区间末端 · " : ""}
              窗口 {detail.progress.start} → {detail.progress.end}
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => setShowForm((open) => !open)}
            className="ml-auto h-8 rounded-[var(--radius)] border border-border px-3 text-xs hover:bg-muted"
          >
            {showForm ? "收起" : "＋ 新建会话"}
          </button>
        </div>
      ) : null}

      {showForm && sessions && sessions.length ? (
        <PaperForm
          onCreated={(created) => {
            setShowForm(false);
            onCreated(created);
          }}
        />
      ) : null}

      {detail ? (
        <div className="mt-4 flex flex-col gap-6">
          <AccountCard detail={detail} />

          {/* 出研报：绩效研报是账户的产物，入口跟着账户走（页头不加导航项，M5b 记过窄屏溢出） */}
          <ReportEntry accountId={detail.account.id} sessionName={detail.account.name} />

          <Section
            title={
              detail.pending.length
                ? `决策闸门（待审批 ${detail.pending.length}）`
                : "决策闸门"
            }
            hint={
              waiting
                ? `批准只改状态；成交发生在下一交易日的开盘（${waiting} 条已批准待成交）`
                : "批准只改状态；成交发生在下一交易日的开盘"
            }
          >
            <DecisionCards
              decisions={gate}
              names={names}
              busy={busy}
              onDecide={(id, action) => void decide(id, action)}
            />
            <StepControls
              detail={detail}
              busy={busy}
              onStep={() => void step()}
              onRun={(approve) => void runToEnd(approve)}
            />
          </Section>

          <Section title="净值曲线" hint="每日按收盘价结算；▲ 买入、▼ 卖出为成交日">
            <EquityPanel curve={detail.equity_curve} decisions={detail.decisions} />
          </Section>

          <Section title="持仓" hint="每只标的各拿一份等额额度">
            <PositionsTable positions={detail.positions} names={names} />
          </Section>

          <Section title="决策流水" hint="六态：待审批 / 已批准 / 已成交 / 已驳回 / 未审批过期 / 已批准未成交">
            <DecisionTable decisions={detail.decisions} names={names} />
          </Section>

          <Section title="每日结算" hint="现金 + 持仓市值 = 净值；停牌日按最近一次已知收盘价估值">
            <SettlementTable curve={detail.equity_curve} />
          </Section>
        </div>
      ) : loading ? (
        <Bootstrap />
      ) : sessions && !sessions.length ? (
        <>
          <h2 className="mt-6 font-heading text-base font-semibold">建一个会话</h2>
          <PaperForm onCreated={onCreated} />
        </>
      ) : null}
    </main>
  );
}

function Bootstrap() {
  return (
    <div className="mt-8 h-40 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
  );
}

/**
 * 「出研报」入口：生成 → 跳到报告页；**同一批数据已有报告时不再重算**（后端幂等复用，
 * 回执 `reused=true`），这时直接把已有那份打开——文案照实说，不假装又算了一遍。
 */
function ReportEntry({ accountId, sessionName }: { accountId: string; sessionName: string }) {
  const router = useRouter();
  const { reports, reload } = useAccountReports(accountId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reused, setReused] = useState(false);

  const latest = reports?.[0] ?? null;

  const generate = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.createReport(accountId);
      setReused(result.reused);
      await reload();
      router.push(`/research/${result.id}`);
    } catch (cause) {
      setError(describeError(cause, "出研报失败，稍后重试"));
    } finally {
      setBusy(false);
    }
  }, [accountId, reload, router]);

  return (
    <div className="flex flex-wrap items-center gap-3" data-report-entry>
      <button
        type="button"
        disabled={busy || !accountId}
        onClick={() => void generate()}
        className="h-8 rounded-[var(--radius)] bg-brand px-3 text-xs text-brand-ink disabled:opacity-50"
      >
        {busy ? "生成中…" : "出研报"}
      </button>
      <span className="text-xs text-ink-3">
        绩效研报：指标 / 归因 / 逐笔复盘（含教训）/ 证据链，可分享给未登录的人看
      </span>
      {latest ? (
        <a
          href={`/research/${latest.id}`}
          className="text-xs underline"
          data-report-entry="latest"
        >
          看最近一份（{latest.created_at.slice(0, 10)}
          {latest.share_token ? " · 已分享" : ""}）
        </a>
      ) : null}
      {reused ? (
        <span className="text-xs text-ink-3">
          这批数据已有报告，打开的就是那一份（未重算、未再调用模型）
        </span>
      ) : null}
      {error ? (
        <span role="alert" className="text-xs text-destructive">
          {error}
          <span className="ml-1 text-ink-3">（会话「{sessionName}」）</span>
        </span>
      ) : null}
    </div>
  );
}
