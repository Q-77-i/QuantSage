"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { Pager } from "@/components/ui/pager";
import { useStocksList } from "@/components/stocks/use-stocks";
import { useDebounced } from "@/components/ui/use-debounced";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/form-controls";
import { toneClass } from "@/lib/market-home";
import { listSummary, PAGE_SIZE, SORT_CHOICES, stockRows } from "@/lib/stocks";
import { cn } from "@/lib/utils";

/**
 * 标的浏览 · 全市场列表（M10c）。
 *
 * **定位是「把已有能力摆出来」**：数据全来自 `GET /market/cross-section`，本页只做筛选、
 * 排序、分页与排版。**公司资料、财务、权威行业分类一概没有**，界面上不假装有。
 *
 * `?industry=` 是首页板块条点一条进来的入口——它走的是**与板块块同一支 PIT 累积归属**，
 * 所以这里的 `total` 正好等于那条上标的成分数（服务端有一条用例守着这件事）。
 */
export default function StocksPage() {
  // `useSearchParams` 在生产构建下要求边界（同 `/factor` / `/optimize` / `/space`）
  return (
    <Suspense fallback={<Bootstrap />}>
      <View />
    </Suspense>
  );
}

function View() {
  const params = useSearchParams();
  const router = useRouter();
  const industry = params.get("industry");
  // 页码住在地址栏里：翻到第 37 页 → 点进个股 → 浏览器后退，位置还在
  const page = Number.parseInt(params.get("page") ?? "1", 10) || 1;
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("change_pct");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const settledQ = useDebounced(q);

  const goPage = useCallback(
    (next: number) => {
      const search = new URLSearchParams(params.toString());
      if (next <= 1) search.delete("page");
      else search.set("page", String(next));
      const text = search.toString();
      router.replace(`/stocks${text ? `?${text}` : ""}`, { scroll: false });
    },
    [params, router],
  );

  // 换了筛选就回第一页（否则「第 5 页的茅台搜索结果」多半是空白）。
  // 用 ref 跳过一次：首次渲染不能把深链里的 `?page=5` 拉回第 1 页。
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    const search = new URLSearchParams(params.toString());
    if (search.has("page")) {
      search.delete("page");
      const text = search.toString();
      router.replace(`/stocks${text ? `?${text}` : ""}`, { scroll: false });
    }
    // `params` 每次渲染都是新对象，不能进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, order, settledQ, industry]);

  const { section, loading, error, reload } = useStocksList({
    industry,
    sort,
    order,
    q: settledQ,
    page,
  });
  const rows = stockRows(section);

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="font-heading text-xl font-semibold">标的浏览</h1>
          <p className="mt-1 text-sm text-ink-3" data-list-summary>
            {listSummary(section)}
          </p>
        </div>
        {industry ? (
          <span
            data-industry={industry}
            className="flex items-center gap-2 rounded-[var(--radius)] border border-border px-2 py-0.5 text-xs"
          >
            行业：{industry}
            {/* 清掉行业回到全市场——「点进来就出不去了」是导航的大忌 */}
            <Link href="/stocks" className="text-ink-3 hover:text-foreground">
              清除
            </Link>
          </span>
        ) : null}
      </header>

      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm" data-panel="stock-controls">
        <Input
          aria-label="搜索标的"
          placeholder="代码前缀或名称，如 6005 / 茅台"
          className="w-56"
          value={q}
          onChange={(event) => setQ(event.target.value)}
        />
        <Select
          aria-label="排序键"
          className="w-32"
          value={sort}
          onChange={(event) => setSort(event.target.value)}
        >
          {SORT_CHOICES.map((choice) => (
            <option key={choice.key} value={choice.key}>
              {choice.label}
            </option>
          ))}
        </Select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setOrder((value) => (value === "desc" ? "asc" : "desc"))}
        >
          {order === "desc" ? "降序" : "升序"}
        </Button>
      </div>

      {error ? (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center gap-3 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={reload}
            className="rounded-[var(--radius)] border border-destructive/40 px-2 py-0.5 text-xs"
          >
            重试
          </button>
        </div>
      ) : loading && !section ? (
        <div className="mt-4 h-40 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
      ) : rows.length === 0 ? (
        <p className="mt-4 rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
          没有匹配的标的。代码按前缀匹配、名称按子串匹配——换一个词试试。
        </p>
      ) : (
        <>
          <div data-table="stocks">
            <TableShell head={["代码", "名称", "收盘", "涨跌幅", "成交额（亿）"]}>
            {rows.map((row) => (
              <Row key={row.symbol}>
                <Cell numeric>
                  <Link href={`/stocks/${row.symbol}`} className="underline">
                    {row.symbol}
                  </Link>
                </Cell>
                <Cell>
                  {/* 名称为空的给代码（上面那栏）并如实留白，**不编名字** */}
                  {row.hasName ? (
                    <Link href={`/stocks/${row.symbol}`} className="hover:underline">
                      {row.label}
                    </Link>
                  ) : (
                    <span className="text-ink-3">—</span>
                  )}
                </Cell>
                <Cell numeric>{row.close}</Cell>
                <Cell numeric className={cn(toneClass(row.tone))}>
                  {row.change}
                </Cell>
                <Cell numeric>{row.amount}</Cell>
              </Row>
              ))}
            </TableShell>
          </div>
          <Pager total={section?.total ?? null} limit={PAGE_SIZE} page={page} unit="只" onGo={goPage} />
        </>
      )}

      <details className="mt-4" data-panel="stock-notes">
        <summary className="cursor-pointer text-xs text-ink-3">口径说明</summary>
        <ul className="mt-1 space-y-1 text-xs text-ink-3">
          <li>
            名称来自事件语料抽出的字典（覆盖 96.7%），<strong>不是</strong>行情源给的；
            没有名字的标的仍能按代码搜到。
          </li>
          <li>
            行业过滤用<strong>截至该日收盘的 PIT 累积归属</strong>，与首页板块块同一支口径——
            一只标的可同时属于多个行业。
          </li>
          <li>
            <strong>不列换手率</strong>：源侧 2026-08 起逐步停更、2026-09 起整列为空，
            列一栏恒为「—」是噪声。
          </li>
          <li>
            涨跌幅缺失的行如实留空，<strong>不当成平盘</strong>（那是两句不同的话）。
          </li>
        </ul>
      </details>
    </main>
  );
}

function Bootstrap() {
  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="h-40 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
    </div>
  );
}
