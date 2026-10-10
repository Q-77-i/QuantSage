"use client";

import { useEffect, useState } from "react";

import { Check, Field, Input, Select } from "@/components/ui/form-controls";
import { api, describeError } from "@/lib/api";
import { STRATEGIES, symbolName } from "@/lib/backtest-form";
import { STRATEGY_PARAM_DEFAULTS, parseSymbols, paperRequest } from "@/lib/paper";
import type { PaperFormState } from "@/lib/paper";
import type { PaperAccountDetail, StrategySummary } from "@/lib/types";

/**
 * 建会话：一次把「钱、池子、策略、区间」定死（后端把它们当成契约存进 `config`）。
 *
 * 三处提示照抄后端会做的校验，但**只在本地拦最明显的那几类**（空名字、坏代码、超 20 只）：
 * 「配额买不起一手」这类需要行情的判断归后端（它会指出是哪一只），前端不重写那套话术。
 */

const SAMPLE = "600519 000001";

export function PaperForm({ onCreated }: { onCreated: (detail: PaperAccountDetail) => void }) {
  const [form, setForm] = useState<PaperFormState>({
    name: "双均线纸上交易",
    initialCash: "500000",
    symbolsText: SAMPLE,
    strategy: "ma_cross",
    strategyId: "",
    params: { fast: 5, slow: 20 },
    start: "2026-07-01",
    end: "",
    fees: true,
    slippage: true,
  });
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 用户策略要在建会话时就选出「库里那一条」（模拟盘只认库里的源码，同回测页的契约）
  useEffect(() => {
    api
      .strategies()
      .then(setStrategies)
      .catch(() => setStrategies([]));
  }, []);

  const parsed = parseSymbols(form.symbolsText);

  async function submit() {
    const { body, error: invalid } = paperRequest(form);
    if (!body) {
      setError(invalid);
      return;
    }
    setBusy(true);
    try {
      const detail = await api.createPaperAccount(body);
      onCreated(detail);
      setError(null);
    } catch (cause) {
      // 后端的 422 话术（配额买不起一手 / 本地没有这只标的 / 区间超过 250 个交易日）直接显示
      setError(describeError(cause, "建会话失败：后端未启动或网络不通。"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="mt-3 rounded-[var(--radius)] border border-border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="会话名" htmlFor="paper-name">
          <Input
            id="paper-name"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
        </Field>

        <Field label="初始资金（元）" htmlFor="paper-cash">
          <Input
            id="paper-cash"
            inputMode="decimal"
            value={form.initialCash}
            onChange={(event) => setForm({ ...form, initialCash: event.target.value })}
          />
        </Field>

        <Field label="起点（成交区间从这天开始）" htmlFor="paper-start">
          <Input
            id="paper-start"
            type="date"
            value={form.start}
            onChange={(event) => setForm({ ...form, start: event.target.value })}
          />
        </Field>

        <Field
          label="标的池（1–20 只，六位代码）"
          htmlFor="paper-symbols"
          error={
            parsed.invalid.length
              ? `不是六位代码：${parsed.invalid.join("、")}`
              : parsed.overflow.length
                ? `超出 20 只：${parsed.overflow.join("、")}`
                : undefined
          }
        >
          <Input
            id="paper-symbols"
            value={form.symbolsText}
            onChange={(event) => setForm({ ...form, symbolsText: event.target.value })}
          />
        </Field>

        <Field label="策略" htmlFor="paper-strategy">
          <Select
            id="paper-strategy"
            value={form.strategy}
            onChange={(event) => {
              // 切策略**整份换掉参数**：留着上一个策略的参数会被后端按未知键判 422
              const strategy = event.target.value as PaperFormState["strategy"];
              setForm({ ...form, strategy, params: { ...STRATEGY_PARAM_DEFAULTS[strategy] } });
            }}
          >
            {STRATEGIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
            <option value="user">我的策略</option>
          </Select>
        </Field>

        {form.strategy === "user" ? (
          <Field
            label="选一条已保存的策略（用它保存的参数）"
            htmlFor="paper-strategy-id"
          >
            <Select
              id="paper-strategy-id"
              value={form.strategyId}
              onChange={(event) => setForm({ ...form, strategyId: event.target.value })}
            >
              <option value="">（选一条）</option>
              {strategies.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : (
          <Field label="区间终点（留空 = 用本地行情末端）" htmlFor="paper-end">
            <Input
              id="paper-end"
              type="date"
              value={form.end}
              onChange={(event) => setForm({ ...form, end: event.target.value })}
            />
          </Field>
        )}

        {form.strategy === "ma_cross" ? (
          <>
            <Field label="快线周期" htmlFor="paper-fast">
              <Input
                id="paper-fast"
                inputMode="numeric"
                value={String(form.params.fast ?? 5)}
                onChange={(event) =>
                  setForm({ ...form, params: { ...form.params, fast: Number(event.target.value) } })
                }
              />
            </Field>
            <Field label="慢线周期" htmlFor="paper-slow">
              <Input
                id="paper-slow"
                inputMode="numeric"
                value={String(form.params.slow ?? 20)}
                onChange={(event) =>
                  setForm({ ...form, params: { ...form.params, slow: Number(event.target.value) } })
                }
              />
            </Field>
          </>
        ) : null}

        {form.strategy === "event_driven" ? (
          <>
            <Field label="最低评分" htmlFor="paper-score">
              <Input
                id="paper-score"
                inputMode="decimal"
                value={String(form.params.min_score ?? 50)}
                onChange={(event) =>
                  setForm({
                    ...form,
                    params: { ...form.params, min_score: Number(event.target.value) },
                  })
                }
              />
            </Field>
            <Field label="持有天数" htmlFor="paper-hold">
              <Input
                id="paper-hold"
                inputMode="numeric"
                value={String(form.params.hold_days ?? 5)}
                onChange={(event) =>
                  setForm({
                    ...form,
                    params: { ...form.params, hold_days: Number(event.target.value) },
                  })
                }
              />
            </Field>
          </>
        ) : null}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4 text-xs text-ink-2">
        <Check
          id="paper-fees"
          label="佣金 + 印花税"
          checked={form.fees}
          onChange={(fees) => setForm({ ...form, fees })}
        />
        <Check
          id="paper-slippage"
          label="滑点 5bps"
          checked={form.slippage}
          onChange={(slippage) => setForm({ ...form, slippage })}
        />
        <span className="text-ink-3">
          池子里每只标的拿等额额度（初始资金 ÷ 只数），单只买不起一手时后端会指出是哪只。
        </span>
      </div>

      {parsed.symbols.length ? (
        <p className="mt-3 text-xs text-ink-3">
          {parsed.symbols.length} 只标的：
          {parsed.symbols.map((symbol) => `${symbol}${symbolName(symbol) === symbol ? "" : ` ${symbolName(symbol)}`}`).join("、")}
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="mt-3 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={busy}
        className="mt-4 h-8 rounded-[var(--radius)] bg-brand px-3 text-sm text-brand-ink disabled:opacity-50"
      >
        {busy ? "建会话中…" : "建会话"}
      </button>
    </form>
  );
}
