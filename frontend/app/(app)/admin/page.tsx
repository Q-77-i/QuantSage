"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback } from "react";

import { RejectionsPanel } from "@/components/admin/rejections-panel";
import { UsagePanel } from "@/components/admin/usage-panel";
import { useAuth } from "@/components/auth-provider";

/**
 * 运维面（E2b）：配额视图 + 拒绝记录。**只读**——没有改配额、没有增删用户
 * （配额上限是部署参数，改角色走 `scripts/grant_admin.py`；PRD 写死不做管理后台）。
 *
 * 三处刻意的写法：
 *  * **角色不够不跳登录**——会话是好的，缺的是角色。渲染一句说明，比把已登录的人
 *    送回登录页诚实；
 *  * **服务端才是门**：这里的 `role` 判断只管显示；直接敲 URL 会撞上 403；
 *  * **两个列表各带各的页码**（`?page=` / `?rpage=`）：一页上两个独立列表共用一个页码，
 *    翻上面那个会把下面那个也翻走。页码进地址栏则与全站深链口径一致（刷新、后退都在原位）。
 */
export default function AdminPage() {
  return (
    <main className="mx-auto max-w-[1400px] px-4 py-8">
      <h1 className="font-heading text-xl font-semibold tracking-tight">运维</h1>
      <p className="mt-1 text-sm text-ink-3">
        多用户治理的只读视图：每个账号的配额用量（在飞任务、今日 Agent 调用）与超配额被拒的记录。
        入口只对管理员显示；上限与角色怎么配，见 README。
      </p>
      <Suspense fallback={null}>
        <Panels />
      </Suspense>
    </main>
  );
}

/**
 * 取数与角色判断都在这里，且**必须包在 `Suspense` 里**：`useSearchParams` 要求边界，
 * 生产构建下边界内整棵子树降级为 CSR——把整页包进去会让标题闪一下（同 `/stocks` 的写法）。
 */
function Panels() {
  const { user } = useAuth();
  const params = useSearchParams();
  const router = useRouter();
  const page = Number.parseInt(params.get("page") ?? "1", 10) || 1;
  const rejectionsPage = Number.parseInt(params.get("rpage") ?? "1", 10) || 1;

  const go = useCallback(
    (key: "page" | "rpage", next: number) => {
      const search = new URLSearchParams(params.toString());
      // 第 1 页不写进地址栏：`?page=1` 与「没有 page」是同一件事，留着只会脏
      if (next <= 1) search.delete(key);
      else search.set(key, String(next));
      const text = search.toString();
      router.replace(`/admin${text ? `?${text}` : ""}`, { scroll: false });
    },
    [params, router],
  );

  if (user && user.role !== "admin") {
    return <p className="mt-6 text-sm text-ink-2">需要管理员权限。当前账号是普通用户。</p>;
  }

  return (
    <>
      {/* 筛选一变就回第 1 页：否则「第 7 页的筛选结果」多半是空白（面板内部挡掉首次渲染） */}
      <UsagePanel
        page={page}
        onGo={(next) => go("page", next)}
        onFilterChanged={() => go("page", 1)}
      />
      <RejectionsPanel page={rejectionsPage} onGo={(next) => go("rpage", next)} />
    </>
  );
}
