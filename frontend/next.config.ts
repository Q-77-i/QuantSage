import type { NextConfig } from "next";

/**
 * 安全响应头（P3-E8a，SPEC §8.2）。
 *
 * 与后端 `app/core/security.py` 的 `SECURITY_HEADERS` 是同一组里减去 CSP 的那四条：
 * **前端不加 CSP**——Next 15 + Monaco + ECharts 要 nonce/hash 铺开才不裂，本地 http 部署下
 * 收益极低（SPEC §8.2 已写明这条取舍，不再两处各讲一遍）。
 *
 * 打印页（`/print/[id]`）由服务端 Playwright 打开，不是 iframe，`X-Frame-Options: DENY`
 * 不影响它。
 */
/**
 * CSP：本地前端与 API 不同源（页面 3001、API 8000），`connect-src` 必须显式放行。
 * `'unsafe-inline'` / `'unsafe-eval'` 是 Next 的水合脚本与 Monaco 的代价；它挡的是
 * **外部脚本注入、object/iframe/base 劫持**，不是完整的 XSS 防线——完整方案要 nonce，
 * 留待后续。`worker-src blob:` 给 Monaco 的 worker。
 */
const API_ORIGINS = "http://127.0.0.1:8000 http://localhost:8000 http://*:8000 https://*:8000";
const contentSecurityPolicy = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
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

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "geolocation=(), camera=(), microphone=()" },
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
