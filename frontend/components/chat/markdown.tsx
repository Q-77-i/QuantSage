"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 助手回答的 Markdown 渲染，排版在 `app/globals.css` 的 `.md-body`。
 *
 * 不覆盖 `components`：默认渲染已经够用（且不引 `rehype-raw`，HTML 一律转义）。
 */
export const Markdown = memo(function Markdown({ children }: { children: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
});
