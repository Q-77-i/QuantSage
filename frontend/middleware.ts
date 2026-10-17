import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { buildCsp } from "@/lib/csp";

/**
 * 生产环境按请求生成 nonce 并写进 CSP；开发环境走 `lib/csp.ts` 的折中版。
 *
 * CSP 同时放在**请求头**与响应头：Next 从请求头的 CSP 里取 nonce 注入脚本，
 * 浏览器只看响应头。只放响应头的话，脚本拿不到 nonce，页面会被自己的 CSP 拦掉。
 */
export function middleware(request: NextRequest) {
  const production = process.env.NODE_ENV === "production";
  const nonce = production ? btoa(crypto.randomUUID()) : undefined;
  const csp = buildCsp({ nonce, production });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", csp);
  if (nonce) {
    requestHeaders.set("x-nonce", nonce);
  }

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
