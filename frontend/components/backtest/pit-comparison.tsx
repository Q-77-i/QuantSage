"use client";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { amount, count, num, pct, pp } from "@/lib/format";
import type { BacktestReport, PitComparison } from "@/lib/types";

/**
 * PIT 与非 PIT 的口径对比——本项目的护城河，页面上的头号展示位。
 *
 * **文案一律中性**：标题写「差异」，不写「虚高」。实测非 PIT 的期末权益并不总是更高
 * （600519 上就低了 0.68%），把方向写死会让页面上出现的负数看起来像 bug。
 * 「看到未来」改变的是**入场日**，入场日的优劣取决于样本。
 */
export function PitComparisonSection({ report }: { report: BacktestReport }) {
  const comparison = report.pit_comparison;

  return (
    <section className="border-t border-border pt-4">
      <h2 className="font-heading text-base font-semibold">PIT 与非 PIT 的差异</h2>

      {comparison ? (
        <Body report={report} comparison={comparison} />
      ) : (
        <p className="mt-3 text-sm text-ink-2">
          {report.meta.strategy === "ma_cross"
            ? "双均线不消费事件语料，两种模式必然得到同一份结果，因此不做无意义的二次回测。要看这个对比，请选事件驱动策略。"
            : "本次只跑了单一模式。把模式改成「PIT / 非 PIT 对比」再运行即可看到两口径的差异。"}
        </p>
      )}
    </section>
  );
}

function Body({ report, comparison }: { report: BacktestReport; comparison: PitComparison }) {
  const { pit_metrics, non_pit_metrics, delta, entry_dates } = comparison;
  const onlyPit = entry_dates.pit.filter((date) => !entry_dates.non_pit.includes(date));
  const onlyNonPit = entry_dates.non_pit.filter((date) => !entry_dates.pit.includes(date));

  return (
    <>
      <p className="mt-3 flex flex-wrap items-baseline gap-x-2 text-sm text-ink-2">
        <span>期末权益差异（非 PIT 相对 PIT）</span>
        <span className="num text-lg text-foreground">
          {pct(delta.final_equity_pct, { signed: true })}
        </span>
        <span className="num text-xs text-ink-3">
          {amount(delta.final_equity_abs, { signed: true })} 元
        </span>
      </p>

      <TableShell head={["指标", "PIT", "非 PIT", "差异"]}>
        <Row>
          <Cell className="text-ink-2">总收益</Cell>
          <Cell numeric>{pct(pit_metrics.total_return, { signed: true })}</Cell>
          <Cell numeric>{pct(non_pit_metrics.total_return, { signed: true })}</Cell>
          <Cell numeric>{pp(delta.total_return_pp, { signed: true })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">年化</Cell>
          <Cell numeric>{pct(pit_metrics.annual_return, { signed: true })}</Cell>
          <Cell numeric>{pct(non_pit_metrics.annual_return, { signed: true })}</Cell>
          <Cell numeric>{pp(delta.annual_return_pp, { signed: true })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">最大回撤</Cell>
          <Cell numeric>{pct(pit_metrics.max_drawdown)}</Cell>
          <Cell numeric>{pct(non_pit_metrics.max_drawdown)}</Cell>
          <Cell numeric>{pp(delta.max_drawdown_pp, { signed: true })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">夏普</Cell>
          <Cell numeric>{num(pit_metrics.sharpe, { signed: true })}</Cell>
          <Cell numeric>{num(non_pit_metrics.sharpe, { signed: true })}</Cell>
          <Cell numeric>{num(delta.sharpe_abs, { signed: true })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">胜率</Cell>
          <Cell numeric>{pct(pit_metrics.win_rate)}</Cell>
          <Cell numeric>{pct(non_pit_metrics.win_rate)}</Cell>
          <Cell numeric>{pp(delta.win_rate_pp, { signed: true })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">交易次数</Cell>
          <Cell numeric>{count(pit_metrics.trade_count)}</Cell>
          <Cell numeric>{count(non_pit_metrics.trade_count)}</Cell>
          <Cell numeric>{num(delta.trade_count, { signed: true, digits: 0 })}</Cell>
        </Row>
        <Row>
          <Cell className="text-ink-2">期末权益</Cell>
          <Cell numeric>{amount(pit_metrics.final_equity)}</Cell>
          <Cell numeric>{amount(non_pit_metrics.final_equity)}</Cell>
          <Cell numeric>{amount(delta.final_equity_abs, { signed: true })}</Cell>
        </Row>
      </TableShell>

      <div className="mt-3 text-xs text-ink-2">
        <p>
          <span className="text-ink-3">入场日</span>{" "}
          <span className="num">
            PIT {count(entry_dates.pit.length)} 次 · 非 PIT {count(entry_dates.non_pit.length)} 次
          </span>
          {onlyPit.length === 0 && onlyNonPit.length === 0 ? (
            <span className="text-ink-3">（两口径入场日完全相同）</span>
          ) : (
            <>
              {onlyPit.length > 0 && (
                <>
                  {" "}
                  <span className="text-ink-3">仅 PIT 有</span>{" "}
                  <span className="num">{onlyPit.join("、")}</span>
                </>
              )}
              {onlyNonPit.length > 0 && (
                <>
                  {" "}
                  <span className="text-ink-3">仅非 PIT 有</span>{" "}
                  <span className="num">{onlyNonPit.join("、")}</span>
                </>
              )}
            </>
          )}
        </p>
        <p className="mt-1.5 text-ink-3">
          {describeCutoff(report.meta.cutoff_field)}两口径的差异只在入场时点：看到未来不等于赚得更多，
          入场日不同，孰优孰劣取决于样本。
        </p>
      </div>
    </>
  );
}

function describeCutoff(cutoff: string): string {
  if (cutoff === "available_at") {
    return "PIT 只用当时已经公开的事件（available_at 早于当日 15:00）；非 PIT 改用事发时间，等于允许使用尚未公开的信息。";
  }
  return "本次跑的是非 PIT：事件按事发时间可见，等于允许使用尚未公开的信息。";
}
