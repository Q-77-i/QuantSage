"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CandlestickChart } from "@/components/backtest/candlestick-chart";
import { EventsTable } from "@/components/backtest/events-table";
import { Section } from "@/components/backtest/chart-frame";
import { Button } from "@/components/ui/button";
import { api, describeError } from "@/lib/api";
import { num } from "@/lib/format";
import { addButtonState, symbolTitle } from "@/lib/stocks";
import type { Bar, MarketEvent, SymbolProbe, WatchlistItem } from "@/lib/types";

/**
 * 标的浏览 · 个股页（M10c）。
 *
 * **K 线底下没有买卖点标记**——个股页没有策略信号，硬塞一个会变成「假装这里有结论」。
 * 要看买卖点就去回测页跑一条策略（「去回测」按钮把标的带过去）。
 *
 * 「加自选」是**轻量版**：复用 `lib/watchlist.ts` 的 `validateSymbol` 与 `api.addWatchlist`，
 * 只做「加入默认分组 + 本地判重」。**不移植 `WatchlistPanel` 的整表单**（probe 防抖、
 * 名称搜索、移出二次确认都在那一处内联着，抽出来是一次重构，不在本段）。
 */
export default function SymbolPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = typeof params.symbol === "string" ? params.symbol : "";
  const { bars, events, probe, name, loading, error, add, addState } = useSymbol(symbol);

  if (error) {
    return (
      <main className="mx-auto max-w-[1400px] px-4 py-6">
        <p
          role="alert"
          className="rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
        <Link href="/stocks" className="mt-3 inline-block text-sm underline">
          回到标的列表
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="font-heading text-xl font-semibold" data-symbol={symbol}>
            {symbolTitle(symbol, name)}
          </h1>
          <p className="mt-1 text-sm text-ink-3">
            {probe?.has_data
              ? `最新交易日 ${probe.latest_trade_date} · 收盘 ${num(probe.latest_close, { digits: 2 })}`
              : loading
                ? "读取中…"
                : "本地行情里没有这个标的"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={addState.disabled}
            data-action="watchlist"
            onClick={() => void add()}
          >
            {addState.label}
          </Button>
          {/* 把标的带进回测表单——不带的话点过去等于没带上下文 */}
          <Link
            href={`/backtest?symbol=${encodeURIComponent(symbol)}`}
            data-action="to-backtest"
            className="rounded-[var(--radius)] border border-border px-3 py-1.5 text-sm hover:bg-muted"
          >
            去回测
          </Link>
        </div>
      </header>

      <div className="mt-6 flex flex-col gap-6">
        <Section title="K 线" hint={bars ? `共 ${bars.length} 根日线（后复权口径见下）` : undefined}>
          <div className="relative mt-3 h-[380px] overflow-hidden rounded-[var(--radius)] border border-border bg-chart-surface md:h-[440px]">
            {loading && !bars ? (
              <div className="absolute inset-0 animate-pulse bg-muted/60" />
            ) : bars && bars.length ? (
              // 空 `trades` + `openPosition=null`：这个组件不依赖回测报告，个股页直接可用
              <CandlestickChart bars={bars} trades={[]} openPosition={null} />
            ) : (
              <p className="absolute inset-0 flex items-center justify-center text-sm text-ink-2">
                本地行情里没有这个标的的日线。
              </p>
            )}
          </div>
        </Section>

        <EventsTable events={events} coverage={null} />
      </div>

      <details className="mt-4" data-panel="symbol-notes">
        <summary className="cursor-pointer text-xs text-ink-3">口径说明</summary>
        <ul className="mt-1 space-y-1 text-xs text-ink-3">
          <li>
            K 线走<strong>前复权</strong>（图表口径与回测取数同源，买卖点才落得准）；
            <strong>没有</strong>公司资料、财务数据与权威行业分类——数据源不覆盖，界面上不假装有。
          </li>
          <li>事件时间线列出本地语料覆盖期内该标的的全部事件，每条可展开来源三元组。</li>
        </ul>
      </details>
    </main>
  );
}

function useSymbol(symbol: string) {
  const [bars, setBars] = useState<Bar[] | null>(null);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [probe, setProbe] = useState<SymbolProbe | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [tracked, setTracked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 四件事并行：行情、事件、体检、自选（判重只看本地已加载的自选列表）
      const [barBody, eventBody, probeBody, list, found] = await Promise.all([
        api.bars(symbol),
        api.events(symbol),
        api.probe(symbol),
        api.watchlist(),
        // 名称走**按代码搜**（字典覆盖 96.7%）：精确命中才用，命中不了就只显示代码，不编
        api.symbols(symbol, 5),
      ]);
      setBars(barBody.bars);
      setEvents(eventBody.events);
      setProbe(probeBody);
      setTracked(list.some((row: WatchlistItem) => row.symbol === symbol));
      setName(found.items.find((row) => row.symbol === symbol)?.name ?? null);
    } catch (cause) {
      setError(describeError(cause, "读取该标的失败"));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    void load();
  }, [load]);

  const add = useCallback(async () => {
    setBusy(true);
    try {
      await api.addWatchlist(symbol);
      // 服务端是唯一真源：写完重拉一次，不本地拼状态（同 `WatchlistPanel` 的口径）
      const list = await api.watchlist();
      setTracked(list.some((row: WatchlistItem) => row.symbol === symbol));
    } catch (cause) {
      setError(describeError(cause, "加自选失败"));
    } finally {
      setBusy(false);
    }
  }, [symbol]);

  return {
    bars,
    events,
    probe,
    name,
    loading,
    error,
    add,
    addState: addButtonState(tracked, busy),
  };
}
