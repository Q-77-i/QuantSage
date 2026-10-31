# Changelog

> 按里程碑记录交付内容，版本号 = git tag（`m-p1`、`m-p2`…）。格式：新增 / 调整 / 修复。
>
> **本文只回答「每个里程碑交付了什么」。** 逐次交付的决策、实测读数与验收证据见各 SPEC 的变更记录（P1 §10、P2 §13、P3 §11、P4 §10）；当前状态见 [ROADMAP](ROADMAP.md)。

## [m-p4] — 2026-10-29

> P4-Sn 单机工程化交付：S1–S6。逐次明细见 [P4-Sn SPEC §10](docs/specs/P4-Sn.md#10-变更记录)。

### 新增

- **S1 `/events` 读路径**：默认窗口 + `limit` + `truncated`，L1 结果缓存；c=50 1,098 req/s / p95 69ms（旧 63 / 1,045ms）。
- **S2 队列 v2**：`SKIP LOCKED` + 租约/心跳 + 有限次重排（`max_attempts=2`）+ `task_id` 结果幂等；kill -9 后 8.3s 重排、0 重复。
- **S3 RAG 过载治理**：独立 RAG 推理服务（单进程/BGE-M3 + reranker）、有界队列/超时/降级；API 52MB / RAG 2.3GB；交互 10c p95 1.91s、研究 5c 4.20s。
- **S4 资源上限与底座**：Docker 四件 mem/cpus、PG 1.25g/120、Redis 512MB volatile-lru、Qdrant 2.5g；DuckDB 每连接 threads=2/1GB/闸门 4；prod.sh + 15s drain + 日志轮转。
- **S5 双 API Worker（条件）**：`API_WORKERS=2` + redis-coord 共享限流 + Prometheus 多进程 + `tasks.progress.cells` 跨进程进度；bars 1,699 req/s / p95 231ms、health 11,810；限流 5/15、coord 降级 4/16。
- **S6 验证、备份与文档收口**：1w/2w 压测矩阵、8 行故障演练、PG+Qdrant+data 备份/恢复（`scripts/backup.sh` / `restore.sh` / `install_backup_job.sh`）、`drill_s6.py`；2w bars 1,710 req/s / p95 280ms、health 11,599；备份 4.6GB、恢复 36.3s。

### 调整

- **S6 口径**：回测矩阵加一条确定性失败（断言 `failed` 且 `attempts=1`）；events 6/12/24 窗口只验契约（语料约 3 个月不可区分）；备份默认 `~/QuantSageBackups`、保留 7 日 + 4 周、Qdrant 未变复用；恢复默认 scratch。
- **文档**：PRD/ROADMAP/README/architecture/PROJECT_STRUCTURE/SPEC 同步到 P4-Sn 交付状态；对外口径固定为“经过压测与故障演练验证的单机工程化方案，不是生产级高可用/SLA”。

### 修复

- **S6 磁盘只读**：`POST /api/v1/data/rollback` 把 `OSError` 映射为 503，回滚 staging 保证不写半截。
- **S6 PG 重启**：业务池与 checkpointer 池取件 `check_connection`，池内空闲旧连接被替换，重启后首个 `/auth/me` 不再 500。
- **S6 发现边界**：`/health/ready` 是启动态探针；LLM 超时因策略重试约 120s；RAG rerank c10 2.97s（8/10 降级）——均如实记录，不写成达标。

### P4-Sn 增补（S7–S9，2026-10-30）

> 同属 `m-p4` 里程碑的单机工程化增补，不另打 tag；逐次明细见 [P4-Sn SPEC §10](docs/specs/P4-Sn.md#10-变更记录)。

- **S7 worker 池**：单进程内按 kind 池化（backtest×3 + grid×1 + batch×1），20 回测 queue p95 **1,312ms**；RAG 合批门禁未过，按 A 不做。
- **S8 可观测性与缓存**：缓存命中率可算（实测 99.5%）、队列 p95 / PG 连接 / 宿主内存 / 备份年龄指标、`monitoring` profile + 告警规则 + 本地 `alert_hook`；P1 增 BackupPartial / ServiceDown，P2 增 DiskPressure（现 **9 条规则**）。
- **S9 数据层热点优化**：热点缓存与执行体进度合并，20 回测 queue p95 **503ms**、run p50 **86ms**、bars 1,660 req/s / p95 240ms；S7 的 ≤1s 复验成立。

## [m-p3] — 2026-10-20

> P3-En 交付：E1–E9（E6 已砍）+ 收尾项 5/6。逐次明细见 [P3 SPEC §11](docs/specs/P3-En.md#11-变更记录)。

### 新增

- **E1 架构分层**：`tasks` 表即队列 + 单 worker + 后台主实例锁；网格/批量迁入队列，断线不丢；LLM 调用面改为取用式接缝。
- **E2 多用户治理**：在飞任务数与每日 Agent 请求数两个配额维度、RBAC（`users.role` + `require_admin`，ETL 归 admin）、admin 分页搜索与 23 例越权矩阵。
- **E3 数据工程**：体检定时化 + X7 源侧停滞告警 + `/admin` 看板；快照清单归档 + 按 sha 重物化回滚；数据层热点缓存（L1 进程内 + L2 Redis，代际键，可降级）。
- **E4 可靠性 L0–L7**：统一 policy（分类 / 重试 / 熔断 / 限流 / 降级 / 幂等）、数据源熔断、chat 护栏与告知面、故障注入双轨（12 条矩阵 + 四幕演练）。
- **E5 风控**：四闸门（总仓位 / 单标的集中度 / 止损 / 回撤）纯函数 + `decision_audit` + 前端档位/状态/日志；止损自动批准、回撤只禁新开仓。
- **E7 工程化**：CI 三 job、覆盖率门槛 88%、Langfuse 四字段、`data_digest` 实验可复现、Prometheus `/metrics` + request id + JSON 日志。
- **E8 安全 + 对外 MCP**：CSP nonce + 安全响应头 + 会话续期/撤销 + 依赖审计；对外 stdio MCP Server（PIT 检索）；注入边界标记；OS 级策略沙箱。
- **E9 企业级文档**：`docs/enterprise/` 架构设计 / 风控专题 / 代码评审记录 / 事故复盘 + 索引；定向两轴评审 26 条 findings（12 修 / 14 记录）；MQ / 容器化 / 多 worker 边界落文档。
- **收尾项 5**：Agent 下单提案（HITL）、RAG 一轮纠错 + A2A 静态能力卡、append-only 策略版本 + diff/回滚、12 题深研报 rubric。
- **收尾项 6**：CSP 生产验证 33/33、容器化 spike、running 取消改 DB 标志、不引 MQ。

### 调整

- **单实例契约**：task worker / ETL / 结算调度器收口到后台主实例；非持锁进程只服务 HTTP（`POST /etl/run`、`POST /data/rollback` 返回 409）。
- **会话策略**：滑动续期 + `token_version` 撤销；remember 状态进 token，续期不再升级为持久 cookie。
- **模拟盘来源**：决策来源由调用方给定，用户策略不能伪造 `agent` / `risk`。

### 修复

- **E9 定向评审必须修复**：admin 登录漏 `role`（`find_user_by_email` 投影）；停牌顺延买单未计入总仓位 `pending_notional`。
- 高置信建议修复：空 `JWT_SECRET` 走 500、MCP 12KB 上限被超长 query 突破、沙箱墙钟未覆盖 `proc.wait()`、精排运行期异常不降级。
- **供应链审计**：`aiohttp` 3.14.3 → 3.14.5（新披露 3 个漏洞；本地 `pip-audit --strict` 0 漏洞）。

### 验收

- 离线 **1797 passed / 136 deselected**、`ruff` 全绿；集成 auth 2 / paper E5+M6 6 / RAG PIT 1；`check_docs.py` 10 份 0（链接 / 环境变量 / 脚本引用）。
- P3 DoD：故障注入双轨通过；Claude Desktop / Claude Code 可调我们的 MCP 完成一次 PIT 检索。

## [m-p2] — 2026-10-13

> P2-Mn 交付：把「跑通 demo」做到「独立完成写策略 → 回测 → 模拟 → 复盘」。
> M1–M10 全部落地并验收，P1 遗留的「会话无归属校验」阻塞项清零。逐次明细见 [P2 SPEC §13](docs/specs/P2-Mn.md#13-变更记录)。

### 新增

- **M1 用户系统**：注册 / 登录 / 退出 / 当前用户四端点（bcrypt + HS256 JWT，httpOnly + SameSite=Lax cookie）；自建业务表与幂等建表；会话归属改读自有表，越权与不存在同返 404。前端受保护路由组、登录/注册页、`AuthProvider`、请求与 SSE 带 cookie。
- **M1c 自选股与个人空间**：自选股增删改与分组（重命名撞名即合并、删组回落默认分组）；「加自选以来涨幅」取加入时最近可得收盘价，取不到即留空；`/space` 四页签与深链恢复；每次回测落库并可重开。
- **M2 数据层**：行情扩到全市场 A 股（`cn-daily` 21 片 ≈ 2397 万行，DuckDB 直读 Parquet 零 ETL）；交易日历冻结为仓库内文件；事件语料改全市场按日落盘 + 日增量 ETL；数据体检 20 项；行情按需刷新（便宜探测 → 按需整片重下，两路 job 共用一把锁）。
- **M3 RAG 完整化**：BGE-M3 dense + sparse 双索引；管线为 `available_at <= as_of` 服务端硬过滤 → 双路召回 → 服务端 RRF → bge-reranker-v2-m3 精排；自建 200 条中文财经评测集，三档消融 NDCG@10 **dense 0.7764 / hybrid 0.7602 / rerank 0.8142**。
- **M4 策略工作台**：沙箱（`on_bar(ctx[, p])` 契约、受限命名空间、子进程三层配额、11 条故障矩阵）；只读 AST 静态检查 R0–R5；工作台 7 端点 + Monaco 本地资源；error 命中即拒绝执行。
- **M5 回测增强**：A 股规则（T+1、涨跌停、停牌、整手、印花税）；批量与网格（逐格 SSE、热力图、Deflated Sharpe）；因子分析（RankIC / 分层 / 多空价差，毛净双轨）。
- **M6 模拟盘**：回放式纸上交易，六态决策闸门，未审批不成交，按次日开盘价成交；单标的 + 全部批准时与回测 `fills` **逐笔相等**；多标的等额配额。
- **M7 绩效研报**：报告容器（一次生成即冻结、同快照幂等复用）+ 归因 + 逐笔复盘 + 证据追溯 + 决策记忆；分享页与登录页共用同一视图；服务端 PDF 导出。
- **M8 深度研报多 Agent**：路由 → 五角色并行取证（取数是代码、解读是 LLM）→ 归并 → Bull⇄Bear 辩论（≤3 轮）→ 单点综合 → 代码化风控闸门 → `interrupt()` HITL；过程可视化与 Langfuse 同源。
- **M9 轻量教学**：使用教程（默认读者只有一个浏览器）+ FAQ（按症状查，提示语为界面原文）+ README 同步；新增 `check_docs.py` 三件机检。
- **M10 研究首页**：大盘总览、事件日历/快讯流、全市场标的浏览、事件详情抽屉；DuckDB 多 `unnest()` 按位置 zip、ST 幅度 10%、treemap 被数据否掉等实施期事实已回写 SPEC。

### 调整

- **认证与前端**：登录/注册页改整屏连续画布；标题字体改无衬线；「记住我」默认勾选（只改 cookie 存活方式）；对话页空态三示例问题。
- **回测契约**：`POST /api/v1/backtest` 纳入鉴权并改信封 `{run_id, report}`；`strategy="user"` 增可选 `strategy_id`；鉴权先于参数校验。
- **数据语义**：政策立场纳入方向映射；事件语料通道改归档（MCP 退回只服务对话实时查询）；事件驱动窗口以本地语料覆盖区间为准；`dc.events()` 标的过滤改数组包含；查询层对事件分片严格 glob。
- **RAG / 性能口径**：检索 p95 拆为「召回+融合 ≤500ms」与「含精排中位 ≤3s、p95 ≤5s」；深路径端到端定为冷启动 ≤60s、预热 ≤30s；并发 2 的价值改为单格隔离，不承诺提速。
- **依赖与文档链**：新增 `bcrypt`、`pyjwt` 显式 pin；PRD / SPEC / CLAUDE.md / README 随各段同步。

### 修复

- **登录守卫会吃掉深链**：重定向只带 `pathname`，改为带上查询串。
- **`localhost` 打开时登录看似失败**：API 基址写死 `127.0.0.1` 导致跨站丢 cookie，改为跟随页面 host。
- **DuckDB 多个 `unnest()` 是按位置 zip，不是笛卡尔积**：行业成交额之和从「274%（假）」订正为 **469%（真）**。
- **涨跌停单侧比较在除权日误判**：判据改对称；主板 ST 幅度实测 10% 不是 5%。
- **前端图表**：`useEChart` 宿主改回调 ref，修复切回时空画布；`/optimize` 两张图 diverging 色阶方向统一；切策略后不再发送不可见参数。
- **同一当日截面两个定义**：板块条 786 只与钻取列表 787 只对齐成一处并留测试。

## [m-p1] — 2026-10-06

> P1-Tn 跑通 demo：双闭环（数据 → 策略 → 回测 → 结果 / 对话 → 工具 → 回答）均可在浏览器内走完，
> 前视偏差两口径差异可量化，示例策略教程可复现。逐次明细见 [P1 SPEC §10](docs/specs/P1-Tn.md#10-变更记录)。

### 新增

- **T1 地基**：uv + Python 3.12 monorepo；Docker Compose（PostgreSQL 18 / Redis / Qdrant；Langfuse 挂 `observability` profile）；小石 CLI 独立 venv + stdio MCP；Postgres checkpointer。
- **T2 样例数据**：3 标的 × raw/qfq 落 Parquet；事件语料经 MCP 拉取并保留来源三元组；DuckDB 只读视图；下载清单与校验证据落 `data/_meta/`。
- **T3 Agent 对话**：LangGraph 单 Agent（`deepseek-flash` 经 LiteLLM）、行情工具 + 小石 MCP 白名单、SSE 五类帧、会话三端点、Langfuse 全链路 trace。
- **T4 最小回测**：事件循环驱动，信号当根收盘生成、次根开盘成交；A 股撮合与费用滑点独立开关；`ma_cross` / `event_driven` 两策略。
- **T5 分析输出**：六项指标 + 份额化买入持有基准；`pit_comparison` 量化两口径差异（方向不预设）；样本量提示进报告与 UI。
- **T6 极简前端**：Next.js 15 + TS + Tailwind + shadcn/ui（端口 3001）；对话页与回测页；前端纯函数单测。
- **T7 示例策略教程**：`ma_cross.md` / `event_driven.md`，含逻辑、参数、复现命令与期望数字。

### 调整

- PIT 口径定为**收盘时刻级**（每根 bar 的 15:00，Asia/Shanghai），闸门只作用于事件语料；日线 OHLC 按「当日收盘已知」处理。
- 前视偏差表述统一中性化：写「两口径差异」，不写「虚高」——T5 实测非 PIT 期末权益均未高于 PIT，方向不预设。
