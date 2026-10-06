"use client";

import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";

// 与后端 ChatRequest 的 max_length 对齐，免得白跑一趟 422
const MAX_LENGTH = 4000;

export function Composer({
  disabled,
  isStreaming,
  onSend,
  onStop,
}: {
  disabled: boolean;
  isStreaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [draft, setDraft] = useState("");
  const composingRef = useRef(false);
  const justComposedRef = useRef(false);

  function submit() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    onSend(text);
  }

  /**
   * 这一次回车是不是「上屏候选」而不是「发送」。
   *
   * 三层都要，缺一不可：
   *   * `isComposing`——常见情形，但 Safari 在候选上屏的**同一次**按键里会先结束组合
   *     再补一个 keydown，此时它已经是 false；
   *   * `composingRef`——keydown 早于 compositionstart 的浏览器；
   *   * `justComposedRef`——上面那个 Safari 时序的兜底，只挡组合刚结束的那一帧。
   */
  function isImeEnter(event: React.KeyboardEvent<HTMLTextAreaElement>): boolean {
    return (
      event.nativeEvent.isComposing ||
      composingRef.current ||
      justComposedRef.current ||
      event.nativeEvent.keyCode === 229 // 按键仍由输入法接管（老式信号，仍有用）
    );
  }

  return (
    <div className="border-t border-border p-3">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <label htmlFor="composer" className="sr-only">
          输入问题
        </label>
        <textarea
          id="composer"
          rows={2}
          maxLength={MAX_LENGTH}
          value={draft}
          disabled={disabled}
          placeholder="问行情，查事件。例如「贵州茅台最近行情怎么样」"
          onChange={(event) => setDraft(event.target.value)}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
            justComposedRef.current = true;
            requestAnimationFrame(() => {
              justComposedRef.current = false;
            });
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey) return;
            if (isImeEnter(event)) return;
            event.preventDefault();
            submit();
          }}
          className="min-h-9 flex-1 resize-none rounded-[var(--radius)] border border-border bg-card px-3 py-2 text-sm outline-none placeholder:text-ink-3 focus-visible:border-ring disabled:opacity-60"
        />

        {isStreaming ? (
          <Button variant="outline" onClick={onStop}>
            停止
          </Button>
        ) : (
          <Button onClick={submit} disabled={disabled || !draft.trim()}>
            发送
          </Button>
        )}
      </div>
    </div>
  );
}
