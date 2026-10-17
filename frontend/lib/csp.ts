/**
 * CSP 的唯一拼装点。
 *
 * 生产用 nonce + `strict-dynamic`：Next 会从请求头的 CSP 里取 nonce 注入自己的脚本；
 * 开发保留 `'unsafe-inline'`——Next 的 HMR / React Refresh 会注入内联脚本，nonce
 * 铺不到那些脚本上，强行收紧只会让本地开发白屏。
 *
 * `'unsafe-eval'` 是 Monaco 的代价；`worker-src blob:` 也是 Monaco 的 worker。
 * 这条 CSP 挡的是外部脚本注入与 object/iframe/base 劫持，不是完整的 XSS 防线。
 */

export const API_ORIGINS =
  "http://127.0.0.1:8000 http://localhost:8000 http://*:8000 https://*:8000";

export type CspOptions = {
  nonce?: string;
  production: boolean;
};

export function buildCsp({ nonce, production }: CspOptions): string {
  if (production && !nonce) {
    throw new Error("production CSP requires a nonce");
  }
  const scriptSrc = production
    ? `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' 'unsafe-eval' blob:`
    : "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:";
  return [
    "default-src 'self'",
    scriptSrc,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self' ${API_ORIGINS}`,
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}
