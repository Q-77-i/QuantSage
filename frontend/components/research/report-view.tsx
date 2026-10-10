"use client";

/**
 * 研报渲染器：**登录页与公开页共用这一份**。
 *
 * 「分享出去的那份」是别人唯一会看到的，写两份必然漂移——所以壳（页头导航、所有者动作、
 * 守卫）留在两个页面里，正文一律走这里。渲染**只认 `blocks[]`**：未知块兜底渲染 `text`，
 * M8 的深度研报接进来不用改这页（SPEC §8 的容器约定）。
 */

import type { ReactNode } from "react";
import { useState } from "react";

import { EvidenceList } from "@/components/research/panels";
import { AttributionTables } from "@/components/research/panels";
import { MetricsCards, NumberChips } from "@/components/research/panels";
import { ReviewCards } from "@/components/research/review-cards";
import { EquityBenchmarkChart } from "@/components/research/equity-benchmark-chart";
import {
  attributionTables,
  blockView,
  evidenceForBlock,
  evidenceIndex,
  fingerprintLine,
  kindBadge,
} from "@/lib/research";
import type { ReportBlock, ReportBody } from "@/lib/types";

export function ReportView({
  body,
  reportHash,
  actions,
  notice,
}: {
  body: ReportBody;
  reportHash: string;
  /** 所有者动作（分享 / 撤销 / 导出）；公开页只给导出 */
  actions?: ReactNode;
  /** 页头下方的一条提示（如「同一批数据已有报告」） */
  notice?: ReactNode;
}) {
  const index = evidenceIndex(body.evidence);
  return (
    <article className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="font-heading text-xl font-semibold">
          {body.account.name} · 策略复盘研报
        </h1>
        <p className="text-sm text-ink-3">
          {body.account.strategy_name ?? body.account.strategy} ｜ 标的{" "}
          {body.account.symbols.join("、")} ｜ 区间 {body.account.start} → {body.account.as_of} ｜{" "}
          <span className="text-foreground">数据止于 {body.account.data_end}</span>
        </p>
        <p className="font-mono text-xs text-ink-3" data-fingerprint>
          {fingerprintLine(reportHash, body.snapshot?.bars?.digest)}
        </p>
        {actions ? <div className="mt-1 flex flex-wrap items-center gap-2">{actions}</div> : null}
        {notice}
      </header>

      {body.blocks.map((block) => (
        <BlockSection key={block.id} block={block} body={body} index={index} />
      ))}

      <section data-block-id="warnings" className="border-t border-border pt-4">
        <h2 className="text-sm font-medium">如实标注</h2>
        {body.warnings.length ? (
          <ul className="mt-2 flex flex-col gap-1 text-sm text-ink-3">
            {body.warnings.map((warning) => (
              <li key={warning}>· {warning}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-ink-3">本次没有需要额外说明的口径问题</p>
        )}
      </section>

      <footer className="border-t border-border pt-4 text-xs text-ink-3">
        本报告由 QuantSage 生成，仅供研究用途，不构成投资建议。
      </footer>
    </article>
  );
}

function BlockSection({
  block,
  body,
  index,
}: {
  block: ReportBlock;
  body: ReportBody;
  index: ReturnType<typeof evidenceIndex>;
}) {
  const [showEvidence, setShowEvidence] = useState(false);
  const view = blockView(block);
  const badge = kindBadge(block.kind);
  const items = evidenceForBlock(block, index);

  return (
    <section data-block-id={block.id} data-kind={block.kind} className="border-t border-border pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-heading text-base font-semibold">{block.title}</h2>
        <span
          className={`rounded-[2px] border px-1.5 py-0.5 text-[11px] ${
            badge.tone === "inference" ? "border-warn/50 text-warn" : "border-border text-ink-3"
          }`}
          data-claim={badge.tone}
        >
          {badge.label}
        </span>
        {items.length ? (
          <button
            type="button"
            aria-expanded={showEvidence}
            onClick={() => setShowEvidence((value) => !value)}
            className="ml-auto h-7 rounded-[var(--radius)] border border-border px-2 text-xs hover:bg-muted"
          >
            证据（{items.length}）{showEvidence ? " ▴" : " ▾"}
          </button>
        ) : null}
      </div>

      <div className="mt-3">
        <BlockBody block={block} body={body} view={view} />
      </div>

      {showEvidence && items.length ? (
        <div className="mt-3">
          <EvidenceList items={items} />
        </div>
      ) : null}
    </section>
  );
}

function BlockBody({
  block,
  body,
  view,
}: {
  block: ReportBlock;
  body: ReportBody;
  view: ReturnType<typeof blockView>;
}) {
  switch (view) {
    case "overview":
      return (
        <div>
          <p className="text-sm">{block.text}</p>
          <NumberChips numbers={block.numbers} />
        </div>
      );
    case "performance":
      return (
        <div className="flex flex-col gap-4">
          <MetricsCards metrics={body.metrics} />
          <EquityBenchmarkChart points={body.equity_curve} />
          {body.benchmark?.note ? (
            <p className="text-xs text-ink-3">基准口径：{body.benchmark.note}</p>
          ) : null}
        </div>
      );
    case "attribution":
      return (
        <div className="flex flex-col gap-3">
          {block.text ? <p className="text-sm text-ink-3">{block.text}</p> : null}
          <AttributionTables tables={attributionTables(body)} />
        </div>
      );
    case "review":
      return (
        <div className="flex flex-col gap-3">
          {block.text ? <p className="text-sm text-ink-3">{block.text}</p> : null}
          <ReviewCards review={body.review} dataEnd={body.account.data_end} />
        </div>
      );
    case "narrative":
      return (
        <div>
          {block.text ? (
            <p className="text-sm leading-relaxed">{block.text}</p>
          ) : (
            <p className="text-sm text-ink-3">（{block.note ?? "本次没有综述"}）</p>
          )}
          <p className="mt-2 text-[11px] text-ink-3">
            模型 {block.model ?? "—"}
            {block.prompt_version ? ` · ${block.prompt_version}` : ""}（本节为模型综合，标注为推断型）
          </p>
        </div>
      );
    default:
      // M8 追加的块：兜底渲染 text + note，这一页不必为它改一行
      return (
        <div>
          {block.text ? <p className="text-sm leading-relaxed">{block.text}</p> : null}
          {block.note ? <p className="mt-1 text-xs text-ink-3">{block.note}</p> : null}
        </div>
      );
  }
}
