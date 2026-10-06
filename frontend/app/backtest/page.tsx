"use client";

import { useEffect, useState } from "react";

import { AppHeader } from "@/components/app-header";
import { ApiError, api } from "@/lib/api";
import type { BarsResponse, EventsResponse } from "@/lib/types";

const DEFAULT_SYMBOL = "600519";

/**
 * 回测页骨架（T6a）。
 *
 * 参数表单、指标卡、两张图、两张表留 T6c。本阶段先把**数据通路**打通并显示出来：
 * 行情与事件两个 GET 端点各自返回多少行，是「前后端真的连上了」最直接的证据。
 */
export default function BacktestPage() {
  const [bars, setBars] = useState<BarsResponse | null>(null);
  const [events, setEvents] = useState<EventsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([api.bars(DEFAULT_SYMBOL), api.events(DEFAULT_SYMBOL)])
      .then(([barsData, eventsData]) => {
        if (!alive) return;
        setBars(barsData);
        setEvents(eventsData);
      })
      .catch((cause: unknown) => {
        if (!alive) return;
        setError(cause instanceof ApiError ? cause.message : "数据加载失败");
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <>
      <AppHeader />
      <main className="mx-auto max-w-[1400px] px-4 py-6">
        <h1 className="font-heading text-xl">回测</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          参数区、指标卡、净值曲线与 K 线在图表的下一阶段接入。
        </p>

        <section className="mt-6 border-t border-border pt-4">
          <h2 className="text-sm text-ink-2">数据通路自检</h2>
          {error ? (
            <p className="mt-3 text-sm text-destructive">{error}</p>
          ) : (
            <dl className="mt-3 grid gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="标的" value={DEFAULT_SYMBOL} />
              <Stat label="日线根数" value={bars ? String(bars.count) : null} />
              <Stat label="区间" value={bars ? `${bars.bars[0]?.time} 起` : null} />
              <Stat label="事件条数" value={events ? String(events.count) : null} />
            </dl>
          )}
        </section>
      </main>
    </>
  );
}

/** 指标用「标签 + 数字」的裸布局，不套卡片盒（设计规范：数据区不用卡片）。 */
function Stat({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt className="text-xs text-ink-3">{label}</dt>
      <dd className="num mt-0.5 text-lg">
        {value ?? <span className="inline-block h-6 w-20 animate-pulse rounded-[var(--radius)] bg-muted" />}
      </dd>
    </div>
  );
}
