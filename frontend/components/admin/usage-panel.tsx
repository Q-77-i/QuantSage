"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { Button } from "@/components/ui/button";
import { Check, Input, Select } from "@/components/ui/form-controls";
import { Pager } from "@/components/ui/pager";
import { useDebounced } from "@/components/ui/use-debounced";
import { orderLabel, toggleOrder, USAGE_SORT_CHOICES, type SortOrder, type UsageSortKey } from "@/lib/admin";
import { api, describeError } from "@/lib/api";
import { count } from "@/lib/format";
import { offsetOf } from "@/lib/paging";
import type { AdminUsage } from "@/lib/types";

/** 一页多少行（2026-10-16 用户定：50 太挤，20 一页）。两个列表同值——翻页手感不该两个样。 */
export const PAGE_SIZE = 20;

/** 角色筛选的三个取值；空串 = 全部（与服务端的「留空不过滤」同源）。 */
const ROLE_OPTIONS = [
  { value: "", label: "全部角色" },
  { value: "user", label: "普通用户" },
  { value: "admin", label: "管理员" },
] as const;

/**
 * 配额用量表（E2c：分页 + 搜索 + 两个筛选）。
 *
 * **服务端分页与筛选**：视图规模随用户数走，前端切片意味着接口仍要吐全量。
 * 页码由调用方给（它同时是地址栏里的 `?page=`），这里只把它换成 offset；
 * **请求前不夹上界**（首屏还不知道总数），上界交给 `Pager` 在渲染层夹。
 *
 * 搜索**边输边查 + 300ms 防抖**（与标的浏览同一种手感），用 `seq` 挡回包乱序——打字比响应
 * 快时，慢的那一发回来会盖掉新结果。**筛选一变就回第 1 页**（否则「第 7 页的筛选结果」
 * 多半是空白），首次渲染跳过一次，免得把深链里的 `?page=` 拉回来。
 */
export function UsagePanel({
  page,
  onGo,
  onFilterChanged,
}: {
  page: number;
  onGo: (next: number) => void;
  onFilterChanged: () => void;
}) {
  const [data, setData] = useState<AdminUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [typing, setTyping] = useState("");
  const [role, setRole] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [sort, setSort] = useState<UsageSortKey>("id");
  const [order, setOrder] = useState<SortOrder>("asc");
  const q = useDebounced(typing);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const ticket = ++seq.current;
    try {
      const result = await api.adminUsage({
        limit: PAGE_SIZE,
        offset: offsetOf(page, PAGE_SIZE),
        q,
        role,
        // 关掉时不发这个参数（URL 干净，服务端默认也是 false）
        active_only: activeOnly || undefined,
        // 默认排序也不发（「登记顺序」是服务端的默认，URL 里不必带）
        sort: sort === "id" && order === "asc" ? undefined : sort,
        order: sort === "id" && order === "asc" ? undefined : order,
      });
      if (ticket !== seq.current) return; // 回包乱序：丢掉旧的那一发
      setData(result);
      setError(null);
    } catch (cause) {
      if (ticket !== seq.current) return;
      setError(describeError(cause, "用量加载失败：后端未启动或网络不通。"));
    }
  }, [page, q, role, activeOnly, sort, order]);

  useEffect(() => {
    void load();
  }, [load]);

  // 回调放进 ref：它由父组件内联传下来，**每次渲染都是新身份**——直接进 effect 依赖的话，
  // 每次重渲染都会触发一次「筛选变了，回第 1 页」（界面验证逮到：URL 停在 `?page=2`、
  // 行也换了页，页码标签却被拽回第 1 页）。依赖里只留**筛选值本身**。
  const filterChanged = useRef(onFilterChanged);
  filterChanged.current = onFilterChanged;
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    filterChanged.current();
    // 首次渲染跳过一次：不能把深链里的 `?page=` 拉回来（与标的浏览同一个做法）。
  // 排序也在这里回第 1 页——「第 7 页 + 换排序键」同样多半是空白
  }, [q, role, activeOnly, sort, order]);

  const filtered = Boolean(q || role || activeOnly);
  return (
    <section className="mt-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 className="font-heading text-base font-medium">配额用量</h2>
        <div className="flex flex-wrap items-center gap-3">
          <Check
            id="usage-active-only"
            label="只看有读数的"
            checked={activeOnly}
            onChange={setActiveOnly}
          />
          <Select
            aria-label="按角色筛选"
            className="w-32"
            value={role}
            onChange={(event) => setRole(event.target.value)}
          >
            {ROLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          <Select
            aria-label="排序键"
            className="w-40"
            value={sort}
            onChange={(event) => setSort(event.target.value as UsageSortKey)}
          >
            {USAGE_SORT_CHOICES.map((choice) => (
              <option key={choice.key} value={choice.key}>
                {choice.label}
              </option>
            ))}
          </Select>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setOrder(toggleOrder(order))}
          >
            {orderLabel(order)}
          </Button>
          <Input
            aria-label="按邮箱搜索账号"
            className="w-56"
            placeholder="按邮箱搜索"
            value={typing}
            onChange={(event) => setTyping(event.target.value)}
          />
        </div>
      </div>
      {data === null && error === null ? (
        <p className="mt-3 text-sm text-ink-3">加载中…</p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      ) : null}
      {data ? (
        <>
          <p className="mt-1 text-xs text-ink-3" data-usage-summary>
            在飞任务上限 {data.limits.active_tasks || "不限"} · 每日 Agent 调用上限{" "}
            {data.limits.agent_calls_per_day || "不限"}（0 = 不限额；admin 不在限额内） ·
            {filtered ? `筛选后 ${count(data.total)} 个账号` : `共 ${count(data.total)} 个账号`}
            {q ? `（匹配「${q}」）` : ""}
          </p>
          {data.users.length === 0 ? (
            <p className="mt-3 text-sm text-ink-2">没有匹配的账号。</p>
          ) : (
            <TableShell head={["账号", "角色", "在飞任务", "今日 Agent 调用", "今日被拒"]}>
              {data.users.map((row) => (
                <Row key={row.id}>
                  <Cell>{row.email}</Cell>
                  <Cell className="text-ink-3">{row.role}</Cell>
                  <Cell numeric>{count(row.active_tasks)}</Cell>
                  <Cell numeric>{count(row.agent_calls_today)}</Cell>
                  <Cell numeric>{count(row.rejected_today)}</Cell>
                </Row>
              ))}
            </TableShell>
          )}
          <Pager total={data.total} limit={PAGE_SIZE} page={page} unit="个账号" onGo={onGo} />
        </>
      ) : null}
    </section>
  );
}
