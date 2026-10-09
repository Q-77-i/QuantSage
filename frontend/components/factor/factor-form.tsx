"use client";

import { Field, Check, Input, Select } from "@/components/ui/form-controls";
import { Button } from "@/components/ui/button";
import type { FactorQueryInput } from "@/lib/factor-report";

export interface FactorFormState {
  source: "event" | "price";
  direction: "reversal" | "momentum";
  start: string;
  end: string;
  costs: boolean;
}

/**
 * 筛选行：**一行管住整页**（dataviz：不做 per-chart filter，所有图对着同一份跑出来的报告）。
 *
 * 因子源与方向走地址栏（深链可分享、刷新不丢），窗口与费用是本次请求的参数，
 * 改动后点「运行」重取——**报告是算出来的**，不是随输入框实时变的。
 */
export function FactorForm({
  value,
  onChange,
  onRun,
  running,
  onSourceChange,
}: {
  value: FactorFormState;
  onChange: (next: FactorFormState) => void;
  onRun: () => void;
  running: boolean;
  onSourceChange: (source: FactorFormState["source"]) => void;
}) {
  return (
    <form
      className="mt-4 flex flex-wrap items-end gap-x-5 gap-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        onRun();
      }}
    >
      <Field label="因子源" htmlFor="fa-source">
        <Select
          id="fa-source"
          className="w-44"
          value={value.source}
          onChange={(event) => onSourceChange(event.target.value as FactorFormState["source"])}
        >
          <option value="event">事件信号（有向事件，池子薄）</option>
          <option value="price">价格动量 / 反转（全市场池）</option>
        </Select>
      </Field>

      {value.source === "price" ? (
        <Field label="方向" htmlFor="fa-direction">
          <Select
            id="fa-direction"
            className="w-28"
            value={value.direction}
            onChange={(event) =>
              onChange({ ...value, direction: event.target.value as FactorFormState["direction"] })
            }
          >
            <option value="reversal">反转（20 日）</option>
            <option value="momentum">动量（20 日）</option>
          </Select>
        </Field>
      ) : null}

      <Field label="起始日（缺省取语料起点）" htmlFor="fa-start">
        <Input
          id="fa-start"
          type="date"
          className="w-36"
          value={value.start}
          onChange={(event) => onChange({ ...value, start: event.target.value })}
        />
      </Field>
      <Field label="结束日（缺省取行情末端）" htmlFor="fa-end">
        <Input
          id="fa-end"
          type="date"
          className="w-36"
          value={value.end}
          onChange={(event) => onChange({ ...value, end: event.target.value })}
        />
      </Field>

      <div className="pb-1.5">
        <Check
          id="fa-costs"
          label="扣费用（毛/净双轨）"
          checked={value.costs}
          onChange={(costs) => onChange({ ...value, costs })}
        />
      </div>

      <Button type="submit" disabled={running} className="mb-0.5">
        {running ? "运行中…" : "运行"}
      </Button>
    </form>
  );
}

/** 表单 → 请求参数（方向只在价格源下带出，见 `factorQuery`）。 */
export function queryInputOf(value: FactorFormState): FactorQueryInput {
  return {
    source: value.source,
    direction: value.source === "price" ? value.direction : undefined,
    start: value.start || undefined,
    end: value.end || undefined,
    costs: value.costs,
  };
}
