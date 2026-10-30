# QuantSage Roadmap

> 状态图例：✅ 已完成 · 🚧 进行中 · ⬜ 未开始 · ✂️ 已砍（后置）
>
> **范围冻结暂缓（2026-10-30）**：原计划的冻结**暂不生效**——系统性遗留审计尚未清零，遗留问题需要继续收口。允许/不允许清单作为草案保留在文末「范围冻结（草案）」，待遗留问题清零后再决定是否生效。
>
> **本文只回答「做到哪了、下一步是什么」。** 功能定义与验收见 [PRD](docs/PRD.md)，技术规格见 [specs](docs/specs/)，里程碑级交付见 [CHANGELOG](CHANGELOG.md)，逐次交付过程见各 SPEC 的变更记录。

## 当前焦点

**阶段**：P1-Tn / P2-Mn / P3-En / P4-Sn 全部交付（`m-p1` / `m-p2` / `m-p3` / `m-p4`；P4-Sn S1–S6 于 2026-10-29 收口）；**P4-S7（承压）、P4-S8（可观测性与缓存）与 P4-S9（数据层热点）已交付（2026-10-30）**；后续愿景仍不排期。

**进行中**：P2 审计修复（P2a/P2b 已交付；P2c 测试/运维/文档已实现并验证）。**范围冻结暂缓（2026-10-30）**——先收口遗留问题，草案见文末「范围冻结（草案）」。**待办**：全量容器化（阻塞在 linux/arm64 的 CPU-only torch 方案）。

**下一个动作**：P2c 提交并复跑 CI；CI 绿后按审计剩余项与触发条件评估下一步。S8 见下（9 条规则、3 条真实告警 firing/resolved、缓存命中率可算、备份 launchd）；S7 复测（20 回测 queue p95 1,312ms、bars 1,583/236ms）；S9 数据层缓存后 queue p95 **503ms**、run p50 86ms、bars 1,660/240ms；S6 最终复测（2w bars 1,710 req/s / p95 280ms、health 11,599；8 行故障演练通过；备份 4.6GB / 恢复 36.3s）。后续愿景（角色拆分顺延 P4-S10 及以后）仍按触发条件评估。

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

## P4-Sn · 单机工程化（S1–S9 已交付）

> 定位：**能力演练/工程化加固**，不是“真实流量触发”的后续愿景。完成后是“经过压测与故障演练验证的单机工程化方案”，不是生产级高可用。
> 规格：[`docs/specs/P4-Sn.md`](docs/specs/P4-Sn.md)；基线读数见 `docs/private/Pn-n/P4-Sn/P4-S0-基线报告.md`（私有，不入库）。

| 工作包 | 状态 | 验收口径 | 预估 |
|---|---|---|---:|
| S1 修 `/events` 读路径 | ✅ | c=50 p95 <500ms、吞吐 ≥150 req/s；不静默改变“全部事件”语义 | 1–1.5 天 |
| S2 队列可靠性 v2 | ✅ | `SKIP LOCKED` + 租约/心跳；kill -9 重启后 60s 内重排；`max_attempts=2`；0 重复结果 | 1.5–2 天 |
| S3 RAG 过载治理 | ✅ | 单 RAG 进程；交互 10 并发 p95 ≤2s、研究 5 并发 p95 ≤5s 或明确降级；0 MPS 崩溃；降级可见 | 1.5–2 天 |
| S4 资源上限与底座 | ✅ | Docker/DuckDB/Redis/PG 上限生效；soak 无 OOM；swap 不增长 | 0.5–1 天 |
| S5 双 API Worker（条件） | ✅ | health ≥1.8×、bars c300 p95 ≤300ms、限额 5 仍只放行 5；coord 降级、多进程指标、跨进程进度、停机 | 1.5–2 天 |
| S6 验证与收口 | ✅ | 压测/故障/备份恢复/文档；每项 before/after；8 行故障通过、恢复 36.3s | 1–1.5 天 |
| S7 RAG SLO + worker 池 | ✅ | 合批门禁未过（按 A 不做）；RAG SLO 记录；worker 3+1+1 queue p95 1,312ms；bars 1,583/236ms | 4–5 天 |
| S8 可观测性与缓存 | ✅ | 缓存命中率 L1/L2/合计可算（验收 99.5%）；5 组指标 + 9 条规则（3 条真实 firing/resolved）+ 备份年龄；launchd 每日 03:30 | 3.5–4.5 天 |
| S9 数据层热点优化 | ✅ | `cached_st_symbols` + benchmark/digest 缓存 + 开关；20 回测 queue p95 **503ms**、run p50 86ms、bars 1,660/240ms；黄金对照四项全等 | 2–3.5 天 |

**S1 复验（2026-10-27，同机 `ab`，单进程）**：旧代码 c=50 63 req/s / p95 1,045ms；新代码 c=50 **1,098 req/s / p95 69ms**、c=10 1,186 req/s / p95 12ms（缓存预热）。冷缓存单次 73ms；50 个不同标的顺序跑 38 req/s（如实记录，**不列入 SLO**——S1 的达标读数是同一 URL 的缓存命中）。规格 v3.1：`dc.events_page` 有界结果缓存 + 默认窗口/limit/truncated。

**S3 复验（2026-10-27，同机测试栈 + 真模型）**：进程边界 **API 52MB / RAG 服务 2.3GB**，MCP 经服务检索通过；交互 10 并发全链路 p95 **1.91s**（0.5s 等待，9/10 按 timeout 降级；5 轮 1.91–2.85s，高 swap 轮略超 2s，如实记录）；研究 5 并发 p95 **4.20s**（2.5s 等待，2 条降级）；隔离精排接口 10 并发 p95 **0.51s**；200 条评测 rerank NDCG@10 **0.8142**（与 M3c 基线一致）；0 MPS 崩溃，队列/降级/等待指标可从 `/metrics` 读到。规格 v3.3：独立 RAG 服务 + 有界队列 + 降级原因可见。

**S4 复验（2026-10-27，同机）**：Docker 三件上限生效——PG **1.25g / max_connections 120 / shared_buffers 512MB**、Redis cache **640m / maxmemory 512MB volatile-lru / 无 RDB**、Qdrant **2.5g**，`OOMKilled=false`；DuckDB 每连接 **threads=2 / memory_limit=1GB / 独立 temp**，进程查询闸门 4；业务池 `statement_timeout=5s`、checkpointer 30s。5min soak（50 读 + 10 SSE + RAG 循环 + 20 回测）：API RSS 503→480MB（峰值 875MB，无泄漏）、swap **13.16→13.11GB**（未增长）、PG 连接峰值 21；回归 bars c=300 p95 **370ms**（基线 369ms）、events c=50 p95 **33ms**（基线 1,028ms）、SSE 20 并发 p95 **311–403ms**（中位 318ms，基线 314ms）、20 回测 queue p95 **2,482ms**（基线 2,224ms）。prod.sh start/health/stop 与 copytruncate 轮转通过。如实记录：soak 读 47,373 条中 **8 条 `RemoteProtocolError`**（keepalive 断连，0.017%）、1 条 chat LLM 连接中断（0.27%），无 OOM/进程崩溃。规格 v3.4：DuckDB 闸门 + PG/Redis 预算 + worker 释放租约 + prod 运行底座。

**S5 预测量（2026-10-28，harness 8100，S0 同口径）**：1w bars c=300 p95 中位 **390ms / 904 req/s**；2w **234ms / 1,651 req/s（1.83×）**、health **5,813 → 11,754（2.02×）**；2w 进程树峰值 **2.40GB**、RAG 0.47GB（空闲）、docker 0.37GB，系统 available 5.84–6.18GB、swap **9.20→8.68GB（未增长）**；2w 5min soak 89,128 读 0 错误、PG 连接峰值 22。限流缺口复现：`RATE_LIMIT_CHAT=5`、2w、100 并发 → **10 放行 / 90 个 429**（5×2）。Go 条件 1–3 成立；第 4 条随后实现并验收（见下条）。原始文件在 `docs/private/Pn-n/P4-Sn/baseline/s5-premeasure-20261028/`。

**S5 复验（2026-10-28，2 workers + redis-coord，harness 8100）**：实现后 bars c=300 **1,699 req/s / p95 231ms**（3 次中位）、health **11,810 / p95 9ms**，与预测量同量级无回归；共享限流 `RATE_LIMIT_CHAT=5` 20 并发 **5 放行 / 15 个 429**，停 coord **4 / 16**（floor(5/2)×2），恢复后 5；`/metrics` 2,000 请求 delta **2,001**、任务五状态各一行（自定义 collector）、死 pid 文件被清；81 格网格 10 条订阅**各 81 格、0 重复**、DB 轮询补 **509** 格；在飞网格 SIGTERM 后回 `queued`、attempts 0，重启跑完 attempts 1、81 格、`optimization_runs` **仅 1 条**；20 条 SSE 在飞 SIGTERM → 20/20 打开 200、drain 内跑完、0 个 500/Traceback。Redis coord 6380 / 128MB noeviction / 192m。kill -9 非 master / master 两种情形均通过（master 被杀后新进程接管后台主实例）。

**S2 复验（2026-10-27，同机测试栈）**：4 路并发 ×100 件取件 **0 重复 / 0 遗漏**；任务执行中 `kill -9` 后端 → 重启后 **8.3s** 回 `queued`，第二次执行 `attempts=2` 跑完、`optimization_runs` **仅 1 条**；第二次再杀 → `failed/lease_expired`（不无限重排）；20 任务 queue p95 **2,365ms**（基线 2.2s）。规格 v3.2：`SKIP LOCKED` + 租约/心跳 + `worker_id` 栅栏 + 启动全量扫尾 + `task_id` 结果幂等。

**S6 复验（2026-10-29，最终 HEAD，harness 8100）**：1w/2w 全矩阵。2w bars c=300 **1,710 req/s / p95 280ms**（3 次中位；S5 1,699/231ms）、health **11,599 / p95 9ms**（S5 11,810）；1w bars 864 / p95 361ms、health 5,784（S5 预测量 904/390、5,813）；SSE 5/10/20/50/80 并发 **0 错误**，2w 20/50/80 首 token p95 **312/557/635ms**；events 6/12/24 窗口 2,401–2,993 req/s、p95 22–38ms（语料约 3 个月，不可区分）；20 回测 queue p95 **2,423ms**、run p95 247ms；RAG hybrid 交互 c5 p95 **794ms**（0 降级）、rerank 交互 c10 **2,973ms**（8/10 timeout 降级）、研究 c5 **4,851ms**（2/5 降级）；2w 5min soak **89,113 读 / 1 个 ReadError**、p95 296ms、API 树峰值 **2.64GB**、PG 连接 30、swap **9.24→9.23GB**；混合 bars c=100 + SSE 20 3min：bars **1,637/s / p95 86ms**、SSE 首 token p95 888ms，0 错误。8 行故障演练通过：kill -9 worker **5.68s 重排**、kill -9 API worker 补进程/接手 master、coord **4/16**、cache 挂请求不失败、PG 重启任务 0 重复、Qdrant 挂明确“本地事件检索不可用”、LLM 429 **1.5s** / 超时 **120.6s**、磁盘只读 **503** 无半截。备份 `~/QuantSageBackups` **4.6GB**，PG+Qdrant+data 恢复 **36.3s**。代码修复：`OSError→503`、PG 池 `check_connection`。规格 v3.6。

**S7 复验（2026-10-29/30，harness 8100）**：RAG 合批原型门禁（真 MPS / 50 篇）N=2/4/5/8 speedup **1.13–1.26×**、`5×50` **4.65s**，全部未过 → 拍板 **A 不做合批**，只记录 SLO。worker 池 3+1+1：20 回测 queue p95 **1,312ms**（3 轮中位；S6 2,423ms）、4 道 1,401ms 无改善；bars c=300 **1,583 req/s / p95 236ms**；20/20 done、0 错误。测试：worker 单测 29、真库 kinds 过滤 4。规格 v3.9/v3.10。

**S9 复验（2026-10-30，harness 8100，2 API workers）**：数据层三处缓存 + `BT_DATA_CACHE_ENABLED` + `progress+finish` 合并。20 回测 queue p95 **503ms**（3 轮中位：1,160 / 473 / 503；5 轮中位 505ms）、run p50 **86ms**（S7 1,312ms / 209–304ms）；bars c=300 **1,660 req/s / p95 240ms**（3 次中位：1,708/240、1,660/248、1,651/215），不回归；真数据黄金对照 metrics / digest / equity_curve / trades **四项全等**（缓存热 50.5ms vs 直算 143.7ms）；后端进程树 RSS 1.83GB、无 OOM。**S9 达标，S7 的 ≤1s 复验成立**。测试：新增 5 个缓存用例 + finish 进度合并单测/真库用例。规格 v3.11（§10）。

**S8 复验（2026-10-30，同机）**：`/metrics` 新增队列 p95、PG 连接、宿主内存/swap、备份年龄；2 worker 下每指标**一条值**；`qs:cache_hit_ratio:5m` 压测后 **0.969**。缓存：dev 栈 2 worker + `CACHE_ENABLED=true` 一轮 L1 **385** / L2 **13** / miss **2**（合计 **99.5%**，L2 占 L1 未命中 **86.7%**），Redis 2 键 / 1.51MB、键含代际；harness bars c=300 cache on/off 中位 **1,656 / 1,714 req/s**（0 失败、无回归）。冷查询 50 个不同标的顺序读 **39.97 req/s**（p50 18.7ms / p95 37.4ms；S1 38.1 / 24.3 / 38.2）→ **不新增限流**。告警：`monitoring` profile（Prometheus :9092 + Alertmanager :9093）默认不启动；8 条规则 promtool 全过；真实演练 **RAG 过载 / PG 105 连接 / 伪造旧备份** 三条 firing → resolved 全落 `logs/alerts.log`。备份 launchd 已装并 kickstart，manifest `20261030.json`、年龄 432s。修复：RAG `/metrics` 配 token 后永远 401、Prometheus 缺 `alerting.alertmanagers`、SIGHUP 热加载会清空 scrape 目标。规格 v3.13（§9）。

**提交配额优化（2026-10-30，两阶段）**：① 先把「占着业务池连接阻塞在 `pg_advisory_xact_lock`」改为 try-lock + 抢不到归还连接再退避——DB 执行 **14.22s → 0.30s**、>50ms 慢查询 **18 → 0**、池等待均值 **45.5 → 30.5ms**，但提交 p50 持平（串行化仍在）。② 再把准入从「锁 + `count(*)`」改为 `tasks.quota_slot`（`0..limit-1`）+ 部分唯一索引：随机起点环扫空槽、撞槽重试，任务进终态索引项消失即释放。20 并发 mixed：配额 1000 提交 p50 **3.2s → 1.1s**、配额 3 **4.9s → 2.1s**；真库并发**恰好放行 limit**、槽位随终态释放。证据：`docs/private/Pn-n/P4-Sn/baseline/s8-db-20261030-212105`（前）/`215824`（try-lock 后）/`225808`（槽位后）、`s8-submit-stages-*`（P1/P2/P4 分阶段）。

**不做**：Redis Streams 全量改造、**多进程** Worker 池扩容、K8s、Qdrant 集群、多机 HA、Vault、Kafka/Celery、把 agent/backtest/report 拆成独立 HTTP 服务。P4-S7 的「单进程内按 kind 多协程」见 SPEC §8；P4-S8 的监控/缓存见 §9；P4-S9 的数据层缓存见 §10。

## P4-Sn 后续愿景（不排期）

多租户 / 分布式回测 / 策略市场 / 商业化——见 [PRD §2.4](docs/PRD.md#24-p4-sn--单机工程化与愿景)。触发条件（满足任一才启动）：出现真实多用户流量 / 商业化诉求 / 团队扩张。**本轮 S1–S9 不改变这条触发条件。** 角色拆分等原「P4-S8」事项顺延为 **P4-S10 及以后**。

## 范围冻结（草案，暂缓生效）

**暂缓原因（2026-10-30）**：系统性遗留审计尚未清零，先收口问题；以下清单作为**草案**保留，待遗留问题清零后再决定是否生效。

**拟冻结依据**：P1–P3 全部交付；P4-Sn S1–S9 交付；S8 补测（资源 / 真实 Agent / RAG 质量 / DB 内部 / 数字闸门 / 报告一致性）完成；提交配额优化完成；数据基线见 `docs/private/Pn-n/P4-Sn/baseline/BASELINE-FROZEN-20261030.md`。

**允许的变更**：缺陷修复、安全加固、依赖升级、文档/注释订正、为修复引入的必要测试。

**不允许的变更**：新功能/新页面/新端点、技术栈替换、模块拆分或重写、新外部服务依赖、改变既有 API 语义或配额。

**明确不做**（沿用原清单并在冻结时确认）：Redis Streams 全量改造、多进程 worker 池扩容、K8s、Qdrant 集群、多机 HA、Vault、Kafka/Celery、独立微服务拆分、GraphRAG、A2A 协议、多租户 / 分布式回测 / 策略市场 / 商业化。

**解冻条件**（满足任一，且需单独立项评审）：真实多用户流量、商业化诉求、团队扩张、安全漏洞修复确需架构变更。解冻后新建 `BASELINE-<日期>` 做 before/after，不修改已冻结基线。
