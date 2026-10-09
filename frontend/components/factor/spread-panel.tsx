"use client";

import { useTheme } from "next-themes";
import { useMemo } from "react";

import { chartTokens } from "@/lib/chart-theme";
import { longShortLines } from "@/lib/factor-report";
import { num } from "@/lib/format";
import type { FactorReport } from "@/lib/types";

import { CurveChart } from "./curve-chart";
import type { CurveSeries } from "./curve-chart";

/**
 * 多空价差曲线（Q5 − Q1）：**单独一张图**。
 *
 * 与分层曲线同单位但不同性质（一个价差、一个组合净值），叠在一张图上价差会被组合曲线压平——
 * **绝不双轴**（dataviz 的第一反模式），拆成两张。
 *
 * 毛/净这里**同时画**（只有两条，画得下）：**颜色定身份**（净 = `series2` 橙 / 毛 = `series3` 紫）
 * **+ 线型做二次编码**（实线 = 净、虚线 = 毛）。关掉费用时只剩毛一条，图例也少一项。
 * 「不可交易」读的是响应字段（`tradable`），不是前端写死。
 */
export function SpreadPanel({ report }: { report: FactorReport }) {
  const isDark = useTheme().resolvedTheme === "dark";
  const tokens = chartTokens(isDark);
  const payload = useMemo(() => longShortLines(report), [report]);

  const series = useMemo(() => {
    const list: CurveSeries[] = [];
    if (payload.net) {
      // **颜色定身份 + 线型做二次编码**（回测页三条曲线的既有约定）：
      // 一度只用线型区分，结果在每个小尺寸承载面上都读不出来——30px 图例（虚线节奏太密）
      // 与 8px tooltip 色块（根本画不下线型）先后被用户逮到（2026-10-10 两轮反馈）。
      // 毛用 `series3` 而不是蓝：本页蓝色已承载两个含义（IC 负值 / 分层 Q1–Q5），
      // 再让「毛」用蓝就是一色三义。橙↔紫实测 CVD ΔE 27.0（浅）/ 26.2（深），六项全 PASS。
      list.push({ name: "净价差", values: payload.net, color: tokens.series2 });
      list.push({ name: "毛价差", values: payload.gross, color: tokens.series3, dashed: true });
    } else {
      list.push({ name: "毛价差", values: payload.gross, color: tokens.series2 });
    }
    return list;
  }, [payload, tokens]);

  return (
    <div className="mt-2">
      {payload.dates.length === 0 ? (
        <p className="rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
          本窗口没有有效信号日，没有价差可画。
        </p>
      ) : (
        <CurveChart
          dates={payload.dates}
          series={series}
          height={240}
          ariaLabel={`多空价差净值，共 ${payload.dates.length} 个信号日`}
        />
      )}
      <p className="mt-1 text-xs text-ink-3">
        价差 t 值 {num(report.long_short.t_stat, { signed: true })} ·{" "}
        {report.long_short.tradable ? (
          "该组合可交易"
        ) : (
          <>
            A 股不可做空：这是<strong className="font-medium text-ink-2">统计量</strong>
            ，不是可交易组合
          </>
        )}
      </p>
    </div>
  );
}
