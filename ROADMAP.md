# QuantSage Roadmap

> 状态图例：✅ 已完成 · 🚧 进行中 · ⬜ 未开始 · ✂️ 已砍（后置）
>
> **本文只回答「做到哪了、下一步是什么」。** 功能定义与验收见 [PRD](docs/PRD.md)，技术规格见 [specs](docs/specs/)，里程碑级交付见 [CHANGELOG](CHANGELOG.md)，逐次交付过程见各 SPEC 的变更记录。

## 当前焦点

**阶段**：P1-Tn / P2-Mn / P3-En 全部交付（`m-p1` / `m-p2` / `m-p3`，P3 收口于 2026-10-20）；**P4-Sn 单机工程化（S1–S4 已交付；S5 预测量 Go、待拍板实现；S6 待审）**；后续愿景仍不排期。

**进行中**：P4-Sn 单机工程化（S1–S4 已交付；S5 预测量 Go、待拍板实现；S6 待审）。**待办**：全量容器化（阻塞在 linux/arm64 的 CPU-only torch 方案）；按 S5→S6 实施。

**下一个动作**：拍板是否实现 S5（预测量 2026-10-28：bars c=300 p95 1w **390ms** → 2w **234ms**，吞吐 **1.83×**、health **2.02×**，内存/swap 达标；Go 条件 1–3 成立，第 4 条待实现验收）；S6 待 S5 决定后收口。

**待决事项**

- **冻结交易日历止于 2026-12-31**：上游 `exchange-calendars==4.13.2` 的 XSHG 仍止于该日，升级重生成暂时走不通；X1 体检按 90 天告警（看板 / ETL note 均可见），不会静默到期。**不要用工作日近似硬扩**（春节 / 调休会静默算错交易日）。
- **教程数字绑定数据快照，而事件语料是滚动保留的**（新闻类约 3 个月）：`event_driven.md` 的窗口起点会随保留期前移，读者回填到的语料起点通常晚于文档，跑出的数字不同属正常。
- **E1a 的取件是轮询（200ms）**：单次回测端到端因此多约 0.2s 启动延迟。可延：提交时置一个进程内唤醒事件可压到即时，但当前不值得为 0.2s 加一处跨 loop 状态。

## P1-Tn · 跑通 demo

| 功能 | 状态 |
|---|---|
| T1 地基 | ✅ |
| T2 样例数据 | ✅ |
| T3 Agent 对话 | ✅ |
| T4 最小回测 | ✅ |
| T5 分析输出 + 前视偏差对比 | ✅ |
| T6 极简前端 | ✅ T6a · T6b · T6c · T6d |
| T7 示例策略教程 | ✅ |

## P2-Mn · MVP

| 功能 | 状态 |
|---|---|
| M1 用户系统 + 自选股 | ✅ M1a · M1b · M1c |
| M2 数据层 + 日增量 ETL + 数据体检 + 行情按需刷新 | ✅ M2a · M2b · M2c · M2d |
| M3 RAG 完整化 | ✅ M3a · M3b · M3c |
| M4 策略工作台 + 前视静态检查 | ✅ M4a · M4b · M4c · M4c-3 |
| M5 回测增强 + A 股规则 + 因子分析 | ✅ M5a · M5b · M5c |
| M6 模拟盘 + 费用滑点模型 | ✅ M6a · M6b |
| M7 绩效分析 + 决策反思 + 证据面板 | ✅ M7a · M7b · M7c · M7d |
| M8 深度研报多 Agent + 过程可视化 | ✅ M8a · M8b · M8c |
| M9 轻量教学 | ✅ |
| M10 研究首页（大盘总览 + 事件日历 + 标的浏览 + 事件详情） | ✅ M10a · M10b · M10c · M10d |

## P3-En · 企业级思维

| 功能 | 状态 |
|---|---|
| E1 架构分层 | ✅ 任务队列 + 后台主实例锁 + 取用式接缝 |
| E2 多用户治理 | ✅ 配额 · RBAC · admin 视图 · 越权矩阵 |
| E3 数据工程 | ✅ 体检定时化 + 快照/回滚 + Redis 缓存 |
| E4 可靠性 L0–L7 | ✅ 统一 policy · 限流 · 熔断 · 幂等 · 护栏 |
| E5 风控 | ✅ 四闸门 + 自动止损 + 审计 + 前端 |
| E6 组合 | ✂️ 已砍（后置） |
| E7 工程化 | ✅ CI · ruff · 覆盖率门槛 · Langfuse · `data_digest` · `/metrics` |
| E8 安全 + 对外 MCP | ✅ 服务侧加固 · 对外 MCP Server · 注入边界 · OS 级沙箱 |
| E9 企业级文档 | ✅ `docs/enterprise/` 四篇 + 索引 |
| P3 收尾项 5 | ✅ Agent 提案 · RAG 一轮纠错 + 静态能力卡 · 策略版本 · 深研报评测 |

## P4-Sn · 单机工程化（S1–S4 已交付；S5/S6 待审）

> 定位：**能力演练/工程化加固**，不是“真实流量触发”的后续愿景。完成后是“经过压测与故障演练验证的单机工程化方案”，不是生产级高可用。
> 规格：[`docs/specs/P4-Sn.md`](docs/specs/P4-Sn.md)；基线读数见 `docs/private/Pn-n/P4-Sn/P4-S0-基线报告.md`（私有，不入库）。

| 工作包 | 状态 | 验收口径 | 预估 |
|---|---|---|---:|
| S1 修 `/events` 读路径 | ✅ | c=50 p95 <500ms、吞吐 ≥150 req/s；不静默改变“全部事件”语义 | 1–1.5 天 |
| S2 队列可靠性 v2 | ✅ | `SKIP LOCKED` + 租约/心跳；kill -9 重启后 60s 内重排；`max_attempts=2`；0 重复结果 | 1.5–2 天 |
| S3 RAG 过载治理 | ✅ | 单 RAG 进程；交互 10 并发 p95 ≤2s、研究 5 并发 p95 ≤5s 或明确降级；0 MPS 崩溃；降级可见 | 1.5–2 天 |
| S4 资源上限与底座 | ✅ | Docker/DuckDB/Redis/PG 上限生效；soak 无 OOM；swap 不增长 | 0.5–1 天 |
| S5 双 API Worker（条件） | ⬜ | S4 后 go/no-go；health ≥1.8×、bars c300 p95 ≤300ms、限额 5 仍只放行 5 | 1.5–2 天 |
| S6 验证与收口 | ⬜ | 压测/故障/备份恢复/文档；每项 before/after | 1–1.5 天 |

**S1 复验（2026-10-27，同机 `ab`，单进程）**：旧代码 c=50 63 req/s / p95 1,045ms；新代码 c=50 **1,098 req/s / p95 69ms**、c=10 1,186 req/s / p95 12ms（缓存预热）。冷缓存单次 73ms；50 个不同标的顺序跑 38 req/s（如实记录，**不列入 SLO**——S1 的达标读数是同一 URL 的缓存命中）。规格 v3.1：`dc.events_page` 有界结果缓存 + 默认窗口/limit/truncated。

**S3 复验（2026-10-27，同机测试栈 + 真模型）**：进程边界 **API 52MB / RAG 服务 2.3GB**，MCP 经服务检索通过；交互 10 并发全链路 p95 **1.91s**（0.5s 等待，9/10 按 timeout 降级；5 轮 1.91–2.85s，高 swap 轮略超 2s，如实记录）；研究 5 并发 p95 **4.20s**（2.5s 等待，2 条降级）；隔离精排接口 10 并发 p95 **0.51s**；200 条评测 rerank NDCG@10 **0.8142**（与 M3c 基线一致）；0 MPS 崩溃，队列/降级/等待指标可从 `/metrics` 读到。规格 v3.3：独立 RAG 服务 + 有界队列 + 降级原因可见。

**S4 复验（2026-10-27，同机）**：Docker 三件上限生效——PG **1.25g / max_connections 120 / shared_buffers 512MB**、Redis cache **640m / maxmemory 512MB volatile-lru / 无 RDB**、Qdrant **2.5g**，`OOMKilled=false`；DuckDB 每连接 **threads=2 / memory_limit=1GB / 独立 temp**，进程查询闸门 4；业务池 `statement_timeout=5s`、checkpointer 30s。5min soak（50 读 + 10 SSE + RAG 循环 + 20 回测）：API RSS 503→480MB（峰值 875MB，无泄漏）、swap **13.16→13.11GB**（未增长）、PG 连接峰值 21；回归 bars c=300 p95 **370ms**（基线 369ms）、events c=50 p95 **33ms**（基线 1,028ms）、SSE 20 并发 p95 **311–403ms**（中位 318ms，基线 314ms）、20 回测 queue p95 **2,482ms**（基线 2,224ms）。prod.sh start/health/stop 与 copytruncate 轮转通过。如实记录：soak 读 47,373 条中 **8 条 `RemoteProtocolError`**（keepalive 断连，0.017%）、1 条 chat LLM 连接中断（0.27%），无 OOM/进程崩溃。规格 v3.4：DuckDB 闸门 + PG/Redis 预算 + worker 释放租约 + prod 运行底座。

**S5 预测量（2026-10-28，harness 8100，S0 同口径）**：1w bars c=300 p95 中位 **390ms / 904 req/s**；2w **234ms / 1,651 req/s（1.83×）**、health **5,813 → 11,754（2.02×）**；2w 进程树峰值 **2.40GB**、RAG 0.47GB（空闲）、docker 0.37GB，系统 available 5.84–6.18GB、swap **9.20→8.68GB（未增长）**；2w 5min soak 89,128 读 0 错误、PG 连接峰值 22。限流缺口复现：`RATE_LIMIT_CHAT=5`、2w、100 并发 → **10 放行 / 90 个 429**（5×2）。Go 条件 1–3 成立；第 4 条（共享限流 / 多进程指标 / 跨进程进度 / 停机）待实现验收。原始文件在 `docs/private/Pn-n/P4-Sn/baseline/s5-premeasure-20261028/`。

**S2 复验（2026-10-27，同机测试栈）**：4 路并发 ×100 件取件 **0 重复 / 0 遗漏**；任务执行中 `kill -9` 后端 → 重启后 **8.3s** 回 `queued`，第二次执行 `attempts=2` 跑完、`optimization_runs` **仅 1 条**；第二次再杀 → `failed/lease_expired`（不无限重排）；20 任务 queue p95 **2,365ms**（基线 2.2s）。规格 v3.2：`SKIP LOCKED` + 租约/心跳 + `worker_id` 栅栏 + 启动全量扫尾 + `task_id` 结果幂等。

**不做**：Redis Streams 全量改造、Worker 池扩容、K8s、Qdrant 集群、多机 HA、Vault、Kafka/Celery、把 agent/backtest/report 拆成独立 HTTP 服务。

## P4-Sn 后续愿景（不排期）

多租户 / 分布式回测 / 策略市场 / 商业化——见 [PRD §2.4](docs/PRD.md#24-p4-sn--单机工程化与愿景)。触发条件（满足任一才启动）：出现真实多用户流量 / 商业化诉求 / 团队扩张。**本轮 S1–S6 不改变这条触发条件。**
