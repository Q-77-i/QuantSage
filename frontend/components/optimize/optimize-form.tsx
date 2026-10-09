"use client";

import { Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Check, Field, Input, Select } from "@/components/ui/form-controls";
import { SAMPLE_SYMBOLS, STRATEGIES } from "@/lib/backtest-form";
import { MAX_AXES, gridSize } from "@/lib/optimize-form";
import type { AxisInput, BatchFormState, BatchPick, GridFormState, ParamDescriptor } from "@/lib/optimize-form";
import type { StrategySummary } from "@/lib/types";

/**
 * 网格 / 批量表单：**一行 filters 管住下面所有的图**（dataviz：过滤器不放进图的卡片里）。
 *
 * 参数在这里**全部显式发出**（默认值预填、随请求走），与回测页同一姿态：
 * 用户看得见将要跑的是什么，不依赖后端缺省；两处缺省万一漂移，写死的那份至少是可见的。
 *
 * 被选作参数轴的那个参数，它的基座输入框**收起**——后端对「同一个参数既在基座又在轴上」
 * 是直接 422 的（`grid_cells`），与其让用户点了运行才撞，不如在界面上就不给这个机会。
 */
export function OptimizeForm({
  mode,
  grid,
  batch,
  picks,
  userStrategies,
  descriptors,
  loading,
  errors,
  running,
  onGridChange,
  onBatchChange,
  onSubmit,
  onStop,
}: {
  mode: "grid" | "batch";
  grid: GridFormState;
  batch: BatchFormState;
  picks: BatchPick[];
  userStrategies: StrategySummary[];
  descriptors: ParamDescriptor[];
  loading: boolean;
  errors: Record<string, string>;
  running: boolean;
  onGridChange: (next: GridFormState) => void;
  onBatchChange: (next: BatchFormState) => void;
  onSubmit: () => void;
  onStop: () => void;
}) {
  const patchGrid = (part: Partial<GridFormState>) => onGridChange({ ...grid, ...part });
  const patchBatch = (part: Partial<BatchFormState>) => onBatchChange({ ...batch, ...part });
  const axisParams = grid.axes.map((axis) => axis.param);
  const baseFields = descriptors.filter((field) => !axisParams.includes(field.key));
  const size = gridSize(grid.axes);

  function setAxis(index: number, part: Partial<AxisInput>) {
    patchGrid({ axes: grid.axes.map((axis, i) => (i === index ? { ...axis, ...part } : axis)) });
  }

  return (
    <form
      className="rounded-[var(--radius)] border border-border bg-card p-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {mode === "grid" ? (
        <>
          <div className="flex flex-wrap items-start gap-x-5 gap-y-3">
            <Field label="策略" htmlFor="op-strategy" error={errors.strategy}>
              <StrategySelect
                id="op-strategy"
                strategy={grid.strategy}
                strategyId={grid.strategyId}
                items={userStrategies}
                onChange={(strategy, strategyId) =>
                  patchGrid({
                    strategy,
                    strategyId,
                    // 换策略即整组重建：两套参数没有可复用的语义（同回测页的 switchStrategy）
                    baseParams: {},
                    axes: [{ param: "", values: "" }],
                  })
                }
              />
            </Field>

            <Field label="标的" htmlFor="op-symbol" error={errors.symbol}>
              <div className="flex h-8 items-center gap-1.5">
                <Input
                  id="op-symbol"
                  className="num w-24"
                  inputMode="numeric"
                  autoComplete="off"
                  value={grid.symbol}
                  aria-invalid={Boolean(errors.symbol)}
                  onChange={(event) => patchGrid({ symbol: event.target.value })}
                />
                {SAMPLE_SYMBOLS.map((symbol) => (
                  <button
                    key={symbol.code}
                    type="button"
                    title={symbol.code}
                    onClick={() => patchGrid({ symbol: symbol.code })}
                    className="rounded-[var(--radius)] px-1.5 py-1 text-xs text-ink-3 transition-colors hover:bg-muted hover:text-foreground"
                  >
                    {symbol.name}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="起始日" htmlFor="op-start">
              <Input
                id="op-start"
                type="date"
                className="w-36"
                value={grid.start}
                onChange={(event) => patchGrid({ start: event.target.value })}
              />
            </Field>

            <Field label="结束日" htmlFor="op-end" error={errors.end}>
              <Input
                id="op-end"
                type="date"
                className="w-36"
                value={grid.end}
                aria-invalid={Boolean(errors.end)}
                onChange={(event) => patchGrid({ end: event.target.value })}
              />
            </Field>
          </div>

          <div className="mt-3 flex flex-wrap items-start gap-x-5 gap-y-3 border-t border-border pt-3">
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-xs text-ink-3">
                参数轴 {loading ? "（读取参数中…）" : `（最多 ${MAX_AXES} 条）`}
              </legend>
              <div className="flex flex-col gap-2">
                {grid.axes.map((axis, index) => (
                  <div key={index} className="flex items-start gap-2">
                    <Field label="参数" htmlFor={`op-axis-${index}`} error={errors[`axis-${index}`]}>
                      {/* 窄屏要能收缩换行：写死 `w-28 + w-64` 在 390px 下会横溢出 140px
                          （界面验证逮到的），故取值框用 `min-w-0 flex-1` 并在小屏换行 */}
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Select
                          id={`op-axis-${index}`}
                          className="w-24 sm:w-28"
                          value={axis.param}
                          onChange={(event) => setAxis(index, { param: event.target.value })}
                        >
                          <option value="">选择参数</option>
                          {descriptors.map((field) => (
                            <option key={field.key} value={field.key}>
                              {field.label}
                            </option>
                          ))}
                        </Select>
                        <Input
                          id={`op-axis-${index}-values`}
                          aria-label={`${axis.param || "参数"} 的取值`}
                          className="min-w-0 flex-1 sm:w-64 sm:flex-none"
                          placeholder="用逗号分隔，如 3, 5, 8"
                          value={axis.values}
                          onChange={(event) => setAxis(index, { values: event.target.value })}
                        />
                        {grid.axes.length > 1 ? (
                          <button
                            type="button"
                            title="删掉这条轴"
                            aria-label="删掉这条轴"
                            onClick={() => patchGrid({ axes: grid.axes.filter((_, i) => i !== index) })}
                            className="mt-0.5 rounded-[var(--radius)] p-1 text-ink-3 hover:bg-muted hover:text-foreground"
                          >
                            <X className="size-3.5" />
                          </button>
                        ) : null}
                      </div>
                    </Field>
                  </div>
                ))}
                {grid.axes.length < MAX_AXES ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-7 w-fit px-2 text-xs"
                    onClick={() => patchGrid({ axes: [...grid.axes, { param: "", values: "" }] })}
                  >
                    <Plus className="mr-1 size-3.5" />
                    加一条轴
                  </Button>
                ) : null}
              </div>
            </fieldset>

            {baseFields.length > 0 ? (
              <fieldset className="flex flex-col gap-1">
                <legend className="mb-1 text-xs text-ink-3">基座参数（不扫的那些）</legend>
                <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
                  {baseFields.map((field) => (
                    <Field key={field.key} label={field.label} htmlFor={`op-base-${field.key}`}>
                      <Input
                        id={`op-base-${field.key}`}
                        className="num w-24"
                        inputMode="decimal"
                        value={grid.baseParams[field.key] ?? ""}
                        placeholder={field.fallback === null ? "" : String(field.fallback)}
                        onChange={(event) =>
                          patchGrid({
                            baseParams: { ...grid.baseParams, [field.key]: event.target.value },
                          })
                        }
                      />
                    </Field>
                  ))}
                </div>
              </fieldset>
            ) : null}
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-start gap-x-5 gap-y-3">
          <Field label="标的（每行一个，或用逗号分隔）" htmlFor="op-symbols" error={errors.symbols}>
            <textarea
              id="op-symbols"
              rows={3}
              className="w-56 rounded-[var(--radius)] border border-border bg-card px-2 py-1.5 text-sm text-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none aria-invalid:border-destructive"
              aria-invalid={Boolean(errors.symbols)}
              value={batch.symbols}
              onChange={(event) => patchBatch({ symbols: event.target.value })}
            />
          </Field>

          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-xs text-ink-3">策略（可多选）</legend>
            <div className="flex flex-col gap-1.5">
              {picks.map((pick) => (
                <Check
                  key={pick.key}
                  id={`op-pick-${pick.key}`}
                  label={pick.label}
                  checked={batch.picks.includes(pick.key)}
                  onChange={(checked) =>
                    patchBatch({
                      picks: checked
                        ? [...batch.picks, pick.key]
                        : batch.picks.filter((key) => key !== pick.key),
                    })
                  }
                />
              ))}
            </div>
            {errors.picks ? <p className="text-xs text-destructive">{errors.picks}</p> : null}
          </fieldset>

          <Field label="起始日" htmlFor="op-bstart">
            <Input
              id="op-bstart"
              type="date"
              className="w-36"
              value={batch.start}
              onChange={(event) => patchBatch({ start: event.target.value })}
            />
          </Field>

          <Field label="结束日" htmlFor="op-bend" error={errors.end}>
            <Input
              id="op-bend"
              type="date"
              className="w-36"
              value={batch.end}
              onChange={(event) => patchBatch({ end: event.target.value })}
            />
          </Field>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-3">
        {running ? (
          <Button type="button" variant="outline" onClick={onStop}>
            停止
          </Button>
        ) : (
          <Button type="submit" disabled={Boolean(errors.grid) || Boolean(errors.picks)}>
            运行
          </Button>
        )}
        <p className="text-xs text-ink-3">
          {mode === "grid"
            ? size > 0
              ? `本次将跑 ${size} 格（并发 2，逐格出结果）`
              : "填好参数轴后开始"
            : "每个标的 × 每条策略各一格"}
        </p>
      </div>

      {errors.grid ? <p className="mt-2 text-xs text-destructive">{errors.grid}</p> : null}
    </form>
  );
}

/** 策略选择：内置两条 + 我的策略。用户策略在选项里带 `user:<id>`，与批量模式同一套键 */
function StrategySelect({
  id,
  strategy,
  strategyId,
  items,
  onChange,
}: {
  id: string;
  strategy: GridFormState["strategy"];
  strategyId: string | null;
  /** 我的策略（由页面统一取一次，两个模式共用） */
  items: StrategySummary[];
  onChange: (strategy: GridFormState["strategy"], strategyId: string | null) => void;
}) {
  const value = strategy === "user" ? `user:${strategyId ?? ""}` : strategy;

  return (
    <Select
      id={id}
      className="w-44"
      value={value}
      onChange={(event) => {
        const raw = event.target.value;
        if (raw.startsWith("user:")) onChange("user", raw.slice(5));
        else onChange(raw as GridFormState["strategy"], null);
      }}
    >
      {STRATEGIES.map((item) => (
        <option key={item.value} value={item.value}>
          {item.label}
        </option>
      ))}
      {items.length > 0 ? <option disabled value="">── 我的策略 ──</option> : null}
      {items.map((item) => (
        <option key={item.id} value={`user:${item.id}`}>
          {item.name}
        </option>
      ))}
    </Select>
  );
}
