# QuantSage SPEC · P2-Mn（MVP）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.6 待评审）→ 本文（技术规格）→ 代码
>
> 版本 v1.1 ｜ 2026-10-07 ｜ 状态：M1 章节已过审并落地（M1a / M1b 已交付，M1c 待做）；M2–M10 随各功能开工滚动过审
>
> 本 SPEC 覆盖 PRD §2.2 的 M1–M10。正文章节按功能 ID 排序（§2–§11 对应 M1–M10）。
> 功能范围依据竞品调研（docs/private/Pn-n/P2-Mn/P2-Mn-竞品调研.md，2026-10-06）：相对 P1 收尾时点新增 15 项功能并新开 M10，均已获确认。

## 目录

| 节 | 内容 |
|---|---|
| [§1](#1-仓库结构p2-增量) | 仓库结构（P2 增量） |
| [§2](#2-m1-用户系统) · [§3](#3-m2-数据层) · [§4](#4-m3-rag-完整化) · [§5](#5-m4-策略工作台) · [§6](#6-m5-回测增强) · [§7](#7-m6-模拟盘) · [§8](#8-m7-绩效分析) · [§9](#9-m8-深度研报多-agent) · [§10](#10-m9-轻量教学) · [§11](#11-m10-研究首页) | M1–M10 规格 |
| [§12](#12-测试策略) | 测试策略 |
| [§13](#13-变更记录) | 变更记录 |

## 1. 仓库结构（P2 增量）

> 在 P1 结构上新增，其余沿用。

```
backend/app/
├── api/                    # + auth.py / watchlist.py / paper.py / dashboard.py / calendar.py
├── agent/                  # 深路径 fan-out 角色 A 股化（PIT 基本面 / 政策面）
├── backtest/               # + a_share_rules.py / overfit.py / factor_analysis.py
├── data/                   # + etl.py（日增量）/ trading_calendar.py / data_health.py
├── paper/                  # 模拟盘：account.py / broker.py / settlement.py / decisions.py
├── memory/                 # 决策记忆：decision_store.py / settle.py / reflection.py
├── strategy/               # 策略沙箱 + static_check.py（AST 前视检查，纯函数）
├── core/                   # + auth.py（哈希 / JWT / 依赖）/ db.py（自有连接池与幂等建表）
│                           #   scheduler.py（ETL 与到期结算定时）
scripts/                    # + run_etl.py / run_health_check.py / run_settlement.py / run_backfill.py
frontend/app/               # + (app)/（受保护路由组：守卫 + 页头）login/ register/ watchlist/ paper/
│                           #   dashboard/（研究首页）research/[id]/（研报页）
frontend/components/        # + auth-provider.tsx dashboard/ evidence/ agent-run/ calendar/
```

## 2. M1 用户系统

> 本功能同时清理 P1 遗留阻塞项（会话无归属校验，P1 SPEC §4 记录），是 P2 的第一道门。
> 切片：**M1a** 后端地基与归属校验 → **M1b** 前端认证闭环 → **M1c** 自选股 · 个人空间 · 我的回测。

### M1a 后端地基与归属校验

**数据模型**：四表落同一业务库，幂等 DDL（`CREATE TABLE IF NOT EXISTS`）在 lifespan 内执行，用独立于 checkpointer 的连接池；`app.state.db` 可注入（离线测试注入内存实现），连不上库时受保护端点 503、公开端点照常——沿用 P1「能降级就降级」姿态。

| 表 | 列（要点） |
|---|---|
| `users` | `id BIGSERIAL PK` / `email TEXT UNIQUE NOT NULL`（统一存小写）/ `password_hash` / `created_at` |
| `chat_threads` | `thread_id UUID PK` / `user_id FK→users ON DELETE CASCADE` / `created_at` / `last_active_at`；索引 `(user_id, last_active_at DESC)` |
| `watchlist` | `id` / `user_id FK` / `symbol` / `group_name TEXT NOT NULL DEFAULT '默认分组'` / `added_at` / `added_price NUMERIC(18,4)`；`UNIQUE(user_id, symbol)` |
| `backtest_runs` | `id UUID PK` / `user_id FK` / `created_at` / `request JSONB` / `report JSONB`（M1c 用） |

- **归属真源是自建的 `chat_threads`，不是 checkpointer 的 `checkpoints` 表**：后者由 `langgraph-checkpoint-postgres` 自己的迁移管理（`saver.setup()`），不得加列改结构
- 会话列表口径随之改为 `chat_threads.last_active_at DESC`（每轮对话更新），**不再直读 `checkpoints` 内部表**——解除对第三方库内部 schema 的依赖，并使列表逻辑离线可测；标题仍按 checkpointer 官方读接口取
- 项目此前无自有建表机制（不引 Alembic）：本功能立第一套
- 策略表随 M4 建，届时沿用同一 `user_id` 归属约定

**认证**

| 端点 | 行为 |
|---|---|
| `POST /api/v1/auth/register` | `{email, password}` → 201；邮箱唯一冲突 → 409；成功即签发会话（与登录共用签发函数，少一次往返） |
| `POST /api/v1/auth/login` | 成功 → 200 + `Set-Cookie`；失败（邮箱不存在或密码错）→ 401，**同一响应、近似耗时**，不区分原因 |
| `POST /api/v1/auth/logout` | 清除 cookie |
| `GET /api/v1/auth/me` | 已登录 → `{id, email}`；未登录 → 401 |

- 密码：`bcrypt` 直用（不引 passlib——对 bcrypt 4.x 有告警且维护停滞）；哈希只入库、不回显、不进日志
- **密码长度 8 ≤ len ≤ 72 字节在 schema 层显式限制**：bcrypt 只取前 72 字节，超长必须拒绝而非静默截断
- 会话：HS256 单 access token，7 天，claims = `sub`(user id) + `iat` + `exp`；不做 refresh token
- Cookie：`httpOnly` + `SameSite=Lax` + `path=/`（`secure=False` 仅因本地 http，生产必须置 True）；Lax 同时挡掉跨站表单类 CSRF
- `JWT_SECRET` 只读环境变量（`SecretStr`，不回显、不入库）；缺失时启动打 warning 且 `/api/v1/auth/*` 返回 503——**不得静默降级为无鉴权**
- 前端 `127.0.0.1:3001` ↔ API `127.0.0.1:8000`：同 site 跨 origin，SameSite 按 site（忽略端口）计算 → Lax 可行；**两端 host 必须一致（同用 `127.0.0.1` 或同用 `localhost`），混用会静默掉 cookie**

**归属校验（P1 遗留阻塞项，本功能第一优先级）**

- 唯一规则：凡读写用户数据一律带 `user_id`；命中 0 行 → **404（与「不存在」同响应，不区分「无权限」，避免泄露存在性）**；未登录 → **401**
- 覆盖端点：`POST /api/v1/chat`（续聊既有会话）、`GET /api/v1/chat/threads`、`GET|DELETE /api/v1/chat/threads/{id}`；会话 / 回测 / 自选股的一切查询端点同规
- 新建会话在**流开始前**落归属行（失败即 500，不产生无名会话）；客户端传入的 `thread_id` 必须已属于本人，否则 404。P1 无主会话一律孤儿化：不迁移、不认领、不出现在任何列表
- 删除语义：先 `adelete_thread`（对不存在的 thread 静默成功，非新行为），再删归属行；失败残留只可能是「空会话」行，再删一次即可
- 公开 / 受保护边界：公开 = `/health*`、`/api/v1/market/*`、`/api/v1/events`（非用户资产，也是 M7「研报未登录只读」的前置口径）；受保护 = `/api/v1/chat/*`、`/api/v1/watchlist/*`、`/api/v1/auth/me`；`POST /api/v1/backtest` 在 M1a/M1b 仍公开（无状态计算），M1c 随落库一并纳入

### M1b 前端认证闭环

- 受保护页迁入路由组 `app/(app)/`（对话页 / 回测页），`AuthGate` 与 `AppHeader` 上提到组布局——**守卫只有一个落点**
- `AuthProvider`：挂载时 `GET /api/v1/auth/me` 探测登录态（httpOnly cookie 在 JS 侧读不到，这是唯一可行路径）；未定态渲染骨架，避免先闪内容再跳登录；未登录重定向登录页
- `app/login`、`app/register`：表单 + `next` 回跳；注册成功即登录态
- `lib/api.ts` 与 `lib/sse.ts` 的**全部**请求带 `credentials: "include"`；后端 CORS 开 `allow_credentials=True`（`allow_origins` 已是显式列表，符合凭据模式要求）
- 页头显示用户邮箱与「退出」

### M1c 自选股 · 个人空间 · 我的回测

- 自选股：`GET|POST /api/v1/watchlist`、`PATCH|DELETE /api/v1/watchlist/{symbol}`；分组按 SPEC 原文用 `group_name` 字符串列（不建分组实体表），重命名 / 删除各一条 UPDATE，删除分组 = 组内标的回落默认分组
- `added_price` = 加入时**最近可得交易日收盘价**（qfq，走 DuckDB 行情层）；样例数据只覆盖少数标的，取不到即存 NULL，UI 显示「—」，**不得编数**；涨幅 = (最新可得收盘 − `added_price`) / `added_price`
- 个人空间页：会话历史 / 我的策略（M4 前占位）/ 我的回测 / 我的自选
- 回测最小持久化：`POST /api/v1/backtest` 响应改信封 **`{run_id, report}`**（报告结构本身不动，见 P1 SPEC §6；契约改动记录于本阶段）；`request` 存**解析后的 config**（区间已填充，非原始请求）；`GET /api/v1/backtest/runs` 摘要列表 + `GET /api/v1/backtest/runs/{id}` 完整报告（越权 404）

**验收**：注册 → 登录 → 用户 A/B 数据互不可见（含会话列表归属过滤，回归 P1 会话端点）；刷新后会话可恢复；自选股增删分组后涨幅显示正确；越权一律 404、未登录一律 401。
**本轮不做（记录待议）**：密码重置、邮箱验证、refresh token、记住我、第三方登录、资料页（改密 / 改邮箱）、全局 401 拦截（守卫层已覆盖）、旧会话认领脚本、多设备会话管理（P3-E2）。

## 3. M2 数据层

- 行情：扩到数据源覆盖的全市场 `cn-daily` 年度分片；多周期以数据源实际覆盖为准；**退市股覆盖确认**（对照全市场清单核验已退市标的是否在分片中，核实生存者偏差）
- **交易日历**：开源 A 股日历库落 Postgres + DuckDB 各一份；ETL 定时任务与回测撮合引用同一日历；抽查 1 年与实际交易日一致（国庆/春节等长假边界）
- 日增量 ETL：进程内 APScheduler 定时 + 手动触发端点；拉取后按 `event_id` + `content_hash` 去重；幂等（同日重跑不产生重复行）；92 天全量回填一次性执行；长历史仅靠日增量积累（归档同受约 3 个月窗口限制，已证伪）
- **数据质量体检（脚本级）**：缺失值比例、异常步长（单日涨跌超阈值）、`available_at` 空值 / 倒挂（< `event_time`）、重复行检测；输出报告 + 非零退出码（可挂定时告警）。完整版（快照/看板/告警推送）留 P3-E3，本处不建看板
- 待验证（M2 内验证并回填本 SPEC）：小石是否覆盖指数行情（M5 基准、M10 大盘依赖）；分片是否含行业字段（M10 热力图依赖）

**验收**：ETL 连续运行 3 天无重复、无漏拉；全市场日线 DuckDB 秒级查询；注入脏数据时体检脚本报警；交易日历抽查一致。

## 4. M3 RAG 完整化

- 语料 ETL → BGE-M3 嵌入（批处理 + 断点续跑；Mac 无 GPU，全量嵌入小时级一次性成本）→ Qdrant 双索引（dense + sparse）
- 检索管线（规划报告 §五 已定稿）：元数据硬过滤（`available_at <= as_of` / 行业 / 标的 / 重要度）→ 稠密∥稀疏并行 top-100 → 服务端 RRF（k=60）→ bge-reranker-v2-m3 精排 → top-3~5（small-to-big 取回父文档）
- 分块：文档级 + 元数据 header 拼接；仅长文本（公告/研报）走递归分块 + 父子文档
- 自建 200 条中文财经评测集，覆盖事实型 / 事件型 / 数值型 / 时间型 / 多跳型五类
- PIT 单元测试：任意 `as_of` 下检索结果严格不含 `available_at > as_of` 的事件

**验收**：评测集 NDCG@10 达基线；PIT 单元测试通过。

## 5. M4 策略工作台

- Monaco 在线编辑器 + Python 策略沙箱（子进程 + 内存/CPU/超时配额，坏代码不拖垮服务）
- 参数配置；模板库（3–5 个内置策略）
- **策略代码前视泄漏静态检查（AST，纯函数）**：回测提交前对源码做 AST 分析，规则库初版：
  - `shift` 负窗口（取未来值）；`bfill` / `ffill`；显式未来索引
  - 检查结果 `{rule, line, snippet, severity}`；命中即拒绝执行并在编辑器标注行号
  - 规则库是纯函数 + 数据表，新增规则不改执行器（防把检查器写成一次性代码）
- 检查器在服务端对源码运行，与沙箱执行隔离

**验收**：在线写策略可成功回测；坏代码不会拖垮服务；注入含 `shift(-1)` / `bfill` 的策略被拦截并提示行号。

## 6. M5 回测增强

- 批量回测：多策略 × 多标的，后端并发上限 2 的线程池 + 前端进度展示（P3-E1 再升级异步任务队列，本处不建队列）
- 网格参数优化：笛卡尔网格 + 边界校验；结果矩阵热力图 + 最优参数标注
- 基准对比：沪深 300 / 中证 500——指数行情覆盖在 M2 验证；不覆盖则用全市场等权组合代理，口径在报告与 UI 如实标注
- **A 股交易规则内建**（`a_share_rules.py`，纯函数 + 拒绝码）：
  - T+1：当日买入不可当日卖出；涨跌停：涨停价买单（一字板）/ 跌停价卖单不可成交；ST 与板块涨跌停幅差异（ST 5% / 主板 10% / 创业板·科创板 20%）；最小手数 100 股；停牌不可成交（`is_suspended`）；印花税卖出 0.05%（P1 已有）
  - 成交口径与 P1 一致：信号 bar 收盘生成、next bar 开盘成交
  - 每笔拒单带原因码（`REJECT_T1` / `REJECT_LIMIT_UP` / `REJECT_SUSPENDED`…），UI 可见拒单原因
- **过拟合检验最小集**（`overfit.py`）：全网格夏普分布 → Deflated Sharpe（期望最大值校正）→ 最优参数 vs 全网格分布图；轻量自研，不引第三方回测检验库
- **因子分析最小套件**（`factor_analysis.py`）：新闻信号因子（`direction_norm` / `factor_scores.score`）的 RankIC / ICIR、分层收益（按信号分位数分组）、多空价差曲线；复用回测引擎跑分层组合；输出 tear sheet（指标卡 + 图）

**验收**：参数网格任务跑通；策略曲线与基准叠加对比；一字涨停日买单被拒且拒单原因可见；Deflated Sharpe 与因子 IC 报告生成。

## 7. M6 模拟盘

- 模拟账户：初始资金、持仓、订单、每日结算；按日线信号推进，撮合规则与 M5 同口径（A 股规则 + 费用/滑点）
- **费用/滑点/结算模型**：复用 T4 `CostModel`（佣金双边万 2.5 最低 5 元 + 印花税卖出 0.05% + 滑点 bps）；每日按收盘价结算净值 / 市值 / 现金
- 决策审批闸门：信号 → 决策单（标的/方向/数量/理由/来源三元组）→ 待审批状态 → 用户确认或驳回 → 确认后按下一交易日开盘价成交；未审批不成交
- 决策日志落 Postgres（含来源标注），供 M7 到期结算反思消费

**验收**：策略信号 → 模拟成交 → 持仓变化全链路可查；费用扣减与手工样例一致；未审批决策不成交。

## 8. M7 绩效分析

- 研报页：可分享独立 URL（未登录只读）；简单归因（行业/因子）；风险指标在 P1 五指标基础上补波动率与超额收益；页脚免责声明（PRD 已定）
- **决策记忆 + 到期结算反思**：Store 落决策记录（标的/方向/理由/来源三元组/决策时点/有效期）；到期任务按真实收盘价结算 pnl 与相对基准 alpha；反思摘要（flash 一句话教训）写回 Store；跨标的教训聚合查询
- **证据追溯面板**：研报页每个结论块可展开 → 证据列表（标题/摘要/`event_time`/`available_at` 并列/`source`/`original_source`/`content_hash`）；claim 分级（事实型 = 直接引用事件，推断型 = 模型综合）
- **研报导出 + 版本快照**：Markdown 导出（必做）；PDF 经 Playwright 渲染（复用 P1 capture 脚本链路）；研报生成时记录 `report_hash` + 数据快照版本 + 模型版本，同一快照重放结论一致（hash 相同）

**验收**：分享链接在未登录浏览器可打开完整研报；决策到期自动结算并生成反思摘要；导出含证据链；同一数据快照下重放结论一致。

## 9. M8 深度研报多 Agent

- 意图路由：快路径（RAG 直通）⇄ 深路径（多 Agent）
- 深路径拓扑（规划报告 §四 已定稿）：并行取证 fan-out（节点内 try/except）→ 证据归并 → Bull⇄Bear 辩论（≤3 轮硬上限）→ 单点综合撰写 → 代码化风控闸门 → 超限时 `interrupt()` 人工审批
- **取证角色 A 股化**：行情技术面 / 新闻事件（RAG，PIT）/ **PIT 基本面事件**（announcement 语料按事件类型过滤：财报/业绩预告/分红/减持解禁，均自带 `available_at`；不引第二数据源）/ 因子量化 / 政策面（政策类事件语料）
- **Agent 执行过程可视化**：SSE 事件扩展 `node_start` / `node_end`（阶段环、并行瀑布、节点耗时、token/成本，取 LangGraph 回调）；前端 Agent 运行面板，数据与 Langfuse trace 同源一致
- 双档模型：取证/路由 `deepseek-flash`，辩论/综合 `deepseek-v4-pro`
- 决策数据带来源标注，做进 UI（PRD §5 硬性要求）

**验收**：完整研报生成；辩论严格 ≤3 轮；token 成本在预算内；拔掉任一数据源，系统降级而非崩溃；基本面结论均源自带 `available_at` 的公告事件；过程面板与 Langfuse trace 一致。

## 10. M9 轻量教学

- 使用教程 + 示例策略文档 + 常见问题；覆盖 P2 新增流程（自选股/模拟盘/研报导出）

**验收**：文档覆盖全部核心流程，新用户可独立走通。

## 11. M10 研究首页

- 登录后落地页。大盘总览：指数行情（覆盖验证见 §3）、涨跌家数、涨跌停家数、成交额（全市场日线 DuckDB 一次聚合）；板块热力图优先用数据源行业字段，缺则用当日事件 `industries` 聚合（口径在 UI 标注）
- 事件日历/快讯流：按日聚合事件，重要度排序；每条并列 `event_time` 与 `available_at`（PIT 语义展示位）；来源三元组可展开；日期筛选
- 导航入口：个人空间 / 研报 / 回测 / 模拟盘

**验收**：大盘总览与日线数据抽查一致；事件日历点击任意事件可见来源三元组与双时间戳。

## 12. 测试策略

- 单元：AST 检查器规则样例矩阵（M4）；A 股规则各拒绝码（M5）；Deflated Sharpe / 因子 IC 与分层（M5）；交易日历与体检脚本（M2）；到期结算与反思（结算用固定价格快照，M7）；证据链组装（M7）；鉴权纯函数（密码哈希 / JWT / cookie 属性）与归属过滤（M1：离线跑「未登录 401 门 + 内存 DB 的越权矩阵」，注入 `app.state.db` 与 `InMemorySaver`）
- 集成：auth 注册登录流与 A/B 隔离矩阵（M1，真实 Postgres；用唯一邮箱前缀 + teardown 清理，不污染 dev 库）；ETL 幂等重跑（M2）；模拟盘全链路（M6）；研报导出与重放一致（M7）；深路径降级（M8）
- 归属校验是**双跑**的：越权矩阵在离线（内存实现，快反馈）与集成（真实 SQL 过滤，验真）各跑一遍——过滤逻辑写在 SQL 里，离线实现无法证明真库行为
- 前端：Vitest 只测纯函数（日历日期映射、瀑布图数据映射、证据面板分组），不引组件测试框架（沿用 P1 口径）
- 离线用例继续走真实 Parquet，不 mock 查询层（沿用 P1 口径）

## 13. 变更记录

| 版本 | 日期 | 关联 | 变更 |
|---|---|---|---|
| v1.0 | 2026-10-06 | M1–M10 | 初版：P2-Mn 技术规格（含竞品调研后扩容的 15 项新功能与 M10 研究首页） |
| v1.1 | 2026-10-06 | M1 | §2 细化为可执行规格并切 M1a / M1b / M1c：四张自有表与幂等建表机制（不引 Alembic）；**会话归属真源改用自有表，不再直读 checkpointer 内部表**；auth 端点、cookie 属性、密码 72 字节上限、`JWT_SECRET` 缺失的降级姿态；越权 404 规则与公开/受保护边界；`POST /backtest` 响应改信封 `{run_id, report}`（M1c）；§1 补 `core/db.py` 与前端路由组 `(app)/`；§12 补 M1 测试分工与双跑口径 |
