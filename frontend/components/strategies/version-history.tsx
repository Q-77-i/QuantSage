"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { clearDescriptorCache } from "@/components/strategies/use-catalog";
import { api, describeError } from "@/lib/api";
import { dayStamp } from "@/lib/format";
import { diffLines, diffStats, versionLabel } from "@/lib/strategy-versions";
import type {
  StrategyVersionDetail,
  StrategyVersionDiff,
  StrategyVersionSummary,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 策略版本抽屉（5d）：版本列表 + 与当前 head 的统一 diff + 回滚。
 *
 * 口径：**回滚 = 把旧内容作为新版本追加**，不是把版本号倒回去；当前名字不随回滚改变。
 * 回滚后父组件会重新打开策略（`openStrategy`），并在这里清 descriptor 缓存——
 * 不清的话回测页/优化页还会拿着旧 `PARAMS` schema。
 */
export function VersionHistory({
  strategyId,
  currentVersion,
  onClose,
  onRolledBack,
}: {
  strategyId: string;
  currentVersion: number;
  onClose: () => void;
  onRolledBack: () => void;
}) {
  const [versions, setVersions] = useState<StrategyVersionSummary[] | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<StrategyVersionDetail | null>(null);
  const [diff, setDiff] = useState<StrategyVersionDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [rolling, setRolling] = useState(false);

  const loadList = useCallback(async () => {
    try {
      const rows = await api.strategyVersions(strategyId);
      setVersions(rows);
      const firstOther = rows.find((row) => row.version_no !== currentVersion);
      setSelected(firstOther?.version_no ?? rows[0]?.version_no ?? null);
    } catch (cause) {
      setError(describeError(cause, "版本列表加载失败。"));
      setVersions([]);
    }
  }, [currentVersion, strategyId]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    if (selected === null) return;
    let alive = true;
    setLoading(true);
    setError(null);
    setConfirming(false);
    Promise.all([
      api.strategyVersion(strategyId, selected),
      api.strategyVersionDiff(strategyId, selected),
    ])
      .then(([version, compared]) => {
        if (!alive) return;
        setDetail(version);
        setDiff(compared);
      })
      .catch((cause) => {
        if (alive) setError(describeError(cause, "版本内容加载失败。"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selected, strategyId]);

  const lines = useMemo(() => diffLines(diff?.diff ?? ""), [diff]);
  const stats = useMemo(() => diffStats(lines), [lines]);
  const isCurrent = selected === currentVersion;

  const rollback = async () => {
    if (selected === null) return;
    setRolling(true);
    setError(null);
    try {
      await api.rollbackStrategy(strategyId, selected, currentVersion);
      clearDescriptorCache();
      onRolledBack();
    } catch (cause) {
      setError(describeError(cause, "回滚失败：策略可能已在别处更新。"));
      setConfirming(false);
    } finally {
      setRolling(false);
    }
  };

  return (
    <>
      <button
        type="button"
        aria-label="关闭版本历史"
        className="fixed inset-0 z-30 cursor-default bg-black/20"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="策略版本历史"
        data-version-panel
        className="fixed inset-y-0 right-0 z-40 w-full max-w-[560px] overflow-y-auto border-l border-border bg-background p-5 shadow-xl"
      >
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="font-heading text-base font-semibold">版本历史</h2>
            <p className="mt-0.5 text-xs text-ink-3">
              当前 {versionLabel(currentVersion)} · 回滚 = 把旧内容作为新版本追加
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-8 rounded-[var(--radius)] border border-border px-2.5 text-sm hover:bg-muted"
          >
            关闭
          </button>
        </div>

        {error ? <p className="mt-3 text-sm text-destructive">{error}</p> : null}

        <div className="mt-4 grid gap-4 sm:grid-cols-[170px_minmax(0,1fr)]">
          <div className="space-y-1">
            {versions === null ? (
              <p className="text-xs text-ink-3">加载中…</p>
            ) : versions.length === 0 ? (
              <p className="text-xs text-ink-3">还没有版本记录。</p>
            ) : (
              versions.map((version) => (
                <button
                  key={version.id}
                  type="button"
                  data-version-row={version.version_no}
                  onClick={() => setSelected(version.version_no)}
                  className={cn(
                    "flex w-full items-center justify-between gap-2 rounded-[var(--radius)] border px-2.5 py-1.5 text-left text-sm",
                    selected === version.version_no
                      ? "border-brand bg-muted"
                      : "border-transparent hover:border-border hover:bg-muted/60",
                  )}
                >
                  <span className="num font-medium">{versionLabel(version.version_no)}</span>
                  <span className="text-xs text-ink-3">
                    {version.current ? "当前" : dayStamp(version.created_at)}
                  </span>
                </button>
              ))
            )}
          </div>

          <div className="min-w-0">
            {loading ? (
              <p className="text-xs text-ink-3">加载版本内容…</p>
            ) : detail && diff ? (
              <>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3">
                  <span className="num">{versionLabel(diff.to)}</span>
                  <span>对 {versionLabel(diff.from)}</span>
                  <span data-version-stats>
                    +{stats.added} / −{stats.removed}
                  </span>
                  {diff.params_changed ? <span className="text-warn">参数也变了</span> : null}
                </div>
                {lines.length ? (
                  <pre className="mt-2 max-h-[360px] overflow-auto rounded-[var(--radius)] border border-border p-2 text-xs leading-5">
                    {lines.map((line, index) => (
                      <div
                        key={`${index}-${line.text}`}
                        data-diff-kind={line.kind}
                        className={cn(
                          "whitespace-pre-wrap break-all",
                          line.kind === "add" && "bg-up/10 text-foreground",
                          line.kind === "remove" && "bg-down/10 text-ink-3",
                          line.kind === "hunk" && "text-ink-3",
                          line.kind === "context" && "text-ink-2",
                        )}
                      >
                        {line.text || " "}
                      </div>
                    ))}
                  </pre>
                ) : (
                  <p className="mt-2 text-xs text-ink-3">与当前版本一致，没有代码差异。</p>
                )}
                {isCurrent ? null : (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    {confirming ? (
                      <>
                        <button
                          type="button"
                          disabled={rolling}
                          onClick={() => void rollback()}
                          className="h-8 rounded-[var(--radius)] border border-destructive/50 px-3 text-sm text-destructive disabled:opacity-50"
                        >
                          {rolling ? "回滚中…" : `确认回滚到 ${versionLabel(selected ?? 0)}`}
                        </button>
                        <button
                          type="button"
                          disabled={rolling}
                          onClick={() => setConfirming(false)}
                          className="h-8 rounded-[var(--radius)] border border-border px-3 text-sm hover:bg-muted"
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirming(true)}
                        className="h-8 rounded-[var(--radius)] border border-border px-3 text-sm hover:bg-muted"
                      >
                        回滚到此版本
                      </button>
                    )}
                  </div>
                )}
              </>
            ) : null}
          </div>
        </div>
      </aside>
    </>
  );
}
