import type { NextConfig } from "next";

/**
 * 安全响应头（P3-E8a，SPEC §8.2）。
 *
 * CSP 不在 next.config 里写死：生产按请求生成 nonce（`middleware.ts`），开发保留
 * HMR 需要的 `'unsafe-inline'`（`lib/csp.ts` 按 NODE_ENV 分支）。这里只放其余四条。
 *
 * 打印页（`/print/[id]`）由服务端 Playwright 打开，不是 iframe，`X-Frame-Options: DENY`
 * 不影响它。
 */
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "geolocation=(), camera=(), microphone=()" },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
