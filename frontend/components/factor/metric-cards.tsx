"use client";

import { count, num } from "@/lib/format";
import type { FactorHeadline } from "@/lib/factor-report";
import type { FactorParams } from "@/lib/types";

/**
 * 指标卡：RankIC 均值做**英雄数字**，其余四个是统计卡（dataviz：一个数就是全部的图，
 * 不给它画柱）。
 *
 * `|t| < 2` 时挂一枚「不显著」标记——**这不是装饰**：这一页的全部风险是把噪声读成信号，
 * 标记与 `notes` 里那句「噪声区间内的读数」是同一件事的两个落点。判据在纯函数里
 * （`factorHeadline.significant`），组件不自己判。
 */
export function MetricCards({ head, params }: { head: FactorHeadline; params: FactorParams }) {
  return (
    <div className="mt-3 flex flex-wrap items-start gap-x-10 gap-y-4">
      <div>
        <p className="text-xs text-ink-3">RankIC 均值{params.source === "event" ? "（事件）" : ""}</p>
        <div className="mt-1 flex items-baseline gap-2">
          {/* 英雄数字刻意不用 tabular-nums：等宽数字在大字号下会显得松散 */}
          <p className="font-heading text-5xl leading-none font-semibold">
            {num(head.hero, { signed: true, digits: 4 })}
          </p>
          {head.empty ? null : (
            <span
              role="status"
              className={
                head.significant
                  ? "rounded-[var(--radius)] bg-muted px-1.5 py-0.5 text-xs text-ink-2"
                  : "rounded-[var(--radius)] border border-warn/50 px-1.5 py-0.5 text-xs text-warn"
              }
            >
              {head.significant ? "|t| ≥ 2" : "不显著"}
            </span>
          )}
        </div>
        <p className="mt-1 text-xs text-ink-3">
          {head.empty ? "本窗口没有有效信号日" : "逐日横截面秩相关，正负各半即无预测力"}
        </p>
      </div>

      <Stat label="ICIR" value={num(head.icir, { signed: true })} />
      <Stat label="t 值" value={num(head.tStat, { signed: true })} />
      <Stat label="有效信号日" value={`${count(head.days)} 天`} />
      <Stat label="池子日均" value={`${count(head.poolAvg)} 只`} />
      <Stat
        label="IC > 0 天数"
        value={head.empty ? "—" : `${count(head.positiveDays)} / ${count(head.days)}`}
      />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-ink-3">{label}</p>
      <p className="num mt-1 font-heading text-xl leading-none font-semibold">{value}</p>
    </div>
  );
}
