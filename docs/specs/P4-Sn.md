# QuantSage SPEC · P4-Sn（单机工程化）

> 文档链：规划报告（`docs/private/`）→ CLAUDE.md（定稿摘要）→ PRD（v0.18）→ 本文（技术规格）→ 代码
>
> 版本 **v3.13** ｜ 2026-10-30 ｜ 状态：**S1–S6 已交付；P4-S7 worker 池已交付（queue p95 1,312ms；RAG 合批按 A 不做）；P4-S9 数据层热点优化已交付（20 回测 queue p95 503ms、run p50 86ms、bars 1,660/240ms；S7 的 ≤1s 复验成立）；P4-S8（§9）已交付（6 条规则、3 条真实告警 firing→resolved、缓存命中率 99.5%、备份 launchd）**——P4-Sn 单机工程化（S1–S6）规格 + P4-S7 单机承压升级 + P4-S8 可观测性与缓存 + P4-S9 数据层热点优化；S1 按 2026-10-27 实测与拍板修订并实施（v3.1，见 §13），S2 补 `worker_id` 栅栏、启动全量扫尾与异常类型重排口径（v3.2），S3 按同日拍板补 embed 稀疏出参、降级原因、批量索引与嵌入独立等待口径（v3.3），S4 按同日拍板补 DuckDB 闸门、PG/Redis 预算、worker 释放租约与 prod 运行底座并交付（v3.4），S5 按 2026-10-28 预测量（Go 条件 1–3 成立）补共享限流、多进程指标、JSONB 进度、锁重试与内存口径并交付（v3.5），S6 于 2026-10-29 交付（v3.6），P4-S7 写入 §8、P4-S8 写入 §9（v3.8，S7 的 v3.7 初稿见 §13），S7 合批门禁未过并按 A 收窄（v3.9，见 §8.2.2），S7 worker 池实测与 P4-S9 数据层规格写入 §10（v3.10），S9 数据层缓存实施并验收（v3.11，见 §10.5）；P4-S8 按 2026-10-30 拍板订正 5 处口径并进入执行（v3.12，见 §9）。
>
> 本文覆盖 **P4-Sn 单机工程化（S1–S6，已交付）、P4-S7 单机承压升级（§8，已交付）、P4-S8 可观测性与缓存（§9，已交付）与 P4-S9 数据层热点优化（§10，已交付）**。角色拆分、平台化/多租户/分布式（P4-S10 及以后）仍为愿景，**不写规格、不排期**。
> 实测基线：S1–S6 见 `docs/private/Pn-n/P4-Sn/P4-S0-基线报告.md`；S7 基线见 `docs/private/Pn-n/P4-Sn/P4-S6-最终报告.md`；再规划：`docs/private/Pn-n/P4-Sn/P4-Sn-单机工程化-再规划.md`。
> P3 SPEC（v2.46）是历史规格；其中“重启不重跑”“多 worker 不受支持”在本文 S2/S5 有明确沿革，**不是静默改口径**。

## 目录

| 节 | 内容 |
|---|---|
| [§1](#1-规划基线与定位) | 规划基线、定位、范围、非目标 |
| [§2](#2-s1-修-events-读路径) | S1 修 `/events` 读路径 |
| [§3](#3-s2-队列可靠性-v2) | S2 队列可靠性 v2 |
| [§4](#4-s3-rag-过载治理) | S3 RAG 过载治理 |
| [§5](#5-s4-资源上限与运行底座) | S4 资源上限与运行底座 |
| [§6](#6-s5-双-api-worker条件项) | S5 双 API Worker（条件项） |
| [§7](#7-s6-验证备份与文档收口) | S6 验证、备份与文档收口 |
| [§8](#8-p4-s7-单机承压升级规格) | P4-S7 单机承压升级（已交付） |
| [§9](#9-p4-s8-可观测性与缓存规格) | P4-S8 可观测性与缓存（已交付） |
| [§10](#10-p4-s9-数据层热点优化规格) | P4-S9 数据层热点优化（已交付） |
| [§11](#11-测试策略) | 测试策略 |
| [§12](#12-已知边界与不做) | 已知边界与不做 |
| [§13](#13-变更记录) | 变更记录 |

---

## 1. 规划基线与定位

### 1.1 基线读数（2026-10-26 实测）

| 场景 | 1 worker | 2 workers |
|---|---|---|
| `/health` c=100 | 5,946 req/s，p95 17ms | 11,385 req/s，p95 10ms |
| bars c=300 | 855 req/s，p95 369ms | 1,646 req/s，p95 241ms |
| events c=50 | 59 req/s，p95 1,028ms | 未测 |
| Chat SSE 20 并发 | 首 token p95 314ms，0 错误 | 395ms，0 错误 |
| 20 回测任务 | 排队 p95 2.2s、执行 129ms | worker 仍单实例 |
| 同用户限流 5 | 放行 5 / 429 15 | 放行 10 / 429 10 |
| RAG hybrid 10 并发 | p95 1.17s | — |
| RAG rerank 10 并发 | p95 14.32s | — |

系统内存测试期间 78–82% 已用、swap 8.4GB；RAG 进程 RSS 1.7–2.6GB。**这台机器的硬约束是内存余量，不是 8 核 CPU。**

### 1.2 定位

S1–S6 完成后，QuantSage 是：

- **经过压测与故障演练验证的单机工程化方案**；
- 单机内多进程（API / 后台 worker / 单 RAG 进程），不是微服务拆分；
- 明确知道自己的上限、降级路径和恢复方式。

它**不是**：

- 多机高可用、多 AZ、生产级 SLA；
- 多租户 SaaS、弹性计算、策略市场；
- “QPS 十万”的 Web 服务。

本轮 S1–S6 是**能力演练/工程化加固**，不是“真实流量触发”的后续愿景。对外表述必须带“单机、演练、非 HA”的限定。

### 1.3 范围

S1–S6 六个工作包，按“先修瓶颈 → 再补可靠性 → 再验扩容”排序：

1. **S1** `/events` 读路径（字典缓存 + 查询边界）。
2. **S2** 队列可靠性 v2（`SKIP LOCKED` + 租约/心跳 + 有限次重排 + 幂等）；Worker 仍单实例。
3. **S3** RAG 过载治理（单 RAG 进程 + 有界队列 + 超时 + 降级 RRF）。
4. **S4** 资源上限与运行底座（Docker/DuckDB/Redis/PG/进程）。
5. **S5** 双 API Worker + 共享限流 + Prometheus 多进程 + 优雅停机（条件项，S4 后 go/no-go）。
6. **S6** 压测复验、故障演练、备份恢复、文档收口。

> **P4-S7 另立（2026-10-29 拍板）**：S1–S6 已交付；S7 只含 RAG SLO（合批经门禁否决）+ task worker 池化（**§8**），缓存命中率/监控告警整块归 **P4-S8（§9）**；数据层热点优化为 **P4-S9（§10）**。三者都不是 S1–S6 的补丁，也不改 S1–S6 的历史读数。

### 1.4 非目标（写死）

- Redis Streams 全量改造；本 SPEC 仅在 S5 用 **DB 进度轮询**，不引 Streams。
- **多进程**任务 Worker 池 / 多 worker 消费：S1–S6 不做，任务 Worker 保持单进程单实例（S5 只扩 API worker，见 §6）。**P4-S7 由 §8 放开「单进程内按 kind 的多协程消费」**（backtest 3 + grid 1 + batch 1 道，共用一个 `worker_id` 与后台主实例锁），仍不是多进程 worker 池。
- K8s / HPA / 多 AZ / 多机 HA；Qdrant 集群；Redis Cluster；PgBouncer/Read Replica；Vault；Kafka/Celery。
- 把 agent/backtest/report 拆成独立 HTTP 服务。
- RAG 多进程 / 多 MPS 实例 / GPU 化。
- 自动扩缩容；真实 SLA 承诺。

---

## 2. S1 修 `/events` 读路径

### 2.1 现状与根因

`GET /api/v1/events?symbol=...` 当前无 `limit`、无默认区间，返回该标的全部事件；**不读名称字典**——`naming.load_dictionary()`（约 44ms）只在 `/feed` 与 `/{event_id}` 调用。真实成本是每个请求在 DuckDB 上重扫事件视图（307,704 行 × 30 列，约 80MB Parquet），且没有结果缓存。基线 c=50 p95 1,028ms、吞吐 59 req/s；2026-10-27 复测单请求 17–41ms，c=12 吞吐 68–71 req/s、c=50 约 35–42 req/s。

当前语料实际覆盖 **2026-07-07 → 2026-10-08（约 3 个月）**，6/12/24 个月默认窗口扫的是同一批分片，成本不可区分；**默认窗口是语义边界，不是本轮的提速手段**。

**结论**：这是代码/查询级瓶颈，不是进程模型瓶颈；加 worker 只能线性放大，且每请求成本仍在。修法是**有界查询 + 代际结果缓存**。

### 2.2 API 契约

`GET /api/v1/events` 增加：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `symbol` | 6 位代码 | 必填 | 不变 |
| `start` | date | 默认窗口起点 | 显式区间优先 |
| `end` | date | 今天 | 显式区间优先 |
| `limit` | int | 500 | 1–2,000；超出 422，不静默截断 |
| `offset` | int | 0 | 可选；本期前端不暴露分页，仅保留契约 |

**默认窗口**：`EVENTS_DEFAULT_WINDOW_MONTHS`（初始 **12**）。2026-10-27 实测：当前语料仅覆盖约 3 个月，6/12/24 个月的成本不可区分（c=10 分别约 1,096 / 1,186 / 947 req/s，p95 12–13ms）；因此按 **12 个月**固定，作为语义边界而非性能杠杆。显式 `start/end` 总是优先。

**响应**新增字段：

```json
{
  "symbol": "000001",
  "count": 500,
  "events": [...],
  "window": {"start": "2025-10-27", "end": "2026-10-27"},
  "limit": 500,
  "truncated": true
}
```

**语义红线**：

- 不允许静默返回“全部”却只给一部分。`truncated=true` 必须让 UI 知道还有更多。
- 显式 `start/end` 保留完整历史访问；`limit` 上限 2,000，超过要分页/缩小窗口，不假装全量。
- 返回顺序仍按 `event_time` 升序；默认/截断时取**窗口内最近** `limit` 条（先 `ORDER BY event_time DESC, event_id DESC LIMIT limit+1`，再反转）。
- 回测页已传回测窗口，行为不变；若窗口内超过 `limit`，同样返回 `truncated`。
- `window.start` / `window.end` 可为 `null`：默认窗口关闭（`EVENTS_DEFAULT_WINDOW_MONTHS=0`）时表示该侧无界。
- `start > end` → **422**（不返回空壳，避免「怎么一条都没有」无从追查）。
- `offset` 只保留契约：按「最近的在前」语义跳过 `offset` 条；本期前端不暴露分页。

### 2.3 实现要点

1. **结果缓存**：新增 `dc.events_page`——只投影 `_event_row` 用到的 13 列，`ORDER BY event_time DESC, event_id DESC LIMIT ? OFFSET ?`；用 `app/data/cache.py` 的 `cache.through` 按数据代际缓存（键含 `data_dir` / `symbol` / `start` / `end` / `limit` / `offset`）。**不缓存 `dc.events` 的无界全史查询**（回测/报告路径用它，307k 行不能进内存缓存）。
2. **字典缓存**：`naming.cached_primary_names()` 按「字典路径 + mtime_ns + size」+ 数据代际缓存；只接 `/feed` 与 `/{event_id}`（`/events` 不读字典）。第二次请求不读盘，字典重建后失效。
3. **查询边界**：endpoint 传 `limit+1` 判断 `truncated`；默认窗口由 endpoint 计算（`EVENTS_DEFAULT_WINDOW_MONTHS`，初始 **12**；**0 = 关闭默认窗口，回退无界**），不把“现在”硬编码进数据层。
4. **前端**：`EventsResponse` 增加 `window` / `limit` / `truncated`；`api.events` 增加 `limit`；个股页文案从“全部事件”改为按服务端返回的实际窗口/条数显示，并提示可在回测页按窗口查看；回测页保留窗口内展示，截断时同样标注。
5. **不做**：DuckDB 索引/物化表、分页 UI、PIT 口径改动、把 `limit` 做成无上限。

### 2.4 验收

| 项 | 口径 |
|---|---|
| 默认窗口测量 | 6/12/24 个月各跑 c=10/50；记录 p95，最终默认值写进配置与验收记录 |
| events c=50 | p95 **<500ms**、吞吐 **≥150 req/s（目标 200）** |
| 回测窗口路径 | 不回归；窗口内事件数与截断标记正确 |
| 结果缓存 | 同参第二次请求不再查 DuckDB（L1 命中）；数据代际变化后失效 |
| 冷缓存 | 首次请求仍走 DuckDB（约 20–40ms），**如实记录，不承诺冷缓存 SLO** |
| 字典缓存 | 第二次请求不再读盘；字典重建后失效（`/feed`、`/{event_id}`） |
| 契约 | `truncated`/`window` 有单测；超 `limit` 返回 422；`start > end` 返回 422；显式 `start/end` 可取更早 |
| 前端 | `pnpm test && pnpm typecheck && pnpm lint` 通过；个股页不再写“全部事件” |

`events c=50 p95 <500ms、吞吐 ≥150 req/s` 的读数**以缓存预热后为准**；压测记录必须同时给出冷缓存与多标的诊断值，避免把「同一 URL 的缓存命中」说成普遍吞吐。

### 2.5 沿革与回滚

- 旧行为：无 `limit`、无默认窗口，返回全部。新行为：默认窗口 + limit + `truncated`。这是**有意的契约变更**，必须在 PRD/README/前端文案同步。
- 回滚：配置 `EVENTS_DEFAULT_WINDOW_MONTHS=0` 可关闭默认窗口（start/end 都回无界，仍需 `limit`）；若前端未适配，先回滚 endpoint 默认值，不删 `truncated` 字段。
- 结果缓存回退：`cache.clear_local()` / 重启即清 L1；L2 随代际键自然失效。缓存层异常时 `cache.through` 直读，不影响正确性。
- 日界语义（2026-10-27 拍板）：S1 沿用 `dc.events` 现有的 `event_time <= end`（含当日 00:00 前），默认 `end=今天` 会漏掉当天 00:00 后的事件；记为已知边界。若要改成按日含全天，另开一项并同步 `report._events_in_window` 的计数口径。

---

## 3. S2 队列可靠性 v2

### 3.1 现状与语义变更

**现状**：`claim_next_task` 是单条 `UPDATE`，多 worker 并发会重复取同一行（探针 30/30；`SKIP LOCKED` 0/30）。无 lease/heartbeat/attempts；重启把 `running` 标 `failed/interrupted`，**不自动重跑**。

**变更（用户已拍板）**：

> 从“重启不重跑”改为“**崩溃/租约过期有限次自动重排；确定性用户错误不重试；结果幂等**”。

- 只重排基础设施故障：租约过期、worker 崩溃、可分类的瞬时错误（DB/IO/临时内部错误）。
- `max_attempts = 2`：**最多执行两次**。第一次失败可重排一次；第二次失败终态；确定性失败第一次就终态。
- P3 SPEC §2.4 的“重启不重跑”是历史口径；P4 起以本节为准，README/architecture 实施后同步。

### 3.2 表结构

`tasks` 增列（幂等 DDL；旧行不回溯改状态）：

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `priority` | INT | 0 | 单 worker 下只影响排队顺序；按 kind 映射，不暴露给用户 |
| `available_at` | TIMESTAMPTZ | now() | 退避/延迟取件 |
| `attempts` | INT | 0 | 已执行次数；取件时 +1 |
| `max_attempts` | INT | 2 | 最多执行次数 |
| `lease_expires_at` | TIMESTAMPTZ | NULL | worker 心跳续租 |
| `worker_id` | TEXT | NULL | 谁在跑；排障 |
| `last_heartbeat_at` | TIMESTAMPTZ | NULL | 心跳时刻 |

索引：`(status, available_at, priority DESC, created_at)`；`running` + `lease_expires_at` 的部分索引。

### 3.3 取件 / 租约 / 重排

**取件（已验证）**：

```sql
WITH picked AS (
  SELECT id FROM tasks
  WHERE status = 'queued' AND available_at <= now()
  ORDER BY priority DESC, available_at, created_at, id
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE tasks
SET status = 'running', started_at = now(),
    attempts = attempts + 1,
    worker_id = %s,
    lease_expires_at = now() + interval '60 seconds',
    last_heartbeat_at = now(), updated_at = now()
WHERE id = (SELECT id FROM picked)
RETURNING ...;
```

**心跳**：worker 每 10s 更新 `last_heartbeat_at` 与 `lease_expires_at = now()+60s`；任务在子线程/子进程跑时，心跳循环在事件循环里继续。**续租必须带 `status='running' AND worker_id=%s` 条件**——任务被 reaper 回收后，旧 worker 的迟到心跳不能把租约抢回来。

**迟到完成**：被回收后旧 worker 若仍跑完，`finish_task` / `fail_task` / `finish_task_cancelled` / `update_task_progress` 必须带 **`AND worker_id = %s`** 栅栏。只靠 `status='running'` 拦不住：任务被回收并**重新取件**后状态又是 `running`，旧 worker 会覆盖新 worker 的结果。结果写入再靠 `task_id` 唯一约束兜底。

**取件时进度归零**：`progress = jsonb_set(progress, '{done}', '0')`——重排后从 0 重新计，避免「排队中显示上次的 40/100」。

**reaper**：两种模式，共用同一条 CASE UPDATE：

- **周期 reaper**（每 30s）：只扫 `status='running' AND lease_expires_at < now()`，不碰心跳新鲜的行——它是 worker 活着时的安全网。
- **进程启动时**（已持有后台主实例锁，证明没有第二个 worker）：扫**全部** `running` 行。`kill -9` 后重启即重排，不受 60s 租约 + 30s 周期约束。

三种去向：

- `cancel_requested_at` 非空 → `status='cancelled'`、`finished_at=now()`（取消是权威；不重排、不标 failed）。
- `attempts < max_attempts` → `status='queued'`、`available_at = now()+5s`、清 `worker_id/lease`；不写 `error`（还没终态）。
- `attempts >= max_attempts` → `status='failed'`、`error={"kind":"lease_expired","message":"执行中断且已达最大尝试次数","attempts":N}`。

**可重排错误**（worker 仍活着、执行抛异常）走同一套：`attempts < max_attempts` 时回 `queued`，否则终态。

**为什么不自动晋升**：后台主实例仍由 Postgres advisory lock 竞选；主实例退出后由 supervisor 重启并重新竞选。本轮 S1–S6 不改变这条。

### 3.4 重试与幂等

**可重排**（**按异常类型判**，不按 `error.kind`——`internal` 是个大口袋，照 kind 重试等于把代码 bug 跑两遍）：

- 租约过期 / worker 崩溃；
- `psycopg.OperationalError` / `psycopg.InterfaceError`、`ConnectionError`、`TimeoutError`、`psycopg_pool.PoolTimeout`（DB 连接断开、临时网络/池拒绝）。

**不重排**：

- 参数/校验/静态检查/沙箱拒绝/无数据/权限/配额等确定性错误；
- 用户策略运行期异常（同一份源码重跑仍是同样结果）；
- 沙箱 `wall` / `cpu` 超限（确定性）；本仓**没有执行级超时**，故 `timeout` 不是独立的可重排类别；
- 其余 `internal`（代码 bug）；
- 已取消的任务。

**结果幂等**：

- `backtest_runs` 增 `task_id UUID NULL` + **普通唯一索引**（不是部分索引：PG 唯一索引允许多个 NULL，旧行天然不冲突，`ON CONFLICT (task_id)` 靠它推断）；`optimization_runs` 同理。
- 执行体取件后先查 `task_id` 是否已有结果：有则直接收尾返回既有 `run_id`，不重复执行。
- 写结果时用 `INSERT ... ON CONFLICT (task_id) DO NOTHING`；冲突则读既有行返回。
- 旧记录 `task_id` 为 NULL，不回溯。

**取消**：`cancel_requested_at` 仍是权威；取件前/执行中检查；已取消的任务不重排——reaper 遇到该标志直接收成 `cancelled`。单次回测的“在跑不可中止”语义不变（409）。

### 3.5 验收

| 项 | 口径 |
|---|---|
| 取件并发 | 直接 SQL 探针 4 路 ×100 轮 0 重复；另加集成回归用例（4 并发 claim 50 件，0 重复/0 遗漏）；单 worker 不受影响 |
| 崩溃重排 | 任务执行中 `kill -9` 后端进程 → **重启后 60s 内**回 `queued`（启动全量扫尾，实测应为秒级）；重新取件后 `attempts=2`；第二次再杀 → `failed/lease_expired`，不无限重排 |
| 确定性失败 | 无数据/参数错误第一次即 `failed`，`attempts=1`，不重排 |
| 幂等 | 同一任务重排后只产生一条 `backtest_runs`/`optimization_runs`；结果 `run_id` 一致 |
| 进度 | 重排后 `progress.done` 从 0 重新计 |
| 队列不回归 | 20 任务排队 p95 ≤2.5s（基线 2.2s）；取消仍生效 |
| 回归 | 后端离线 1,821 例 + ruff 全绿；新增用例覆盖租约/reaper/幂等 |

### 3.6 沿革与回滚

- 旧：`running` 在重启时一律 `failed/interrupted`。新：启动扫尾 + 过期租约有限次回 `queued`；只有 attempt 用尽才 `failed`。这是**产品语义变更**，必须写进 README/architecture。
- 回滚：配置 `TASK_MAX_ATTEMPTS=1` 可退回“只执行一次、不重排”；`TASK_LEASE_ENABLED=false` 只保留 `SKIP LOCKED`（仍修重复取件），启动扫尾退回 `failed/interrupted` 旧语义，不设租约、不跑周期 reaper。

---

## 4. S3 RAG 过载治理

### 4.1 现状

RAG 当前在 API 进程内懒加载；hybrid 单请求 222ms，10 并发 p95 1.17s；rerank 单请求 1.05s，5 并发 p95 6.8s、10 并发 p95 14.3s（MPS 锁串行）。多 API worker 时每进程重复模型，且 MPS 跨进程可能 abort。

### 4.2 进程边界与契约

**单 RAG 推理进程**（localhost，独立进程；不是微服务拆分）：

- 入口：`python -m app.rag.service`；端口 `RAG_SERVICE_PORT`（初始 8200）；只绑 127.0.0.1。
- 鉴权：`RAG_SERVICE_TOKEN`（内部 token）；不暴露公网。
- 模型：单进程、单 MPS 闸门；启动时懒加载或 `RAG_PRELOAD=true` 预热；**不允许起第二个 RAG 进程 / 第二个 MPS 实例**。
- 端点：

| 端点 | 入参 | 出参 |
|---|---|---|
| `POST /v1/embed` | `texts[]`, `batch_size?`, `request_class?` | `{dense: [[float]], sparse: [{token: weight}], dim}` |
| `POST /v1/rerank` | `query`, `documents[]`, `request_class` | `{scores: [float]}`；过载 `503 {reason}` |
| `GET /health` | — | `{status, models, device, queue}` |
| `GET /metrics` | — | Prometheus 文本 |

- **embed 必须同时返回稀疏**：BGE-M3 一次前向产出 dense + lexical weights，hybrid 检索与建索引都依赖两者；只返回稠密会让稀疏腿永远空。
- **过载不写在 200 响应里**：`503` + `{"reason": "queue_full" | "timeout"}`；降级是调用方的决定。
- API/MCP：通过 `app/rag/remote.py` 的 `RemoteEmbedder` / `RemoteReranker` 实现现有 `Embedder`/`Reranker` 协议；**API 进程不再 import/加载 `app.rag.encoder` 的模型**。离线测试注入假实现，不依赖服务。
- 索引任务（`embed_events.py` / 每晚 ETL 补嵌）同样走服务：`RemoteEmbedder.encode_documents` 按 64 条分块、`request_class=batch`；避免第二个模型实例。

### 4.3 请求分级与有界队列

| 类别 | 调用点 | 队列上限 | 排队超时 | 过载行为 |
|---|---|---|---:|---:|
| `interactive` | Chat 的 `search_events`、MCP | `max_queue=8` | **0.5s** | 快速 503/`overloaded` → 调用方降级 RRF |
| `research` | 深研报 news 角色 | `max_queue=32` | **2.5s** | 有界排队；满/超时后降级 RRF 或明确 503 |
| `batch` | 索引任务（`embed_events.py`） | `max_queue=8` | **600s** | 有界排队；满/超时明确失败，不降级 |

> 等待值是 2026-10-27 实测调定：交互 1.5s → 0.5s（10 并发 p95 3.9s → 1.94s）、研究 10s → 2.5s（5 并发 p95 6.7s → 尾部降级、p95 落回 5s 内）。仍是配置项，换机器按实测调。

- `max_inflight`：embed **4**（实测 2 会在 10 并发下让查询嵌入排队超时）；rerank **1**（MPS 串行，显式承认）。
- **嵌入自成一档等待**（`RAG_EMBED_WAIT_SECONDS`，初始 5s）：查询嵌入是硬依赖，没有 RRF 可降级，不能套用精排的短超时；批量索引仍用 `rag_batch_wait_seconds`（600s）。实测教训：套用 0.5s 时 10 并发下 2 条查询直接 503。
- 实现口径：精排由**单个 worker executor + 信号量**保证串行；排队计数只算「等待中」，拿到信号量即减一。等待超时 → 503 `timeout`；计数到顶 → 503 `queue_full`。
- **不做抢占**：交互请求排在研究/索引后面时同样按自己的超时降级——「交互优先降级」指降级快，不是插队。
- 队列满/超时**必须返回明确结果**：不允许无限等待；调用方把 503 转成 RRF 结果或明确错误帧。
- 超时值进配置，不在代码里写死；研究/批量类可单独调大，但必须有上限。

### 4.4 降级与可观测

- **交互优先降级**：rerank 队列满/超时/服务不可用 → `search` 返回 RRF 顺序，`score_kind="rrf"`，并在工具/MCP 返回里写明 `degraded_reason`（`queue_full` / `timeout` / `unavailable`）。
- **研究类**：允许有界排队；排队失败同样降级 RRF 或返回明确 503；不静默换查询/PIT 条件。
- **指标**：`rag_rerank_queue_depth`、`rag_rerank_inflight`、`rag_rerank_degraded_total{class,reason}`、`rag_rerank_wait_seconds`、`rag_embed_duration_seconds`；RAG 服务 `/metrics` 暴露，Prometheus scrape。
- **指标分工**：服务端记 `queue_full` / `timeout`；API 进程只记 `unavailable`（连接失败/客户端超时）——同一事件不两处计数。
- **返回可见**：`score_kind` 已是既有字段；精排降级时 `RetrievedEvent` 带 `degraded_reason ∈ {queue_full, timeout, unavailable}`（非降级为 `None`），chat 工具文本、MCP JSON、深研报 notes 都要读它；不得只写日志。
- **PIT 红线**：降级只改排序，不改 `as_of`/`symbol`/`event_type`/`top_k`；`available_at <= as_of` 仍在 Qdrant 服务端过滤。

### 4.5 批处理（可选，非前置）

- 只做“有基准收益”的试点：把 rerank 的 50 个候选攒批（batch_size 8/16）测吞吐/延迟；若吞吐 ≥1.5× 且 p95 不恶化才保留。
- 不满足就只保留队列/降级；**批处理不作为 S3 的硬性验收项**。

### 4.6 验收

| 项 | 口径 |
|---|---|
| 进程边界 | API 进程不再加载模型（RSS 证明）；RAG 单进程；`/health` 可见模型/设备/队列 |
| 交互过载 | 10 并发 interactive rerank：p95 ≤2s，`degraded_total>0`，无无限等待；排队超时是配置，实测超线按配置调小（不改代码） |
| 研究排队 | 5 并发 research rerank：p95 ≤5s（有界排队）或明确降级；无死等 |
| 稳定性 | 0 MPS 崩溃；RAG 进程 RSS 与基线（1.7–2.6GB）同量级；soak 不涨 |
| 可见性 | 队列深度/降级计数/等待时间可从 `/metrics` 读到；工具返回带 `score_kind=rrf` 与原因 |
| 回归 | 既有 RAG 评测（200 条）与离线用例不回归；PIT 过滤用例全绿 |

### 4.7 边界与回滚

- 不做多 RAG 进程、不做 GPU/MPS 多实例、不换模型。
- 回滚：`RAG_SERVICE_URL` 置空可回退到进程内（仅单 API 进程；双 worker 不建议）；降级默认值可调，但“队列满/超时必须有明确结果”不退化。
- 鉴权：`RAG_SERVICE_TOKEN` 非空时 `/v1/*` 要求 `Authorization: Bearer`；为空时**只允许回环地址**（与 API `/metrics` 同姿态）并记 warning。服务只绑 `127.0.0.1`。
- 索引任务走服务（`request_class=batch`）：服务在线时补嵌不另载模型；服务不可用时索引任务明确失败，不静默跳过。

---

## 5. S4 资源上限与运行底座

### 5.1 Docker 上限

在 `docker-compose.yml` 给默认服务加 `mem_limit` / `cpus`（初始值，实测后调）：

| 服务 | mem_limit | cpus | 说明 |
|---|---:|---:|---|
| qdrant | 2.5g | 2.0 | 当前 RSS 1.8GB；留索引/压缩余量 |
| postgres | 1.25g | 2.0 | shared_buffers 512MB、`shm_size: 256mb`；连接预算见 §5.3 |
| redis（cache） | 640m | 0.5 | `maxmemory 512mb` + `volatile-lru` + **关闭 RDB/AOF**；缓存键带 TTL |
| redis（coord） | 192m | 0.25 | S5 已建（128MB `noeviction`、无持久化）；只放 `rl:*`，不塞缓存/进度 |

- Docker VM 上限 8GB（实际 7.7GB）；默认四件合计 **约 4.58GB**（S4 三件 4.39GB + coord 0.192GB），给 Docker 开销与镜像缓存留余量。
- `mem_limit` 必须高于 `maxmemory`/`shared_buffers`：Redis 自身开销与 PG 连接私有内存都在容器内。cache 不做持久化——RDB fork 的 copy-on-write 会在限额边缘把内存翻倍。
- Langfuse 全套继续 `observability` profile **按需启动**；不常开。若常开，需先调大 Docker VM 并重测内存预算。
- 不把后端/RAG/worker 放进 Docker（torch linux/arm64 阻塞仍在）；只用 Docker 管有状态基础设施。

### 5.2 DuckDB 上限

`duckdb_client.connect()` 每条连接显式：

```sql
SET threads = 2;
SET memory_limit = '1GB';
SET temp_directory = '<temp-root>/duckdb-<pid>-<thread>-<rand>';
```

- **1GB 是每条连接的上限，不是进程上限**：本仓每线程一条连接、每连接一个 DuckDB 实例，`asyncio.to_thread` 默认线程池最多 12 条连接。`_fetch` 必须加**进程级查询闸门**（初始 4），不能等 p95 不达标才加——没有它，资源上限不成立。
- `temp_directory` 用绝对路径 + **逐连接唯一**子目录；不要用默认 `.tmp`（相对 cwd，多进程会撞）。普通进程放 `<data_dir>/_tmp`；策略沙箱子进程的数据目录是**只读绑定**，放 `TMPDIR/_tmp`（沙箱的 `TMPDIR` = 可写的 work_dir），否则一条 spill 就是 EPERM。进程首次建连时清理临时根下 `duckdb-*` 中 **pid 已死**的残留，不碰其它活进程。
- 闸门只算活跃查询；等待中的连接不占 DuckDB 查询内存。`DUCKDB_THREADS` / `DUCKDB_MEMORY_LIMIT` / `DUCKDB_MAX_CONCURRENT` 都可配置，`0`/空串 = 不设/无闸门，作为无代码回退口。
- 实测权衡（2026-10-27）：单次冷查 bars 默认 157.6ms → `threads=2` 408.6ms、`threads=4` 217.2ms；bars 读缓存命中路径，冷查退化不等于 p95 退化。若 S4 验收 p95 或 soak 内存不达标，优先把 `DUCKDB_THREADS=4 / DUCKDB_MAX_CONCURRENT=2`（仍是 8 线程上限，单查更快），不改代码。
- 大扫描（体检/ETL）继续走低优先级/独立线程，不与回测抢。

### 5.3 Redis / PostgreSQL

- Redis cache：`maxmemory 512MB`、`volatile-lru`、关闭 RDB/AOF；缓存键必须带 TTL；不把限流/进度等协调键放进 cache 实例。
- **Redis coord 不在 S4 建**：S4 的限流/熔断仍进程内，建一个没有消费者的实例只会白占内存；S5 Go 后已与共享限流一起加入（128MB `noeviction`、`mem_limit 192m`，见 §6.3）。
- PG：`max_connections=120`（连接预算 ≤80 + Langfuse ~20 + 余量）、`shared_buffers=512MB`、`shm_size=256MB`；连接预算：API 2×(8 业务 + 8 checkpointer) + worker/scheduler 共用业务池 + Chat advisory ≤24 + 余量，控制在 80 以内。
- 业务池 `statement_timeout=5s`；checkpointer 池 `30s`（LangGraph 写入可能比业务查询大）。worker 与 API 同进程、共用业务池，其 PG 语句（取件/心跳/进度/终态）均为亚秒级，共用 5s；**不为 worker 单开 30s 池**。Chat 的 advisory lock 走独立连接，不受池 `options` 影响（`pool.conninfo` 不含 kwargs）。
- `init_checkpoint_db.py` 的建表连接不加超时（`setup()` 可能跑 `CREATE INDEX CONCURRENTLY`）；只有应用运行期池加。

### 5.4 进程与停机

- 原生进程管理：`scripts/prod.sh`（`QS_MODE=prod` 委派 `dev.sh`），不引 launchd；Docker 只管基础设施。prod/dev 日志与 PID 分目录（`logs/prod` / `logs/dev`），端口相同，不能同时起。
- 健康检查：`/health`、`/health/ready`、RAG `/health`；启动顺序 infra → migrate → RAG → API/worker → 前端。
- 优雅停机：API 用 `uvicorn --timeout-graceful-shutdown 15` 停接新请求、drain 在飞/SSE（上限 15s）；worker 停止取件，**不 await 在飞任务**，退出前对 `_RUNNING` 里的任务调用 `release_task`——回 `queued`、`attempts = greatest(attempts-1, 0)`（优雅停机不算一次失败）、清租约；已请求取消的收成 `cancelled`。旧进程的迟到写入仍由 `worker_id` 栅栏挡住；下次启动的全量扫尾继续兜底（S2）。scheduler `shutdown(wait=False)` 不变。
- 日志轮转：prod 模式每 30min 做一次 copytruncate（20MB 阈值、保留 3 份，`PROD_LOG_*` 可调）；几行丢失窗口如实记录，不假装是 logrotate。不把密钥写进 plist/命令行。
- 迁移：S2 加列用幂等 DDL；`schema_migrations` 作为 S6 可延项，不阻塞本轮。

### 5.5 验收

| 项 | 口径 |
|---|---|
| 容器上限 | `docker stats` 不超表；Qdrant/Postgres `OOMKilled=false` |
| DuckDB | `current_setting('threads')=2`、`memory_limit=1GB`、temp 路径在 `<data_dir>/_tmp/`（沙箱为 `TMPDIR`）下且逐连接唯一；20 并发回测 RSS 不持续增长 |
| Redis | `CONFIG GET maxmemory maxmemory-policy save` = 512mb / volatile-lru / ""；coord 未建（S5 Go 后加） |
| PG | `SHOW max_connections/shared_buffers` = 120 / 512MB；业务池 `statement_timeout=5s`、checkpointer 30s；连接峰值 ≤80 |
| Soak | 5–10min 混合负载无 OOM；swap 以**本次开始前**的 `vm.swapusage` 为基线（规划时 10.63GB，非 S0 的 8.4GB），判「增量 ≤256MB」，绝对值如实写 |
| 停机 | SSE 在飞时 SIGTERM 不出现 500；长任务停止后 `tasks.status=queued`、`attempts` 不涨；重启后重新执行一次 |
| 回归 | bars/SSE 不因资源上限出现 >20% p95 退化；若退化，记录并交给 S5 判断 |

---

## 6. S5 双 API Worker（条件项）

### 6.1 Go/No-Go（S4 后复测）

**预测量（S4 后，1w/2w 各 3 次取中位）**：

1. 单 API Worker 在压力档未达标：bars c=300 p95 中位 **>300ms**。
2. 双 Worker 的收益足以覆盖成本：bars c=300 p95 改善 **≥1.3×** 或吞吐 **≥1.5×**；`/health` ≥1.8×。
3. 内存：**2 个 API 进程树 RSS + RAG 进程 RSS + `docker stats` 默认四件合计峰值 ≤18GB**；系统 available **≥2GB**；swap 增量 **<1GB**（以测试前快照为基线）。

**实现后验收**：共享限流 / 多进程指标 / 优雅停机 / 跨进程进度四项全过（§6.3–§6.7）。

2026-10-28 实测（harness 8100，S0 同口径）：1w bars c=300 p95 中位 **390ms / 904 req/s**；2w **234ms / 1,651 req/s（1.83×）**、`/health` **5,813 → 11,754（2.02×）**；进程合计约 **5.4GB**、available **≥5.84GB**、swap **9.20→8.68GB**。Go 条件 1–3 成立。

**No-Go**：预测量任一不满足 → 保持单 API Worker；S5 可只做“能力演示”或不做，直接 S6 收口。文档如实写“多进程未启用”。

### 6.2 进程模型

- **2 个 uvicorn workers**，共享 8000；无 HPA/自动扩缩。用 `uvicorn --workers N`（0.54.0 的 `keep_subprocess_alive()` 会重启死掉的 worker），父进程 PID 由 `dev.sh`/`prod.sh` 记录。
- 后台主实例仍由 Postgres advisory lock 只选一个：只有持锁进程跑 task worker/scheduler；另一个只服务 HTTP。主实例被 kill 后，uvicorn 重启的新进程在启动时**重新竞选**；不做运行中晋升。抢锁在 `API_WORKERS>1` 时重试 3×0.25s，避免旧连接未释放时新进程永远不跑后台。
- 每个 API worker 不加载 RAG 模型（S3 已剥离）。`langchain-mcp-adapters 0.3.2` 是**每次工具调用新建会话**，不是常驻每 worker 一份；内存成本按瞬时子进程记录，必要时再懒加载。
- `init_schema` 加跨进程 `pg_advisory_xact_lock`，两个 worker 同时启动时串行建表。
- Chat 同 thread 的跨进程串行仍靠 PG advisory lock；池预算见 §5.3。

### 6.3 共享限流

- 独立 `redis-coord` 服务：`127.0.0.1:6380`、`maxmemory 128mb`、`noeviction`、无 RDB/AOF、`mem_limit 192m`；只在 S5 Go 后加入 compose 默认服务。
- Lua 令牌桶：键 `rl:{bucket}:{user_id}`，TTL = 窗口；放行/拒绝语义与现有 `Limiter` 一致；`Retry-After` 用剩余等待（Lua 返回毫秒整数，避免浮点截断）。
- 配置：`REDIS_COORD_URL`、`RATE_LIMIT_SHARED`（默认 false）、`API_WORKERS`（默认 1）；shared 只在多 worker 形态启用。
- Redis coord 不可用：降级为进程内限流，额度 **`max(1, capacity // N)`**（保守），全部桶同策略，记 warning + `quantsage_rate_limit_degraded_total`；**不默认 fail-closed**（避免 Redis 抖动把 chat/research 打成 503）。
- 验收：同用户 `RATE_LIMIT_CHAT=5`，2 workers 下 20 并发 → **5 放行 / 15 个 429**（不是 10）；停 coord 后约 4 放行（2×floor(5/2)）。

### 6.4 Prometheus 多进程

- `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`；HTTP 计数/耗时聚合。目录由 `dev.sh`/`prod.sh` 在**后端子进程环境**里每次启动清空重建；不全局 export（避免 RAG 进程继承）。
- `TASKS` 改为**自定义 collector**：scrape 时从 DB 取一次，只产出一份序列，不每进程各 set 一份。
- **worker 启动（uvicorn 补进程时）**清理**已死 pid 的 mmap 文件**（`mark_process_dead` 只覆盖 live gauge；Counter/Histogram 文件按文件名里的 pid 直接删），防 kill -9 后计数翻倍；不在 scrape 里删，避免与并发 scrape 抢文件。
- `worker/RAG 各自小端口`：RAG 服务保留自己的 `/metrics`；API 的 `/metrics` 由任意 worker 服务，多进程聚合。
- 验收：多次 scrape 的 `sum(quantsage_http_requests_total)` 与总请求数同量级；任务状态不重复计数。

### 6.5 优雅停机

- SIGTERM：停止接新请求 → drain 在飞/SSE（上限 15s）→ 退出；worker 停止取件、续租到结束或释放租约；scheduler 停 job。
- 主实例被 kill 后由 uvicorn 重启的新进程按启动逻辑竞选（§6.2）；不做运行中晋升。
- 验收：20 条 SSE 期间 SIGTERM，不出现 500；客户端可重连；任务不 orphan；kill -9 非 master 后另一 worker 继续服务、uvicorn 补进程。
- **2026-10-28 实施记录**：2 workers 干净停机；在飞网格 SIGTERM → 回 queued、重启仅 1 条结果；20 条 SSE 在飞时 SIGTERM → 20/20 打开 200、任务在 drain 内跑完、服务端 0 个 500/Traceback；**kill -9 非 master** → uvicorn 补进程、另一 worker 继续；**kill -9 master** → 新进程在锁重试窗口内接手，日志打印「本进程为后台主实例 / 任务 worker 就绪」，health 仍 200。

### 6.6 跨进程网格进度（DB 轮询）

- **不引 Redis Streams**。`tasks.progress` JSONB 增 `cells`（有界，最多 1,000 格；当前 `MAX_CELLS=100`），worker 逐格追加；沿用 `worker_id` 栅栏。
- API `task_frames` 对 `grid`/`batch` 每 **1s** 读一次轻量进度（`status/progress/result/error`），按 `index` 去重后发 SSE；终态仍从 `optimization_runs.summary.cells` 回放，并跳过已发 index。任务列表摘要同时去掉 `progress.cells`（只留 done/total），避免 50 条列表被逐格数组撑大。
- 进程内 `bus` 保留为本地加速，但**DB 是跨进程权威**；任意 API worker 都能服务进度；新增 `quantsage_task_progress_db_cells_total` 记录由 DB 轮询补出的格数。
- 验收：网格任务在非 master worker 上也能看到逐格进度；断线重连不重复/不丢格；100 格任务 DB 写入不拖慢主流程。

### 6.7 验收

| 项 | 口径 |
|---|---|
| 吞吐 | `/health` ≥1.8×（基线 5,946 → ≥10,700）；bars c=300 p95 ≤300ms |
| 限流 | 5→5（不是 10）；停 coord 后约 4 放行，恢复后 5 |
| 指标 | 多进程聚合可读；任务 gauge 不重复；kill -9 后计数不翻倍 |
| 停机 | SIGTERM 不丢 SSE/任务；重启健康；kill -9 非 master 后另一 worker 继续；kill -9 master 后新进程接管后台主实例 |
| 进度 | 任意 worker 可见网格进度；重连不重复/不丢；DB 轮询计数 >0 |
| 内存 | 2 API 进程树 + RAG + docker stats 合计峰值 ≤18GB；available ≥2GB；swap 增量 <1GB |
| 回退 | 任何一项失败 → `API_WORKERS=1` + `RATE_LIMIT_SHARED=false`，保留 S1–S4；无 schema 迁移 |

---

## 7. S6 验证、备份与文档收口

### 7.1 压测矩阵

- **两套栈、两张表，不混读**：
  - **A 栈（对照）**：harness `:8100` + 假 LLM + 测试库 + `CACHE_ENABLED=false`（L1 开），与 S0/S5 同口径，用于 before/after。
  - **B 栈（生产形态抽测）**：dev/prod 栈（L1+L2、真 RAG 服务、真数据），只做抽测，不与 A 栈同表比较。
- 读：`/health` c=100、`/auth/me` c=50、bars c=100/300、events 默认窗口 c=10/50；1w/2w 各 3 次取中位。
- events 6/12/24 个月窗口：当前语料仅约 3 个月，三者不可区分；照跑以验 `window` / `truncated` 契约，**不据此下性能结论**。
- SSE：5/10/20/50/80 并发，各 3 轮；80 并发只记录、不设 SLO。混合：bars c=100 + SSE 20 同时 3min。
- 回测：20 任务；kill -9 中途；一条确定性失败（未知策略/空数据标的），断言 `failed` 且 `attempts=1`。
- RAG：hybrid/rerank × interactive/research；过载 c=10；记录服务 503/降级与 API `degraded_reason`。
- Soak：5min（与 S4/S5 同口径）+ 10min（最终）；记录 RSS/swap/PG 连接/队列深度/错误数；测前记录 swap 基线，避开 21:10–22:00。
- 产物：`docs/private/Pn-n/P4-Sn/baseline/s6-<YYYYmmdd>/`。

### 7.2 故障演练

| 故障 | 方法 | 期望 |
|---|---|---|
| kill -9 worker 中途 | 复用 S2 探针（私有 harness）；最终 HEAD 复跑 | 60s 内重排；max_attempts 后终态；0 重复结果 |
| kill -9 API worker | 复用 S5 探针（2 workers） | 另一 worker 继续；SSE 可重连；任务不受影响 |
| Redis coord 挂 | 停 `redis-coord`；复跑 S5 限流探针 | 限流降级 `floor(capacity/N)`；进度回退 DB；任务继续 |
| Redis cache 挂 | 带 `CACHE_ENABLED=true` 的实例预热 L2 后停 `redis` | 熔断直读；请求全 200；`redis-cache` 开路；恢复后 L2 命中回升 |
| PG 重启 | `docker compose restart postgres`；重启前后各提交长任务 | 重启窗口可能出现 503/连接错误（**`/health/ready` 只反映启动态，可能仍 200**，以 `/auth/me`、任务、chat 验证恢复）；恢复后新请求成功；任务不丢、0 重复；若池内陈旧连接导致**多于 1 次/池槽**的用户可见失败，修（`check` 或一次幂等重试） |
| Qdrant 挂 | 停 `qdrant`；chat 问事件检索；深研报一次 | 明确“本地事件检索不可用”，不假装“查不到”；深研报明确失败；恢复后正常 |
| LLM 429/超时 | fake LLM `FAKE_LLM_429_RATE=1` / `FAKE_LLM_TIMEOUT_RATE=1`；chat 各一次 | `error` 帧 `rate_limited` / `upstream_timeout`；429 带 `Retry-After`；墙钟有界（≤ 2×`CHAT_TIMEOUT` + 余量；LLM 策略默认重试 1 次）；E4 真实四幕引用既有证据，不重跑 |
| 磁盘只读 | 临时 `DATA_DIR` 复制后 `chmod -R a-w`；调 `POST /data/rollback` | **503**；无 `.partial`/半截文件；原数据未动。**只演练 EACCES**；ENOSPC 未单独造盘，按同一 `OSError` 分支处理 |

- 新增 `backend/scripts/drill_s6.py`（`cache-down` / `pg-restart` / `qdrant-down` / `llm-429` / `llm-timeout` / `disk-readonly` / `all`）；S2/S5 进程级探针继续留在私有 harness。
- 产物：`logs/s6-drill/<ts>/report.md` + `journal.log`；脚本默认只动 dev/测试栈，`QS_MODE=prod` 或非回环 base URL 需 `--yes`。

### 7.3 备份与恢复

- **脚本**：`scripts/backup.sh` / `scripts/restore.sh` / `scripts/install_backup_job.sh`（launchd 03:30，**安装器提供但不自动安装**，由用户执行）。
- **落点**：参数化 `--target DIR`，默认 `~/QuantSageBackups`；外置盘用 `--target /Volumes/...`。目标不可写/未挂载 → 非零退出并明确报错，不静默跳过。
- **PG**：`quantsage` + `langfuse`，`pg_dump -Fc --no-owner`；排除 `quantsage_loadtest_*` 等测试库。每日；保留 7 日 + 4 周。
- **Qdrant**：`POST /collections/cn_events_v1/snapshots`；`data/_meta/embeddings.json` 未变则复用上一份（`reused: true`），变更后必做；快照用 `docker compose cp` 从容器 `/qdrant/snapshots/` 拷到宿主。
- **data**：`rsync -a --link-dest=<上一份>/ --exclude='_tmp/' data/ <target>/data/<date>/`；生成 `<date>.data.sha256`；目标 FS 不支持硬链接时回退普通复制并 warning（空间预检）。
- **恢复**：默认恢复到 scratch 目标（新库/新集合/临时目录），不覆盖 `quantsage` / `cn_events_v1` / `data/`；覆盖需 `--force` + 二次确认。
- **空环境演练**：scratch PG DB + 临时 Qdrant 容器/集合 + 临时 `data` 目录；校验表数/关键行数、`points_count`、全量 sha256；E2E（scratch API 接恢复目标）可延。
- **RPO/RTO**：本地演练口径 RPO 24h / RTO ≤1h（以实测墙钟为准）；**不是 SLA**。
- **不在范围**：`.env`/密钥、`logs/`、Redis（cache/coord 可丢）、测试库；manifest 只记非敏感配置指纹。

### 7.4 文档同步

| 文档 | 改什么 | 时点 |
|---|---|---|
| `docs/PRD.md` | P4-Sn 状态（已交付）/NFR 读数/变更记录 | 收口 |
| `ROADMAP.md` | S6 状态/P4-Sn DoD/下一个动作 | 收口 |
| `docs/specs/P4-Sn.md` | 本文 §7 + §13 v3.6 | S6 开工前 |
| `README.md` | P4-Sn 验证结果摘要 + 备份/恢复命令 + 当前事实 | 收口 |
| `docs/enterprise/architecture.md` | §2.1/§9.3 最终读数与故障语义；§7.3 备份边界 | 收口 |
| `docs/PROJECT_STRUCTURE.md` | 新增脚本/drill 落点 | 收口 |
| `CHANGELOG.md` | `[m-p4]` 里程碑 | 收口 |
| `docs/private/Pn-n/P4-Sn/P4-S6-最终报告.md` | before/after/演练/备份恢复/边界 | 收口（私有） |

### 7.5 最终报告

- 落点：详细版 `docs/private/Pn-n/P4-Sn/P4-S6-最终报告.md`（私有，含原始读数与演练日志索引）；公开面只放 README/CHANGELOG 摘要。
- 每项 before/after 表（用基线报告的格式）；未达标项如实写原因。
- 故障 8 行逐项表、备份/恢复演练表（含 RPO/RTO 实测）、S1–S6 回退口径。
- 明确写出：**“单机工程化、经过压测/演练验证；不是生产级高可用/SLA。”**
- 不把“压力档一次通过”写成“已具备生产级高可用”。
- 里程碑：`m-p4` tag（CHANGELOG 更新、提交/推送获同意后）。

---

## 8. P4-S7 单机承压升级（规格）

> 2026-10-29 拍板：S7 是 P4-Sn 之后的**纯数字迭代**（P4-S7 → P4-S8…），不回到 a/b/c 分段。**S7 只含 RAG 合批 + worker 池**；缓存命中率/冷查询与监控告警/备份定时归 **P4-S8（§9）**；角色拆分等愿景顺延到 **P4-S10 及以后**，**不写规格、不排期**。
> 基线：`docs/private/Pn-n/P4-Sn/P4-S6-最终报告.md`（S6，HEAD `d5bcae3`）。私有评估底稿：`docs/private/Pn-n/P4-Sn/P4-S7.md`。
> 定位：**单机承压升级**——把 S6 暴露的两个瓶颈（RAG 精排降级、任务排队）做成可验收的工程化改动。仍是单机、演练、非 HA；不承诺 SLA。

### 8.1 目标、范围与非目标

**目标（按优先级）**：

1. **RAG 分级/SLO 合同**：合批经 2026-10-29 原型门禁否决（见 8.2.2），不做；只把 interactive/research 的 SLO 写成可复验、如实记录的测量口径。
2. **task worker 池化**：单进程内按 `kind` 起多条 `serve()`，backtest 3 道 + grid 1 + batch 1；20 回测 queue p95 **≤1s**；S2 的租约/幂等/kill -9 语义不变。

**拆分（2026-10-29 拍板）**：缓存命中率/冷查询与监控告警/备份定时**不在 S7**，整块归 **P4-S8（§9）**。S7 只保留以上两项。

**迭代粒度（2026-10-29 拍板）**：S7 是一个迭代包，内部只按主题分节，**不再用 S7-1/S7-2 这类子 ID**。若 5–7 天做不完，按整块工作流顺延到 **P4-S10 及以后**（P4-S8/S9 已定），不在 S7 内部再分。

**对既有边界的取代（必须显式）**：

- 本节 **取代 §1.4 第 2 条与 §12「不做」中的「Worker 池」**：S1–S6 不做多进程 worker 池；S7 只放开**单进程内按 kind 的多协程消费**。仍不引多进程 worker、不引 Redis Streams、不做任务 Worker 多实例竞选。
- 本节 **不改变** §1.4 的其他条（K8s / 多机 HA / Qdrant 集群 / RAG 多进程 / GPU 化等）。
- S1–S6 的历史读数与验收不改；S7 的 before 一律取 S6 最终报告。

**非目标（S7 写死）**：

- 不做 API/worker/RAG/PDF 角色拆分、反向代理、多 API 实例（P4-S10 及以后）。
- 不做 RAG 多进程、多 MPS 副本、GPU 化；不做 Qdrant 集群、PG 只读副本、pgBouncer。
- 不做 Kubernetes / Kafka / Celery / Vault；不承诺多机 HA 或 SLA。
- 不把 RAG 合批写成「必然收益」：原型门禁不通过就只调 SLO/等待/队列，不改架构。
- 不改用户可见的 API 语义（`/events` 窗口/limit/truncated、任务五状态、降级原因）与配额（`QUOTA_ACTIVE_TASKS` 仍 3；压测的 1000 只用于 harness）。
- 缓存命中率/冷查询/预热、监控告警/备份定时不在 S7，见 §9 P4-S8。

### 8.2 RAG 跨请求合批与 SLO

#### 8.2.1 现状与判断

- 精排单进程单槽：`rag_rerank_inflight=1`；`Runtime.admit` 按 interactive/research/batch 三档排队（8/32/8，等待 0.5/2.5/600s）。
- 单请求内部已批：`FlagReranker.rerank` 用 `compute_score(pairs, batch_size=rag_rerank_batch_size)`；缺的是**跨请求**合批。
- 生产精排候选 **50 篇**（`rag_rerank_candidates`），不是 8 篇；S3 的「隔离精排 0.51s」是隔离接口读数、未写候选数，不能直接当作 50 篇基线；200 条评测（生产路径）的均值 1.91s / p95 2.66s 才是可用基线。
- S6 读数：interactive c10 p95 **2,973ms / 8/10 timeout 降级**；S3 的 200 条评测精排延迟 **均值 1.91s / p95 2.66s**。**单请求 50 篇本身已超过 1.5s**，合批不会让某一条自己的 50 篇变快；它只可能提高吞吐、降低降级率，代价是批内请求的尾延迟上升。

#### 8.2.2 原型门禁（先做，0.3–0.5 天）

- 脚本放 `docs/private/Pn-n/P4-Sn/harness/rag_batch_probe.py`（私有证据，不入库），同一进程、同一工作线程、模型已预热。
- 载荷必须用**生产候选数 50**：至少跑 `2×50`、`4×50`、`5×50`、`8×50`；`4×8` 只作对照，不作为门禁依据。
- 每组比较：
  - 串行：逐请求 `rerank(query, docs)`；
  - 合批：把 N×50 对 flatten 成一次 `compute_score(pairs, batch_size=...)`，再按请求切分分数。
- 记录：批墙钟、每条请求的完成时间（min/median/max）、top-10 顺序一致性（Spearman ≥0.99 或不要求逐位分数相同）、MPS 是否 abort。
- **门禁（对选定的 N 三条全过才实现）**：
  1. `batch_wall(N) ≤ 0.7 × serial_wall(N)`（吞吐）；
  2. `batch_wall(N) ≤ 3.5s`（不让非降级请求超过 SLO 上限）；
  3. top-10 顺序一致（分数 max abs diff 只记录，因 padding 会带来微小差异）。
- **选 N 的硬约束**：c10 + 0.5s 等待 + 单槽下，第一轮批次之外的请求在批推理期间等不到槽位，会在 0.5s 超时降级；因此**降级率 ≤50% 要求至少 5 条进同一批（`batch_max ≥5`）**，而**非降级 p95 ≤3.5s 要求 `batch_wall(5×50) ≤3.5s`**。原型必须同时给出 `5×50` 的结果；只测 4×50 不能证明 SLO 可达成。
- 若 `5×50` 过不了 3.5s：两条 SLO 不能同时成立。**不自行二选一**——按 2026-10-29 拍板先记录数据，回到用户拍板（候选：放宽降级率 / 降 candidates 50→20 / 调等待）。在拍板前，合批项只做 SLO/等待/队列调参，不合批；**门禁不通过时不以合批交付、不宣称 SLO 达标**，只交付测量与调参记录。

**2026-10-29 门禁实测（真 MPS，50 篇真语料，`max_length=256`，model batch 8，3 次中位）**：

| N | serial wall | batch wall | speedup | 门禁 |
|---:|---:|---:|---:|---|
| 2 | 2,205ms | 1,946ms | 1.13× | FAIL（吞吐） |
| 4 | 4,410ms | 3,635ms | 1.21× | FAIL（吞吐 + 3.5s） |
| 5 | 5,681ms | 4,652ms | 1.22× | FAIL（吞吐 + 3.5s） |
| 8 | 9,599ms | 7,634ms | 1.26× | FAIL（吞吐 + 3.5s） |

- 分数一致：max abs diff **0.0**、Spearman **1.0**、top-10 **10/10**；合批不改变结果，只是没有足够吞吐收益（`rerank` 内部本来就按 batch_size=8 批处理）。
- 额外：N=5、model batch=16 → **5,391ms**，比 batch 8 更慢。
- **拍板 A（2026-10-29）：不合批**。S7 的 RAG 项只交付 SLO 测量与调参记录，不宣称降级率 ≤50% 达标。证据：`docs/private/Pn-n/P4-Sn/baseline/s7-rag-batch-probe-20261029-232704/`。

#### 8.2.3 合批契约（设计记录；门禁未过，不实施）

> 2026-10-29 门禁未过（见 8.2.2），本节保留为设计记录，不进入实现。

- 新增 `RerankBatcher`（RAG 服务内）：**单 batcher + 按 `request_class` 分的队列**（interactive/research；batch 若调用也排队），每次组批先取 interactive、再 research（同级 FIFO），同一时刻只跑一个批次；单槽 `rag_rerank_inflight=1` 由 **batcher 独占**，不再由每个请求各自 `admit`。这样「交互优先」是调度顺序，不是两个 batcher 抢同一个槽。
- HTTP 契约不变：`POST /v1/rerank` 的 `{query, documents, request_class}` 与 `{scores}` 不变；合批是服务内实现，API/MCP 无感。
- 提交：`submit_rerank(query, documents, klass)` → future；入队前做该 class 的 `max_queue` 检查，满则 `RagOverloaded("queue_full")`。
- 成批：batcher 收集到 `RAG_RERANK_BATCH_MAX` 或等满 `RAG_RERANK_BATCH_WINDOW_MS`；然后拿单槽，在线程池（1 worker）里跑一次 `rerank_many(items)`；成功按请求切分 scores，异常给所有 future 置同一异常（调用方按 `unavailable` 降级）。
- 逐请求等待语义：从提交到**被编入批次**之间受该 class 的 `wait_seconds` 约束；超时 → `RagOverloaded("timeout")` + `RERANK_DEGRADED{reason=timeout}`；**一旦批次开跑，请求等推理完成，不再套等待超时**（与今天 `admit` 后推理不受 wait 约束同义）。
- 取消：客户端断线不取消批次（与 `Runtime.run` 的 shield 同义）；批次跑完再释放槽位，避免下一条与 MPS 并发。
- 单请求/回滚：`RAG_RERANK_BATCH_MAX=1` 时**绕过 batcher**，走现有 `Runtime.run` 单请求路径；`≥2` 才启用合批。
- 配置：`RAG_RERANK_BATCH_MAX`（代码默认 **1**＝不合批、走旧路径；门禁通过后把默认改为验证过的 N（2–5，上限 8），并在 §13 记读数）、`RAG_RERANK_BATCH_WINDOW_MS`（默认 15，范围 0–50）；`RAG_RERANK_INFLIGHT` 仍 1；`RAG_RERANK_BATCH_SIZE`（默认 8）是**模型内部** batch，不与跨请求 `BATCH_MAX` 混用，文档与注释必须分开写。
- 指标：沿用 `rag_rerank_wait_seconds`（提交→编入批次）、`rag_rerank_queue_depth`、`rag_rerank_degraded_total{reason}`；新增 `rag_rerank_batch_requests`（每次批次的请求数直方图）与 `rag_rerank_batch_seconds`（批次推理墙钟直方图）。

#### 8.2.4 SLO 合同（写死；每次复验同时给三列）

| 类别 | 口径 | 目标 | S6 基线 |
|---|---|---|---|
| interactive c10 | 非降级 p95 | **≤3.5s** | 2,973ms（总 p95；非降级样本 2 条） |
| interactive c10 | 降级率 | **≤50%** | 80%（8/10 timeout） |
| research c5 | 总 p95 | **≤5s** | 4,851ms（2/5 降级） |
| research c5 | 降级率 | **≤20%** | 40%（2/5 timeout） |
| hybrid c5 | p95 / 降级 | **≤1s / 0** | 794ms / 0 |
| batch | 不设交互 SLO | 有界队列 600s | 现状 |

- 「非降级」= `degraded_reason is None`；「降级率」= 降级请求数 / 总请求数；报告必须同时给总 p95、非降级 p95、降级率，**不许只挑一条**。
- interactive 的两条目标互相牵制（见 8.2.2 的 `5×50` 约束）：合批后若只能做到「非降级达标、降级率高于 50%」或反之，按 8.2.2 回到用户拍板，不在验收里把其中一条悄悄删掉。
- 若合批后仍达不到：如实写「c10 降级率 X%，单用户交互达标」，不把降级后的数字写成达标；可选把交互等待从 0.5s 调到 0.8–1.0s 再测一轮，但改等待值要单独复验 research 不受影响。
- 复验口径：`harness/rag_bench.py --mode rerank --request-class interactive --concurrency 10` 同口径 3 次中位；模型真权重、RAG 服务独立进程。
- **2026-10-29 门禁未过、拍板 A**：不合批；上表仍作为测量口径，但 S7 的 RAG 验收只要求「如实记录现状」，不要求 c10 降级率 ≤50% 或非降级 ≤3.5s 同时成立。

#### 8.2.5 测试与回滚

- 离线：假精排记录调用次数/批大小/时间；4 请求合成 1 次调用、分数按请求切分正确；一条超时不串味；客户端取消不中断批次；`BATCH_MAX=1` 走旧路径。
- 真机：MPS 0 abort；`/metrics` 能读到 batch 指标；SLO 三列齐全。
- 回滚：`RAG_RERANK_BATCH_MAX=1`（或删配置）→ 旧单请求路径；无需回滚代码。

### 8.3 task worker 池化

#### 8.3.1 现状

- `worker.start()` 只起一个 `serve(db)`；`serve` 循环 `run_once`，`claim_next_task` 用 `FOR UPDATE SKIP LOCKED`，无 `kind` 过滤；`KIND_PRIORITY={"backtest":10}` 只在单 worker 内排序。
- `run_once` 的 `_RUNNING` / heartbeat / lease / `worker_id` / reaper 都是**进程级**；`stop()` 快照 `_RUNNING` 后逐条 `release_task`。
- 任务执行体不是纯事件循环：内置策略走 `asyncio.to_thread(build_report)`，用户策略走子进程；多协程能并行，但纯 Python 段受 GIL 限制。
- S6：20 回测 queue p95 **2,423ms**、run p95 **247ms**；`QUOTA_ACTIVE_TASKS=3`（harness 压测用 1000）。

#### 8.3.2 进程模型（写死）

- **不是多进程 worker 池**：仍由持后台主实例锁的那个 API 进程起 N 条 `serve()` 协程；`worker_id` 每进程一个（`host:pid:rand`），所有协程共用；`_RUNNING`、`_reaper_loop`、advisory lock 均共用。
- 道数配置：`TASK_WORKERS_BACKTEST=3`、`TASK_WORKERS_GRID=1`、`TASK_WORKERS_BATCH=1`；默认合计 5 条。每道 `serve(db, kinds=(kind,))`，只取本 kind。
- 主开关：`TASK_WORKER_POOL_ENABLED=true`；`false` → 完全回到今天的一个 `serve(db)`（无 kinds 过滤），作为回滚。
- 未知/未配置 kind：`start()` 校验 `EXECUTORS` 全覆盖；未配置的 kind 默认起 1 道并打 warning；显式配 0 视为运维意图（允许），但启动 warning「该 kind 无人消费」；全 0 回退单循环。
- 配额不变：`QUOTA_ACTIVE_TASKS=3` 仍在提交端；池化只改消费端。压测沿用 harness 的 `QUOTA_ACTIVE_TASKS=1000`，生产语义不变。

#### 8.3.3 取件契约

- `claim_next_task(worker_id, *, lease_seconds, kinds: tuple[str, ...] | None = None)`：SQL 增加 `AND (%s::text[] IS NULL OR kind = ANY(%s))`；`ORDER BY priority DESC, available_at, created_at, id` 不变；默认 `None` 与旧调用完全兼容。
- `serve(db, *, interval=POLL_SECONDS, kinds=None)`；`run_once` 接收 kinds 并透传；其余租约/心跳/重排/幂等逻辑**一行不改**。
- 任务行时间列已含 `created_at` / `started_at`（`_TASK_COLUMNS`），queue 直方图在取件后用 `started_at - created_at` 观察，无需改 SQL。

#### 8.3.4 验收

| 项 | 目标 |
|---|---|
| 20 回测 queue p95 | 目标 **≤1s**（S6 2,423ms）；S7 实测 **1,312ms**（3 道 3 轮中位；4 道 1,401ms 无改善）；P4-S9 复验 **503ms**（2 API workers，3 轮中位；5 轮中位 505ms）→ **≤1s 成立**；20/20 done、0 重复结果 |
| 5 grid + 20 backtest 并发 | backtest 不被 grid 长期饿死；grid/batch 各 ≤1 道；无「取错 kind」 |
| kill -9 worker | S2 语义不变：60s 内重排、`max_attempts=2`、0 重复 |
| 停机 | `stop()` 释放全部在飞租约；重启后 attempts/结果不重 |
| 回归 | bars c=300 p95 回退 **≤10%**（S6 280ms → ≤308ms）、API 进程树峰值 ≤3.0GB、available ≥2GB、swap 不增长 |
| 降档 | 任一条不过 → `2+1+1` 复测；仍不过 → `TASK_WORKER_POOL_ENABLED=false` |

#### 8.3.5 风险与回滚

- DuckDB 查询闸门 4：5 道任务 + API 读会争；以 bars p95 与 gate 等待（日志）判，不先改闸门。
- GIL：内置策略的纯 Python 段并行度低于道数；验收以实测 queue p95 为准，不按理论外推。
- 回滚：`TASK_WORKER_POOL_ENABLED=false`（一键回单循环）；不需回滚数据。

### 8.4 验收矩阵（before / after）

| 项 | S6 before | S7 目标 | 判定 |
|---|---|---|---|
| 20 回测 queue p95 | 2,423ms | ≤1,000ms | S7 实测 1,312ms；**S9 复验 503ms**（2w，3 轮中位；5 轮 505ms） |
| bars c=300 p95 | 280ms | ≤308ms（+10%） | S7 实测 1,583 req/s / 236ms |
| health c=100 | 11,599 req/s | 不回归（±10%） | 2w |
| RAG interactive c10 | 2,973ms / 80% 降级 | 记录现状（门禁未过，A 不合批） | 不宣称 ≤50% 达标 |
| RAG research c5 | 4,851ms / 40% | 记录现状（同上） | 不宣称 ≤20% 达标 |
| RAG hybrid c5 | 794ms / 0 | ≤1s / 0 | |
| API 进程树峰值 | 2.64GB | ≤3.0GB | available ≥2GB、swap 不增 |
| 回归 | 离线/集成/前端/ruff | 全绿 | CI 三 job |

**S8 的缓存/监控验收见 §9.4；本表只判 S7。**

**压测口径**：沿用 `harness/` 的 2w/1w、c=300 bars、c=100 health、20/50/100 任务、SSE 80/100/150、RAG c5/c10；3 次中位；原始文件落 `docs/private/Pn-n/P4-Sn/baseline/s7-*/`。任务压测沿用 `QUOTA_ACTIVE_TASKS=1000`，**生产配额不变**。

### 8.5 风险、回滚与文档同步

| 风险 | 应对 |
|---|---|
| 合批在 MPS 上无收益 | **已实测**：1.13–1.26×、5×50 4.65s，未过门禁；拍板 A 不做，只记录 SLO |
| 合批拉长非降级尾延迟 | 交互/研究分桶；窗口 10–20ms；`batch_wall(N) ≤3.5s` 门禁 |
| 降级率与延迟两条 SLO 互斥 | 原型必测 `5×50`；不自行二选一，回用户拍板 |
| worker 池打满 DuckDB/CPU | 3+1+1 实测 queue 1,312ms、bars 1,583/236ms；4 道无改善；S9 数据层缓存后 queue **503ms**、bars 1,660/240ms；GIL 以实测为准 |
| 24GB 内存不够 | 不加 RAG 副本；API 进程树峰值 ≤3.0GB、available ≥2GB |
| 角色拆分被误读成 HA | §8.1 写死：S7 是单机、非 HA、非 SLA；角色拆分是 P4-S10 及以后 |

**回滚开关**：`RAG_RERANK_BATCH_MAX=1`；`TASK_WORKER_POOL_ENABLED=false`。回滚不需要改数据。

**文档同步（S7/S8 执行时一并做）**：

- 本 SPEC：头部版本/状态、目录、§1.3、§1.4、§8（S7）、§9（S8）、§10（S9）、§11（测试策略）、§12（边界）、§13（变更记录）。
- `ROADMAP.md`：P4-S7 行、P4-S8 行、当前焦点/下一个动作。
- `docs/PRD.md`：P4 节追加「承压升级」（S7）与「可观测性与缓存」（S8）两段。
- `CLAUDE.md`：路线图表 P4 行（S7/S8）、§六路线图。
- `.env.example`：S7 变量（`RAG_RERANK_BATCH_MAX` / `WINDOW_MS` / `TASK_WORKER_POOL_ENABLED` / `TASK_WORKERS_BACKTEST|GRID|BATCH`）+ S8 变量（`METRICS_TOKEN` / `RAG_SERVICE_TOKEN` 已有注释位）。
- `CHANGELOG.md`：S7 `m-p4-s7`、S8 `m-p4-s8`（待定）。
- README/教程：只有用户可见行为变化才改；S7/S8 不改 API 语义，预计不动。

### 8.6 实施顺序与工作量（估算）

| 步骤 | 内容 | 估时（天） |
|---|---|---:|
| 1 | 本 §8 过审 + 落地记录 | 0.5 |
| 2 | RAG SLO 测量/调参（合批已否决） | 0.5–1 |
| 3 | worker 池 + 测试 | 2 |
| 4 | 真机压测 + 文档 | 1 |
| | **合计** | **4–5** |

---

## 9. P4-S8 可观测性与缓存（规格）

> 2026-10-29 拍板：缓存命中率/冷查询与监控告警/备份定时从原 S7 拆出，作为独立的 **P4-S8**（纯数字迭代）。S7 只留 RAG 合批 + worker 池（§8）；S8 不改 S7 的验收，也不把监控当作 HA/SLA。
> 2026-10-30 订正（v3.12）：停 RAG 不产生降级比率告警（改为过载注入验收）；queue 直方图不走 scrape 时 collector；备份年龄取 manifest mtime；API/RAG 用两个 token 文件；宿主端口 9092/9093。规则数仍为 6 条。
> 基线：S6 `P4-S6-最终报告.md`；S7 完成后复测 S8 的前置读数。
> 定位：**可观测性与数据层可见性**——让缓存命中率、冷查询、队列/资源/备份年龄可见并能告警。仍是单机、演练、非 HA。

### 9.1 目标与范围

1. **缓存命中率与冷查询**：命中率可算、可查；冷查询先测后治；热标预热可选。
2. **监控与告警**：Prometheus + Alertmanager（compose `monitoring` profile，默认不启动）；6 条告警；备份年龄与 launchd。

**范围边界**：只做可观测性/缓存，不改 S7 的 RAG/worker 行为；不做 API/worker/RAG 角色拆分、反向代理、多 API 实例（P4-S10 及以后）；不承诺多机 HA 或 SLA。

### 9.2 缓存命中率与冷查询

- **命中率口径**：`hits/(hits+misses)`，分 `tier=l1|l2` 的命中数与合计命中率（`CACHE_MISSES` 无 tier）；用 Prometheus 记录规则 `qs:cache_hit_ratio:5m`，不新增业务指标（`CACHE_HITS`/`CACHE_MISSES` 已在 `app/core/metrics.py`）。「看板」= Prometheus 记录规则 + 查询片段，不新增 Grafana。
- **L2 开关**：默认仍 `CACHE_ENABLED=false`；S8 验收时**临时开 true** 跑一轮，记录 L1/L2 命中率、Redis 内存与代际键是否正确（清单变 → 键变）。是否把默认改 true 单独拍板，不在 S8 写死。
- **冷查询**：先测 50 个不同标的顺序读（S1 基线 38 req/s），记录 p95/吞吐；只有在压测证明冷查询是主矛盾时，才加每用户/IP 的冷查询令牌桶或调小 `DUCKDB_MAX_CONCURRENT`；不先加限流。
- **热标预热（可延）**：`scripts/prewarm_cache.py --symbols <top N>`，ETL 后由 launchd/cron 调一次；L1 是进程内，多 worker 各暖各的，收益有限——默认不做，只有压测显示首页冷启动明显时才做。
- **验收**：`/metrics` 能算出 L1 / L2 / 合计三个命中率；`CACHE_ENABLED` true/false 无回归；冷查询有 before/after 数字；不新增限流（除非有证据）。
- **回滚**：`CACHE_ENABLED=false`；预热脚本不装。

### 9.3 监控、告警与备份

#### 9.3.1 指标（scrape 时取，不按进程各报）

- `quantsage_task_queue_seconds{kind}` Histogram：取件后观察 `started_at - created_at`；buckets 0.25/0.5/1/2.5/5/10/30/60。
- `quantsage_pg_connections` Gauge：`/metrics` handler 查 `pg_stat_activity`（或池统计），一次一值。
- `quantsage_host_available_bytes` / `quantsage_host_swap_used_bytes` Gauge：`psutil` 现查，一次一值。
- `quantsage_backup_age_seconds` Gauge：取 `~/QuantSageBackups/manifests/20*.json` 中最新一份，年龄按 **mtime** 算（文件名日期只用于选最新；按文件名算会在每日 03:30 备份前固定假报一次）；无备份返回一个大值（如 1e9）触发告警。
- `rag_rerank_requests_total{class}` Counter（RAG 服务）：`/v1/rerank` 入口计数（成功与过载都算），作为告警降级率的分母；分子只用 `rag_rerank_degraded_total` 的 queue_full/timeout，`unavailable` 由 API 侧记，不进这个比率。
- **多进程口径**：PG / host / backup **三个 Gauge** 用 **scrape 时自定义 collector**（与 `TaskCountsCollector` 同模式：handler 现查 → 设模块值 → `render()` 注册），**不注册进 prometheus multiprocess mmap**；否则 2 个 API worker 会各报一条，或留下死进程的陈旧值。**queue 直方图例外**：它在**持锁进程**取件时 observe（只有一个写入者），正常注册 mmap，不做 scrape 时现查——每次 scrape 重算分桶既无数据来源也无意义。
- RAG 侧沿用 `rag_rerank_degraded_total` / `rag_rerank_wait_seconds` / `rag_rerank_queue_depth`，另加 `rag_rerank_requests_total`；Prometheus 单独 scrape RAG `:8200/metrics`。

#### 9.3.2 鉴权（容器 scrape 的前提）

- `.env` 设 `METRICS_TOKEN`（API）与 `RAG_SERVICE_TOKEN`（RAG）；**不入库、不回显**（`.env.example` 已有注释位）。
- API `_metrics_allowed` 增加 `Authorization: Bearer <METRICS_TOKEN>` 分支（保留 `X-Metrics-Token`）；RAG `require_auth` 已支持 Bearer。
- Prometheus 用 `authorization: {type: Bearer, credentials_file: ...}`。**两个 token 不能共用一个文件**：API 用 `METRICS_TOKEN`、RAG 用 `RAG_SERVICE_TOKEN`，分别由 `dev.sh` 从环境变量写入 `logs/metrics_token` 与 `logs/rag_token`（gitignore），compose 只读挂载，不在仓库里落明文。
- 若不想设 token：Prometheus 跑宿主机（回环放行），但这就不在 compose `monitoring` profile 里——二选一，不能既不设 token 又用容器 scrape。
- **网络前提**：`dev.sh` 的 uvicorn 未显式 `--host`，默认绑 `127.0.0.1`；Docker Desktop 的 `host.docker.internal` 通常可达宿主机回环，但以实测为准。若不通，改为 `--host 0.0.0.0` + token（不裸奔），或 Prometheus 跑宿主机。

#### 9.3.3 compose 与告警规则

- `docker-compose.yml` 新增 `monitoring` profile：`prometheus`（约 150–250MB）+ `alertmanager`（约 30MB）；**默认不启动**，需要时 `docker compose --profile monitoring up -d`；镜像按仓库惯例 pin digest（与 PG/Redis/Qdrant 同）。宿主端口用 **9092（Prometheus）/ 9093（Alertmanager）**——9090/9091 已被 `observability` profile 的 MinIO 占用。
- `docker/prometheus/prometheus.yml`：scrape API `host.docker.internal:8000/metrics` 与 RAG `:8200/metrics`；`evaluation_interval: 15s`。
- 6 条规则（起始值，压测后可调；用实际指标名）：
  1. `histogram_quantile(0.95, sum by (le, kind) (rate(quantsage_task_queue_seconds_bucket[5m]))) > 5` for 10m；
  2. `sum(rate(rag_rerank_degraded_total[5m])) / sum(rate(rag_rerank_requests_total[5m])) > 0.5` for 10m；
  3. `quantsage_pg_connections > 80` for 5m；
  4. `sum(rate(quantsage_http_requests_total{status=~"5.."}[5m])) / sum(rate(quantsage_http_requests_total[5m])) > 0.005` for 5m；
  5. `quantsage_host_available_bytes < 2e9` 或 `quantsage_host_swap_used_bytes > 1.06e10`（2026-10-30 实测基线 9.61GB + 1GB）for 10m；
  6. `quantsage_backup_age_seconds > 93600`（26h）for 10m。
- 告警的降级率用**服务端** `degraded_total/requests_total`；SLO 验收用 **harness 客户端**的 `degraded_reason` 口径，两者不混写成同一个数。
- 规则文件要过 `promtool check rules`；能跑 `promtool test rules` 就加一条规则单测；不能跑则在 `scripts/alert_hook.py` 的单测里覆盖阈值计算。

#### 9.3.4 告警接收端

- `scripts/alert_hook.py`：标准库 `http.server`，只绑 `127.0.0.1:9099`，收 Alertmanager webhook，写 `logs/alerts.log`（JSON 行）+ macOS `osascript` 通知；不接外部付费服务、不写密钥。
- 由 `dev.sh` 在 `monitoring` profile 启动时一并起（`ALERT_HOOK_ENABLED=true`）；默认不启动，避免日常占端口。
- 备选（不推荐）：FastAPI 加 `POST /internal/alerts`，需 token（Alertmanager 从容器来，非回环）；这会扩大 API 的写面，S8 不采用。

#### 9.3.5 备份定时

- `bash scripts/install_backup_job.sh --hour 3 --minute 30`（默认 03:30，ETL 21:10 之后）；安装前需用户确认，装后 `launchctl kickstart -k gui/$UID/com.quantsage.backup` 验一次。
- 验收：manifest 新日期、`quantsage_backup_age_seconds` 下降、`backup_age > 26h` 规则在伪造旧 manifest 时触发；`--uninstall` 可回滚。
- 备份内容与保留沿用 S6，不改口径（本机/受控外置盘，不进仓库/公共云盘）。

#### 9.3.6 监控项验收与回滚

- `/metrics` 在 2 API workers 下每个新指标**一条值**（不是两条），RAG 指标可读。
- RAG **过载**（`RAG_INTERACTIVE_MAX_QUEUE=0` → 全 `queue_full`）→ 降级告警；停 RAG 只会在 Prometheus 里看到 `up{job="rag"}=0`，**不产生 ratio 告警**（分母在服务端，停采集后是 0/0）。PG 连接压到阈值 → 告警；伪造旧 manifest → 备份告警；队列 / 5xx / 宿主规则用 `promtool test rules` 单测（真实注入成本高或危险）。
- 回滚：`docker compose --profile monitoring down`；`ALERT_HOOK_ENABLED=false`；launchd `--uninstall`；指标 collector 保留不影响主路径。

### 9.4 验收与回滚

| 项 | 当前 before | S8 目标 | 判定 |
|---|---|---|---|
| 缓存命中率 | L1 常开 / L2 默认 false，无看板 | L1/L2/合计可算；`CACHE_ENABLED=true` 验收一轮 | 不新增限流 |
| 冷查询 | 50 标的顺序读约 38 req/s（S1） | 记录 before/after；有证据才加限流/调闸门 | 不先加限制 |
| 监控 | 无 Prometheus/告警 | 4 组新指标（queue/PG/host/backup）+ 6 条规则 + 本地 receiver | scrape 一次一值 |
| 备份 | 手动 4.6GB / 36.3s | launchd 每日 + 年龄告警 | 需用户确认安装 |
| 回归 | 离线/集成/前端/ruff | 全绿 | CI 三 job |

**压测口径**：沿用 `harness/` 的 2w/1w、RAG c5/c10；指标与告警用故障注入（停 RAG、压 PG 连接、伪造旧 manifest）；原始文件落 `docs/private/Pn-n/P4-Sn/baseline/s8-*/`。

**回滚**：`CACHE_ENABLED=false`；`monitoring` profile 不启动；`ALERT_HOOK_ENABLED=false`；`launchd --uninstall`；指标 collector 保留不影响主路径。

### 9.5 实施顺序与工作量（估算）

| 步骤 | 内容 | 估时（天） |
|---|---|---:|
| 1 | 本 §9 过审 + 落地记录 | 0.5 |
| 2 | 缓存命中率/冷查询 | 1 |
| 3 | 指标/鉴权/compose profile | 1.5 |
| 4 | 6 条规则 + alert_hook + 备份 launchd | 1 |
| 5 | 故障注入 + 文档 | 0.5–1 |
| | **合计** | **3.5–4.5** |

### 9.6 风险

| 风险 | 应对 |
|---|---|
| 容器 Prometheus 403 | 设 `METRICS_TOKEN`/`RAG_SERVICE_TOKEN` + Bearer；否则宿主机跑 |
| Alertmanager 无接收端 | `scripts/alert_hook.py` 本地监听；不接外部服务 |
| 24GB 内存不够 | 监控 profile 默认不启动；不加 RAG 副本 |
| 备份 launchd 未装 | S8 启动时单独确认；未装则 `backup_age` 一直是高值，告警可见 |
| 缓存 L2 默认开关 | 验收时临时开；默认是否改 true 单独拍板 |
| 角色拆分被误读成 P4-S8 | §9.1 写死：S8 是可观测性/缓存；角色拆分是 P4-S10 及以后 |

### 9.7 交付记录（2026-10-30）

- **缓存实测**：dev 栈 2 worker + `CACHE_ENABLED=true` 一轮 L1 **385** / L2 **13** / miss **2**（合计 **99.5%**；L2 占 L1 未命中 **86.7%**），Redis 2 键 / 1.51MB、键 `qs:cache:v1:<gen>:bars:qfq:...`；`qs:cache_hit_ratio:5m` 压测后 **0.969**；harness bars c=300 cache on/off 中位 **1,656 / 1,714 req/s**（0 失败）。
- **冷查询**：50 个不同标的顺序读 **39.97 req/s**（p50 18.7ms / p95 37.4ms；S1 38.1 / 24.3 / 38.2）→ **不新增限流/不调闸门**。
- **指标**：2 worker 下 `quantsage_pg_connections` / `host_available_bytes` / `host_swap_used_bytes` / `backup_age_seconds` 各 **1 条样本**；`quantsage_tasks` 5 条（按状态）；队列直方图在取件时观察。
- **告警**：`promtool check rules` 7 条通过、`test rules` 通过；真实演练 **RAG 过载**（`RAG_INTERACTIVE_MAX_QUEUE=0`）/ **PG 105 连接** / **伪造旧备份** 三条 firing → resolved，全程落 `logs/alerts.log` 并经 osascript 通知。
- **备份**：launchd `com.quantsage.backup` 已安装（每日 03:30），kickstart 生成 `20261030.json`，`quantsage_backup_age_seconds` **432s**。
- **修复**：① RAG `/metrics` 直接调 `require_auth(request)`，FastAPI 不解析 `Header` 默认值，配 token 后永远 401 → 改 `Depends`；② Prometheus 缺 `alerting.alertmanagers`，规则不外发；③ v3.15.1 的 SIGHUP 热加载会把 scrape 目标清空 → 改重启容器（README 已记）；④ `alert_hook.py` 兼容系统 python3.9（`timezone.utc`）。
- **回滚**：`CACHE_ENABLED=false`、`bash scripts/dev.sh monitoring stop`、`bash scripts/install_backup_job.sh --uninstall`；指标 collector 保留不影响业务。

---

## 10. P4-S9 数据层热点优化（规格）

> 2026-10-30 拍板：S9 是 S7 之后的纯数字迭代，目标是把 20 回测 queue p95 从 **1,312ms** 压到 **≤1,000ms**；S7 的原始 ≤1s 目标由 S9 复验（S7 收口口径待用户后续拍板）。
> 基线：S7 worker 池 3+1+1 实测（SPEC §8，v3.9/v3.10）；S6 的 20 回测 queue p95 2,423ms。私有评估底稿：`docs/private/Pn-n/P4-Sn/P4-S9.md`。
> 定位：**数据层热点缓存**，不改引擎、不改 PIT、不改用户策略沙箱；仍是单机、演练、非 HA。
> **2026-10-30 已交付**：目标全部达成（queue p95 503ms、run p50 86ms、bars 1,660/240ms）；实测明细与回滚开关见 §10.5/§10.6。

### 10.1 目标、范围与非目标

**目标**：

1. 20 回测 queue p95 **≤1,000ms**（warmup 后 3 次中位；3+1+1）。
2. run p50 **≤150ms**；报告 metrics / `meta.data_digest` 逐键不变。
3. bars c=300（`ab`）p95 ≤308ms、吞吐 ≥1,500 req/s（不回归）。
4. 缓存失效**只认代际/文件戳**，不靠「ETL 后记得清」；`BT_DATA_CACHE_ENABLED=false` 一键回旧路径。

**范围**：

- `naming.cached_st_symbols`：复用 S1 的 `cached_primary_names`（字典文件戳 + 数据代际），消掉 `_limit_bands` 每任务 21.5ms 的 `load_dictionary`。
- `market_benchmark._daily_rows`：全市场等权基准的 daily rows 走 `cache.through`（35.2ms → 命中）。
- `window_digest`：窗口指纹走 `cache.through`（22.4ms → 命中）。
- 执行体「顺带」：`progress+finish` 合并为一次 UPDATE（用户 2026-10-30「顺带做了」）。
- 新增 `BT_DATA_CACHE_ENABLED`（默认 true）作为以上缓存的总开关。

**非目标**：

- 不改回测引擎 / 不把 engine 搬子进程 / 不改 `_limit_bands` 的涨跌停口径。
- 不改 PIT 事件过滤、数据源、用户策略沙箱子进程。
- 不做用户策略路径的缓存（子进程不共享父进程 L1）。
- 不新增缓存模块（复用 `app/data/cache.py`）；不做 band 结果缓存（除非复测 >1s，见 10.3.5）。
- 不承诺冷启动首任务（1.8s 冷查询如实记录）。

### 10.2 现状与门禁（2026-10-30 实测）

**warm 分项（000001 / 2024 / ma_cross，3 次中位）**：

| 分项 | ms | 判断 |
|---|---:|---|
| `_load_bars`（窗口 qfq） | 1.0 | 已 L1 |
| `_limit_bands` 合计 | **28.7** | 可缓存 |
| ├ `naming.load_dictionary` | **21.5** | **第一优先**（复用 `cached_primary_names`） |
| ├ `naming.st_symbols` | 1.4 | 随字典缓存 |
| ├ `dc.bars(raw)` 全史 | 3.3 | 已 L1（冷查询 530ms） |
| └ band 循环（1,636 行） | 2.2 | 可选，不是大头 |
| engine（252 bar 循环） | **32.4** | GIL 地板 |
| `market_benchmark._daily_rows` | **35.2** | 直查无缓存 |
| `window_digest` | **22.4** | 读文件无缓存 |
| 执行体/DB | ~87 | 待拆；顺带合并 progress+finish |

- **GIL 地板**：`_limit_bands` 28.7 + engine 32.4 = 61.1ms/任务 → 20 × 61.1 = **1.22s**，对应 S7 queue p95 1.31–1.41s；4 道实测 1,401ms（无改善）。
- **门禁结论（2026-10-30）**：字典缓存后 `_limit_bands` ~6.9ms → GIL 地板 **(32.4+6.9)×20 = 0.79s**；engine×20=0.65s；字典缓存省 21.5ms/任务（≥25ms 门槛按「含 st/raw 的 28.7 → 6.9」算达标）。**门禁通过**；band 结果缓存（2.2ms）不先做。
- 冷启动：3 次 `dc.bars` 冷查询 1.825s（cProfile）；首任务冷，验收前必须 warmup。

### 10.3 缓存契约

#### 10.3.1 naming ST 缓存

- `app/data/naming.py` 新增：

  ```python
  def cached_st_symbols(data_dir=None) -> set[str]:
      from app.backtest.a_share_rules import is_st_name
      return {s for s, n in cached_primary_names(data_dir).items() if is_st_name(n)}
  ```

- `app/backtest/engine.py::_limit_bands` 把 `naming.st_symbols(naming.load_dictionary(...))` 换成 `naming.cached_st_symbols(config.data_dir)`；`is_st` / `limit_pct` / `RuleStatus` 语义不变。
- 键/失效：复用 `cached_primary_names` 的 `(str(path), mtime_ns, size)` + `cache.through` 的数据代际；字典手工重建（`_meta` 未变）也会换键。
- 预期：`_limit_bands` 28.7 → ~6.9ms/任务；20 任务省 ~430ms。

#### 10.3.2 market_benchmark 缓存

- `app/backtest/benchmark.py::_daily_rows` 改成 `cache.through("bt.market_daily", (start, end, adjust), producer, data_dir=data_dir)`；缓存 **daily rows**（`trade_date`/`ret`/`kept`/`dropped`），levels/describe 现算（252 循环可忽略）。
- 键含 `start/end/adjust/data_dir` + 数据代际；**`initial_cash` 不进键**（只缩放 levels）。
- 20 个同窗任务 19 次命中；不同窗口各自一份；JSON `_encode` 处理 date 值。

#### 10.3.3 window_digest 缓存

- `app/report/snapshot.py::window_digest` 改成 `cache.through("bt.window_digest", (start, end, adjust), producer, data_dir=data_dir)`；载荷是 hex 字符串；键含代际。
- 同代际同窗口必得同一指纹；清单变 → 新键 → 重算，不读旧值。

#### 10.3.4 开关与回滚

- 新增 `BT_DATA_CACHE_ENABLED`（默认 `true`）；false 时三个缓存全部绕过，走原路径。与 `CACHE_ENABLED`（只管 L2）分开。
- 缓存复用 `app/data/cache.py` 的 L1/L2、代际键、失败开路；不新造模块。
- 回滚：`BT_DATA_CACHE_ENABLED=false`；不改数据、不改表。

#### 10.3.5 band 结果缓存（可选，非第一版）

- 仅当复测 queue p95 仍 >1s 时再加；键 `(symbol, end, naming_stamp)`；载荷 `[{day: ISO, up: float, down: float}]` + `status` dict（`_encode` 是 JSON，**不能**缓存 `{date: LimitBand}`）；预计再省 2.2ms/任务，不先做以避免多一层序列化。

### 10.4 执行体「顺带」（用户 2026-10-30 拍板）

- `app/tasks/worker.py::run_once` 成功路径目前 `progress(total)` + `finish_task` 两次 UPDATE；合并为 `finish_task` 内一次写 `progress.done=total` / `total`，保留 `cells`，去掉单独 `progress(total)`。
- `app/core/db_tasks.py::finish_task` 增加可选 `progress_done/progress_total`（默认 None = 旧行为）；`tests/fakes.py::finish_task` 同步；S5 的「done 必满格」不变量不变。
- 87ms 的其余部分（`save_backtest_run` 的 JSONB 写等）先量；**不**改报告落库语义、**不**为压指标牺牲正确性。

### 10.5 验收矩阵

| 项 | S7 before | S9 目标 | S9 实测（2026-10-30） | 判定 |
|---|---|---|---|---|
| 20 回测 queue p95 | 1,312ms（3 道中位） | **≤1,000ms** | **503ms**（2w，3 轮中位：1,160 / 473 / 503；5 轮中位 505ms） | warmup + 3×median，**达标** |
| run p50 | 209–304ms | **≤150ms** | **86ms**（3 轮中位 150 / 86 / 86；5 轮 84–89） | 3×median，**达标** |
| bars c=300（`ab`） | 1,583 req/s / 236ms | ≥1,500 / ≤308ms | **1,660 req/s / 240ms**（3 次：1,708/240、1,660/248、1,651/215） | 不回归，**达标** |
| metrics / digest | — | **逐键相等** | 真数据四项全等（metrics / digest / equity_curve / trades）；缓存热 50.5ms vs 直算 143.7ms | 黄金对照，**达标** |
| PIT / 用户策略 | 绿 | 绿 | `test_backtest_events` / `test_backtest_user` 定向用例绿（用户策略文件有既有顺序性 flaky，隔离复跑绿） | 定向，**达标** |
| 缓存失效 | — | 清单/字典戳变 → 键变 | 5 个新单测：字典文件戳、清单代际、分片文件戳、总开关 ×2 | 单测，**达标** |
| 内存 | — | 无 OOM；L1 ≤256 条 | 后端进程树 RSS 1.83GB（4 进程）；L1 由 `LOCAL_MAX=256` 强制淘汰；swap 90.6% 为环境既有 | 20 回测 + bars，**达标** |
| 冷首任务 | 1.8s | 如实记录，不承诺 | 首个缓存调用 1,008ms（含 DuckDB 冷查询）；后续 47.7 / 50.5ms | 单独记录 |

**判定（2026-10-30）**：主指标全部通过，**S9 达标**；S7 的原始 ≤1s 目标由 S9 复验**成立**。原始产物见 `docs/private/Pn-n/P4-Sn/baseline/s9-*` 与 `s9-golden-*`。

### 10.6 测试与回滚

- 单测（tmp 数据目录）：`cached_st_symbols` 命中/文件戳变；benchmark daily rows round-trip（date 值）；digest 同代际命中、`_meta` 变换键；`BT_DATA_CACHE_ENABLED=false` 走旧路径。
- 黄金：同一回测的 `metrics`/`meta.data_digest`/交易明细逐键相等；PIT 用例全绿；用户策略路径不变。
- 真机：warmup 1 次 → 20 回测 ×3；bars `ab -n 3000 -c 300` ×3；混合标的（次要）观察收益边界；内存/swap。
- 回滚：`BT_DATA_CACHE_ENABLED=false`；不改数据/表；S9 的代码与 S7 的 `TASK_WORKER_POOL_ENABLED` 独立。

### 10.7 风险与文档同步

| 风险 | 应对 |
|---|---|
| 同窗口才共享 benchmark、同标/同字典戳才共享 ST | 20 回测验收成立；混合负载收益更小，**如实写**，不外推 |
| 用户策略走子进程 | 父进程缓存不共享；S9 只覆盖内置策略 |
| 首任务冷 1.8s | warmup 后 3×median；冷/热分开记录 |
| engine 32.4ms 的 GIL 地板 | 20×32.4=0.65s <1s；换 event_driven/更大窗口后重测 |
| 执行体 87ms 若为串行 DB | `progress+finish` 合并后复测；若仍不够，另开 DB 小优化，不在 S9 硬塞 |
| 缓存载荷 JSON 化 | 优先复用 `cached_primary_names` 的 dict；band 结果缓存才需 ISO/float |
| bars 口径 | 必须 `ab`，harness `http` 是 httpx（4.8s 是假象） |
| `test_optimize_api.py` 14 失败 | HEAD 就有，不是 S9；用定向用例/CI |
| PIT 护城河 | 缓存只认代际/文件戳，不碰 `as_of`；PIT 全绿 |

**文档同步（S9 执行时）**：本 SPEC 头/目录/§1.3/§8.3.4/§8.4/§11/§12/§13；ROADMAP 加 S9 行、角色拆分改 P4-S10+；PRD v0.18 加 S9 段；CLAUDE.md 路线图表加 P4-S9、后续改 P4-S10+；`.env.example` 加 `BT_DATA_CACHE_ENABLED`（编辑被权限挡，贴给用户）；CHANGELOG 到里程碑。

### 10.8 排期

| 步骤 | 内容 | 估时（天） |
|---|---|---:|
| 1 | 门禁（已完成） | 0.25–0.5 |
| 2 | `cached_st_symbols` + 单测 | 0.25–0.5 |
| 3 | market_benchmark 缓存 | 0.5 |
| 4 | window_digest 缓存 | 0.25–0.5 |
| 5 | 执行体 progress+finish 合并 | 0.25–0.5 |
| 6 | band 结果缓存（可选） | 0.25 |
| 7 | 20 回测 + bars + 黄金 + 文档 | 0.5–1 |
| | **合计** | **2–3.5** |

---

## 11. 测试策略

| 层 | 落点 |
|---|---|
| 后端离线 | `pytest`（1,842 例基线）；新增 S1 契约/缓存、S2 租约/reaper/幂等、S3 服务队列/远程客户端/降级、S4 DuckDB 上限/闸门/超时/停机释放、S5 限流/进度、S6 只读 `DATA_DIR` → 503、S7 合批/worker 池、S8 指标/告警、S9 缓存/开关 |
| 集成 | `pytest -m integration`：真 PG/Redis/Qdrant；迁移/备份可选 |
| 前端 | `pnpm test && pnpm typecheck && pnpm lint`；events 截断/窗口类型 |
| 压测 | `docs/private/Pn-n/P4-Sn/harness/`（甲案已有；S6 扩到 events 窗口/SSE 50–80/混合/2w；S7 再加 20/50/100 任务、SSE 80/100/150、RAG c5/c10；S8/S9 复用） |
| S7 承压 | 合批（fake 精排：调用次数/批大小/分数切分/超时/取消）、worker 池（kinds 过滤/0 重复/kill -9/停机释放） |
| S8 可观测性 | 指标（queue/PG/host/backup 一次一值）、告警（`promtool` 或 `alert_hook` 单测）、缓存命中率/冷查询 |
| S9 数据层 | `cached_st_symbols`（文件戳/代际）、benchmark daily rows / digest round-trip、开关 off、20 回测与 bars 黄金对照 |
| 故障 | `backend/scripts/drill_s6.py`；S2/S5 进程级探针复用；每次演练留 `logs/s6-drill/<ts>/`；S7 留 `logs/s7-drill/<ts>/`；S8 留 `logs/s8-drill/<ts>/` |
| 备份脚本 | `bash -n` + 演练为准；可选 integration `pg_dump` + scratch `pg_restore` |
| 覆盖率 | 继续按 CI `--cov-fail-under=88`；不因新代码降低门槛 |

**闸门**：`.githooks/pre-commit` ruff；CI 三 job；PIT 用例（`as_of` 过滤）必须全绿；沙箱隔离不得静默降级；S7 的 SLO 与回滚开关见 §8.4/§8.5，S8 见 §9.4，S9 见 §10.5。

---

## 12. 已知边界与不做

- **单机、无 HA**：没有第二台机器；不承诺多 AZ/故障转移/SLA。
- **macOS 资源隔离弱于 Linux**：Docker 有硬上限；原生 API/RAG 进程只有并发闸门/监控，没有 cgroup RSS 硬限。
- **RAG 精排是设备串行**：单 MPS 进程，吞吐有上限；高并发靠降级/排队，不靠加进程。
- **S5 是条件项**：单 worker 达标或双 worker 不划算时不启用；文档如实写。
- **依赖审计例外（2026-10-27，2026-10-29 扩列）**：LiteLLM 1.106.6 仍硬约束 `filelock<4.0`，而审计要求的修复版是 4.2.1，装不上。CI **仅**对 filelock 的 `CVE-2026-112940` / `112913` / `112907` / `112908` / `112909` / `112910` / `112911` / `112912` / `112915` / `112935` / `112936` / `112938` / `112939` 加 `--ignore-vuln`，其余审计照常；`langgraph-sdk` 已升到 **0.4.10**（`CVE-2026-118667` 修复）。LiteLLM 放开 `filelock<4.0` 后删除例外并升级 `filelock>=4.2.1`。
- **不做**：Redis Streams 全量、**多进程** Worker 池、K8s、Qdrant 集群、多机 HA、Vault、Kafka/Celery、把 agent/backtest/report 拆服务。P4-S7 的「单进程内按 kind 的多协程消费」见 §8——它不是多进程 worker 池，也不是 HA；P4-S9 的数据层缓存见 §10，不是引擎/子进程优化。
- **P4-S7 的边界**：RAG 合批只提高吞吐/降级覆盖率，不改变「精排是单 MPS 设备串行」；worker 池是单进程内协程。SLO 以 §8.2 为准，不把降级后的数字写成达标。
- **P4-S8 的边界**：监控是单机 Prometheus/Alertmanager，不是生产 SLA；缓存命中率看板不改数据层语义（键带代际）。
- **P4-S9 的边界**：数据缓存只覆盖内置策略路径；同窗口才共享 benchmark、同标/同字典戳才共享 ST；用户策略子进程不共享父进程 L1；不改 engine/PIT/数据源。S9 达标与否见 §10.5。
- **P4-Sn 后续愿景**（多租户/分布式/商业化）仍按 PRD 触发条件评估，不在本轮。

---

## 13. 变更记录

> 本节是历史变更记录。P4-S7 紧接 §7 落在 **§8**；P4-S8 落在 **§9**；P4-S9 落在 **§10**；测试策略、边界与本节顺延为 §11/§12/§13。P4-S10 及以后按纯数字续在 §10 之后（届时顺延后续节号）或独立计划。

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-30 | v3.13 | S8 交付并验收：新增队列 p95 直方图、PG 连接 / 宿主内存 / swap / 备份年龄三个 scrape 快照 Gauge（2 worker 各一条）、`rag_rerank_requests_total` 分母、`/metrics` Bearer、`QS_BACKUP_DIR`；`monitoring` profile（Prometheus :9092 + Alertmanager :9093）+ 6 条规则 + 本地 `alert_hook`；备份 launchd 已装并 kickstart。实测：缓存 L1 385 / L2 13 / miss 2（**99.5%**）、`qs:cache_hit_ratio:5m` 0.969、bars c300 cache on/off 1,656/1,714 req/s、冷查询 39.97 req/s（不新增限流）；RAG 过载 / PG 105 / 旧备份三条真实告警 firing→resolved。修复 RAG `/metrics` 配 token 后 401、Prometheus 缺 `alerting` 目标、SIGHUP 热加载清空 scrape 目标。 |
| 2026-10-30 | v3.12 | S8 执行前订正 5 处口径：①「停 RAG → 降级告警」不成立（服务端比率停采集后 0/0，`unavailable` 按 §9.3.3 不进比率）→ 改为 RAG 过载注入验收，停 RAG 只记录 `up{job="rag"}=0`；②queue 直方图在持锁进程取件时 observe、正常走 mmap，只有 PG/host/backup 三个 Gauge 用 scrape 时 collector；③备份年龄取最新 manifest 的 mtime（文件名只用于选最新），避免每日备份前假报；④API/RAG 用 `logs/metrics_token` 与 `logs/rag_token` 两个文件（`METRICS_TOKEN` ≠ `RAG_SERVICE_TOKEN`）；⑤Prometheus/Alertmanager 宿主端口 9092/9093（9090/9091 被 MinIO 占用），规则 5 基线字面量 `1.06e10`（实测 9.61GB + 1GB）。保持 6 条规则；备份 launchd 本次安装并 kickstart。 |
| 2026-10-30 | v3.11 | S9 交付并验收：`cached_st_symbols`（复用 `cached_primary_names` 的文件戳+代际缓存）、`market_benchmark._daily_rows` 与 `window_digest` 走 `cache.through`（digest 键额外带窗口分片文件戳）、`BT_DATA_CACHE_ENABLED`（默认 true）、执行体 `progress+finish` 合并。实测（2 API workers，harness :8100）：20 回测 queue p95 **503ms**（3 轮中位；5 轮 505ms；S7 1,312ms）、run p50 **86ms**、bars **1,660 req/s / p95 240ms**（3 次中位）；真数据黄金对照 metrics/digest/equity/trades 四项全等。**S9 达标，S7 的 ≤1s 复验成立**。测试：新增 5 个缓存用例 + finish 进度合并单测/真库用例；离线/集成定向绿。 |
| 2026-10-30 | v3.10 | 写入 P4-S9 数据层热点优化规格（§10）：门禁实测 `_limit_bands` 28.7ms（**字典 21.5** + st 1.4 + raw 3.3 + band 循环 2.2）、engine 32.4、`market_benchmark` 35.2、`window_digest` 22.4；GIL 地板 1.22s → 字典缓存后 **0.79s**。范围：`cached_st_symbols`（复用 `cached_primary_names` 的文件戳+代际缓存）、benchmark daily rows、window_digest 三个缓存 + `BT_DATA_CACHE_ENABLED` 开关 + 执行体 `progress+finish` 合并。S7 worker 池实测 queue p95 **1,312ms**（3 道中位；4 道 1,401ms 无改善）、bars **1,583/236ms**；S7 的 ≤1s 由 S9 复验，S7 收口口径待拍板（2026-10-30「之后再商榷」）。角色拆分顺延 **P4-S10 及以后**；测试策略/边界/变更记录顺延 §11/§12/§13。 |
| 2026-10-29 | v3.9 | S7 执行：RAG 合批原型门禁实测（真 MPS / 50 篇，N=2/4/5/8 speedup **1.13–1.26×**、`5×50` **4.65s**）全部未过；拍板 **A：不合批**，RAG 项只交付 SLO 测量，不宣称降级率 ≤50%；S7 范围收窄为 worker 池 + SLO 记录，估 **4–5 天**。worker 池实现结果待回填。 |
| 2026-10-29 | v3.8 | 拆分迭代：缓存命中率/冷查询与监控告警/备份定时从 S7 整块移到 **P4-S8（§9）**；S7（§8）只留 RAG 合批/SLO + task worker 池化，估 **5–7 天**；测试策略/边界/变更记录顺延为 §10/§11/§12。角色拆分等愿景顺延到 P4-S9 及以后。S7/S8 均待执行，S1–S6 行为不变。 |
| 2026-10-29 | v3.7 | 写入 P4-S7 单机承压升级规格（§8）：RAG 跨请求合批 + 分级 SLO（先原型门禁；interactive c10 非降级 p95 ≤3.5s、降级率 ≤50%）、task worker 单进程内按 kind 多协程池化（3+1+1，20 回测 queue p95 ≤1s；配额/租约/幂等不变）、缓存命中率与冷查询（L2 验收时临时开，默认仍 false）、监控告警（scrape 时 collector、METRICS_TOKEN/RAG_SERVICE_TOKEN + Bearer、compose monitoring profile、6 条规则、本地 alert_hook、备份 launchd）；同步 §1.3/§1.4/§9/§10 的边界。S7 待执行，S1–S6 行为不变。 |
| 2026-10-29 | v3.6 | S6 口径细化：§7.1 固定 A/B 双栈与 1w/2w 3 次中位，events 6/12/24 仅验契约（语料约 3 个月，不可区分）；§7.2 补 5 行新故障的方法与期望（cache 需 `CACHE_ENABLED=true`、PG 重启“>1 次/池槽”才修、Qdrant 明确不可用、fake 429/超时、磁盘只读 503 + 不造 ENOSPC），明确 `backend/scripts/drill_s6.py` 与 `logs/s6-drill/`；§7.3 定 `scripts/backup.sh` / `restore.sh` / launchd 安装器、`--target` 默认 `~/QuantSageBackups`、PG 两库、Qdrant 未变复用、data 硬链接快照、恢复默认 scratch；§7.4/§7.5 定报告落点与 `m-p4`。不改 S1–S5 行为。 |
| 2026-10-28 | v3.5 | S5 按预测量与拍板修订：Go/No-Go 拆成「预测量（1–3 条）+ 实现后验收（第 4 条）」；内存口径写死为「2 API 进程树 + RAG + docker stats ≤18GB」，另判 available/swap；明确用 `uvicorn --workers`（0.54 的 worker 重启）与主实例锁重试；订正 MCP 为每次调用新建会话；`init_schema` 加跨进程串行；coord 定 6380/128MB/192m，降级 `floor(capacity/N)` 不全量 fail-closed；指标改自定义 TASKS collector + 死 pid 清理；进度选 `tasks.progress.cells` JSONB + 1s 轮询，不新增 `task_events` 表。实现并验收通过：限流 5/15、bars 1,699 req/s / p95 231ms、10×81 格 0 重复、停机重排仅 1 条结果。 |
| 2026-10-27 | v3.4 | S4 按拍板修订：DuckDB `memory_limit` 明确为**每连接**上限，查询闸门改为无条件开启（初始 4），temp 目录逐连接唯一并清扫死 pid 残留；Docker 预算改为 PG 1.25g + `max_connections=120` + `shm_size`，Redis cache 640m / `maxmemory 512MB` / 关闭 RDB，**coord 移到 S5**；PG 业务池 5s、checkpointer 30s，worker 共用业务池（不为 worker 单开池）；worker 正常停机补 `release_task`（queued、attempts−1、取消收 cancelled），S2 的「stop() 不变」由本条取代；`prod.sh` 用 `QS_MODE=prod` 委派 `dev.sh`，补 15s drain 与 copytruncate 轮转；swap 验收改为按本次基线判增量。 |
| 2026-10-27 | v3.3 | S3 按拍板修订：`/v1/embed` 出参补稀疏权重（hybrid/索引依赖）；过载改用 503 + `reason`（删除含义模糊的 `degraded?`）；补 `batch` 类与索引任务走服务；精排用单 worker + 信号量、排队计数只算等待中、不做抢占；降级原因落到 `RetrievedEvent.degraded_reason`，指标按服务/API 分工；鉴权在 token 为空时回环放行；嵌入用独立的 `RAG_EMBED_WAIT_SECONDS`（初始 5s），不套用精排短超时；交互/研究等待按 2026-10-27 实测调为 0.5s / 2.5s。 |
| 2026-10-27 | v3.2 | S2 按拍板修订：终态/进度写入补 `worker_id` 栅栏（重新取件后 `status='running'` 拦不住迟到写入）；启动扫尾改扫全部 `running`（持锁即无第二 worker，kill -9 后秒级重排），周期 reaper 仍只看过期租约；重排按异常类型判（`internal` 代码 bug 不重排）；明确本仓无执行级超时；`task_id` 用普通唯一索引（多 NULL 不冲突）；取件归零进度、reaper 收尾已取消任务。 |
| 2026-10-27 | v3.1 | S1 按实测与拍板修订：订正根因（`/events` 不读字典；真实瓶颈是无结果缓存的 DuckDB 重扫）、当前语料仅 3 个月故 6/12/24 窗口不可区分、新增 `dc.events_page` 有界结果缓存与 `naming.cached_primary_names()`；补 `start > end` 422、`window` 可为 null、缓存验收与回滚、日界已知边界。 |
| 2026-10-27 | v3.0 | 初稿：基于 P4-S0 基线与用户 5 项拍板，定义 S1–S6；本轮定位为单机工程化演练，非 HA。 |
