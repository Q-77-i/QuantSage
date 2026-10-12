"use client";

import { useEffect, useState } from "react";

import { Button } from "./button";
import { Input } from "./form-controls";
import { count } from "@/lib/format";
import { clampPage, totalPages } from "@/lib/paging";

/**
 * 列表翻页（M10c 建、M10b 复用）。
 *
 * **为什么不是「加载更多」**（用户 2026-10-13 反馈后定）：全市场 5,561 只 / 50 一页 = 112 页、
 * 单日事件 6,512 条 / 20 一页 = 326 页，靠点一百多次翻到中段不现实。**排序表要随机访问**
 * ——「加载更多」适合按时间顺着读的流，而这两处都是「按名次/重要度排的榜」。
 *
 * `total` 传 `null` 表示**还没加载**：那时不显示上界、也不夹页码（把 `?page=7` 夹成第 1 页
 * 会让深链失效，见 `lib/paging.ts` 的 `clampPage`）。
 */
export function Pager({
  total,
  limit,
  page,
  unit = "条",
  onGo,
}: {
  total: number | null;
  limit: number;
  page: number;
  /** 计数的量词：标的是「只」、事件是「条」 */
  unit?: string;
  onGo: (page: number) => void;
}) {
  const last = total === null ? Math.max(1, page) : totalPages(total, limit);
  const current = clampPage(page, total, limit);
  // 输入框自己存一份：跟着 `page` 走会在用户还没输完时被覆盖掉
  const [jump, setJump] = useState(String(current));
  useEffect(() => setJump(String(current)), [current]);

  function submitJump() {
    const parsed = Number.parseInt(jump, 10);
    if (Number.isFinite(parsed)) onGo(clampPage(parsed, total, limit));
    else setJump(String(current));
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-3 text-sm" data-pager>
      <Button variant="outline" size="sm" disabled={current <= 1} onClick={() => onGo(current - 1)}>
        上一页
      </Button>
      <span className="num text-xs text-ink-3" data-page-label>
        第 {count(current)} / {count(last)} 页{total === null ? "" : ` · 共 ${count(total)} ${unit}`}
      </span>
      <Button variant="outline" size="sm" disabled={current >= last} onClick={() => onGo(current + 1)}>
        下一页
      </Button>
      {last > 1 ? (
        <span className="flex items-center gap-1.5 text-xs text-ink-3">
          跳到
          <Input
            aria-label="跳到第几页"
            className="w-16 text-center"
            value={jump}
            onChange={(event) => setJump(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submitJump();
            }}
            onBlur={submitJump}
          />
          页
        </span>
      ) : null}
    </div>
  );
}
