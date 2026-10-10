"use client";

/**
 * 研报的所有者动作：分享 / 复制链接 / 撤销 / 导出。
 *
 * 两条与别处一致的规矩：
   * **撤销要内联二次确认**（不用 `window.confirm`：原生弹窗阻塞整页、样式不跟主题）；
   * **导出两处都能用**——Markdown 走 `<a download>`（公开页走 token 端点），
 *     PDF 是 `window.print()` + 打印样式（SPEC v1.32 拍板：不塞 Playwright 进服务端）。
 */

import { useCallback, useState } from "react";

import { api, apiBase, describeError } from "@/lib/api";
import { downloadName, markdownHref, shareUrl } from "@/lib/research";

export function ReportActions({
  reportId,
  reportHash,
  accountName,
  shareToken,
  onShareChange,
}: {
  reportId: string;
  reportHash: string;
  accountName: string;
  shareToken: string | null;
  onShareChange: (token: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  const share = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.shareReport(reportId);
      onShareChange(result.share_token);
      const link = shareUrl(window.location.origin, result.share_path);
      if (!link) return;
      try {
        await navigator.clipboard.writeText(link);
        setCopied(link);
      } catch {
        // 剪贴板被拒（非 https / 无权限）时**把链接显示出来让人手抄**，不静默失败
        setCopied(link);
        setError("浏览器拒绝了剪贴板，请手动复制下面的链接");
      }
    } catch (cause) {
      setError(describeError(cause, "分享失败，稍后重试"));
    } finally {
      setBusy(false);
    }
  }, [reportId, onShareChange]);

  const revoke = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.unshareReport(reportId);
      onShareChange(null);
      setConfirmRevoke(false);
      setCopied(null);
    } catch (cause) {
      setError(describeError(cause, "撤销失败，稍后重试"));
    } finally {
      setBusy(false);
    }
  }, [reportId, onShareChange]);

  return (
    <div className="flex flex-col gap-2" data-report-actions>
      <div className="flex flex-wrap items-center gap-2">
        {shareToken ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => void share()}
              className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs hover:bg-muted disabled:opacity-50"
            >
              复制分享链接
            </button>
            {confirmRevoke ? (
              <span className="flex items-center gap-2 text-xs text-ink-3">
                撤销后旧链接立刻失效，确定？
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void revoke()}
                  className="text-destructive underline disabled:opacity-50"
                >
                  撤销
                </button>
                <button type="button" onClick={() => setConfirmRevoke(false)} className="underline">
                  取消
                </button>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmRevoke(true)}
                className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs text-ink-3 hover:bg-muted"
              >
                撤销分享
              </button>
            )}
          </>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => void share()}
            className="h-8 rounded-[var(--radius)] bg-brand px-3 text-xs text-brand-ink disabled:opacity-50"
          >
            {busy ? "生成链接…" : "生成分享链接"}
          </button>
        )}

        <a
          href={markdownHref(apiBase(), reportId, shareToken)}
          download={downloadName(accountName, reportHash)}
          className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs leading-8 hover:bg-muted"
          data-export="markdown"
        >
          导出 Markdown
        </a>
        <button
          type="button"
          onClick={() => window.print()}
          className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs hover:bg-muted"
          data-export="print"
        >
          打印 / 存 PDF
        </button>
      </div>

      {copied ? (
        <p className="text-xs text-ink-3">
          已复制：<span className="font-mono break-all">{copied}</span>
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** 公开页的导出（无分享动作）：Markdown 走 token 端点，PDF 走打印。 */
export function PublicExport({
  token,
  accountName,
  reportHash,
}: {
  token: string;
  accountName: string;
  reportHash: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2" data-report-actions>
      <a
        href={markdownHref(apiBase(), "", token)}
        download={downloadName(accountName, reportHash)}
        className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs leading-8 hover:bg-muted"
        data-export="markdown"
      >
        导出 Markdown
      </a>
      <button
        type="button"
        onClick={() => window.print()}
        className="h-8 rounded-[var(--radius)] border border-border px-3 text-xs hover:bg-muted"
        data-export="print"
      >
        打印 / 存 PDF
      </button>
    </div>
  );
}
