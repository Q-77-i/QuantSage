# QuantSage SPEC · P4-Sn（单机工程化）

> 文档链：规划报告（`docs/private/`）→ CLAUDE.md（定稿摘要）→ PRD（v0.15）→ 本文（技术规格）→ 代码
>
> 版本 **v3.1** ｜ 2026-10-27 ｜ 状态：**S1 已交付；S2–S6 待审**——P4-Sn 单机工程化（S1–S6）规格；S1 按 2026-10-27 实测与拍板修订并实施（v3.1，见 §10）。
>
> 本文只覆盖 **P4-Sn 单机工程化（S1–S6）**。后续的平台化/多租户/分布式仍为愿景，**不写规格、不排期**。
> 实测基线：`docs/private/Pn-n/P4-Sn/P4-S0-基线报告.md`；再规划：`docs/private/Pn-n/P4-Sn/P4-Sn-单机工程化-再规划.md`。
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
| [§8](#8-测试策略) | 测试策略 |
| [§9](#9-已知边界与不做) | 已知边界与不做 |
| [§10](#10-变更记录) | 变更记录 |

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

### 1.4 非目标（写死）

- Redis Streams 全量改造；本 SPEC 仅在 S5 用 **DB 进度轮询**，不引 Streams。
- Worker 池扩容 / 多 worker 消费；Worker 保持单实例。
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

**迟到完成**：被回收后旧 worker 若仍跑完，`finish_task` / `fail_task` 的 `status='running'` 条件会让它落空；结果写入再靠 `task_id` 唯一约束兜底。两者都不能覆盖新 worker 的行。

**reaper**：每 30s 扫 `status='running' AND lease_expires_at < now()`：

- `attempts < max_attempts` → `status='queued'`、`available_at = now()+5s`、清 `worker_id/lease`；不写 `error`（还没终态）。
- `attempts >= max_attempts` → `status='failed'`、`error={"kind":"lease_expired","message":"执行中断且已达最大尝试次数","attempts":N}`。
- 进程启动时先跑一次 reaper，替代旧的“把所有 running 标 failed”的扫尾；只动**过期租约**，不碰心跳新鲜的行。

**为什么不自动晋升**：后台主实例仍由 Postgres advisory lock 竞选；主实例退出后由 supervisor 重启并重新竞选。本轮 S1–S6 不改变这条。

### 3.4 重试与幂等

**可重排**（示例，最终以 `error_of`/`classify` 的 kind 映射为准）：

- 租约过期 / worker 崩溃；
- `internal` 中可判为瞬时的（DB 连接断开、临时 IO、线程池拒绝）；
- `timeout`（执行超时，且不是用户代码死循环——用户策略沙箱的墙钟/CPU 超限是确定性的，不重排）。

**不重排**：

- 参数/校验/静态检查/沙箱拒绝/无数据/权限/配额等确定性错误；
- 用户策略运行期异常（同一份源码重跑仍是同样结果）；
- 已取消的任务。

**结果幂等**：

- `backtest_runs` 增 `task_id UUID NULL` + 唯一索引；`optimization_runs` 同理。
- 执行体取件后先查 `task_id` 是否已有结果：有则直接 `finish_task` 返回既有 `run_id`，不重复执行。
- 写结果时用 `INSERT ... ON CONFLICT (task_id) DO NOTHING`；冲突则读既有行返回。
- 旧记录 `task_id` 为 NULL，不回溯。

**取消**：`cancel_requested_at` 仍是权威；取件前/执行中检查；已取消的任务不重排。单次回测的“在跑不可中止”语义不变（409）。

### 3.5 验收

| 项 | 口径 |
|---|---|
| 取件并发 | 直接 SQL 探针 4 路 ×100 轮 0 重复；单 worker 不受影响 |
| 崩溃重排 | 任务执行中 `kill -9` worker → 60s 内回 `queued`，`attempts=2`；第二次再杀 → `failed/lease_expired`，不无限重排 |
| 确定性失败 | 无数据/参数错误第一次即 `failed`，`attempts=1`，不重排 |
| 幂等 | 同一任务重排后只产生一条 `backtest_runs`/`optimization_runs`；结果 `run_id` 一致 |
| 队列不回归 | 20 任务排队 p95 ≤2.5s（基线 2.2s）；取消仍生效 |
| 回归 | 后端离线 1,797 例 + ruff 全绿；新增用例覆盖租约/reaper/幂等 |

### 3.6 沿革与回滚

- 旧：`running` 在重启时一律 `failed/interrupted`。新：过期租约有限次回 `queued`；只有 attempt 用尽才 `failed`。这是**产品语义变更**，必须写进 PRD/README/architecture。
- 回滚：配置 `TASK_MAX_ATTEMPTS=1` 可退回“只执行一次、不重排”；`TASK_LEASE_ENABLED=false` 只保留 `SKIP LOCKED`（仍修重复取件）。

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
| `POST /v1/embed` | `texts[]`, `batch_size?` | `vectors[][]` |
| `POST /v1/rerank` | `query`, `documents[]`, `request_class` | `scores[]`, `degraded?` |
| `GET /health` | — | `{status, models, device, queue}` |
| `GET /metrics` | — | Prometheus 文本 |

- API/MCP：通过 `app/rag/remote.py` 的 `RemoteEmbedder` / `RemoteReranker` 实现现有 `Embedder`/`Reranker` 协议；**API 进程不再 import/加载 `app.rag.encoder` 的模型**。离线测试注入假实现，不依赖服务。

### 4.3 请求分级与有界队列

| 类别 | 调用点 | 队列上限 | 排队超时 | 过载行为 |
|---|---|---|---:|---:|---|
| `interactive` | Chat 的 `search_events`、MCP | `max_queue=8` | **1.5s** | 快速 503/`overloaded` → 调用方降级 RRF |
| `research` | 深研报 news 角色 | `max_queue=32` | **10s** | 有界排队；满/超时后降级 RRF 或明确 503 |

- `max_inflight`：embed 2–4；rerank **1**（MPS 串行，显式承认）。
- 队列满/超时**必须返回明确结果**：不允许无限等待；调用方把 503 转成 RRF 结果或明确错误帧。
- 超时值进配置，不在代码里写死；研究类可单独调大，但必须有上限。

### 4.4 降级与可观测

- **交互优先降级**：rerank 队列满/超时/服务不可用 → `search` 返回 RRF 顺序，`score_kind="rrf"`，并在工具/MCP 返回里写明 `degraded_reason`（`queue_full` / `timeout` / `unavailable`）。
- **研究类**：允许有界排队；排队失败同样降级 RRF 或返回明确 503；不静默换查询/PIT 条件。
- **指标**：`rag_rerank_queue_depth`、`rag_rerank_inflight`、`rag_rerank_degraded_total{class,reason}`、`rag_rerank_wait_seconds`、`rag_embed_duration_seconds`；RAG 服务 `/metrics` 暴露，Prometheus scrape。
- **返回可见**：`score_kind` 已是既有字段；降级原因进工具文本/MCP `query` 旁注/结构化字段（具体形状实施时定，但不得只写日志）。
- **PIT 红线**：降级只改排序，不改 `as_of`/`symbol`/`event_type`/`top_k`；`available_at <= as_of` 仍在 Qdrant 服务端过滤。

### 4.5 批处理（可选，非前置）

- 只做“有基准收益”的试点：把 rerank 的 50 个候选攒批（batch_size 8/16）测吞吐/延迟；若吞吐 ≥1.5× 且 p95 不恶化才保留。
- 不满足就只保留队列/降级；**批处理不作为 S3 的硬性验收项**。

### 4.6 验收

| 项 | 口径 |
|---|---|
| 进程边界 | API 进程不再加载模型（RSS 证明）；RAG 单进程；`/health` 可见模型/设备/队列 |
| 交互过载 | 10 并发 interactive rerank：p95 ≤2s，`degraded_total>0`，无无限等待 |
| 研究排队 | 5 并发 research rerank：p95 ≤5s（有界排队）或明确降级；无死等 |
| 稳定性 | 0 MPS 崩溃；RAG 进程 RSS 与基线（1.7–2.6GB）同量级；soak 不涨 |
| 可见性 | 队列深度/降级计数/等待时间可从 `/metrics` 读到；工具返回带 `score_kind=rrf` 与原因 |
| 回归 | 既有 RAG 评测（200 条）与离线用例不回归；PIT 过滤用例全绿 |

### 4.7 边界与回滚

- 不做多 RAG 进程、不做 GPU/MPS 多实例、不换模型。
- 回滚：`RAG_SERVICE_URL` 置空可回退到进程内（仅单 API 进程；双 worker 不建议）；降级默认值可调，但“队列满/超时必须有明确结果”不退化。

---

## 5. S4 资源上限与运行底座

### 5.1 Docker 上限

在 `docker-compose.yml` 给默认服务加 `mem_limit` / `cpus`（初始值，实测后调）：

| 服务 | mem_limit | cpus | 说明 |
|---|---:|---:|---|
| qdrant | 2.5g | 2.0 | 当前 RSS 1.8GB；留索引/压缩余量 |
| postgres | 1g | 2.0 | shared_buffers 512MB；连接预算见 §5.3 |
| redis（cache） | 512m | 0.5 | `volatile-lru`，缓存键带 TTL |
| redis（coord） | 128m | 0.25 | `noeviction`，限流/协调键 |

- Docker VM 上限 8GB；默认四件合计 ≤4.5GB，给 Docker 开销留余量。
- Langfuse 全套继续 `observability` profile **按需启动**；不常开。若常开，需先调大 Docker VM 并重测内存预算。
- 不把后端/RAG/worker 放进 Docker（torch linux/arm64 阻塞仍在）；只用 Docker 管有状态基础设施。

### 5.2 DuckDB 上限

`duckdb_client.connect()` 每个线程连接显式：

```sql
SET threads = 2;
SET memory_limit = '1GB';
SET temp_directory = '<data_dir>/_tmp/duckdb-<pid>';
```

- `temp_directory` 用绝对路径 + 进程唯一子目录；不要用默认 `.tmp`（相对 cwd，多进程会撞）。
- 若复测 p95 仍不达标，再加进程级查询闸门（初始 4；API/worker 各一），但不先上复杂调度。
- 大扫描（体检/ETL）继续走低优先级/独立线程，不与回测抢。

### 5.3 Redis / PostgreSQL

- Redis cache：512MB、`volatile-lru`；缓存键必须带 TTL；不把限流/进度等协调键放进 cache 实例。
- Redis coord：128MB、`noeviction`；限流键带 TTL；满了要报警，不静默丢键。
- PG：`max_connections=200`、`shared_buffers=512MB`；连接预算：API 2×(8 业务 + 8 checkpointer) + worker 4 + scheduler 2 + Chat advisory ≤24 + 余量，控制在 80 以内；API `statement_timeout=5s`、worker 30s。

### 5.4 进程与停机

- 原生进程管理：`scripts/prod.sh`（start/stop/status/logs/health）或 launchd；Docker 只管基础设施。
- 健康检查：`/health`、`/health/ready`、RAG `/health`；启动顺序 infra → migrate → RAG → API/worker → 前端。
- 优雅停机：API 停止接新请求、drain 在飞/SSE（上限 15s）；worker 停止取件、续租到结束或释放租约；scheduler `shutdown(wait=False)`。
- 日志轮转；不把密钥写进 plist/命令行。
- 迁移：S2 加列用幂等 DDL；`schema_migrations` 作为 S6 可延项，不阻塞本轮。

### 5.5 验收

| 项 | 口径 |
|---|---|
| 容器上限 | `docker stats` 不超表；Qdrant/Postgres 不 OOM |
| DuckDB | `current_setting('threads')=2`、`memory_limit=1GB`、temp 路径正确；并发回测不 OOM |
| Redis | cache/coord 策略与上限生效；coord 不被 LRU 淘汰 |
| Soak | 5–10min 混合负载无 OOM、swap 不增长（允许基线 8.4GB 不变） |
| 回归 | bars/SSE 不因资源上限出现 >20% p95 退化；若退化，记录并交给 S5 判断 |

---

## 6. S5 双 API Worker（条件项）

### 6.1 Go/No-Go（S4 后复测）

**Go 条件（同时满足）**：

1. 单 API Worker 在压力档未达标：bars c=300 p95 **>300ms**（基线 369ms；S4 后复测）。
2. 双 Worker 的收益足以覆盖成本：bars c=300 p95 改善 **≥1.3×** 或吞吐 **≥1.5×**；`/health` ≥1.8×。
3. 内存有余量：2 workers + RAG + 基础设施峰值 **≤18GB**，系统可用 **≥2GB**，swap 增长 **<1GB**。
4. 共享限流/指标/优雅停机/DB 进度四项验收全过。

**No-Go**：以上任一不满足 → 保持单 API Worker；S5 可只做“能力演示”或不做，直接 S6 收口。文档如实写“多进程未启用”。

### 6.2 进程模型

- **2 个 uvicorn workers**（或两个受 supervisor 管理的 API 进程），共享 8000；无 HPA/自动扩缩。
- 后台主实例仍由 Postgres advisory lock 只选一个：只有持锁进程跑 task worker/scheduler；另一个只服务 HTTP。
- 每个 API worker 不加载 RAG 模型（S3 已剥离）；xiaoshi MCP 子进程会每 worker 一份，S5 验收记录内存成本，必要时懒加载。
- Chat 同 thread 的跨进程串行仍靠 PG advisory lock；池预算见 §5.3。

### 6.3 共享限流

- 独立 `redis-coord`，Lua 令牌桶；键 `rl:{bucket}:{user_id}`，TTL = 窗口。
- API 每进程调用同一 Lua；放行/拒绝语义与现有 `Limiter` 一致；`Retry-After` 用剩余等待。
- Redis coord 不可用：降级为进程内限流，额度 **1/N**（保守），记 warning/指标；chat/research 等昂贵端点可 fail-closed。
- 验收：同用户 `RATE_LIMIT_CHAT=5`，2 workers 下 20 并发 → **5 放行 / 15 个 429**（不是 10）。

### 6.4 Prometheus 多进程

- `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`；HTTP 计数/耗时聚合。
- `TASKS` gauge 由**单一 exporter 从 DB 查**，不每进程各算一份；worker/RAG 各自小端口。
- 验收：多次 scrape 的 `sum(quantsage_http_requests_total)` 与总请求数同量级；任务状态不重复计数。

### 6.5 优雅停机

- SIGTERM：停止接新请求 → drain 在飞/SSE（上限 15s）→ 退出；worker 停止取件、续租到结束或释放租约；scheduler 停 job。
- supervisor 重启后重新竞选后台主实例；不自动晋升。
- 验收：20 条 SSE 期间 SIGTERM，不出现 500；客户端可重连；任务不 orphan。

### 6.6 跨进程网格进度（DB 轮询）

- **不引 Redis Streams**。`tasks.progress` JSONB 增 `cells`（有界，最多 1,000 格）或新增 `task_events` 表；worker 逐格写入 DB。
- API `task_frames` 每 0.5–1s 读 `tasks.progress`，按 seq/index 去重后发 SSE；终态仍从 `optimization_runs.summary.cells` 回放。
- 进程内 `bus` 可保留为本地加速，但**DB 是跨进程权威**；任意 API worker 都能服务进度。
- 验收：网格任务在非 master worker 上也能看到逐格进度；断线重连不重复/不丢格；100 格任务 DB 写入不拖慢主流程。

### 6.7 验收

| 项 | 口径 |
|---|---|
| 吞吐 | `/health` ≥1.8×（基线 5,946 → ≥10,700）；bars c=300 p95 ≤300ms |
| 限流 | 5→5（不是 10） |
| 指标 | 多进程聚合可读；任务 gauge 不重复 |
| 停机 | SIGTERM 不丢 SSE/任务；重启健康 |
| 进度 | 任意 worker 可见网格进度；重连正确 |
| 内存 | 2 workers + RAG + infra 峰值 ≤18GB；swap 增长 <1GB |
| 回退 | 任何一项失败 → 回退单 API worker，保留 S1–S4 |

---

## 7. S6 验证、备份与文档收口

### 7.1 压测矩阵

- 读：`/health`、`/auth/me`、bars、events（6/12/24 个月窗口）；1w vs 2w。
- SSE：5/10/20/50/80 并发；混合 ab 读 + SSE。
- 回测：20 任务；kill -9 中途；确定性失败。
- RAG：hybrid/rerank，interactive/research，过载/超时/降级。
- Soak：5–10min 混合；记录 RSS/swap/PG 连接/队列深度。

### 7.2 故障演练

| 故障 | 期望 |
|---|---|
| kill -9 worker 中途 | 60s 内重排；max_attempts 后终态；0 重复结果 |
| kill -9 API worker | 另一 worker 继续；SSE 可重连；任务不受影响 |
| Redis coord 挂 | 限流降级 1/N；进度回退 DB；任务继续 |
| Redis cache 挂 | 熔断直读；请求不失败 |
| PG 重启 | 池重连；任务不丢；Chat 明确 503/重试 |
| Qdrant 挂 | RAG 明确错误/降级；不假装有结果 |
| LLM 429/超时 | Retry-After/熔断；不雪崩 |
| 磁盘满/只读 | 明确 503；不写半条记录 |

### 7.3 备份与恢复

- PG：`pg_dump -Fc` 每日；保留 7 日 + 4 周；恢复脚本 + 空环境演练。
- Qdrant：collection snapshot（`POST /collections/{name}/snapshots`）每日/变更后；恢复演练。
- 数据：`data/_meta` 清单 + `data/_snapshots` 已有；另备份 `data/` 到外置盘（落点待用户定）。
- RPO/RTO：本地演练口径 RPO 24h / RTO 1h；**不是 SLA**。

### 7.4 文档同步

| 文档 | 改什么 | 时点 |
|---|---|---|
| `docs/PRD.md` | S1–S6 定位/范围/NFR/里程碑/变更记录 | 编码前 |
| `ROADMAP.md` | P4-Sn 单机工程化状态/DoD/排期；后续愿景仍不排期 | 编码前 |
| `docs/specs/P4-Sn.md` | 本文 | 编码前 |
| `README.md` | 多 worker/指标/队列语义的**当前事实**；S1–S6 成果 | 各 S 完成后 |
| `docs/enterprise/architecture.md` | §2.1 进程模型、§9.1 队列语义、§9.3 多 worker 边界 | 各 S 完成后 |
| `docs/PROJECT_STRUCTURE.md` | RAG 进程/共享限流/双 worker 角色 | S5 后 |
| `CHANGELOG.md` | S1–S6 里程碑 | 收口后 |

### 7.5 最终报告

- 每项 before/after 表（用基线报告的格式）；未达标项如实写原因。
- 明确写出：**“单机工程化、经过压测/演练验证；不是生产级高可用/SLA。”**
- 不把“压力档一次通过”写成“已具备生产级高可用”。

---

## 8. 测试策略

| 层 | 落点 |
|---|---|
| 后端离线 | `pytest`（1,797 例基线）；新增 S1 契约/缓存、S2 租约/reaper/幂等、S3 降级/队列、S5 限流/进度 |
| 集成 | `pytest -m integration`：真 PG/Redis/Qdrant；迁移/备份可选 |
| 前端 | `pnpm test && pnpm typecheck && pnpm lint`；events 截断/窗口类型 |
| 压测 | `docs/private/Pn-n/P4-Sn/harness/`（甲案已有；扩展到 events/RAG/2w） |
| 故障 | `scripts/drill_*` 扩展；每次演练留日志 |
| 覆盖率 | 继续按 CI `--cov-fail-under=88`；不因新代码降低门槛 |

**闸门**：`.githooks/pre-commit` ruff；CI 三 job；PIT 用例（`as_of` 过滤）必须全绿；沙箱隔离不得静默降级。

---

## 9. 已知边界与不做

- **单机、无 HA**：没有第二台机器；不承诺多 AZ/故障转移/SLA。
- **macOS 资源隔离弱于 Linux**：Docker 有硬上限；原生 API/RAG 进程只有并发闸门/监控，没有 cgroup RSS 硬限。
- **RAG 精排是设备串行**：单 MPS 进程，吞吐有上限；高并发靠降级/排队，不靠加进程。
- **S5 是条件项**：单 worker 达标或双 worker 不划算时不启用；文档如实写。
- **依赖审计例外（2026-10-27）**：LiteLLM 直到 1.106.4 都硬约束 `filelock<4.0`，而审计要求的修复版是 4.2.1，装不上。CI **仅**对 `CVE-2026-112940` / `CVE-2026-112913` / `CVE-2026-112907` 加 `--ignore-vuln`，其余审计照常；LiteLLM 放开 `filelock<4.0` 后删除例外并升级 `filelock>=4.2.1`。
- **不做**：Redis Streams 全量、Worker 池、K8s、Qdrant 集群、多机 HA、Vault、Kafka/Celery、把 agent/backtest/report 拆服务。
- **P4-Sn 后续愿景**（多租户/分布式/商业化）仍按 PRD 触发条件评估，不在本轮。

---

## 10. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-27 | v3.1 | S1 按实测与拍板修订：订正根因（`/events` 不读字典；真实瓶颈是无结果缓存的 DuckDB 重扫）、当前语料仅 3 个月故 6/12/24 窗口不可区分、新增 `dc.events_page` 有界结果缓存与 `naming.cached_primary_names()`；补 `start > end` 422、`window` 可为 null、缓存验收与回滚、日界已知边界。 |
| 2026-10-27 | v3.0 | 初稿：基于 P4-S0 基线与用户 5 项拍板，定义 S1–S6；本轮定位为单机工程化演练，非 HA。 |
