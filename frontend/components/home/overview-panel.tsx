"use client";

import {
  amountCard,
  breadthCard,
  indexCard,
  limitsCard,
  limitNote,
  toneClass,
  type StatCard,
} from "@/lib/market-home";
import type { MarketOverview } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 大盘块：一张**英雄卡**（全市场等权代理）+ 三张读数卡。
 *
 * 沿用 `/factor` 指标卡的排版语言（裸数字块 + `flex-wrap`，不套卡片盒）——全站的「数据区
 * 不套盒子」是定过的事，这里不另起一套。英雄数字不用 `tabular-nums`（等宽数字在大字号下
 * 显得松散，同 `metric-cards`）。
 *
 * **「不是指数」必须长在卡上**：代理指数与真实指数长得一模一样，那句口径小字是它与
 * 「编一个沪深 300」之间唯一的区别。文案由 `lib/market-home.ts` 生成，组件不自己拼。
 *
 * 口径长文走 **`<details>` 折叠**（同 M8c 的事件时间线）：需要核对的人点得开，默认不占版面。
 * 不用 `--warn` 标「非指数」——那是「需要人看一眼」的专用色（M5c 定的），
 * 一句每次打开都在的常规口径借它就成了狼来了。
 */
export function OverviewPanel({ overview }: { overview: MarketOverview | null }) {
  if (!overview) return null;
  const hero = indexCard(overview.index);
  const stats = [breadthCard(overview), limitsCard(overview), amountCard(overview)].filter(
    (card): card is StatCard => card !== null,
  );
  const notes = [overview.index?.note, limitNote(overview)].filter(Boolean);

  return (
    <div data-panel="overview">
      <div className="mt-4 flex flex-wrap items-start gap-x-10 gap-y-5">
        {hero ? (
          <div data-card="index">
            <p className="text-xs text-ink-3">
              全市场等权代理
              <span className="ml-1.5 rounded-[var(--radius)] bg-muted px-1.5 py-0.5 text-ink-2">
                非指数
              </span>
            </p>
            <div className="mt-1 flex items-baseline gap-2">
              <p className="font-heading text-5xl leading-none font-semibold">{hero.level}</p>
              <span className={cn("num text-base font-medium", toneClass(hero.tone))}>
                {hero.change}
              </span>
            </div>
            <p className="mt-1 max-w-xs text-xs text-ink-3">{hero.caption}</p>
          </div>
        ) : null}

        {stats.map((card) => (
          <div key={card.key} data-card={card.key}>
            <p className="text-xs text-ink-3">{card.label}</p>
            <p
              className={cn(
                "num mt-1 font-heading text-xl leading-none font-semibold",
                toneClass(card.tone),
              )}
            >
              {card.value}
            </p>
            {/* 副读数里的数字**不上涨跌色**：它是构成说明，不是第二个结论 */}
            <p className="num mt-1 text-xs text-ink-3">{card.note}</p>
          </div>
        ))}
      </div>

      {notes.length ? (
        <details className="mt-3" data-panel="overview-notes">
          <summary className="cursor-pointer text-xs text-ink-3">口径说明</summary>
          <ul className="mt-1 space-y-1 text-xs text-ink-3">
            {notes.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
