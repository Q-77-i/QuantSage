"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { ApprovalPayload } from "@/lib/agent-run";

/**
 * 人工审批卡（M8c）：**形态跟随 M6 的决策卡**——实心 = 等你动手。
 *
 * 措辞命中与预算超限**共用同一张卡**，只在标题上区分：用户要做的事是同一件
 * （继续还是收尾），把它拆成两张卡只会让人多认一次界面。
 *
 * 但要说清「继续」意味着什么——这两件事的代价不同（措辞继续=照发；预算继续=还会花钱）。
 */
const KIND_TITLE: Record<string, string> = {
  wording: "措辞需要你确认",
  budget: "预算已到上限",
};

const KIND_HINT: Record<string, string> = {
  wording: "报告正文里出现了绝对化表述或买卖指令。选「继续」表示你知道并接受这段措辞。",
  budget: "继续会再花掉一份预算；选「用现有结果收尾」则按已取到的证据出报告。",
};

export function ApprovalCard({
  payload,
  busy,
  onDecide,
}: {
  payload: ApprovalPayload;
  busy: boolean;
  onDecide: (choice: "continue" | "stop") => void;
}) {
  const [note, setNote] = useState("");
  const title = KIND_TITLE[payload.kind] ?? "需要你确认";
  const hint = KIND_HINT[payload.kind] ?? "";

  return (
    <section
      data-approval={payload.kind}
      className="flex flex-col gap-3 rounded-[var(--radius)] border border-warn/50 bg-warn/10 p-3"
    >
      <header className="flex flex-col gap-1">
        <h2 className="font-heading text-sm font-semibold text-warn">{title}</h2>
        <p className="text-xs text-ink-2">{payload.detail}</p>
        {hint ? <p className="text-xs text-ink-3">{hint}</p> : null}
      </header>
      <label className="flex flex-col gap-1 text-xs text-ink-2">
        备注（可选，会记进这次运行）
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={200}
          className="rounded-[var(--radius)] border border-border bg-card px-2 py-1 text-sm"
        />
      </label>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => onDecide("continue")}>
          {payload.kind === "budget" ? "加预算继续" : "继续"}
        </Button>
        <Button variant="outline" disabled={busy} onClick={() => onDecide("stop")}>
          {payload.kind === "budget" ? "用现有结果收尾" : "放弃这段"}
        </Button>
      </div>
    </section>
  );
}
