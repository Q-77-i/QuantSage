"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { TableShell, Cell, Row } from "@/components/backtest/table";
import { api, describeError } from "@/lib/api";
import { EMPTY, dayStamp, num, pct } from "@/lib/format";
import { DEFAULT_GROUP, addFormHint, groupItems, groupNames, validateSymbol } from "@/lib/watchlist";
import type { AddHint, Probe } from "@/lib/watchlist";
import type { WatchlistItem } from "@/lib/types";

/** 体检防抖：输满六位后停手 300ms 才发（逐位发会在集群查询上白烧三次） */
const PROBE_DEBOUNCE_MS = 300;

/**
 * 我的自选：加自选、分组管理、加自选以来涨幅。
 *
 * 分组是每行上的一个字符串（后端没有分组实体表），「有哪些组」由前端聚合
 * （`lib/watchlist.ts`，有单测）。价格与涨幅**一律用后端给的值**：取不到就是 null、
 * 显示「—」，前端不自己算也不编数——加入价是不动的历史事实，只有服务端知道。
 *
 * 写操作统一走 `act()`：成功后重拉整表。服务端是唯一真源，本地拼状态省下的那点
 * 往返，换来的是一堆「删了还在、移了没变」的错位。
 *
 * 加自选表单**边输边给反馈**（2026-10-09 加）：原来要提交才知道结果——重复加等一次
 * 409、代码打错一位也照样加得进去，之后价格永远显示「—」。判据与文案在
 * `addFormHint`（纯函数，有单测），这里只管发请求与渲染。
 */
export function WatchlistPanel() {
  const [items, setItems] = useState<WatchlistItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [group, setGroup] = useState("");
  const [probe, setProbe] = useState<Probe>({ status: "idle" });
  // 存代码而不是布尔：输入一改，这个确认就自动失效（与体检结果同一个防错口径）
  const [removing, setRemoving] = useState<string | null>(null);
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

  const code = symbol.trim();
  // 判重是本地事实：列表本来就全量加载，已在自选里就不必再问服务端（零往返）
  const tracked = items?.some((row) => row.symbol === code) ?? false;

  useEffect(() => {
    if (validateSymbol(code) !== null || tracked) {
      setProbe({ status: "idle" });
      return;
    }
    const timer = window.setTimeout(() => {
      setProbe({ status: "checking", code });
      api
        .probe(code)
        .then((body) =>
          setProbe(
            body.has_data
              ? { status: "found", code, date: body.latest_trade_date, close: body.latest_close }
              : { status: "missing", code },
          ),
        )
        // 503（行情层没就绪）与断网都落这里：**查不了不等于没有**，界面上不禁用
        .catch(() => setProbe({ status: "unknown", code }));
    }, PROBE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [code, tracked]);

  const hint = addFormHint(symbol, items, probe);
  const confirmRemove = removing === code && hint.kind === "duplicate";
  const hintText = hintLine(hint, code, confirmRemove);

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

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const problem = validateSymbol(symbol);
    if (problem) {
      setError(problem);
      return;
    }
    // 已在自选：这个按钮此刻的职责变成「移出」，且要点两下——删掉再加会把
    // added_price（加入时的历史事实）重置，「加自选以来涨幅」的起点跟着变
    if (hint.kind === "duplicate") {
      if (!confirmRemove) {
        setRemoving(code);
        return;
      }
      void act(async () => {
        await api.removeWatchlist(code);
        setSymbol("");
        setRemoving(null);
      }, "移出自选失败。");
      return;
    }
    const target = group.trim();
    void act(async () => {
      await api.addWatchlist(code, target || undefined);
      setSymbol("");
      setGroup("");
    }, "加自选失败：后端未启动或网络不通。");
  }

  const groups = items ? groupItems(items) : [];
  const names = items ? groupNames(items) : [];

  return (
    <section>
      <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
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
        <Button
          type="submit"
          size="sm"
          variant={hint.kind === "duplicate" ? "outline" : "default"}
          disabled={busy || hint.kind === "missing"}
        >
          {buttonLabel(hint, confirmRemove)}
        </Button>
        {confirmRemove && (
          <button
            type="button"
            onClick={() => setRemoving(null)}
            className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-2 hover:bg-muted"
          >
            取消
          </button>
        )}
      </form>

      {hintText && (
        <p
          className={
            hint.kind === "missing" || confirmRemove
              ? "mt-2 text-xs text-destructive"
              : "mt-2 text-xs text-ink-3"
          }
        >
          {hintText}
        </p>
      )}

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

/** 提交键此刻是干什么的：默认加自选；已在自选里就变成移出（要点两下）。 */
function buttonLabel(hint: AddHint, confirming: boolean): string {
  if (hint.kind !== "duplicate") return "加自选";
  return confirming ? "确认移出" : "移出";
}

/**
 * 表单下方那行提示。`idle` 与 `unknown` 都返回 null，但理由不同：前者没得说
 * （还没输满六位），后者是**不拦人的降级**（行情层没就绪，说一句「查不了」只会
 * 添乱，加自选照旧）。
 */
function hintLine(hint: AddHint, code: string, confirming: boolean): ReactNode {
  switch (hint.kind) {
    case "checking":
      return "查行情…";
    case "found":
      return `最近交易日 ${hint.date ?? EMPTY} 收盘 ${num(hint.close)}`;
    case "duplicate":
      return confirming
        ? `把 ${code} 移出？「加自选以来」的起点会一并清掉。`
        : `已在自选 · 分组「${hint.group}」`;
    case "missing":
      return `本地行情数据里没有 ${code}，核对一下代码。`;
    default:
      return null;
  }
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
