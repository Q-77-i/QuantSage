"use client";

import { useState } from "react";

import { Select } from "@/components/ui/form-controls";
import { stepLines } from "@/lib/paper";
import type { StatusTone } from "@/lib/paper";
import type { PaperAccountDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 推进控制：**这一页唯一会改变账户的两个动作**。
 *
 * 「跑到结束」不可逆（会话就此终结），故走**内联二次确认**而不是 `window.confirm`
 * （原生弹窗阻塞整页、样式不跟主题——`thread-list` 的删除是同一套做法）。
 *
 * 推进之后把「这次发生了什么」逐条列出来：成交 / 过期 / 未成交 / 新生成。
 * 没有这行回执的话，用户点完只看到数字变了一点，看不出中间发生了什么。
 */

const TONE_CLASS: Record<StatusTone, string> = {
  action: "text-warn",
  waiting: "text-warn",
  normal: "text-foreground",
  void: "text-ink-3",
  alert: "text-destructive",
};

export function StepControls({
  detail,
  busy,
  onStep,
  onRun,
}: {
  detail: PaperAccountDetail;
  busy: boolean;
  onStep: () => void;
  onRun: (approve: "all" | "none") => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [approve, setApprove] = useState<"all" | "none">("all");
  const finished = detail.account.status === "finished";
  const lines = stepLines(detail.this_step);

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy || finished}
          onClick={onStep}
          className="h-8 rounded-[var(--radius)] bg-brand px-3 text-sm text-brand-ink disabled:opacity-50"
        >
          推进一天
        </button>
        <span className="text-xs text-ink-3">
          {finished
            ? "会话已跑到区间末端。"
            : "推进 = 成交上一日已批准的单（按当日开盘价）→ 收盘结算 → 生成当日决策。"}
        </span>

        {finished ? null : (
          <div className="ml-auto flex items-center gap-2">
            <Select
              aria-label="跑到结束时怎么处理新生成的决策"
              value={approve}
              disabled={busy || confirming}
              onChange={(event) => setApprove(event.target.value as "all" | "none")}
              className="w-[8.5rem]"
            >
              <option value="all">全部批准</option>
              <option value="none">全部驳回</option>
            </Select>
            {confirming ? (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setConfirming(false);
                    onRun(approve);
                  }}
                  className="h-8 rounded-[var(--radius)] border border-destructive/50 px-3 text-sm text-destructive disabled:opacity-50"
                >
                  确认跑到 {detail.progress.end}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirming(false)}
                  className="h-8 rounded-[var(--radius)] border border-border px-3 text-sm hover:bg-muted"
                >
                  取消
                </button>
              </>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirming(true)}
                className="h-8 rounded-[var(--radius)] border border-border px-3 text-sm hover:bg-muted disabled:opacity-50"
              >
                跑到结束
              </button>
            )}
          </div>
        )}
      </div>

      {confirming ? (
        <p className="mt-2 text-xs text-ink-3">
          跑到结束会一路推进到 <span className="num">{detail.progress.end}</span>，
          新生成的决策按「{approve === "all" ? "全部批准" : "全部驳回"}」处理，
          <strong className="font-medium">跑完不能再推进</strong>。
        </p>
      ) : null}

      {lines.length ? (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {lines.map((line) => (
            <li key={line.text} className={cn(TONE_CLASS[line.tone])}>
              {line.text}
            </li>
          ))}
        </ul>
      ) : detail.this_step ? (
        <p className="mt-2 text-xs text-ink-3">
          本次推进（{detail.this_step.trade_date}）没有发生任何事。
        </p>
      ) : null}
    </div>
  );
}
