"use client";

/**
 * 逐笔复盘卡：已到期（带教训）→ 未到期（没有结果可总结）→ 定了没交易（只列状态）。
 *
 * 三段的语气差别是刻意的：**教训只从已了结的结果里长出来**，未到期的卡只报「现在多少」，
 * 定了没交易的卡一行带过——它们不做反事实收益（SPEC §8 D2），界面也不假装有。
 */

import { EMPTY } from "@/lib/format";
import { reviewCards, type ReviewCardView } from "@/lib/research";
import type { ReportReview } from "@/lib/types";

const PNL_TONE = { plain: "text-foreground", up: "text-up", down: "text-down" } as const;

export function ReviewCards({
  review,
  dataEnd,
}: {
  review: ReportReview | null;
  dataEnd: string;
}) {
  const { settled, open, unfilled } = reviewCards(review, dataEnd);
  if (!review) {
    return (
      <p className="text-sm text-ink-3">
        本次报告没有逐笔复盘（决策记忆未就绪或该账户还没有回合）
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-ink-3">
        已到期 {review.summary.settled} 笔（其中 {review.summary.lessons} 条有教训）｜未到期{" "}
        {review.summary.open} 笔｜定了没交易 {review.summary.unfilled} 笔
      </p>

      {settled.length ? (
        <div className="flex flex-col gap-3" data-review-group="settled">
          {settled.map((card) => (
            <Card key={card.key} card={card} />
          ))}
        </div>
      ) : null}

      {open.length ? (
        <div className="flex flex-col gap-3" data-review-group="open">
          {open.map((card) => (
            <Card key={card.key} card={card} />
          ))}
        </div>
      ) : null}

      {unfilled.length ? (
        <div data-review-group="unfilled">
          <h4 className="text-sm font-medium">定了没交易</h4>
          <p className="mt-0.5 text-xs text-ink-3">
            做了裁决但没成交的决策——只列状态，不做反事实收益
          </p>
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {unfilled.map((row) => (
              <li key={row.key} className="flex flex-wrap items-center gap-2">
                <span className="font-mono">{row.symbol}</span>
                <span className="text-ink-3">{row.tradeDate}</span>
                <span>{row.sideLabel}</span>
                <span className="text-ink-3">·</span>
                <span>{row.statusLabel}</span>
                {row.reason ? <span className="text-ink-3">（{row.reason}）</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Card({ card }: { card: ReviewCardView }) {
  const open = card.tone === "open";
  return (
    <article
      data-review={card.key}
      data-tone={card.tone}
      className={`rounded-[var(--radius)] border px-4 py-3 ${
        open ? "border-warn/40" : "border-border"
      }`}
    >
      <header className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono font-medium">{card.symbol}</span>
        <span
          className={`rounded-[2px] px-1.5 py-0.5 text-[11px] ${
            open ? "border border-warn/50 text-warn" : "bg-muted text-ink-3"
          }`}
        >
          {card.statusLabel}
        </span>
        <span className="text-xs text-ink-3">{card.window}</span>
        <span className={`ml-auto font-mono text-xl font-semibold ${PNL_TONE[card.pnlTone]}`}>
          {card.pnl}
        </span>
      </header>

      <div className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
        <Metric label="回合收益" value={card.returnText} />
        <Metric label="同期全市场等权" value={card.benchmarkText} />
        <Metric label="超额" value={card.alphaText} />
      </div>

      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-3">
        <span>买入理由：{card.entryReason}</span>
        {!open ? <span>卖出理由：{card.exitReason}</span> : null}
      </div>

      {card.reflectionText ? (
        <blockquote className="mt-3 border-l-2 border-warn/50 pl-3 text-sm">
          {card.reflectionText}
          <footer className="mt-1 text-[11px] text-ink-3">{card.reflectionByline}</footer>
        </blockquote>
      ) : open ? (
        <p className="mt-3 text-xs text-ink-3">还没有结果可总结——到期后才有教训。</p>
      ) : card.reflectionNote ? (
        <p className="mt-3 text-xs text-warn">教训不可用：{card.reflectionNote}</p>
      ) : null}
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-ink-3">{label}</span>
      <span className="font-mono tabular-nums">{value || EMPTY}</span>
    </div>
  );
}
