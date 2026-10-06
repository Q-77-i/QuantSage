"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { TableShell, Cell, Row } from "@/components/backtest/table";
import { api, describeError } from "@/lib/api";
import { EMPTY, dayStamp, num, pct } from "@/lib/format";
import { DEFAULT_GROUP, groupItems, groupNames, validateSymbol } from "@/lib/watchlist";
import type { WatchlistItem } from "@/lib/types";

/**
 * 我的自选：加自选、分组管理、加自选以来涨幅。
 *
 * 分组是每行上的一个字符串（后端没有分组实体表），「有哪些组」由前端聚合
 * （`lib/watchlist.ts`，有单测）。价格与涨幅**一律用后端给的值**：取不到就是 null、
 * 显示「—」，前端不自己算也不编数——加入价是不动的历史事实，只有服务端知道。
 *
 * 写操作统一走 `act()`：成功后重拉整表。服务端是唯一真源，本地拼状态省下的那点
 * 往返，换来的是一堆「删了还在、移了没变」的错位。
 */
export function WatchlistPanel() {
  const [items, setItems] = useState<WatchlistItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [group, setGroup] = useState("");
  const [renaming, setRenaming] = useState<{ from: string; value: string } | null>(null);
  const [dropping, setDropping] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await api.watchlist());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "自选股加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(work: () => Promise<unknown>, fallback: string) {
    setBusy(true);
    try {
      await work();
      await load();
    } catch (cause) {
      setError(describeError(cause, fallback));
    } finally {
      setBusy(false);
    }
  }

  function handleAdd(event: FormEvent) {
    event.preventDefault();
    const problem = validateSymbol(symbol);
    if (problem) {
      setError(problem);
      return;
    }
    const target = group.trim();
    void act(async () => {
      await api.addWatchlist(symbol.trim(), target || undefined);
      setSymbol("");
      setGroup("");
    }, "加自选失败：后端未启动或网络不通。");
  }

  const groups = items ? groupItems(items) : [];
  const names = items ? groupNames(items) : [];

  return (
    <section>
      <form onSubmit={handleAdd} className="flex flex-wrap items-center gap-2">
        <input
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
          placeholder="股票代码"
          inputMode="numeric"
          className="num w-28 rounded-[var(--radius)] border border-border bg-card px-2 py-1.5 text-sm"
        />
        <input
          value={group}
          onChange={(event) => setGroup(event.target.value)}
          placeholder={`分组（缺省「${DEFAULT_GROUP}」）`}
          list="watchlist-groups"
          className="w-48 rounded-[var(--radius)] border border-border bg-card px-2 py-1.5 text-sm"
        />
        <datalist id="watchlist-groups">
          {names.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <Button type="submit" size="sm" disabled={busy}>
          加自选
        </Button>
      </form>

      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      )}

      {items === null ? (
        <Skeleton />
      ) : items.length === 0 ? (
        <p className="mt-6 text-sm text-ink-2">还没有自选股。填个六位代码加进来。</p>
      ) : (
        <div className="mt-6 space-y-6">
          {groups.map((bucket) => (
            <div key={bucket.name}>
              <div className="flex items-center gap-2">
                {renaming?.from === bucket.name ? (
                  <>
                    <input
                      autoFocus
                      value={renaming.value}
                      onChange={(event) =>
                        setRenaming({ from: bucket.name, value: event.target.value })
                      }
                      className="w-48 rounded-[var(--radius)] border border-border bg-card px-2 py-1 text-sm"
                    />
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        const to = renaming.value.trim();
                        setRenaming(null);
                        if (to && to !== bucket.name) {
                          void act(
                            () => api.renameWatchlistGroup(bucket.name, to),
                            "重命名分组失败。",
                          );
                        }
                      }}
                      className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-brand hover:bg-muted"
                    >
                      保存
                    </button>
                    <button
                      type="button"
                      onClick={() => setRenaming(null)}
                      className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-2 hover:bg-muted"
                    >
                      取消
                    </button>
                  </>
                ) : (
                  <>
                    <h3 className="font-heading text-sm font-semibold">{bucket.name}</h3>
                    <span className="num text-xs text-ink-3">{bucket.items.length}</span>
                    {/* 「默认分组」是回落目标，后端拒绝改名与删除，这里也不给入口 */}
                    {bucket.name !== DEFAULT_GROUP && (
                      <span className="ml-auto flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => setRenaming({ from: bucket.name, value: bucket.name })}
                          className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-3 hover:bg-muted hover:text-foreground"
                        >
                          重命名
                        </button>
                        {dropping === bucket.name ? (
                          <>
                            <span className="text-xs text-ink-2">标的会移回默认分组</span>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => {
                                setDropping(null);
                                void act(
                                  () => api.deleteWatchlistGroup(bucket.name),
                                  "删除分组失败。",
                                );
                              }}
                              className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-destructive hover:bg-muted"
                            >
                              删除分组
                            </button>
                            <button
                              type="button"
                              onClick={() => setDropping(null)}
                              className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-2 hover:bg-muted"
                            >
                              取消
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setDropping(bucket.name)}
                            className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-3 hover:bg-muted hover:text-destructive"
                          >
                            删除分组
                          </button>
                        )}
                      </span>
                    )}
                  </>
                )}
              </div>

              <TableShell head={["代码", "加入价", "最新价", "加自选以来", "加入时间", "分组", ""]}>
                {bucket.items.map((item) =>
                  confirming === item.symbol ? (
                    <Row key={item.symbol}>
                      <Cell numeric>{item.symbol}</Cell>
                      {/* 跨列写整行：TableShell 不管单元格，直接用 td 比给 Cell 加 colSpan 更省 */}
                      <td colSpan={6} className="px-2 text-right">
                        <span className="mr-2 text-xs text-ink-2">移出自选？</span>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setConfirming(null);
                            void act(() => api.removeWatchlist(item.symbol), "移出自选失败。");
                          }}
                          className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-destructive hover:bg-muted"
                        >
                          移出
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirming(null)}
                          className="ml-1 rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-2 hover:bg-muted"
                        >
                          取消
                        </button>
                      </td>
                    </Row>
                  ) : (
                    <Row key={item.symbol}>
                      <Cell numeric>{item.symbol}</Cell>
                      <Cell numeric>{num(item.added_price)}</Cell>
                      <Cell numeric className="text-ink-2" >
                        <span title={item.latest_trade_date ? `${item.latest_trade_date} 收盘` : "行情数据里没有这个标的"}>
                          {item.latest_close === null ? EMPTY : num(item.latest_close)}
                        </span>
                      </Cell>
                      {/* 涨跌色只在**有数**时上：给一个「—」涂上红色会读成「跌了」 */}
                      <Cell numeric className={changeTone(item.change_pct)}>
                        {pct(item.change_pct, { signed: true })}
                      </Cell>
                      <Cell numeric className="text-ink-3">
                        {dayStamp(item.added_at)}
                      </Cell>
                      <Cell>
                        <select
                          value={item.group_name}
                          disabled={busy}
                          aria-label={`把 ${item.symbol} 移到别的分组`}
                          onChange={(event) =>
                            void act(
                              () => api.moveWatchlist(item.symbol, event.target.value),
                              "移动分组失败。",
                            )
                          }
                          className="rounded-[var(--radius)] border border-border bg-card px-1.5 py-1 text-xs"
                        >
                          {names.map((name) => (
                            <option key={name} value={name}>
                              {name}
                            </option>
                          ))}
                        </select>
                      </Cell>
                      <Cell className="text-right">
                        <button
                          type="button"
                          onClick={() => setConfirming(item.symbol)}
                          className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-3 hover:bg-muted hover:text-destructive"
                        >
                          移出
                        </button>
                      </Cell>
                    </Row>
                  ),
                )}
              </TableShell>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** 涨跌色：A 股口径红涨绿跌（token 在 `globals.css` 的 `--up` / `--down`）。 */
function changeTone(changePct: number | null): string {
  if (changePct === null) return "text-ink-3";
  return changePct >= 0 ? "text-up" : "text-down";
}

function Skeleton() {
  return (
    <div className="mt-6 space-y-2">
      {[0, 1, 2].map((index) => (
        <div key={index} className="h-9 animate-pulse rounded-[var(--radius)] bg-muted" />
      ))}
    </div>
  );
}
