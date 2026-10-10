"use client";

import { useState } from "react";

import { StatusBadge } from "@/components/paper/status-badge";
import { canDecide, decisionCard } from "@/lib/paper";
import type { PaperDecision } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 决策闸门：**待审批 + 已批准**两张面孔在同一处（这一页唯一「等你动手」的东西）。
 *
 * 「已批准」的卡片**不撤走**——第一版只渲染待审批，结果点完「批准」卡片当场消失，
 * 用户既看不到回执，也看不到自己刚批的那张在等什么（界面验证逮到）。它现在的样子是：
 * 徽章变「已批准（待次日开盘成交）」，按钮换成一句「点推进一天才会成交」。
 *
 * 两句话必须写在按钮旁边（这是本功能最容易被误解的地方）：
 *   * **批准后按次日开盘价成交**——批的这一刻成交价还不存在，界面不能让人以为点完就成交了；
 *   * **批准只是改状态**，还要「推进一天」才会成交。
 *
 * 数量与价格都标「预计」：`est_qty` 是按决策日收盘价估的，真正的股数在次日开盘价上重算
 * （跳空日两者会不同，成交后卡片会把实际成交那一行也显示出来）。
 */
export function DecisionCards({
  decisions,
  names,
  busy,
  onDecide,
}: {
  decisions: PaperDecision[];
  names: Record<string, string>;
  busy: boolean;
  onDecide: (id: string, action: "approve" | "reject") => void;
}) {
  if (!decisions.length) {
    return (
      <p className="mt-3 rounded-[var(--radius)] border border-dashed border-border px-4 py-6 text-center text-sm text-ink-2">
        没有待审批的决策。点「推进一天」看看下一个交易日有没有信号。
      </p>
    );
  }

  return (
    <ul className="mt-3 flex flex-col gap-3">
      {decisions.map((decision) => (
        <DecisionCardItem
          key={decision.id}
          decision={decision}
          name={names[decision.symbol]}
          busy={busy}
          onDecide={onDecide}
        />
      ))}
    </ul>
  );
}

function DecisionCardItem({
  decision,
  name,
  busy,
  onDecide,
}: {
  decision: PaperDecision;
  name?: string;
  busy: boolean;
  onDecide: (id: string, action: "approve" | "reject") => void;
}) {
  const [showSources, setShowSources] = useState(false);
  const card = decisionCard(decision);
  const isBuy = decision.side === "buy";

  return (
    <li
      data-decision={decision.id}
      className="rounded-[var(--radius)] border border-border p-3.5"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className={cn("font-heading text-sm font-semibold", isBuy ? "text-up" : "text-down")}>
          {card.sideLabel}
        </span>
        <span className="num font-medium">{decision.symbol}</span>
        {name && name !== decision.symbol ? (
          <span className="text-sm text-ink-2">{name}</span>
        ) : null}
        <StatusBadge status={decision.status} label={decision.status_label} />
        <span className="num ml-auto text-xs text-ink-3">决策日 {decision.trade_date}</span>
      </div>

      <p className="num mt-2 text-sm">{card.estimate}</p>
      {card.actual ? <p className="num mt-1 text-sm text-ink-2">{card.actual}</p> : null}
      <p className="mt-1 text-sm text-ink-2">{card.reason}</p>
      {card.reject ? (
        <p className="mt-1 text-sm text-destructive">未成交原因：{card.reject}</p>
      ) : null}

      {card.sources.length ? (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowSources((open) => !open)}
            className="text-xs text-brand hover:underline"
            aria-expanded={showSources}
          >
            {showSources ? "收起来源" : `来源（${card.sources.length} 项）`}
          </button>
          {showSources ? (
            <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-[var(--radius)] bg-muted px-3 py-2 text-xs">
              {card.sources.map((row) => (
                <div key={row.label} className="col-span-2 grid grid-cols-[auto_1fr] gap-x-3">
                  <dt className="text-ink-3">{row.label}</dt>
                  <dd className="num break-all">
                    {row.href ? (
                      <a
                        href={row.href}
                        target="_blank"
                        rel="noreferrer"
                        className="text-brand hover:underline"
                      >
                        {row.value}
                      </a>
                    ) : (
                      row.value
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
        </div>
      ) : (
        <p className="mt-1 text-xs text-ink-3">无事件来源（策略信号，不挂具体事件）。</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canDecide(decision.status) ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(decision.id, "approve")}
              className="h-8 rounded-[var(--radius)] bg-brand px-3 text-sm text-brand-ink disabled:opacity-50"
            >
              批准
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(decision.id, "reject")}
              className="h-8 rounded-[var(--radius)] border border-border px-3 text-sm hover:bg-muted disabled:opacity-50"
            >
              驳回
            </button>
            <span className="text-xs text-ink-3">
              批准后按<strong className="font-medium">次日开盘价</strong>成交——此刻成交价还不知道，
              还要点一次「推进一天」才会成交。
            </span>
          </>
        ) : (
          <span className="text-xs text-warn">
            已批准，等下一交易日开盘成交——点「推进一天」，成交价与成交股数到那时才确定。
          </span>
        )}
      </div>
    </li>
  );
}
