"use client";

import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { Pager } from "@/components/ui/pager";
import { api, describeError } from "@/lib/api";
import { count, dayStamp } from "@/lib/format";
import { offsetOf } from "@/lib/paging";
import type { AdminRejections } from "@/lib/types";

/** 与用量表同值：同一页上的两个列表，翻页手感不该两个样。 */
const PAGE_SIZE = 20;

/**
 * 拒绝记录（E2c 起分页）。
 *
 * **不做搜索**：它是**事件日志**、只增不减，按时间倒序扫即可；要按人查时用量表那一列更直接
 * （SPEC §3.5）。分页是必需的——日志涨得比账号快。
 */
export function RejectionsPanel({ page, onGo }: { page: number; onGo: (next: number) => void }) {
  const [data, setData] = useState<AdminRejections | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(
        await api.adminRejections({ limit: PAGE_SIZE, offset: offsetOf(page, PAGE_SIZE) }),
      );
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "拒绝记录加载失败：后端未启动或网络不通。"));
    }
  }, [page]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="mt-8">
      <h2 className="font-heading text-base font-medium">最近的拒绝记录</h2>
      <p className="mt-1 text-xs text-ink-3">
        「超配额被拒」的可查证据：谁、哪条配额、当时的用量与上限。
      </p>
      {error ? (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      ) : null}
      {data === null && error === null ? (
        <p className="mt-3 text-sm text-ink-3">加载中…</p>
      ) : null}
      {data ? (
        data.rejections.length === 0 ? (
          <p className="mt-3 text-sm text-ink-2">还没有被拒的记录。</p>
        ) : (
          <>
            <TableShell head={["时间", "账号", "配额", "当时用量", "上限"]}>
              {data.rejections.map((row) => (
                <Row key={row.id}>
                  <Cell className="text-ink-3">{dayStamp(row.at)}</Cell>
                  <Cell>{row.email ?? "—"}</Cell>
                  <Cell>{row.metric}</Cell>
                  <Cell numeric>{count(row.used)}</Cell>
                  <Cell numeric>{count(row.limit_value)}</Cell>
                </Row>
              ))}
            </TableShell>
            <Pager
              total={data.total}
              limit={PAGE_SIZE}
              page={page}
              unit="条"
              onGo={onGo}
            />
          </>
        )
      ) : null}
    </section>
  );
}
