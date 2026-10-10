# QuantSage SPEC · P2-Mn（MVP）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.6 待评审）→ 本文（技术规格）→ 代码
>
> 版本 v1.35 ｜ 2026-10-10 ｜ 状态：M1 已落地（M1a / M1b / M1c；**M1c 补「加自选表单的即时反馈」——判重 + 代码体检**）；M2a / M2b 已落地；**M2c 已收口——M2 整段完成（三天台账核验通过，2026-10-09）**；**M3 已落地并验收通过（M3a / M3b / M3c）**；**M4a 已落地（沙箱与用户策略 API）、M4b 已落地（静态检查器与模板库）**；**M4c 已落地并验收（M4c-1 持久化与端点 / M4c-2 前端工作台）——M4 整段完成**；**M5a 规则与地基已落地并验收（A 股规则接引擎 / 横截面 / 名称字典 / 等权基准，界面验证 13/13）**；**M5b 批量 / 网格 / 过拟合检验已落地并验收（后端 5×5 网格 + DSR 交叉验证 / 前端 `/optimize`，界面验证 13/13）**；**M5c 已细化并拍板（开工前，D1–D6 见 §6 M5c v1.25）**；**M5c-1 后端已落地（`app/factor/` + `GET /api/v1/factor/report`，同尺子 30.7 万行零不一致、离线 958 全绿）**；**M5c-2 前端已落地（`/factor` 页，界面验证 12/12）——M5 整段完成**；**M6 已细化并拍板（开工前，D1–D8 见 §7）**；**M6a 后端已落地（`app/paper/` + 7 端点 + 四张表，全批 == 回测逐笔相等）**；**M6b 前端已落地（`/paper` 页，界面验证 18/18）——M6 整段完成**；**M7 已细化并拍板（D1–D4 与 F1–F11 见 §8）；M7a 后端已落地（报告容器 + 八端点 + 快照指纹）**；**M7b 后端已落地（决策记忆 + 到期结算反思 + 三端点 + 独立定时，离线 1113 / 集成 97 全绿，真 Store 跨进程幂等已验）**；**M7c 前端已落地（研报页 + 公开只读 + 证据追溯面板，界面验证 17/17）——M7 整段完成**
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
├── api/                    # + auth.py / watchlist.py / etl.py / paper.py / dashboard.py / calendar.py
│                           #   optimize.py（批量与网格，SSE 进度，M5b）/ factor.py（因子报告，M5c）
│                           #   reports.py（研报容器 / 分享 / 公开只读 / 复盘与教训，M7）
├── agent/                  # 深路径 fan-out 角色 A 股化（PIT 基本面 / 政策面）
├── backtest/               # + a_share_rules.py / benchmark.py（全市场等权代理，M5a）
│                           #   batch.py（多策略×多标的，并发 2，M5b）/ overfit.py（Deflated Sharpe，M5b）
├── factor/                 # 因子分析（M5c，独立数据管道，不属回测引擎）：
│                           #   panel.py（PIT 因子面板：事件面板 / 价格面板 / 前向收益）
│                           #   analysis.py（RankIC·ICIR·t / 分层 / 多空 / 换手与费用 / tear sheet）
├── data/                   # + calendar.py（冻结交易日历）/ trading_calendar.json（冻结文件）
│                           #   data_health.py（体检检查器，M2c）/ delisting.py（退市覆盖核实，M2c）
│                           #   naming.py（名称字典：构建 / 归一化 / as-of 查询，M5a）
├── etl/                    # 事件语料接入（M2b）：archive.py（归档通道）/ store.py（日分区落盘）
│                           #   runner.py（回填与日增量编排）/ scheduler.py（APScheduler 定时）
├── rag/                    # RAG（M3）：encoder.py（编码器协议 + 懒加载）/ collection.py（Qdrant 索引）
│                           #   embed.py（逐日分区批处理 + 断点续跑）/ retrieve.py（过滤 → 双路 → RRF → rerank）
├── paper/                  # 模拟盘（M6，回放式纸上交易；见 §7）：types.py（决策/状态/配置）
│                           #   account.py（账户账本：共享现金 + 每标的配额 + 持仓 + 估值）
│                           #   market.py（批量取数 + 窗口涨跌停价，不逐标的取）
│                           #   replay.py（★ 纯函数重放：闸门语义 / 顺延 / 结算全在这里）
│                           #   store.py（决策与账户状态 ↔ DB 行 / JSON 信封的形状转换）
├── memory/                 # 决策记忆（M7）：settle.py（回合配对 + 未平仓估值 + **到期结算与窗口 alpha**，纯函数）
│                           #   reflection.py（flash 一句话教训，≤120 字，超时降级）/ decision_store.py（Store 封装：
│                           #   namespace / put / search + **按需持有者 MemoryHolder**）/ service.py（结算编排：
│                           #   取数 → 结算 → 反思 → 落 Store → 复盘载荷）/ scheduler.py（独立定时，MEMORY_SETTLE_ENABLED）
├── report/                 # 绩效研报容器（M7a，独立于回测引擎与模拟盘账本）：
│                           #   performance.py（账户绩效指标，复用 backtest.metrics）/ attribution.py（标的级与事件级归因）
│                           #   evidence.py（来源快照 → 语料行 join，含修订标注）/ snapshot.py（数据指纹与 report_hash）
│                           #   narrative.py（flash 综述，超时与降级）/ builder.py（冻结产物组装 + claim 校验闸门）
│                           #   markdown.py（Markdown 导出，含证据链）
├── strategy/               # 用户策略与沙箱（M4）：sandbox.py（子进程执行器：配额/超时/输出上限）
│                           #   worker.py（沙箱子进程入口）/ static_check.py（AST 前视检查，纯函数 + 规则表）
│                           #   params.py（PARAMS schema 校验，纯函数）/ templates/（模板源码，服务端为唯一真源）
├── core/                   # + auth.py（哈希 / JWT / 依赖）/ db.py（自有连接池与幂等建表）
│                           #   scheduler.py（ETL 与到期结算定时）
scripts/                    # + generate_calendar.py（生成冻结日历）/ audit_calendar.py（日历×行情对账）
│                           #   download_events.py（改薄壳：回填 / 日增量 / 状态）
│                           #   run_health_check.py / embed_events.py（嵌入薄壳）
│                           #   spike_rag_encoder.py（M3 运行时实测，结论见 §4）/ build_rag_eval.py（评测集：生成/池化/标注/报告）
frontend/app/               # + (app)/（受保护路由组：守卫 + 页头）login/ register/ space/（个人空间）paper/（模拟盘，M6）
│                           #   strategies/（策略工作台，M4）dashboard/（研究首页）research/[id]/（研报页，M7）
│                           #   optimize/（网格与批量，M5b）factor/（因子报告，M5c）
│                           #   r/[token]/（公开只读分享页，M7——**不在守卫组内**）
│                           #   自选股不单开路由，是 space/ 的一个页签（M1c 定）；模拟盘另加 space/ 页签（M6）
frontend/components/        # + auth-provider.tsx space/ strategies/（编辑器/参数表单/检查面板）dashboard/ evidence/ agent-run/ calendar/
│                           #   paper/（会话创建 / 账户卡 / 决策卡 / 持仓表 / 决策流水 / 结算表，M6）
│                           #   research/（report-view 共享渲染器 / 指标卡 / 归因表 / 复盘卡 / 证据面板 / 所有者动作，M7c）
```

## 2. M1 用户系统

> 本功能同时清理 P1 遗留阻塞项（会话无归属校验，P1 SPEC §4 记录），是 P2 的第一道门。
> 切片：**M1a** 后端地基与归属校验 → **M1b** 前端认证闭环 → **M1c** 自选股 · 个人空间 · 我的回测（M1a / M1b 已交付；M1c 按 v1.2 规格实施中）。

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
| `POST /api/v1/auth/login` | 成功 → 200 + `Set-Cookie`；失败（邮箱不存在或密码错）→ 401，**同一响应、近似耗时**，不区分原因；请求可带 `remember`（默认 true）：true → 持久 cookie（带 `Max-Age`），false → 会话 cookie（关浏览器即失效）。这只改 cookie 存活方式，不改 JWT 有效期，也不建服务端会话表 |
| `POST /api/v1/auth/logout` | 清除 cookie |
| `GET /api/v1/auth/me` | 已登录 → `{id, email}`；未登录 → 401 |

- 密码：`bcrypt` 直用（不引 passlib——对 bcrypt 4.x 有告警且维护停滞）；哈希只入库、不回显、不进日志
- **密码长度 8 ≤ len ≤ 72 字节在 schema 层显式限制**：bcrypt 只取前 72 字节，超长必须拒绝而非静默截断
- 会话：HS256 单 access token，7 天，claims = `sub`(user id) + `iat` + `exp`；不做 refresh token
- Cookie：`httpOnly` + `SameSite=Lax` + `path=/`（`secure=False` 仅因本地 http，生产必须置 True）；Lax 同时挡掉跨站表单类 CSRF
- `JWT_SECRET` 只读环境变量（`SecretStr`，不回显、不入库）；缺失时启动打 warning 且 `/api/v1/auth/*` 返回 503——**不得静默降级为无鉴权**
- 前端与 API 同 site 跨 origin：SameSite 按 site（忽略端口）计算 → Lax 可行
- **API 基址跟随页面 host**（`apiBase()` 取 `window.location.hostname` + 端口），不在 `.env.local` 里写死 host：页面在 `localhost` 而 API 写死 `127.0.0.1` 时两者算跨站，httpOnly cookie 会被浏览器静默丢弃（表现为「注册/登录成功却立刻回到登录页」）。需要指向别的后端时才用 `NEXT_PUBLIC_API_BASE` 显式覆盖

**归属校验（P1 遗留阻塞项，本功能第一优先级）**

- 唯一规则：凡读写用户数据一律带 `user_id`；命中 0 行 → **404（与「不存在」同响应，不区分「无权限」，避免泄露存在性）**；未登录 → **401**
- 覆盖端点：`POST /api/v1/chat`（续聊既有会话）、`GET /api/v1/chat/threads`、`GET|DELETE /api/v1/chat/threads/{id}`；会话 / 回测 / 自选股的一切查询端点同规
- 新建会话在**流开始前**落归属行（失败即 500，不产生无名会话）；客户端传入的 `thread_id` 必须已属于本人，否则 404。P1 无主会话一律孤儿化：不迁移、不认领、不出现在任何列表
- 删除语义：先 `adelete_thread`（对不存在的 thread 静默成功，非新行为），再删归属行；失败残留只可能是「空会话」行，再删一次即可
- 公开 / 受保护边界：公开 = `/health*`、`/api/v1/market/*`、`/api/v1/events`（非用户资产，也是 M7「研报未登录只读」的前置口径）；受保护 = `/api/v1/chat/*`、`/api/v1/watchlist/*`、`/api/v1/auth/me`。`POST /api/v1/backtest` 与 `GET /api/v1/backtest/runs*` 自 M1c 起纳入受保护（落库需要归属；M1a/M1b 时它还是无状态计算、公开）

### M1b 前端认证闭环

- 受保护页迁入路由组 `app/(app)/`（对话页 / 回测页），`AuthGate` 与 `AppHeader` 上提到组布局——**守卫只有一个落点**
- `AuthProvider`：挂载时 `GET /api/v1/auth/me` 探测登录态（httpOnly cookie 在 JS 侧读不到，这是唯一可行路径）；未定态渲染骨架，避免先闪内容再跳登录；未登录重定向登录页
- `app/login`、`app/register`：表单 + `next` 回跳（只认站内路径，挡开放重定向）；注册成功即登录态；登录页带「记住我」（默认勾选）
- 对话页空态做**引导**：三条示例问题按能力分类（行情 / 对比 / 事件），文案照本地数据能力写（示例点下去答不出来比没有示例更伤）；点选只把问题灌进输入框并聚焦，发不发由用户决定
- 页头显示**数据时点**（「数据截至 X」）：值来自 `GET /api/v1/market/freshness`（查 DuckDB 的 `max(trade_date)` 与 `max(available_at)`），**不写死**——M2 日增量 ETL 接上后随每次拉取自动前移；取不到（数据未落盘 503）时不显示这枚标签，不编日期
- 两页共用 `AuthShell`：整屏连续画布（网格 / 光晕 / 双曲线装饰铺满，CSS+SVG 无图片素材）+ 浮起的半透明表单卡，主题切换器放登录页且与登录后同源
- `lib/api.ts` 与 `lib/sse.ts` 的**全部**请求带 `credentials: "include"`；后端 CORS 开 `allow_credentials=True`（`allow_origins` 已是显式列表，符合凭据模式要求）
- 页头显示用户邮箱与「退出」

### M1c 自选股 · 个人空间 · 我的回测

**自选股**（`app/api/watchlist.py`；表结构见 M1a）

| 端点 | 行为 |
|---|---|
| `GET /api/v1/watchlist` | 本人全部自选，**扁平数组**（分组由前端聚合）：`[{symbol, group_name, added_at, added_price, latest_close, latest_trade_date, change_pct}]` |
| `POST /api/v1/watchlist` | `{symbol, group_name?}` → 201 + 同形状单条；`UNIQUE(user_id, symbol)` 冲突 → **409**；symbol 非六位数字 → 422；`group_name` 缺省「默认分组」 |
| `PATCH /api/v1/watchlist/{symbol}` | `{group_name}` 改分组（一条 UPDATE）；不属于本人 / 不存在 → 404 |
| `DELETE /api/v1/watchlist/{symbol}` | 200 `{symbol, deleted: true}`；不存在 → 404 |
| `PATCH /api/v1/watchlist/groups/{name}` | `{name}` 重命名分组；**撞名即合并**（`UPDATE ... SET group_name` 的自然语义，不设 409）；原名不存在 → 404 |
| `DELETE /api/v1/watchlist/groups/{name}` | 组内标的回落「默认分组」（一条 UPDATE）；原名不存在 → 404 |
| `GET /api/v1/market/{symbol}/probe` | **代码体检**（2026-10-09 增，路由在行情层、消费方是自选股表单）：`{symbol, has_data, latest_trade_date, latest_close}`。**「没有」是答案不是错误**，故无数据回 **200 `has_data: false`**（与 `/{symbol}/bars` 无数据 404 的区别：那里是「你要的序列给不出」，这里是「这个代码在不在本地」）；行情层整体不可用仍由 `DataNotReady` → 503。复用 `latest_closes`（实测热 34ms / 冷 311ms），不落缓存 |

- **加自选表单的即时反馈**（2026-10-09，用户反馈「现状是提交才知道」；两项都不引第二数据源）：

  | 输入满 6 位后的状态 | 判据 | 界面 | 按钮 |
  |---|---|---|---|
  | 已在自选 | 前端已加载的列表（**不走服务端**，零往返） | 「已在自选 · 分组「X」」 | 变「移出」（**带一步确认**） |
  | 本地行情里没有这个代码 | probe 200 且 `has_data: false` | 「本地行情数据里没有 600519，核对一下代码」 | **禁用** |
  | 行情层不可用 / 请求失败 | probe 503 或网络错 | 不显示体检结果 | **不禁用**，照旧可加 |
  | 有数据 | probe 200 且 `has_data: true` | 「最近交易日 2026-09-30 收盘 1258.62」 | 照旧 |

  - 输入即查（**防抖 300ms**，输满 6 位才发），不必用户再点一次「查一下」
  - 「已在自选」时的「移出」要确认：`added_price` 是**加入时**的历史事实，删掉再加重来会把它重置，「加自选以来涨幅」跟着变——与行内「移出」的既有确认同口径
  - **无数据禁加**只针对「行情层可用 + 这个代码查不到」。本地覆盖面 5,798 只 A 股，代码打错一位即落这一档；提示里**不重复数据截止日**——页头那枚「数据截至 X」是全站常驻的，本地再说一遍只是同一条信息换个地方（也省掉一次查询）；**行情层整体不可用时不拦**——沿用下面那条降级口径，行情依赖不该把自选股拖死
  - 前端判重与体检结果都只是**提示**：真正的裁决仍是 `POST` 的 409 与 `UNIQUE(user_id, symbol)`，不因为前端拦了就改后端语义

- 分组名去首尾空白后 1–24 字符，**不得含 `/`**（`%2F` 在路由层被解码成路径分隔符 → 404）与控制字符，否则 422
- **「默认分组」是回落目标**：重命名与删除它一律 **400**（改名会让回落目标与建表默认值脱节）
- `added_price` = 加入时**最近可得交易日收盘价**（qfq，走 DuckDB 行情层）；取不到即存 NULL
- `change_pct` = (最新可得收盘 − `added_price`) / `added_price`；分母 ≤ 0 或任一为 NULL → `null`，UI 显示「—」，**不得编数**
- **降级**：行情层不可用（`DataNotReady`）时本端点**不 503**——价格字段全 null 并打 warning（自选股是用户资产，不该被行情依赖拖死，与「取不到即留空」同口径）；加自选照常，`added_price` 记 NULL。Postgres 不可用仍由 `require_user` 503
- 分组无实体表（见 M1a）⇒ **不存在「空分组」**，分组视图由 items 去重得出；`GET /api/v1/watchlist/groups` 这类单段路由会与 `/watchlist/{symbol}` 相撞，不设

**回测最小持久化**

- `POST /api/v1/backtest` **纳入鉴权**（`require_user` + `require_db`），响应改信封 **`{run_id, report}`**（报告结构本身不动，见 P1 SPEC §6；契约变更同步记入 P1 SPEC §10）
- 落库 `request` = **解析后的 config**：请求体 `model_dump(mode="json")` 后覆盖 `resolve_window` 解出的 `start`/`end`，并显式补 `adjust: "qfq"`；`costs` 取请求的 `CostOptions`（`CostModel` 不可序列化）；`pit_mode` 存**请求值**（`"both"`），不存归一后的 `Mode.PIT`
- 落库时机：`build_report` **成功之后**（异常路径不落库），失败即 500
- `GET /api/v1/backtest/runs?limit=20` → `[{id, created_at, symbol, strategy, start, end, pit_mode, metrics}]`；`metrics` 取 `report.metrics` **整块**（9 键）——逐键用 `->>` 抽取会把数字静默变成字符串
- `GET /api/v1/backtest/runs/{id}` → `{id, created_at, request, report}`；越权 / 不存在 → 404；非 UUID → 422
- **鉴权先于参数校验**：`POST` 未登录 + 非法体返回 **401**（FastAPI 先解依赖再校验 body；唯一例外是 JSON 本身解析失败，仍 422）

**个人空间页**（`app/(app)/space/`，导航只加这一项）

- 四页签（`?tab=watchlist|runs|threads|strategies`，默认 `watchlist`）：我的自选（完整增删改分组）/ 我的回测 / 会话历史 / 我的策略（M4 前占位）
- 会话历史端点（M1c）增返 `running`（2026-10-09，对话可用性）：服务端此刻是否有图为该会话在跑。断连不再取消图之后，刷新那一刻回答还在路上——前端据此显示「回答中…」并按 2s 轮询（上限 3 分钟），跑完自动出现，不必再手动刷一次；`running` 期间**不给**「重新生成」（那一轮没死）
- 深链回原页完整恢复：会话历史 → `/?thread=<id>`；我的回测 → `/backtest?run=<id>`（载入存下来的完整报告，**并把表单回填成该次请求**；跑完一次把地址更新为 `?run=`）
  - **对话页的 `?thread=` 与「当前打开的会话」双向同步**（2026-10-09 订正，用户反馈刷新丢会话）：状态变了写地址栏（发消息新建的会话号也进），地址栏变了开对应会话（深链、浏览器前进后退都走这条），两边用「最近一次已同步的号」去重。原「用完即摘」的写法（T6b）防的是「切侧栏被旧 id 拽回去」，代价是**刷新落到欢迎页**；双向同步同时解决这两件事，与 `/backtest?run=`、`/strategies?id=` 统一成一套做法
- 深链的 `useSearchParams` 以「只渲染 `null` 的子组件」形式包在 `Suspense` 内：生产构建下边界内整棵子树降级为 CSR，把整页包进去会让全高布局塌陷（同 `app/login/page.tsx`）
- `(app)/layout.tsx` 的守卫把 `next` 写成 `pathname + window.location.search`，否则深链在重新登录后丢失
- `GET /api/v1/chat/threads` 响应**增补 `last_active_at`**（additive）：会话列表口径不变，只是把排序依据一并带出，供个人空间显示「最近活动」

**验收**：注册 → 登录 → 用户 A/B 数据互不可见（含会话列表归属过滤，回归 P1 会话端点）；刷新后会话可恢复；自选股增删分组后涨幅显示正确；回测跑完可在「我的回测」里重开且表单与报告对得上；越权一律 404、未登录一律 401。
**本轮不做（记录待议）**：密码重置、邮箱验证、refresh token、记住我、第三方登录、资料页（改密 / 改邮箱）、全局 401 拦截（守卫层已覆盖）、旧会话认领脚本、多设备会话管理（P3-E2）。

## 3. M2 数据层

> 切片：**M2a** 全市场行情扩容 → **M2b** 交易日历 + 日增量 ETL → **M2c** 数据体检 + 退市股覆盖核实。

### M2a 全市场行情扩容

- 行情从 P1 的 3 个标的扩到数据源给的全市场：`cn-daily` 共 **21 片 = 7 年（2020–2026）× 3 复权（raw/qfq/hfq）**，896MB / 2397 万行；整市场年度分片，服务端**不做按标的裁剪**
- **落盘扁平命名** `data/bars/cn-daily_CN_{adjust}_{year}.parquet`（整片物化，不再按标的切分）：查询层 `data/bars/*.parquet` 的 glob 语义**保持不变**，视图、测试夹具与 `_meta` 消费方都不改。**P1 的按标的文件必须清掉**——留着会被 glob 捞进去，同一标的算两遍（唯一的静默错误点，有专门用例守着）
- **物化先暂存后换入**：全部落 `data/bars/_staging/`、逐片核对 sha 与行数通过后，才清旧文件并整体换入。边写边删会在任一片失败时留下「旧的已删 + 新的不齐」，而视图只判「有没有 parquet」，不齐不报错
- **数据版本指纹**（用户拍板的复权口径：不动价量，只记指纹）：`data/_meta/bars.json` 的 `fingerprint.shards` = 逐片 `{object_key, sha256}`。**锚点是分片本身，不是回执顶层的 `manifest_version`**——实测同一批分片来自多个 release（2020–2022 的 raw/hfq 出自 `20260908`、qfq 出自 `20260930`），没有任何一片来自顶层那个版本号。前复权只在新的除权除息后整体重算，跨版本比数字前先比指纹
- **无价 bar 不进定价路径**：`trading_status='no_turnover_observed'` 的日子可能整行价量为空（全市场 271 行/复权，全部落在 2026-08-19 之后）。`bars()` 与 `latest_closes()` **只返回四价齐全的行**——无价即无价，不是零价；留它进去会被 `Bar.from_row` 读成 0.0，持仓估值当日期末权益直接塌到现金、回测静默给出 -100%。原始行仍在 Parquet，SQL 可查（体检脚本 M2c 统计其数量）
- 查询性能（满规模压力测试：同一片复制 21 份凑 710MB，比真实更严苛）：单标的全区间 207ms、单日全市场聚合 9ms、冷启动建视图 + 首查 23ms。满足「秒级」验收，**不需要重建布局或建索引**

**两项待验证的结论（M2a 实测回填，原「待验证」条目据此关闭）**

| 待验证项 | 结论 | 对下游的影响 |
|---|---|---|
| 小石是否覆盖指数行情 | **不覆盖**。`cn-daily` 的标的前缀全集为 `000/001/002/003/300/301/302/600/601/603/605/688/689/920`，全部是个股；`000300`、`399xxx` 查无此码，Manifest 亦未声明任何 index 数据集 | M5 基准对比走**全市场等权组合代理**（口径在报告与 UI 如实标注）；M10 大盘总览的指数位改用全市场聚合自算 |
| 分片是否含行业字段 | **不含**。`cn-daily` 的 27 列里没有行业；行业只存在于 `sector-constituents` 数据集（归档仅 1 片 / 数日）与 `event-timeline` 的 `sector` / `sector_constituent` 事件（2026-08 起才有） | M10 板块热力图走**当日事件 `industries` 聚合**，口径在 UI 标注 |

**事件长历史口径复核（M2a 实测）**：归档自 2026-07 起才有量（7 月 10.6 万行 / 8 月 67.5 万行 / 9 月 159 万行），2026-07 之前合计约 460 行。**P1「长历史只能靠日增量积累」的结论成立**，92 天全量回填口径不变。

### M2b 交易日历 + 日增量 ETL

- **交易日历**（来源已拍板）：用 `exchange_calendars` 的 XSHG 日历**一次性生成并冻结为仓库内数据文件**（`backend/app/data/trading_calendar.json`，入库）；生成脚本与来源 URL 一并入库，**只有生成脚本依赖该库，运行期零依赖**。落**冻结文件 + DuckDB 视图**两处（同一份文件的两个读法），ETL 定时任务与回测撮合引用同一模块；**Postgres 表本轮不做**——当前无消费方，等 M5/M7 出现真实读者再加，不为「表」而「表」
- **日历验证升级为全期对账**：不再只抽查 1 年——拿 21 片全市场行情的 `distinct trade_date`（2020-01-02 → 2026-09-30）与日历**双向**比对（日历有而数据无、数据有而日历无都要报），例外逐条解释；另显式断言国庆/春节长假边界
- **事件语料通道 = 归档按日整片（CLI）；MCP 退回只服务对话实时查询**。实测依据：MCP 单次硬顶 500 行，`cursor` 参数被 FastMCP 拒收（**结构上不可翻页**），而单日任一主要类型（news / announcement）都已触顶 → MCP 无法作为全市场语料采集通道。归档 `event-timeline` 是 `date=YYYY-MM-DD/event_type=*/data.parquet` 的（日 × 事件类型）整市场分片，逐片自带 `sha256` + `object_key` + `min/max_event_time`；保留期是**滚动**窗口（news 与其他事件 3 个月、公告 24 个月，轴均为 `event_time`，超期请求为 410 硬拒且平台声明不补采）
- **保留期的两条产品后果**（做成行为，不靠文案打补丁）：① 日增量**不能断**——断了超过保留期，缺口永久不可得；② 本地语料覆盖区间 = `[首次回填日, 最新可用日]`，**起点固化、终点随日增前移**。一切「事件驱动能回测到哪」的判定都从数据里查，**不写死「最近 3 个月」**（那会让人误以为「等三个月就能回测 2020 年」）
- **事件驱动策略的时间收口**：请求的 `start` 若**显式**早于本地语料覆盖起点 → 拒绝（400）并给可执行出路（把起点改到覆盖起点之后，或改用不消费事件的 `ma_cross`）；`start` 缺省口径不变（仍取语料窗口起点）。报告 `meta` 增 `event_coverage`（语料覆盖起止日）与窗口内事件数，前端在事件表处展示——**任何一次事件驱动回测都自证「我看的是哪一段语料」**，不留静默空转
- **平台限流是停止信号，不是单日失败**：实测连续申请约 50 次（一天一次会话）会被平台 429，返回 `rate_limited_no_retry` + `Retry-After` ≈ 44 分钟。ETL 收到即**中止整轮**（换下一天接着撞只会把窗口越推越远，也是平台明确要求停下的语义），把 `retry_after_seconds` 与未处理天数写进台账，重跑同命令即续；同时**回填只补缺**——已有本地日分区的日子不再重复申请，「近期是否被平台修订」交给日增量的回落窗口负责
- 日增量 ETL：进程内 APScheduler 定时 + 手动触发端点（登录必需 + 并发锁）；**拉最近 N 个自然日**（回落窗口吸收迟到与修订——**M2c 复测订正**：全语料 p50 = 9 分钟、p90 = 17.2 小时、p99 = 17 天、最大 32.5 天；分月看才清楚长尾的来源——`event_time` 在 2026-07 的 p90 是 **265 小时**，8 月 16 小时、9 月 9 小时，因为 7 月的 7,719 条迟到事件 `available_at` 整批落在 2026-08-03→08-08，是**平台一次性回填**而非常态）而非只拉昨天。**据此的口径**：7 天窗口覆盖稳态迟到（p90 一天内），但挡不住平台批量补十几天的批次——那种只能靠「归档有、本地无」的对账发现（体检 E7）；**归档按自然日发布，不是交易日**（实测中秋 09-25、周末 09-26/27 都有分片，新闻不停市）——采集窗口一律按自然日枚举，日历只用于**分类缺口**（落在交易日上的缺口更严重）与回测撮合；按平台 `dedup_key` 去重（`(event_id, content_hash)` 降为兜底）；幂等（同日重跑逐字节一致）；**92 天全量回填一次性执行**（2026-07-07 → 最新可用日），逐日可续跑
- **落盘 schema 按 `quant-event-v2` 重定**：P1 的 21 列不够——实际落盘共 **30 列**，相对 P1 新增 `dedup_key` / `revision_id` / `record_version` / `revision_time` / `is_corrected` / `correction_count` / `reported_available_at` / `quality_status` / `source_time_quality` / `person` 等（**原文此处写的 `original_url` / `lineage` 并不存在**：url 列是 `source_url`，且无 `lineage`，M2c 按实测回填更正）。落列前先读一次 `xiaoshi-data schema --dataset event-timeline` 定稿，不靠推测
- **方向映射分三类，不能混为一谈**：① 情绪（news/person）`利多/看多/positive` → bullish；② **政策立场**（policy）`dovish`（鸽派/宽松）→ bullish、`hawkish`（鹰派/紧缩）→ bearish——这两词原先不在表里被整批归 NULL（实测 220 + 165 条），那是**语义被丢掉**而不是「无方向」；③ **公告类别不映射**（中性 83% 之外是高管人事/融资定增/业绩预告等 ~20 种，「融资定增」既可能是扩张也可能是摊薄）——类别→方向是业务判断，须有业务认可的表，属 M8 的 PIT 基本面事件
- **改口径后用本地分片重物化**（`download_events.py --rematerialize`）：分片是内容寻址的本地对象，`day_shards()` 只读回执，**零网络会话**——与 `--refresh`（要重新下发请求）成本完全不同，别混用。改映射表后实测：84 天全量重物化约 90 秒、只改动内容受影响的日子（63 天含 dovish/hawkish ↔ 63 片字节变化，完全对应）
- **幂等的准确表述**：来源分片未变 → **不重写**（跳过）；被迫重写时**内容与字节都一致**（已用「强制重物化同一天两次」验证，不靠「跳过所以没变」自证）
- **两条实测数据语义（下游都要知道）**：① **归档是「当前版本快照」**——平台会对既有事件重发修订版（内容一改 `content_hash` 就变；353 条 P1 锚点里 255 条在一天之内被改过），落盘以**拉取当刻的修订**为准，`event_id` 稳定、内容会变，故锚点比对锚 `event_id` 而不是 `content_hash`；② **`event_id` 不是全局唯一键**——它在日分区内唯一（物化时校验），但平台对「按月复发的同题事件」复用 id（实测 84 天里 18 例，如 `news:1818329` 既是 8 月 CPI 也是 9 月 CPI），因此查询层不得拿它当主键去重，前端列表的 React key 必须带上时间
- 事件语料改**全市场按日**落盘（一条事件一行，不再按标的切分）：`data/events/cn-events_{YYYY-MM-DD}.parquet`（扁平命名，查询层 `*.parquet` glob 语义不变）+ 逐日原始档 `data/raw/events/{date}.jsonl`；`stocks` 归一化出 `symbols VARCHAR[]`（去 `.SZ/.SH` 后缀、`code` 为 null 时回退按 `name` 取六位码、去重排序，`stocks` 原样保留备审计）。P1 数据实测：709 条目标中 94 条 `code` 为 null、10 条带后缀；跨标的重复会**自然收敛**（355 行 → 353 条唯一）——这是回归锚点。**P1 的 `{symbol}.parquet` 必须清掉**（否则同一事件被算两遍，与 M2a 同款静默错误点，用例守着）

### M2c 数据体检 + 退市股覆盖核实

> 本片是 M2 数据层的**护栏与自证**：日增量 ETL 已按日自动跑，没有体检则缺口只能靠人肉发现；而「生存者偏差」是护城河「前视偏差为零」的镜像——若行情分片只有活下来的公司，回测收益天然偏高。
> 四条边界（用户 2026-10-07 拍板）：**分两段收口**（真实三天连跑证据 10-09/10 回填）、退市核实走**数据内双证据链**、**只报告不修**、体检**只给脚本 + 退出码**（不挂调度器、不进 API、不建看板，完整版留 P3-E3）。

**落点与接口**

- `app/data/data_health.py`：检查器。`Check{id, level ∈ error|warn|info, title, detail, data}`；`run_all(*, data_dir=None, only=None)` / `summarize()` / `exit_code(checks, *, strict)`；每项一个函数、签名统一
- `app/data/delisting.py`：退市覆盖核实（独立模块——它是一次性验收证据，不是日复一日跑项，且 226 只清单会撑爆人读报告）
- `scripts/run_health_check.py`：薄壳，逻辑全在 app 层；`--json PATH` / `--data-dir PATH` / `--strict`；有 error 退 1，`--strict` 时 warn 也退 1

三条硬规则：① **每个检查自带 try/except**，异常转成 error 级 Check——一条 SQL 炸掉不该吞掉整份报告；② 全部检查**共用一条 `duckdb_client.connect(data_dir)`**，抛 `DataNotReady` 时生成一条 error Check 并短路（不打 traceback）；③ **集合式 SQL、一条连接**，禁止 `bars()` / `latest_closes()` 这类逐标的 API、禁止对全量结果 `to_pylist()`，读 TIMESTAMPTZ 必须走 arrow（缺 pytz 时裸 `fetchall()` 直接报错）。

**检查项**（`级别`列为命中该条件时的级别；括号内为规划期实测基线）

| ID | 检查 | 级别与判据 |
|---|---|---|
| B1 | 分片对账 | **error**：`_meta/bars.json` 的 `outputs[].rows/sha256` 与实际 Parquet 不符、缺片或多片（sha 逐片重算，不采信清单——清单是记录不是测量） |
| B2 | 重复行 `(symbol, trade_date, adjustment)` | **error**：非 0 即报（实测 0） |
| B3 | 缺失值比例 | **info**：逐列 null 率只打印（`adj_factor` 99.31% 仅 raw 有值、`turnover_pct` 2.33%、`is_suspended` 0.29% 皆为设计）。**仅** `close` / `volume` / `available_at` 零容忍 → error |
| B4 | 无价 bar 计数 | **info**：`trading_status='no_turnover_observed' AND close IS NULL`（基线 271 行/复权、49 只，全在 2026-08-19 后）；与 `bars()` 的剔除口径显式对齐 |
| B5 | 异常步长（两条分列） | **warn**：`abs(change_pct) > 31%`（超北交所 30% 上限）且不在该标的前 5 个交易日内（排除新股无涨跌幅限制），实测 **106 条**（复牌首日/重新上市的参考价重置，如 `000670@2022-08-22 +488%`），列条数与样本；**warn + 基线**：close 环比 vs `change_pct` 自洽性——**容差写死 0.011pp**（基线 hfq 40 / qfq 448 / raw 28,686；容差取 0 时为 762 万条，判据不可复现），并单列「偏差 >1pp」计数（qfq 448 中 314 条，除权/上市首日类，属正常） |
| B6 | `trading_status` 取值 | **warn**：白名单外取值（现会报出 qfq 独有的 `unknown` 5,099 行，价格齐全、无消费方） |
| B7 | 每日 bar 数包络 | **error**：逐交易日标的数，相邻日跌幅 >10% 或 >50% 即报（基线 3,761 → 5,572 单调上升，实测命中 0）。抓的是**分片截断**——日期级连续性已被 B2 + B8 覆盖，原「跨年边界」判据冗余，且「每标的少几天」会命中 500 只停牌股 |
| B8 | 日历双向对账 | **error**：复用 `cal.audit()`；`audit_calendar.py` 内嵌的 `trade_dates()` **下沉到 app 层**，脚本反过来调它，避免两份实现漂移 |
| E1 | 日分区完整性 | **error**：`_meta/events.json` 的 `days` 与磁盘实际文件比对，尊重「归档当天真没内容」的 `empty` 语义；`read_day_entries` 对缺旁注文件返回 `rows=-1` 必须当 error |
| E2 | 重复行 | **error**：日内 `dedup_key` / `(event_id, content_hash)`（实测 0） |
| E3 | `available_at` | **error**：空值 / 倒挂（< `event_time`），实测 0 / 0 |
| E4 | `direction` 合法性 | **error**：白名单针对 `direction_norm`（实测 4 值、0 违规）；`direction` 是 45 个公告类别词表，**另设**一条长串脏值检查（基线恰 1 条：`融资 from theellsellsellsellsell`） |
| E5 | `symbols` 形态 | **info**：空数组比例（基线 35.3%，设计如此）、非六位码（实测 0）、与 bars 的差集（多为 ETF / B 股 / 基金代码，分类说明非退市） |
| E6 | `event_id` 跨日复用 | **info，永不 error**：平台行为（基线 18 例、最多跨 6 天，M2b 已记录） |
| E7 | 覆盖缺口 | **error / warn，必须本地算**：**不得复用 `runner.status()`**——它会调 `archive.coverage()`（子进程调 xiaoshi CLI，需网络与密钥、300s 超时），失败时 `archive_last=None` 使 `missing` 静默为空，把「查不到归档」打印成「缺口 0」。改为**本地实现**（日历 × 本地日分区）：落在交易日上的缺口 error、自然日缺口 warn；平台侧口径仅 `--online` 时取 |
| X1 | 日历余量 | **warn**：余量 < 90 天（实测 85 天，**出生即告警**）。`detail` 写明出路是升级 `exchange_calendars` 后重新生成，不是「再等等」 |
| X2 | 遗留文件 | **warn**：`data/raw/events/*.jsonl` 三个 P1 残留；另查陈旧锁 `.xiaoshi-execution.lock`（既有用例只查 `bars/` 与 `events/`，`raw/` 无人管） |
| X3 | 台账可读 | **error**：`_meta/etl_runs.jsonl` 逐行可解析——**宽松解析、缺键容忍**（schema 已演进：实测 6 行中前 3 行 6 键、后 3 行 9 键） |
| X4 | **向量索引落后**（2026-10-09 补口） | **warn**：本地事件日分区 ↔ Qdrant 的 `day` 分区，**缺任一天即报**（detail 列条数与最近几天）。Qdrant 不可达降级为 **info**——索引是**派生物**，它没跟上不该把整份体检判失败，但也不能静默 |
| X5 | **行情停更**（同上） | **warn**：本地行情末端 vs 交易日历的最近交易日，落后 **> 5 个交易日**即报（阈值可配）。detail 须写明**行情是年度整片的重下载、不是日更**，否则读者会以为坏了 |
| X6 | **归档自己停更**（同上） | **warn**：从 **ETL 台账**读最近一次的 `archive_last`（**本地读，不联网**——E7 的教训：不调 xiaoshi CLI 的 `coverage()`），距今 **> 3 天**即报。它抓的是「源侧不发了」，与 E7 的「源侧有、本地无」正好互补 |

**M2c 补口：四通道的新鲜度（2026-10-09，收口当天发现）**

数据层有四条通道，各自的更新节奏**不一样**，而收口当天发现其中两条根本没有自动化、落后了也**没人知道**（两次缺口都是偶然撞见的）：

| 通道 | 谁在更新 | 收口前的状态 |
|---|---|---|
| 事件语料 | 日更（进程内调度器 21:10 + 重启补缺口） | ✅ 自动 |
| 名称字典 | **挂在 `runner.run()` 里**，每次 ETL 跑完重建（1.3s，失败不拖垮 ETL） | ✅ 自动 |
| **向量索引** | 只有手动 `embed_events.py` | ❌ **每天落后一天**，且落后无人知 |
| **行情** | 只有手动 `download_bars.py`（年度整片 / 21 会话 / ~99MB 一天） | ❌ 停在 2026-09-30 |

- **可见性**：上表 X4 / X5 / X6 三条 warn 就是这条的落地——**先让落后看得见**
- **自动补嵌**：ETL 跑完（**成功且确有物化**）之后触发一次**增量**嵌入，daily / catchup / backfill 都覆盖
  - **走子进程**（拉起 `scripts/embed_events.py`），**不塞进 API 进程**。理由：批量嵌入跑 CPU，而 API 进程里的检索用**同一个 CPU 编码器**——同一个 FlagEmbedding 对象被两个线程并发调用，就是 2026-10-09 那次 SIGABRT 的同类风险（当时在 MPS 上，且 `_run_on_device` **刻意不给 CPU 加锁**）；子进程还额外买到「崩了不拖垮 API」。代价：多一份模型内存（~1GB）、每次重载 ~30s，一天一次
  - 护栏：**独立锁**（不与 ETL 的 `_LOCK` 争）、超时、**失败只记台账不抛**（同字典那条口径）、配置开关 `etl_embed_after_run` 默认与 `etl_enabled` 同风格（**默认关**）
  - 放进 **⑦ 收口清单之外**：行情仍未自动化——它是年度整片，重构成可调用函数后进调度器仍留在待办（可延）

**只报告不修的两条挂账**（各出一条 info Check 明示，否则「只报」没有落地）：`DataNotReady` 判据仍是「目录里有没有 parquet」；`backtest_runs.request` 不含数据版本指纹与快照版本（M2a 遗留①，留 M5）。

**退市股覆盖确认（生存者偏差核实）**

口径必须是**本地可得清单**，不是「全市场清单」——本地无标的清单文件，小石 10 个数据集里没有证券主数据 / 退市清单，归档是 `(date, event_type)` 分区、**无 symbol 维度**。故走数据内双证据链：

1. **全期结构证据**：按 `max(trade_date)` 早于数据末端的标的清单与年度分布（基线 226 只 = 16 / 20 / 43 / 45 / 52 / 30 / 20）。完整清单只进 `--json`，人读只出一行
2. **窗口内逐只实证**：事件语料覆盖区间内，末日落在区间内的标的中，有退市类事件（`direction ∈ {退市风险, 停复牌}` 或标题含「退市」）者**确证**，无公告者列为**待解释**（长期停牌与退市在纯行情数据里不可辨）。基线：**5 只候选 / 2 只确证 / 3 只待解释**（`002898`、`920305` 均有「进入退市整理期」公告；`000004`/`002808`/`300029` 窗口内无公告）——**如实写，不得表述为「逐只实证通过」**。候选的判据是「末日落在**语料窗口内**」：早于窗口起点结束的标的，语料里本来就不可能有它的事件，拿它当候选是把「无从取证」混进「取了证」

**验收**（§3 共享，M2c 结束后整段可签）：全市场日线 DuckDB 秒级查询；**注入脏数据时体检脚本报警**（三步证据：离线单测逐项断言 → 脚本级用例 `main([...])` 退 1 且 JSON 里能定位到对应 error → 一次真实 CLI 调用留终端输出）；**交易日历与全市场行情 `trade_date` 全期双向对账一致**（例外逐条解释）；全市场语料对 P1 三标的的 **353 条锚点包含性回归通过——锚 `event_id`，不锚 `content_hash`**（平台一天内修订过 255/353 条，锚 hash 会把「平台改内容」误判成「我们丢数据」；既有用例在 `tests/integration/test_data_t2.py`）；**事件驱动回测在语料覆盖区间外显式报错，不空转**（`app/backtest/report.py` 已实现，本轮补一条断言该 400 的用例）；**ETL 连续运行 3 天无重复、无漏拉**——本片**分两段收口**：第一段交付脚本与核实，第二段（10-09/10）核对 `_meta/etl_runs.jsonl` 出现 3 个自然日的运行记录并逐日核验无重复、无漏拉。触发方式须写进交付文档：调度器是**进程内** APScheduler，只在 uvicorn 存活时 21:10 才跑；进程不在则该日补跑一次日增量并在文档里如实标注是调度还是手动；不足 3 天如实记录并顺延。

**收口读数（2026-10-09，证据 `logs/m2c/etl_three_days.md` + `health-after-backfill.json`）**：三天齐（10-07 / 10-08 / 10-09），其中两天由 21:10 定时触发、第三天由重启兜底 catchup + 一次手动 backfill 触发，**没有任何一天漏跑**；无重复四层与无漏拉四层全部通过（全量 303,887 行零重复、逐片 sha 零不一致、`gap` 归零、体检 E7 交易日与自然日缺口均为 0）。**当日归档停更 9 天后恢复发布**（09-29 → 10-07），积压 9 天撞上 7 天回落窗口，`2026-09-30` 这个交易日被滑窗漏掉——正是第一段交付时预警的那个风险，按同一份交付文档写下的约定 `--backfill` 补回。**两条据此写进交付文档的判据**：① 台账里的 `failed` **不等于**缺数据（当次失败的是「对已有分区的重拉」，撞的是小石 CLI 的版本门槛），真正的缺口判据只有本地算的 `--status` 的 `gap`——E7 走「日历 × 本地日分区」而不是数台账，是对的；② 「归档恢复发布那天先看 gap、>0 就 backfill」这条**不依赖知道平台何时恢复**，任何一天跑都对，可当例行。

## 4. M3 RAG 完整化

> 切片：**M3a** 嵌入流水线与索引 → **M3b** 检索管线与 PIT 接线 → **M3c** 评测集与消融。
> 语料形态由 M2b 定死（日分区 Parquet、一条事件一行、`symbols` 为数组）；M3 **只读语料、不重跑 ETL**。

### 共同口径（三条实测约束，2026-10-07 规划期实测）

1. **无长文本 ⇒ 分块与父子文档本轮不落地**：全量 289,519 条的标题+摘要中位 29–198 字、最长 2,065 字，远低于 BGE-M3 的 8,192 token 上限。「仅长文本走递归分块 + 父子文档」保留为设计预留，**不写代码**——为空集写的分支只会烂在那里。
2. **公告是标题流**：`announcement` 90,362 条中 83% 摘要为空（标题即全文）；`industries` 覆盖 news 44% / policy 100% / **announcement 0%**。故 `event_type` 与 `industries` 过滤一律**可选**，不得默认开启。
3. **主键既不是 `event_id` 也不是 `dedup_key`**：实测 `dedup_key` 289,494 distinct / 289,519 行（18 组重复，与 M2b 记的「同题事件跨月复用 id」同源）。point id 一律 `uuid5(NS, f"{day}|{event_id}")`。

### M3a 嵌入流水线与索引

**落点**

```
backend/app/rag/encoder.py     # 编码器协议 + 实现（懒加载；不可用抛 RagNotReady）
backend/app/rag/collection.py  # collection schema / point id / upsert / delete_day / 逐日点数对账
backend/app/rag/embed.py       # 离线批处理：逐日分区、长度分桶、断点续跑
scripts/embed_events.py        # 薄壳：--full / --incremental / --status / --rebuild-day
```

- **运行时已由 spike 定档（2026-10-07 实测，见 §13 v1.7）**：
  - **fastembed 路线否决**——它到 0.8.1（PyPI 最新）仍**不支持 `BAAI/bge-m3`**（dense 与 sparse 都不在 `list_supported_models()` 里），reranker 列表里也没有 `bge-reranker-v2-m3`；
  - **选 FlagEmbedding + torch，跑 CPU**：199 条真实语料上 CPU batch=4 **22.8 条/s**（batch=8 → 18.5、batch=32 → 13.6）、峰值内存约 1.0GB；MPS 只快 1.57 倍（29.0 条/s）却吃 4.2GB 内存，且该后端有长跑内存泄漏——**收益不抵风险**；
  - 全量 289,519 条外推 **约 3 小时**（插入式逐日分区断点，中断即续）。
- 模型 `BAAI/bge-m3`（dense 1024 + sparse lexical weights）+ `BAAI/bge-reranker-v2-m3`；缓存指向 `.tools/models`（`HF_HOME`），不入库。**两个下载期的坑都实测过**：① 默认的 **Xet 通道在本机会卡死**（自适应并发一路降到 50，20 分钟只落 43MB），必须 `HF_HUB_DISABLE_XET=1` 回落普通 HTTP（实测 3–4 MB/s，主站比 hf-mirror 略快）；② `cache_dir` 必须给 **`<模型目录>/hub`**——给 `<模型目录>` 会让同一份权重落进第二套缓存（实测白下 2.3GB）
- 若走 MPS：**按日分区分数段重启进程**——该后端有实测长跑内存泄漏（3,000 句后 >20GB OOM），批处理本来就有检查点，重启是零成本的规避
- collection `cn_events_v1`：命名向量 `dense`（1024，Cosine）+ `sparse`
- **payload 索引必建**：`available_at`（datetime range）+ `day` / `event_type` / `symbols` / `industries`（keyword）——PIT 过滤走服务端 range，无索引即全表扫
- 嵌入文本 = 元数据 header + 标题 + 摘要（`【标题】【类型】【时间】【方向】【重要度】【行业】【标的】`）；**空字段不写标签**，不留 `【行业】` 空壳
- 断点单位 = 日分区；状态文件 `data/_meta/embeddings.json` 逐日记 `{date, rows, sha256, points, embedded_at}`，**sha 与 `_meta/events.json` 的 `days[].sha256` 同源**——sha 未变跳过，变了**先删该日 points 再重嵌**（平台会改既有事件内容，M2b 实测 353 条里 255 条一天内被改过）
- 幂等表述沿用 M2b 口径：强制重嵌同一天两次，点数与向量**逐点一致**（不靠「跳过所以没变」自证）
- `--status`：本地逐日行数 vs Qdrant 逐日点数**双向对账**（差集两个方向都要报）

### M3b 检索管线与 PIT

**落点** `backend/app/rag/retrieve.py`

- 过滤构造：`available_at <= as_of` **必带**；`symbol` / `industries` / `event_type` / `min_importance` 可选
- **PIT 过滤必须写进 Qdrant 服务端 filter**，不得「先取 top-100 再本地过滤」——后者会让未来事件挤掉合法的历史结果，召回静默塌陷（这是护城河的实现细节，不是优化项）
- 双路召回 dense top-100 ∥ sparse top-100 → 服务端 RRF → rerank → `top_k`（默认 5）
- **RRF 的 k 必须显式传 `k=61`**：Qdrant 默认 `k=2`（rank 从 0 起计数），规划报告写的「k=60」在 Qdrant 公式下等价于 `k=61`；不显式传就等于悄悄换了算法。自定义 k 需服务端 ≥ v1.16（本机 v1.19.1 满足）
- **rerank 候选数默认 50，由端到端实测定档**（M3c，200 条评测集 + 30 样本延迟）：

  | 候选数 | NDCG@10 | 重排延迟（中位 / p95） |
  |---|---|---|
  | 10 | 0.7615 | 0.84s / 1.57s |
  | 20 | 0.7768 | 1.27s / 2.31s |
  | **50（默认）** | **0.8142** | 5.17s / 7.55s（端到端，与文档长度相关） |

  **关键读数：精排的增益主要来自第 21–50 名那一段**——只精排 20 篇时 NDCG@10 几乎与稠密档打平（0.7768 vs 0.7764），「精排值得一跳」这个结论在 20 篇候选下不成立。所以默认保持 50；要压延迟就得接受质量回落到「与稠密档持平」。
  瓶颈是 CPU 上的 cross-encoder（单篇 44–68 ms，随文档长度上升，非并行可解——本机实测多进程无增益）。
- NFR「检索 p95 ≤ 500ms」与上面这组数字冲突（**召回+融合一档 p95 0.21s 是达标的**，超的是精排那一跳）。**由用户裁决，不擅自改 PRD**：① 改措辞（检索不含精排 ≤500ms、检索链路含精排 ≤ 数秒）；② 减候选（质量代价见上表）；③ 换 0.3B 档重排模型（需重新验证质量）

**精排的设备与截断（2026-10-08 实测，用户拍板「先做 B」）**

| 配置 | NDCG@10 | 精排延迟 | 判读 |
|---|---|---|---|
| CPU + 截断 512（原默认） | 0.8142 | 均值 5.17s / p95 7.55s | 基线 |
| CPU + 截断 256 | — | 长文档 165 → 73 ms/篇 | 截断是真杠杆（下表） |
| **MPS + 截断 256（现默认）** | **0.8142（逐位不变）** | **中位 2.9–3.0s / 均值 3.0–3.4s** | 见下 |

- **设备**：`rag_rerank_device = auto`（有 MPS 就用，否则 CPU）；**嵌入仍钉 CPU**——问答路径只要 0.15s，而批量嵌入走 MPS 只快 1.57 倍、内存翻倍且有长跑泄漏记录
- **截断**：精排单独一档 `rag_rerank_max_length = 256`（与建索引用的 512 分开）。同样 50 篇：512 → 165ms/篇、256 → 73、128 → 36（**只对长文档有效**，候选文本中位 80 字）。注意 `FlagReranker` 的 `max_length` **在构造时固化**，改属性不生效（踩过一次）
- **质量**：MPS + fp16 + 截断 256 下三档 NDCG@10 与 CPU 基线**逐位相同**（0.7764 / 0.7602 / 0.8142），精排排序未被数值精度或截断改变
- **延迟的方差是真实的，必须如实记录**：同一配置连测三轮，p95 在 **3.9–6.6s** 之间波动（最大 9.7s），取决于机器负载（Docker 的虚拟化进程常占 ~90% CPU）。**单轮测出的 p95 2.45s 属偏乐观的一次**，不能当承诺值
- **内存**：两模型共存的进程峰值 RSS——CPU 2,954MB / MPS 3,327–4,231MB；MPS 连续 120 次重排（6,000 对）内存**无增长**（社区报的泄漏是嵌入长跑，与「每问一次 50 对」的重排不同风险面）
- **MPS 调用必须串行（2026-10-09 订正）**：PyTorch 的 MPS 后端**不是线程安全的**——两个线程同时进去会在 Metal 层触发 `MTLReleaseAssertionFailure` → `abort()` **整个服务进程**（实测崩溃：uvicorn 被 SIGABRT 打死，崩溃报告的线程快照里一个线程卡在 `MPSStream::synchronize`、另一个正在 `setCurrentCommandEncoder`）。调用方是 `asyncio.to_thread` 的线程池，两条检索重叠就够了。落地：`app/rag/encoder.py::_run_on_device` 给 MPS 加进程级 `threading.Lock`，**CPU 不加锁**（没有共享的 GPU 命令队列，串行只会白白压吞吐）；单测同时断言「MPS 串行」与「CPU 不串行」。**代价如实记**：并发的检索会排队（每次精排 3–5s），换来的是进程不会再被 GPU 驱动打死
- **顺手**：`RAG_WARMUP_ON_START`（默认 false）启动期后台预热，把首次提问的 7–15s 载入挪到启动期；默认关是因为集成测试也跑 lifespan
- 返回结构：`{event_id, day, title, summary, event_time, available_at, event_type, direction_norm, importance_score, symbols, industries, source, original_source, source_url, content_hash, score, score_kind}`——**双时间戳并列 + 来源三元组**（PRD §5 硬性要求），M7 证据面板直接消费
- 降级：Qdrant 或模型不可用 → `RagNotReady`；Agent 工具返回一句人话（同 `query_market_bars` 姿态），不炸整条流；**不得静默返回空列表冒充「查不到」**

**Agent 接线**：`app/agent/tools.py` 新增 `search_events`（本地 RAG）。与小石在线检索**并存**——本地管历史与 PIT 语义检索，在线（`search_financial_news`）管实时快讯；`prompts.py` 写明两条通道的边界，且本地语料覆盖区间**从数据里查、不写死**（M2b 已定口径）。

### M3c 评测集与消融

- 评测集 `backend/tests/rag/eval_set_v1.json`：**200 条**，五类各 40（事实型 / 事件型 / 数值型 / 时间型 / 多跳型）
- 构建（用户 2026-10-07 拍板）：分层抽样真值事件 → LLM 写 query（**不泄漏答案**）→ 候选池 = 真值事件 ∪ 各档 top-20 → LLM 分级 0/1/2 → **人工抽检 20–30 条**并把修正记进文件；标注指南与生成 prompt 一并入库
- 两条纪律：① query 必须**能被本语料回答**（真值事件自身含答案）——数值型只能取标题/摘要里真带数字的事件，否则是在测一个不存在的目标；② 语料窗口随日增前移，评测集**冻结为 v1 快照**，重建另起版本号
- 指标自实现（不引 `pytrec_eval`）：NDCG@10（主）/ MRR / Recall@20
- 消融三档：dense-only / hybrid(RRF) / hybrid+rerank；**延迟分两段单列**（召回+融合 / 含 rerank）
- 验收口径（用户 2026-10-07 拍板）：hybrid+rerank 的 NDCG@10 **≥ 0.5**，且**同时优于**另两档；三档绝对分数与延迟如实记录在 `logs/m3/rag_eval.{json,md}`
- **实测结果（2026-10-08）**：dense **0.7764** / hybrid **0.7602** / rerank **0.8142** ⇒ **验收通过**。两条如实记录、不作修饰的发现：① **稀疏腿在本语料上没有增益反而略损**（dense→hybrid −0.0162；已核实稀疏向量非空、结果确有差异，不是「稀疏没生效」）；② 精排增益 +0.0540，是整条管线里唯一被数据支持的一跳

**验收**（§4 共享）：① PIT 单测——任意 `as_of` 下结果严格不含 `available_at > as_of`（含边界：恰好等于某事件 `available_at` 应含、早一秒应不含、早于语料窗口起点为空集）；② **服务端过滤的行为证据**——注入一条与 query 高度相关但 `available_at` 很晚的事件，证明它不会挤掉合法结果（只断言「结果里没有未来事件」不足以证明过滤发生在服务端）；③ 评测集 NDCG@10 达上述口径；④ `--status` 覆盖对账零差异。

**本轮不做**：分块与父子文档（无长文本）；新 API 端点（等 M7 / M10 的真实消费方）；嵌入挂调度器（可延，同 M2c 口径：先脚本 + 非零退出码）；ColBERT 第三路精排；RAGAS 生成质量指标（M8）；嵌入覆盖对账**不进体检脚本**（体检是数据层护栏，嵌入是派生物，完整版留 P3-E3）。

## 5. M4 策略工作台

> 切片：**M4a** 沙箱与用户策略 API → **M4b** 静态检查器与模板库 → **M4c** 持久化与前端工作台。
> 本轮首次引入「执行用户代码」这条新服务面，**内置策略路径一行不动**（零回归面）。

**一条要先写清的判断（决定各部分分工）**：护城河的**主闸门是结构，不是 AST**。用户策略只拿到 `ctx`——`history` 是 `bars[:index+1]` 的截断元组、`events` 已过 PIT 闸门、命名空间里没有 pandas / duckdb / 文件与网络，未来数据在 API 形状上不可达（与内置策略同一保证）。因此：① **import 白名单是正确性闸门**——它拦的是唯一真实的泄漏路径「`import duckdb` 读全量 Parquet 自己索引未来」，不是可选加固；② **AST 检查器是第二道门**，干三件实事：把数据绕行在运行前拦住并给行号、把从其他框架搬来的未来函数（`shift(-1)` / `bfill`）变成可读报错（否则用户只会得到 `AttributeError: 'tuple' object has no attribute 'shift'`）、声明与代码的一致性等工程检查。**对外表述不得把 AST 写成主闸门。**

### 用户策略 API（契约，M4a 定稿）

```python
PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}, ...}
USES_EVENTS = False          # 驱动两件事：缺省窗口是否取事件语料起点；PIT 对比是否可用
def validate_params(p): ...  # 可选钩子：跨字段约束（fast < slow），返回中文错误串列表
def on_bar(ctx): ...         # 唯一入口；返回 list[Signal]
def on_bar(ctx, p): ...      # 需要参数时用第二个形参（与 validate_params 同形）
```

- 入口是**模块级 `on_bar` 函数**（不用类与继承）；`Signal` / `Side` 预注入命名空间，用户无需 import
- **参数的取值走第二个形参**（M4a 落地时定死）：不注入全局名——来源写在签名上，读代码不必猜；`p` 已由 `PARAMS` 缺省值**填满**，放心 `p["fast"]`。两处校验各司其职：父侧先校验一次是为了**不 spawn 就能给 422**，子侧再确认一次是保证策略拿到的字典一定完整
- `PARAMS` 的 `type` 取 `int|float|bool`、`default` 定缺省、`min`/`max` 可选；**UI 参数表单由它生成，后端按它校验**（键集 / 类型 / 值域）→ 同时关闭待办「后端不校验策略参数的值」（内置策略补上同类校验：`fast<slow`、`hold_days≥1` 等，与 T6c 前端既有规则同源）
- `ctx` 字段与内置策略完全一致（`bar/index/history/position/cash/equity/events/new_events`），**不得新增任何持有全量数据的属性**
- 命名空间白名单（纯计算）：`math` / `statistics` / `itertools` / `functools` / `collections` / `dataclasses` / `typing` / `datetime`；**不给 pandas**——`shift` / `bfill` 正是前视泄漏的常见来源，我们故意不给（产品立场，写进模板注释与 M9 教程）

### M4a 沙箱

```
backend/app/strategy/worker.py    # 子进程入口：读 stdin 源码 → 构建策略 → run_backtest → stdout 出 JSON
backend/app/strategy/sandbox.py   # 父侧执行器：spawn / 配额 / 超时 / 输出上限 / 错误码映射
backend/app/strategy/params.py    # PARAMS schema 校验与类型归一（纯函数）
```

- 源码经 **stdin** 传（不进 argv——`ps` 可见），报告经 **stdout** JSON 出；stderr 截断保留作诊断
- 配额（**规划期实测定档**）：**CPU 用 `RLIMIT_CPU`**（macOS 实测生效：2s 硬限下 SIGXCPU 如期送达）；**内存不用 rlimit**（macOS 实测设不下去：Python 内 `setrlimit` 报 `current limit exceeds maximum limit`，`sh -c 'ulimit -v/-d'` 亦 `cannot modify limit`，且设 512MB 后子进程照样分配 1GB）→ 改**子进程内 `ru_maxrss` 峰值自监控线程**（macOS 返回字节、Linux 返回 KB，单位须归一），超限即 `os._exit`；**不按平台分叉**——Linux 上虚拟地址空间动辄 >2GB，照 512MB 设 `RLIMIT_AS` 会在 import 期就自杀；**墙钟由父进程 `wait_for` + 杀进程组兜底**；输出与 stderr 各有上限。耗时实测：全期 1,636 bars 的 `build_report` 仅 0.25s（含解释器启动整次运行 1.0s）→ 阈值给到 20s 级不误杀
- **内存上限的含义与定档（2026-10-08 M4c 实测订正）**：它量的是**子进程总 RSS**，而引擎自己读全市场 Parquet 就要 300–400MB（实测：上限 300MB 时正常模板也被判超限、400MB 通过）→ 原定 512MB 只剩 1.3–1.7× 余量，**合法策略可能被误判**。默认改为 **1024MB**：看门狗的职责是可用性（拦住失控增长）而不是给策略定预算，1GB 仍是有界的。（如实记录：M4c 的 HTTP 矩阵最初按 200MB 收紧，导致**每条用例含好策略都以「内存超限」告终**——那是假证据，改成只收紧 CPU / 墙钟 / 输出）
- 引擎接线：`run_backtest(config, strategy=None)` **一个注入点**；报告仍由 `build_report` 同一份代码产出，前端整套报告 UI 零改动
- **威胁模型如实写**：目标是**可用性**（坏代码不拖垮服务）与**阻断无意/半有意绕行**，不是对抗恶意作者——同机同用户、无容器，真正的隔离留 **P3-E8**；`fork` 炸弹、文件写入、C 扩展长跑等残余风险在 SPEC 列明，不假装已解决

### M4b 静态检查器（`static_check.py`，纯函数 + 规则表）

**检查只做在 AST 上，不执行代码**（与 `api.parse_meta` 同一路线——编辑器要标注、父侧要不 spawn 就能判，两处都不能跑用户代码）。结果形态 `Finding{rule, line, severity, message, snippet}`，按 `(line, rule)` 排序去重；`severity` 只有 `error` / `warning`：**`error` 命中即拒绝执行**（M4c 在回测提交前强制 `error=0`），`warning` 照跑但显示。`check_source(source) -> list[Finding]` **从不抛异常**（语法错也是一条 finding——抛异常的话编辑器就没得标）。

规则表结构 `Rule(id, severity, node_types, message, match)`：`match(node, ctx) -> str | None` 返回**详情串**（`None` = 未命中），`id` / `severity` / `line` / `snippet` 由单遍遍历器统一组装；`message` 是规则自身的一句话说明（文档与规则表自检用）。遍历器按 `type(node)` 精确分发，**新增规则不改执行器**——同一条规则拆成多条 `Rule` 是允许的（R3 的两个档、R1 的两个臂都这么写），`Finding.rule` 仍是同一个规则号。

| 规则 | 级别 | 判据 | 要点 |
|---|---|---|---|
| R0 语法 | error | `ast.parse` 抛 `SyntaxError` | 此后 R1–R5 全不跑 |
| R1 数据绕行 | error | ① `Import` / `ImportFrom` 的顶层模块名 ∉ 白名单（相对导入同罪）② 出现禁用名 `__import__` / `open` / `eval` / `exec` / `compile` / `importlib` / `__builtins__` | 白名单**读运行时那一份**（`api.ALLOWED_MODULES`），不复制；报错列出可用模块 |
| R2 未来函数 | error | ① `.shift(<负整数>)`（位置实参或 `periods=`）② `.bfill(...)` / `.backfill(...)` ③ `fillna(method="bfill"/"backfill")` | `ffill` / `pad` 与 `.shift(正数)` **不拦**（只用过去值） |
| R3 显式未来索引 | error / warning | 仅当基对象是 **`ctx.history` / `ctx.events` / `ctx.new_events`** 且下标**不是切片**：**error** = 下标里出现 `ctx.index + 正数` 或 `len(ctx.history) + 正数`（明说「当前 + n」）；**warning** = 同对象上「其它名字 + 正数」（如 `ctx.history[i+1]`） | `ctx` 名取 `on_bar` 第一个形参名（改名也认）。**覆盖边界如实记录**：自有列表上的 `closes[i+1]` 不报（静态无数据流，分不清它来自 `ctx.history` 还是自己攒的列表）；helper 里另起名字接 `ctx` 同样漏 |
| R4 声明 | error / warning | error = `parse_meta` 的拒绝（`PARAMS` 非字面量 / 类型不合规 / `USES_EVENTS` 非字面量 / `validate_params` 不是函数）；warning = `USES_EVENTS` 声明值与代码里是否出现 `ctx.events` / `ctx.new_events` 不一致 | warning 要说清后果（缺省窗口与 PIT 对比按声明的来） |
| R5 结构 | error | ① 无模块级 `on_bar` ② `async def on_bar` ③ 形参不是 `(ctx)` 或 `(ctx, p)`（含无默认值的关键字-only）④ `on_bar` 自身（不含嵌套函数）`return` 值为常量或裸 `return`——**非生成器时**才判（体内有 `yield` 豁免）⑤ `return Signal(...)`（要写成 `[Signal(...)]`） | ③ 与 `api._check_signature` 同判据，配一致性测试防漂移 |

- **`ffill` 不拦**（2026-10-08 拍板）：前向填充只用过去值，不是未来函数；PRD 亦只写 `shift(-n)` / `bfill`。原「`bfill` / `ffill` 并列」就此订正
- **R3 的口径（2026-10-08 拍板：双档）**：R3 拦不住真实泄漏（未来在物理上不可达），价值是可读报错 + 拦 off-by-one；代价不对称（`error` 档会让合法策略跑不起来，运行前强制 `error=0`），故 `error` 只留给「明说当前 + n」的写法，其余降 warning
- **不误报是硬要求**：`[-1]`、`i-1`、`ctx.history[a+1:b+1]` 切片、`for i in ...: closes[i+1]` 相邻比较、字符串/注释里出现的 `shift(-1)`、变量名叫 `bfill`、`fillna(method="ffill")`、生成器 `on_bar` 的裸 `return`、嵌套函数里的 `return 1` 一律**零命中**——负样例进单测，另有一条**元测试**断言规则表里每条规则都在样例矩阵里有正/负样例
- **行号归属（M4c 的编辑器标注直接吃这个）**：节点规则指到命中节点那一行；R4 的「声明形态」指到 `parse_meta` 报的那一行；R4 的「读了事件却没声明」指到**第一处**事件读取行（不刷屏）、「声明了却不读」指到 `USES_EVENTS` 那一行
- **检查与执行隔离**：检查在 API 进程内纯函数跑（源码不执行）；运行前对**库里的源码**再跑一次，`error` 即 422 并带 findings
- **它不是安全边界**（同 §5 开篇）：禁用名靠 AST 命中，`getattr` / `type` / `object` 仍在运行时白名单里（M4a 已如实记录的残余风险），本轮不加码、不声称

### 模板库（5 个，M4b）

双均线 / 事件驱动（这两个是现有内置策略的**源码等价版**）/ 唐奇安突破 / RSI 反转 / 放量突破，放服务端 `app/strategy/templates/`，是唯一真源，也进测试。数据结构 `Template{key, title, summary, source, builtin}`——`builtin` 非空即「有内置等价物」，等价性测试**遍历 `TEMPLATES` 自动配对**（并断言恰好 2 个带 `builtin`）：新增模板时忘记配测试会直接红。

- **等价性测试**：模板经沙箱跑出的报告与注册表策略**逐点一致**（离线，`conftest` 的合成 Parquet 小窗口；去掉 `strategy` / `strategy_kind` / `strategy_name` 三个身份键后整份报告相等）——一份证据同时证明「用户 API 表达力等价」与「沙箱执行没有偷偷改口径」
- **3 个新模板（无内置对应物）**：证据 = 检查器零命中 + 沙箱跑通 + 各配一条**合成触发序列**断言至少 1 笔成交（证明模板不是死的）。两处口径要点：唐奇安突破的 level 取**前 n 根**（`ctx.history[-n-1:-1]`）的 `high` 最大值、与**当前收盘**比较（避免「含当根」的口径含糊）；RSI 用纯 Python 实现 Wilder RSI（没有 numpy，也不打算引）
- **模板头注释固定三段**：① 这是交给沙箱的源码文本、**不是可导入模块**；② `Signal` / `Side` 已预注入，无需也不能 import；③ **我们故意不给 pandas**——`shift` / `bfill` 正是前视泄漏的常见来源（产品立场，与 M9 教程同源）
- **模板不进内置注册表**：唐奇安 / RSI / 放量突破只作为「用户策略模板」存在；再写一份内置类 = 两处真源、两处维护（SPEC 只要求模板）
- **装载 API（M4c 直接用）**：`TEMPLATES` 元组（`key` / `title` / `summary` / `source` / `builtin`）、`get_template(key) -> Template | None`、`template_source(key)`（未知 key 抛 `KeyError`，HTTP 层自己映射 404）。源码一律**读文本**，不作模块导入——`Signal` / `ctx` 都是沙箱注入的，`import` 这些文件只会得到未定义的全局名

### 持久化与契约（M4c）

> 切片：**M4c-1** Monaco spike + 持久化 + 端点 + 闸门（本段）→ **M4c-2** design brief + 前端工作台 + 证据。

| 表 | 要点 |
|---|---|
| `strategies` | `id UUID PK` / `user_id FK→users ON DELETE CASCADE` / `name TEXT`（去首尾空白后 1–60 字符、不得含控制字符，超限 422） / `code TEXT`（≤ 64KB，超限 422） / `params JSONB`（最近一次保存的参数值） / `created_at` / `updated_at`（PUT 时刷新）；`UNIQUE(user_id, name)` → 撞名 **409** |
| `backtest_runs` | **增列** `strategy_id UUID NULL` + `code_sha256 TEXT NULL`——`CREATE TABLE IF NOT EXISTS` 不会给既有表加列，须 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（沿用 M1a 幂等 DDL 机制）。`strategy_id` **不设外键**：策略删了记录仍在（报告 JSONB 自足） |

- **运行契约（2026-10-08 拍板）：先保存才能跑**。回测记录存 `strategy_id` + `code_sha256`（**当次**运行的源码 sha256）；「我的回测」重开与 M7 复现成立，改码后 hash 不匹配即如实标注「已非当次运行的代码」——**两个 hash 都由服务端给出、由前端比对**（`GET /backtest/runs/{id}` 给当次的、`GET /strategies/{id}` 给**当前**的），服务端不造派生布尔值。不做内联源码试跑
- **保存不因 findings 被拒**（存的是草稿，响应随带 findings），**运行前强制 `error`=0**——闸门设在回测提交前（PRD 口径），不在保存时
- `POST /api/v1/backtest` 增量（**additive**，信封 `{run_id, report}` 与报告结构不动）：请求体增 `strategy_id`；`strategy="user"` 时必带它，**缺一或与非 user 策略同给即 422**。user 分支**不走 `resolve_window` + 同步 `build_report`** 那条路，改走 `run_user_strategy`（沙箱子进程）；`compare_pit` 判据由「策略名 = event_driven」泛化为 **`USES_EVENTS`**（用户策略取自源码声明）；`resolve_window` 传 `uses_events=`。报告 `meta` 的 `strategy_kind`（`builtin|user`）与 `strategy_name` 是 M4a 已落地的既有字段，此处只记前端消费
- **闸门顺序（HTTP 侧，环环不可省）**：鉴权 → 请求体（pydantic）→ 互斥校验 → 归属 404 → **静态检查 `error`=0** → 参数校验 → 沙箱执行

| 情形 | 码 | 响应体 |
|---|---|---|
| 静态检查有 `error` | **422** | `{detail, findings}`（`findings` 与 `Finding.to_dict()` 同形，编辑器直接标注） |
| 参数校验不过（键 / 类型 / 值域 / 跨字段） | **422** | `{detail}` |
| 运行期被拒（`StrategyRejected`：运行期异常 / 返回值形态 / 装载失败） | **422** | `{detail, line}` |
| 沙箱终止（`SandboxError`：`cpu` / `wall` / `memory` / `output` / `crash`） | **400** | `{detail, kind}`（与既有 `BacktestError → 400` 同档：请求合法，是这次运行没能完成） |
| 撞名 / 越权·不存在 / 未登录 | 409 / 404 / 401 | 同 M1c |

**端点**（`app/api/strategies.py`，全部 `require_user` + 归属 404，同 M1c 口径）：

| 端点 | 行为 |
|---|---|
| `GET /api/v1/strategies` | 本人全部，`ORDER BY updated_at DESC`：`[{id, name, created_at, updated_at}]`（摘要**不带 code**——列表不为每行拖一份源码） |
| `POST /api/v1/strategies` | `{name, code, params?}` → **201** `{id, name, code, params, code_sha256, created_at, updated_at, findings}`（草稿也 201）；撞名 409 |
| `GET /api/v1/strategies/{id}` | 单条：`{id, name, code, params, code_sha256（当前代码的 hash）, created_at, updated_at}`；非 UUID 422；越权 / 不存在 404 |
| `PUT /api/v1/strategies/{id}` | `{name?, code?, params?}` → 200（形状同 POST，含 findings）；404 同上 |
| `DELETE /api/v1/strategies/{id}` | 200 `{id, deleted: true}`；不存在 404；已跑过的回测记录不受影响（`strategy_id` 悬空即可，不级联删） |
| `POST /api/v1/strategies/check` | `{code}` → 200 **`{findings, meta}`**；`meta = {params: {名: {type, default, min, max, label}}, uses_events}`，**`PARAMS` 解析失败时 `meta` 为 null**（问题由 R4 findings 承载）。纯函数：不落库、不 spawn、不执行代码 |
| `GET /api/v1/strategies/templates` | `[{key, title, summary, builtin, source}]`（5 个，服务端为唯一真源） |

- **路由顺序**：`/check` 与 `/templates` 必须声明在 `/{id}` **之前**，否则会被当成 UUID 解析（422）
- **回测记录侧增量**：`GET /backtest/runs` 摘要增 `strategy_name`（取 `report.meta.strategy_name`，内置策略为 null）——列表里用户策略显示「用户策略 · 名称」；`GET /backtest/runs/{id}` 增返 `strategy_id` / `code_sha256`（供前端比对，见上）

### 前端（M4c）

- 新开工作台 `app/(app)/strategies/`：左列表（我的策略 + 模板，模板「另存为我的」→ 内联输入名字）｜ 右编辑器 + 参数表单 + 检查结果面板 + 「保存并运行」；`?id=` 深链直达某条策略
- 编辑器 **Monaco**，**走本地资源**（**2026-10-08 spike 已执行、五项判据全过、回落条件未触发，M4c-2 已按此落地**）：加 `monaco-editor` 依赖 + `scripts/sync-monaco.mjs` 把 `min/vs` 拷到 `public/monaco/vs`（`pnpm dev` / `pnpm build` **前置自动跑**、按版本戳幂等、`public/monaco/` 与依赖都不入库），页面用官方 **AMD loader** 按需加载——资产**不经过打包器**、next 配置零改动、**零外部请求**（spike 把非本机请求全部 abort，编辑器照常工作）。**实测**：磁盘 24.4MB（0.57 是分块布局，含全语言与四个语言 worker；不入库）；`/strategies` 路由包 **3.26 kB**（Monaco 不进 bundle）；编辑器实际加载 19 个文件 / **线上 1.17MB**（gzip，解压 1.98MB）
  - 两条实现期事实：① 0.57 的 `editor.main` **自己赋值** `self.MonacoEnvironment`（blob worker + 同目录 `assets/*.worker-*.js`），预设 `getWorkerUrl` 是无效代码；② `eslint.config.mjs` 必须忽略 `public/monaco/**`，否则 vendored 压缩产物进 lint（实测 25894 条）
  - 依赖用 **pnpm** 装（`pnpm add monaco-editor`）：仓库是 pnpm 工程（`pnpm-lock.yaml` + `node_modules/.pnpm`），用 `npm install` 会撞 arborist 的 `Link.matches` 报错
- findings → 编辑器行号标注（`setModelMarkers`，error / warning 两档、整行波浪线）；参数表单由 `PARAMS` 生成（bool → 开关，数字 → 输入框 + min/max 提示），前端只做即时反馈，**值域规则不重复造**（后端 M4 起已校验）
- **标注与表单同源**：编辑内容防抖后打 `POST /strategies/check`，一次拿到 `{findings, meta}`——面板列表与编辑器标注吃同一份 findings，参数表单的 schema 来自同一次响应的 `meta`（不会出现「标注说改了、表单还是旧的」）
- 「保存并运行」= 先 `PUT` 存草稿（回显 findings）→ 有 `error` 就停在面板、不提交 → 否则 `POST /backtest`（带 `strategy_id` + 表单参数值）→ 跳 `/backtest?run=<id>` 复用整套报告 UI。运行条**只有**标的 + 区间（可留空）+ 「PIT / 非 PIT 对比」开关（按 `USES_EVENTS` 出现），费用与滑点走缺省（要调去回测页）
- `/space?tab=strategies` 从占位改为列表 + 入口；**页头加「策略」一项**（原设计只有「对话 / 回测 / 个人空间」三项，工作台只能从个人空间绕进去——用户 2026-10-09 反馈后补；窄屏下给 nav 加 `whitespace-nowrap`，否则中文会逐字断行把「对话」拆成两行）；「我的回测」列表的策略列显示策略名
- **回测页仍只跑内置策略**（2026-10-08 拍板）：`/backtest?run=` 打开一条用户策略记录时报告照常渲染、表单只回填标的 / 区间 / 费用，并出提示条——当次用的策略名 + 代码是否已变（比对两个 hash）+ 「去工作台重跑」入口。**`lib/backtest-form.ts` 的字面量联合必须显式处置 `"user"`**：`formFromRequest` 里的 `PARAMS[strategy]` 查表会 TypeError（打开一条用户策略的旧回测即触发）
- **主题与全站 token 同源**（M4c-2 落地）：自定义 `quantsage-light` / `quantsage-dark` 两个 Monaco 主题，底/前景/行号/选区全部取自既有 token（`vs-dark` 的 `#1E1E1E` 与面板 `#131722` 并排一眼不同套）；**`editorWarning` 必须覆盖**——Monaco 默认的 warning 是**绿色**，与本项目语义冲突。语法高亮（关键字 / 字符串 / 数字 / 注释）是**第三层色**，与 UI 语义 token 和图表序列色并列：代码里的琥珀色**不代表警告**
- **新增一支语义色 `--warn`**（M4c 唯一一处动全站配色的改动）：浅 `#B45309` / 深 `#E0A030`，对比度实测 5.02（面板底）/ 4.68（页面底）/ 7.88（深色面板底），全过 AA。理由：此前没有 warning 色，检查面板若借 `--destructive`，用户看到一屏同色**分不出哪几条会拦住运行**（error 才拦），而那正是本页的核心交互
- 开工前出 **design brief**（沿用 T6 口径，与 P2-M4c 同目录；**已出并落地**，含两处如实记录的偏离）

**验收**：① 在线写策略可成功回测（模板与自写各一条端到端证据）；② 坏代码不拖垮服务（**HTTP 层**故障注入矩阵：死循环 / 大内存 / 语法错 / 缺 `on_bar` / 超长输出 / 数据绕行——各给明确错误码，且每条之后 `/health` 200、正常策略仍跑通）；③ 注入含 `shift(-1)` / `bfill` 的策略被拦截并提示行号（编辑器标注截图 + API 422 findings）。**分工**：③ 的检查器本体与 CLI/矩阵证据属 **M4b**，编辑器标注与回测提交前的 422 闸门属 **M4c**。

**本轮不做**：并发队列与批量回测（M5）；在线装包；策略分享/市场；用户策略调 RAG（M8）；容器级隔离（P3-E8）；编辑器高级能力（补全 / 跳转 / 实时 lint 之外的特性）。

## 6. M5 回测增强

> **切片（2026-10-09 拍板）**：**M5a 规则与地基**（A 股规则接引擎 + 横截面 + 名称字典 + 等权基准）→ **M5b 批量 / 网格 / 过拟合检验** → **M5c 因子分析**。M5a 是唯一动既有引擎的一段，必须独占并全量回归（见 §12）；b、c 不动既有链路。
> 同批拍板的三条：批量与网格的进度走 **SSE 流式逐格推送**（不建队列，P3-E1 再升级）；网格 / 批量结果**落汇总（`optimization_runs`）、不落完整报告**；涨跌停判定**从 raw 前收自算**。
> 规划与实测底稿：`docs/private/Pn-n/P2-Mn/P2-M5.md`。

### M5a 规则与地基

**横截面数据能力**（`cross-section`，选股与分层的前置）：
- `GET /api/v1/market/cross-section`：某交易日的全市场截面（收盘 / 涨跌幅 / 成交额 / 换手率）+ 排序 + 筛选 + 分页，缺省日期取 `freshness.latest_trade_date`；不落缓存。实测全市场排序 **6–12ms**（5,572 只有价）
- **换手率的字段级事实（2026-10-09 实测）**：`turnover_pct` 在源侧已停更——2026-07 及以前覆盖 100%，**2026-08 起 55%**、**2026-09 起 0%**（116,674 行全空）。端点照常返回该字段、值如实为 `null`，**UI 不渲染这一列**。这不是本项目的缺陷，交付文档须写明

**名称字典**（`symbol → name`）：
- 来源是事件语料的 `stocks` 列（`[{code, name, reason}]`），**同一份小石语料，不是第二数据源**；落 `data/naming/symbol_names.parquet`，随 ETL 刷新
- **覆盖率 5,610 / 5,799 = 96.7%**（189 只无名称的如实留空；2026 片 5,592 只里 44 只无名）。分母取 `dc.BARS_VIEW`（含 qfq / hfq / raw 三种口径的并集）——**与 v1.16 记的 96.7% 是同一个口径**，`5,608 vs 5,610` 的差值来自归一化是否剥 `.SH`/`.SZ` 后缀。`stocks` 归一化后能解析出 5,809 个六位码，其中 **199 个落在行情宇宙外**（ETF / 港美股 / 已退市），由过滤 ② 挡掉。全量重建实测 **约 1.3s**（端到端：宇宙查询 + 30.4 万行解析 + 落盘；纯解析段 435ms）
  > **订正沿革**：规划期一度改成「99.2%」，是**把 2026 分片的「无名 44 只」放进了全期分母**——量错了，已改正。
- **过滤规则（三重，防外部代码注入）**：① 该 `code` 须同时出现在**同一事件的 `symbols` 数组**里；② 须落在**行情宇宙**内；③ 归一化后取**众数**。归档里 `stocks` 混着港股与美股（`{code:"005930.KS",name:"SK海力士"}`），甚至**外部代码与 A 股撞号**（`000660` 既是 A 股 `*ST南华` 又是 SK海力士的韩股码）——① 挡后缀型，② 挡宇宙外，③ 挡撞号型的错配名
- **归一化**：全角→半角（NFKC）、去全部空白、剥 `N` / `C`（新股）与 `XD` / `XR` / `DR`（除权）前缀——**前缀只在紧跟汉字时才剥**（否则 `NIO` 会变成 `IO`）
- **一名多写实测**：原始解析出 11 只，**三重过滤后仍有多个别名的只剩 5 只**——`688111`（华虹宏力被挡）、`600876` 农夫山泉、`600778` 美光科技、`000830` Samsung 这 4 例的错配名连 `symbols` 都没进，直接消失；`920438` XD戈碧迦与 `000002` 万  科Ａ 归一化后与正名合并。存留下来的 5 只是：`600800`（渤海化学 40 / 新华传媒 1）、`688347`（华虹公司 104 / 华虹宏力 5，**真实改名**）、`301689`（电科思仪 64 / 电科思 5）、`920229`（世纪数码 43 / 世纪 1）、`301699`（洛轴 39 / 洛轴股份 12）。**众数选名 11 例中 10 例与人工核对一致**，唯一残留是 `301699`（众数取到新股 `C` 前缀期的截断名「洛轴」）→ 字典**带别名集合与冲突标记**，UI 可展开看全部别名，**对外不宣称 100% 准确**；逐条对照表见 `tests/test_data_naming.py` 的 `_REAL_CONFLICTS` 与 `_CORRECT_NAMES`
- **名称带时点**：改名在语料里有 `available_at`，字典保留 `first_seen` / `last_seen`，因而不是静态的——列表与详情页的日期口径跟着 `freshness` 走。这是 PIT 立场在展示层的延伸，不是装饰

**A 股交易规则内建**（`a_share_rules.py`，纯函数 + 拒绝码）：
- T+1：当日买入不可当日卖出；涨跌停：涨停价买单（一字板）/ 跌停价卖单不可成交；ST 与板块涨跌停幅差异（**ST 5% / 主板 10% / 创业板·科创板 20% / 北交所 30%**）；最小手数 100 股；停牌不可成交（`is_suspended`）；印花税卖出 0.05%（P1 已有）
- **涨跌停价口径（拍板）**：`round(前收_raw × (1 ± 幅度), 2)`，**从 raw 序列自算**——实测 `change_pct` 与 raw 自算日收益有 **0.3% 偏差**（10,785 / 360 万行，集中在除权日）+ **4.8% 缺失**，不可作依据。一字板判据＝买单遇 `open` 与 `low` 双双贴住涨停价（当日未开板）；卖单镜像
- **ST 判定只能来自名称字典**（`ST` / `*ST` 前缀，实测 231 只），且**只有事件语料覆盖期内有名称**——历史区间一律按板块默认幅度并如实标注
- **三条实测边界（如实记录，不假装在跑）**：① **停牌字段几乎不触发**——`is_suspended=True` 全库仅 **7 行**（002336 / 002500，均 2020-06）；无价 bar（`no_turnover_observed` 整行空）已在查询层剔除，等价于「顺延到下一根可交易 bar」；② **T+1 由结构满足**——信号在 bar 收盘生成、next bar 开盘成交，同一根 bar 不可能既买又卖，规则模块把它写成**显式可测的守卫**而非新功能；③ **除权日会漏判（不误判）**——raw 前收与交易所除权参考价不等，识别需比对 qfq/raw 比值跳变，成本高收益低，**不做**，写进已知边界
- **降级口径**：缺 raw 序列时跳过涨跌停判定，并在报告 `meta` 里标注（**不静默按 qfq 猜**）
- 成交口径与 P1 一致：信号 bar 收盘生成、next bar 开盘成交
- 每笔拒单带原因码（`REJECT_T1` / `REJECT_LIMIT_UP` / `REJECT_LIMIT_DOWN` / `REJECT_SUSPENDED` / `REJECT_LOT`），UI 可见拒单原因

**基准对比**（全市场等权组合代理）：
- 指数不覆盖已由 M2a 核实（Manifest 无 index 数据集，`000300` / `399xxx` 查无此码）→ 走**全市场等权组合代理**，口径在报告与 UI 如实标注，**不写成「沪深 300」**
- **剔除口径（2026-10-09 实测）**：聚合等权日收益前剔除 `|change_pct| > 30%` 的样本——2026 年有 **102 行**（新股首日 / 复牌，raw 极值 **+1942%**），单只即可把当日等权均值拉高 **0.36pp**（日波动量级约 1%）。这些样本只可能是「上市首日 / 复牌 / 数据异常」，且在回测里不可投资；**剔除条数写进报告**
- 实测性能：全市场等权日频曲线（全期聚合）**35ms**；2026 日度参与样本 5,188–5,556 只

**落地物（2026-10-09，M5a 全段）**：
- 纯函数层：`backtest/a_share_rules.py`（板别幅度 / `LimitBand` / 一字判据 / 五码 / `REJECT_REASONS` 展示文案）、`data/naming.py`（解析 / 归一化 / 三重过滤 / 众数 / as-of / 落盘读取）、`backtest/benchmark.py`
- 引擎接线：`engine._limit_bands`（**raw 前收**算价，三处降级：板别未知 / 缺 raw 序列 / ST 取回测终点的名字）+ 撮合前 `order_reject`；`BacktestResult.a_share_rules` 把生效情况带出
- 端点：`GET /market/cross-section`（`date` 缺省取**最后有价交易日**、`adjust`、`sort` 走**白名单** `{change_pct, amount, close, turnover_pct, symbol}`、`order`、`q` = 代码前缀或名称子串、`limit` 1–500、`offset`；排序带 `NULLS LAST`）与 `GET /market/symbols?q=&limit=`（**不依赖行情**——查不到当日 K 线不该搜不出标的；缺字典返空列表）
- 报告新增四块：`rejects`（`count` / `by_code` / `items[{date, side, code, reason, signal_reason}]`，非规则原因 `code` 为 null）、`meta.benchmark`（`kind`/`note`/`exclude_rule`/`excluded`/`sample_days`/`avg_samples`/`total_return`）、`meta.a_share_rules`（`limit_check`/`reason`/`is_st`/`limit_pct`）、`equity_curve[].market`
- 字典落 `data/naming/symbol_names.parquet`；`runner.refresh_names` 挂在 `run()` 与 `rematerialize()` 收尾，状态进台账 `RunReport.names`（**失败不拖垮 ETL**，只记「失败：…」）
- CLI：`scripts/build_names.py`（重建 / `--dry-run` 核对 / `--show` 查别名）、`scripts/limit_reject_evidence.py`（真实数据一字板取证）
- **旧报告兼容（界面验证抓到的真问题）**：新增的四块在 M5a 之前落库的记录里**都不存在**——前端一律标为可选、按缺失处理，**不补零、不报错、不崩页**。这是「存过库的 JSON 是历史事实，新代码不能假设它长成今天的形状」
- 证据：`logs/m5a/{limit.md, naming.md, benchmark.md, ui.md, ui.json, *.png}` + `docs/images/backtest-benchmark.png`

### M5b 批量 / 网格 / 过拟合检验

> **规划期实测订正了两处 SPEC 自己的读数**（2026-10-09，本机 8 核；探针与读数见 `P2-M5b.md` F1–F6）。
> ① **v1.18 记的「5×5 网格 ≈ 9s 串行 / ≈ 5s 并发 2」不成立**——实测串行 **7.7s**（短窗 65 bars）/ **12.9s**（全期 1,636 bars），并发 2 只有 **0.83 / 0.90**，且**进程池与线程池几乎一样**（0.86 / 0.91）⇒ 瓶颈**不是 GIL**，而是 **DuckDB 默认 `threads=8` 已吃满全部核心**，再并发只是在同一批核心上分时。**并发上限 2 保留，但价值重新定位**：不是提速，而是 ① 单格失败互不牵连 ② 一个网格不独占服务 ③ P3-E1 异步队列的前置形态——**对外不得宣称接近翻倍**。
> ② 单格 250–500ms 里约 **2/3 是重复取数**（`dc.bars(raw)` 的全史扫描 **164ms** 是最粗的一笔——M5a 为算涨跌停价刻意不带 `start`；qfq 窗口读 37ms、事件 30ms，真实计算只 ~45ms）。实测按 `(标的, 区间, 口径)` 记忆后 **25 格可降到 1.5–1.9s（≈5×）**且 `metrics` 逐格相等。**本轮不做**——它本质是**数据层**优化（受益面是整个 API 而不只是网格），该单独立项，不塞进 M5b；如实记进已知边界。

**端点（两个，均 SSE）**

`POST /api/v1/optimize/grid`：

```json
{
  "strategy": "ma_cross",            // 或 "user"（须带 strategy_id，跑库里的源码）
  "strategy_id": null,
  "symbol": "600519",
  "start": null, "end": null,
  "costs": {"fees": true, "slippage": true, "slippage_bps": 5.0},
  "pit_mode": "pit",                 // 只收 pit / non_pit；"both" 在本端点 422（工作量翻倍且无意义）
  "params": {"slow": 20},            // 基座：不扫的参数
  "axes": [                          // 1–2 条
    {"param": "fast", "values": [3, 5, 8, 10, 12]},
    {"param": "slow", "values": [15, 20, 30, 40, 60]}
  ]
}
```

`POST /api/v1/optimize/batch`：`{symbols[], strategies[{strategy, strategy_id?, params}], start, end, costs, pit_mode}`。

- **轴数 1–2**（2 轴出热力图、1 轴出折线）；**>2 一律 422**，不做「固定其余、切 2D 片」的隐式降维——那是「静默跳过」的另一种形态
- **展开后的每一格都过参数校验**（内置 `strategies.validate_params` / 用户 `params.validate_params`），任一格非法即**整单 422**，错误带**哪一格、哪个参数、为什么**（如 `fast=20 / slow=10`）；`axes[].param` 须属于该策略参数集且**与基座 `params` 不撞键**；轴值去重后 ≥2 且原样不得重复
- 总格数 **2–100**（网格 = 轴值个数之积；批量 = 标的数 × 策略数）；批量标的 1–20 去重六位码、策略 1–10
- 用户策略的**归属 / 静态检查 `error`=0 / 参数 schema 只在开跑前做一次**，不是逐格

**进度（SSE 帧）**

| 帧 | 载荷 |
|---|---|
| `start` | `{kind, total, symbol(s), strategy(ies), window, axes}` |
| `cell` | `{index, symbol, strategy, params, ok, duration_ms, metrics{…}, moments{skew,kurt,n}, window{start,end,bars}}`；失败格以 `error{kind, message}` 换掉 `metrics` / `moments` |
| `done` | `{run_id, cells_ok, cells_failed, best_index, overfit{…}, duration_s}` |
| `error` | 只留给流中途整体崩（请求级错误在流开始前就是 422，与 chat 同姿态） |

- **断线即断、不续传**，刷新等于重跑——**停止派发新格，已在跑的最多 2 格自然跑完且不落库**（不取消 in-flight：`to_thread` 本就取消不了，硬取消还会在沙箱里漏子进程）。不建队列（P3-E1 再升级异步任务队列）
- **并发上限 2 = 进程级全局 Semaphore**（不是每请求一把）——SPEC 风险表写的是「网格把服务打满」，全局才是那道闸；两个用户各跑一个 25 格网格时，每请求一把会变成 4 格同时在跑
- **单格隔离**：每格独立 try/except，失败格记 `kind` + `message`，其余照跑；**全失败也落库**（是事实，不是错误）

**「单格」的唯一定义**（防两条口径漂移）：内置走 `build_report(config)`、用户策略走 `run_user_strategy(...)` 的同一对字段；**都不跑 `compare_pit`**、都用**同一次** `resolve_window` 的结果（批量按 `(标的, 策略)` 分组解析一次——它内部是全史扫描，逐格解析是纯开销）。断言：某格 `metrics` 与直接 `POST /backtest` 同配置报告**逐键相等**。

**最优格判据**：**夏普最大**（`None` 排最后，并列按 `total_return` 破平），**固定不可配**——DSR 的定义就是「在被选中者上做选择偏差校正」，「被选中」必须与选取判据同源；换目标却不换 DSR 口径会自相矛盾。

**结果留存**：`optimization_runs (id, user_id, created_at, request, summary)`，**不含净值曲线与逐笔**；`GET /api/v1/optimize/runs`（摘要，只抽 JSONB 子集）与 `GET /api/v1/optimize/runs/{run_id}`（重开，越权与不存在同 404）。要看某一格的完整报告，从热力图点进去**重跑**——**复用既有 `POST /backtest`，不新增端点**（单格 70–350ms 内置 / 400–500ms 沙箱）。

**`summary` 形状**：`{kind, cells[], best_index, overfit{}, window, costs, pit_mode, adjust, duration_s}`；每格 = `params → metrics` **+ 三个必要字段**（`moments` / `window` / `duration_ms`）——`moments` 是重开时 DSR 能自证的前提，`window` 在批量里**逐格不同**（事件策略的起点、各标的的末根 bar 都不同），不记就分不清那格跑的是哪一段。

**过拟合检验（`overfit.py`，纯函数；不引 numpy、不引第三方回测检验库）**

```
SR₀ = √V[{SRₙ}] · [ (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]     γ = 0.5772156649（Euler–Mascheroni）
DSR = Φ[ (SR − SR₀)·√(T−1) / √(1 − γ₃·SR + ((γ₄−1)/4)·SR²) ]
```

四条口径写进模块 docstring 并逐条钉在测试里：

1. **SR 一律「每期」口径**（日频、未年化）——`metrics.sharpe` 是年化的，要 `/√252` 才进公式；全网格的 `V[{SRₙ}]` 同口径（混用会静默差 16 倍）
2. **N 取全网格格数，不做试验间相关性校正**——相邻参数高度相关、真实独立试验数更少，用格数会把 `SR₀` 抬高、DSR 更**保守**；如实标注，不假装是精确值
3. **γ₃ / γ₄ 取最优格自身的日收益矩**（总体矩；γ₄ 是**原始峰度**不是超额峰度，正态时 γ₄=3）
4. **退化分支返 `None` + `reason`，绝不硬凑**：有效格 <2 / 最优格 sharpe 为 `None` / 观测数 <2；方差为 0 时 `SR₀=0`，仍出数但标注 `sr_variance: 0.0`

**已知边界（如实记录，不假装在做）**：① 本轮**不做取数复用**（见开头 ②，25 格就是 7.7–12.9s，靠 SSE 逐格进度如实呈现）；② 并发 2 换不来速度（0.83 / 0.90），它的价值是隔离与不独占；③ 网格的 DSR 用格数当试验数，是**保守**估计而非精确值。

**落地物（2026-10-09，M5b 全段）**：

- 纯函数层：`backtest/overfit.py`（`daily_returns` / `per_period_sharpe` / `moments` / `expected_max_sharpe` / `deflated_sharpe` / `grid_overfit` + `OVERFIT_REASONS`）、`backtest/batch.py` 的 `cartesian` / `grid_cells` / `pick_best`
- 执行器：`backtest/batch.py` 的 `run_cells`（全局 `Semaphore(2)`、逐格 `try/except`、`_WindowCache` 按 `(标的, 策略)` 复用 `resolve_window`——它内部是全史扫描，逐格解析是纯开销）
- 端点：`api/optimize.py` 四端点；`BatchRequestError` → **422** 的处理器在 `main.py` 注册（**流开始前**判死，与 chat 同约束）
- 表：`optimization_runs (id, user_id, created_at, request, summary)` + `(user_id, created_at DESC)` 索引；`Database` 三个方法 + `FakeDatabase` 同形替身
- CLI：`scripts/run_grid.py`（真实数据 5×5 + 并发探针 + scipy 独立对照，退出码 0/1）
- **两处实施期定死的契约细节**：① 用户策略的**参数归一器返回填满后的字典**（`PARAMS` 的缺省值由此进入每一格），内置策略原样返回、缺省由引擎 `from_params` 补——两条路径的差别收在一个 `normalize` 签名后面，网格不各写一套；② `summary` 增 `kind` / `cells_total` / `cells_ok` 三键（列表页走 `summary - 'cells'` 投影，没有计数就只能把每格矩阵整个拉回来再数）
- 证据：`logs/m5b/{grid.md, concurrency.md, deflated_sharpe.md, grid.json}`

**前端（M5b-2，2026-10-09）**：

- `app/(app)/optimize/page.tsx`（模式页签 / 表单 / 逐格进度 / 结果区 / 最近的优化）、`components/optimize/` 六个组件（表单 / 热力图 / 分布 / DSR 卡 / 批量表 / 列表）、`components/space/optimizations-panel.tsx`（`/space` 第 5 个页签）
- 纯函数层：`lib/optimize-form.ts`（表单 ↔ 请求体 + 边界校验，与后端 `batch.grid_cells` **逐条对应**）、`lib/optimize-matrix.ts`（cells → 热力图 / 分布 / 表格的数据）、`lib/sse.ts` 增 `streamPost`（**复用既有 `parseFrames`**，`streamChat` 一行未动）
- **色阶 `chart-theme.diverge`（7 档，浅深各一套）**：夏普有正负 ⇒ diverging 而非 sequential。**候选是算出来的不是挑出来的**——本项目既有的 A 股红绿（`up`/`down`）跑 CVD 模拟（Machado 2009, severity 1.0）只有 **ΔE 7.6**（deutan），落在 6–8 的 floor 带（仅在有次级编码时合法）；**蓝↔红是 20.9 / 13.9**，远高于目标线 8。色相指派取**红 = 正夏普**，与全站的 A 股口径（红 = 上行）一致。四个臂各自过 `validateOrdinal`（单调 L / 相邻 ΔL ≥ 0.06 / 最浅步对底色 ≥ 2:1 / 单色相 ≤ 40°）全 PASS
- **一处为了「可见性等于真值」而改的实现**：参数被选作轴之后，它的基座输入框会收起，但表单 state 里那份默认值**还在**——第一版照发，后端按「轴参数与基座撞键」判 422（界面验证逮到）。改法是把「生效的基座参数」抽成 `effectiveBaseParams`，**校验与请求体共用同一个口径**；相应地，前端**不再逐条比「撞键」**——那在界面上已结构性地不可能，后端那条规则留给直接打 API 的人
- **一处无障碍硬要求**：连续色标必须有表格孪生（dataviz 反模式清单里唯一一条无障碍项），故「看表格」不是附加功能，是热力图组件的另一半
- 证据：`logs/m5b/{ui.md, ui.json, grid-light.png, grid-dark.png, grid-table.png, grid-rerun.png, batch.png, narrow.png}` + `docs/images/optimize-grid.png`；工具 `frontend/scripts/capture-m5b.mjs`

### M5c 因子分析

> 本段规格 2026-10-09 开工前细化并拍板（v1.25，用户已确认）。规划期实测底稿：`docs/private/Pn-n/P2-Mn/P2-M5c.md`（F1–F9）。

**两个因子源，同一条管道**（因子面板 → RankIC / ICIR → 分层 → 多空 → tear sheet）：

1. **事件信号因子**（`source=event`，主线）：`direction_norm ∈ {bullish, bearish}` 的有向事件，值取 `factor_value`。
   实测 **`corr(|factor_value|, factor_scores.score) = 1.0`**（即 `factor_value = ±score / 100`）⇒ 两者同源，**没有第二个变体可挑**，报告如实写明这一点。
   实测语料 93 天 / 307,704 行，有向事件 **57,403** 条（news 56,185 / person 740 / policy 425 / research 53；**announcement 0**——类别不等于方向，`direction_norm` 为 NULL 是设计而非缺失）；
   其中 **70.5% 未挂标的**（`symbols` 为空）⇒ 面板原料 **21,318** 个 (事件, 标的)；日度覆盖标的 min 2 / avg **199.8** / max 467
2. **价格反转 / 动量因子**（`source=price`，把 `待办` 的「横截面动量 / 反转」并进本段）：`±(close_t / close_{t−lookback} − 1)`，
   **`lookback = 20` 固定**，`direction ∈ {reversal, momentum}`；**全市场池**（实测中位 **5,515** 只）。
   实测（reversal，59 天）：RankIC **+0.0764** / ICIR +0.318 / **t ≈ 2.44**；多空 +43.1 bps/日但 **t ≈ 1.53**、日波动 2.16%
   ⇒ **样本期特定、日间不独立，「形似显著」不等于结论**，如实标注
3. **不做 lookback 扫描**（多档试参就是「挑口径」的入口）；两个因子**全报**，禁止只报好看的那一个

**因子日归属（PIT 口径，v1.25 拍板订正）**：`前一交易日 15:00 < available_at ≤ 当日 15:00` → 归当日。
这条规则**就是引擎 `EventFeed.advance` 的语义**（`available_at ≤ bar_cutoff(bar)` 才放行）；实现必须**复用** `backtest/events.py` 的
`bar_cutoff` / `MARKET_CLOSE`（**同一对象，不复制常量**），并以「同一批事件分别喂 `EventFeed` 与因子面板、**归属日逐条相等**」的断言钉死（同 M4b 的白名单同源口径）。
**本条订正 v1.18 起的「`available_at` 落在当日 15:00 之后的不进当日因子」**——照字面按事件日归日会丢弃 **48.7%** 的面板原料
（周末 / 节假日 / 盘后新闻整类出局；实测按事件排除 30,568 / 57,403 = **53.3%**，同日事件里 45.1% 落在 15:00 之后），
而可用窗口归属只丢 **3.3%**（703 条，全是行情末端之后）。实测池子：可用窗口 p50 **252** 只 vs 事件日 p50 86 只；有效日 54 vs 51

**前向收益（v1.25 拍板）**：`open(t+1) → open(t+2)`——与引擎「信号 bar 收盘生成、next bar 开盘成交」同口径。
实测与 `close(t)→close(t+1)` 结论一致（六种口径 RankIC 均值全在 ±0.01 内、t 全 < 0.55），故取更保守的一条。
缺价（停牌 / 无价 bar 已由查询层剔除）与前向收益缺失如实计数（实测池内缺价 **6.62%**）；`|前向收益| > 30%` 按 M5a 基准同规则剔除（实测池内仅 **1–4** 条）。

**分层与多空**：每日池内按因子值排序 → **等分 5 组**（`quantiles = 5` 固定）→ 组内等权 → 次开建仓、再次开平仓；
同日同标的多事件取 **mean** 聚合（实测仅 **17.3%** 的 (日,标的) 受聚合规则影响，故聚合不是敏感项）；
**多空 = Q5 − Q1，等权**，且**必须标注「A 股不可做空，多空价差是统计量、不是可交易组合」**（响应里 `long_short.tradable = false`）；不做市值加权（SPEC 已定只做等权）。
**分层组合不套单标的引擎**（引擎是 `BacktestConfig.symbol: str`，策略协议只看一只 `BarContext`；分层是日频截面再平衡的组合，套不进去）：
走 `factor/analysis.py` 内的独立小循环，**复用 `CostModel` 与 `metrics.py`**——费用与指标口径与回测同源。本条沿革见 v1.18 ⑥。

**费用与净值（v1.25 拍板）**：**毛 / 净双轨**（毛 = 零费用）。净曲线按 `CostModel` 的**费率口径**施加
（买 = 佣金万 2.5 + 滑点 5bps；卖 = 再 + 印花税 0.05%），**不含最低佣金 5 元**——它是「每笔订单 × 资金规模」的函数，本报告不预设资金规模。
`notes` 里的规模事实**按实际组数现算**——事件因子的分位组约 57 只、价格因子约 1,100 只，两者结论不同：100 万资金下前者每笔佣金 **4.39 元（被 5 元下限抬 1.1×）**、后者 **0.23 元（抬 22×）**，故「最低佣金是否主导」取决于资金规模与持仓分散度，报告不给单一结论。
**本窗口实测日均费用拖累**（毛曲线与净曲线之差）：**事件因子 19.3 bps / 日（年化 48.7%，换手 0.967）**、**价格反转 6.3 bps / 日（年化 15.9%，换手 0.327）**。
调仓只对**进出名单**收费（继续持有不调仓，权重漂移），首日按全新建仓（买 100%）；**换手率序列逐组入报告**（`groups[].turnover`，逐日给买 / 卖两条腿）。
> ⚠️ **v1.25 初稿那两处读数是错的**：「换手 96.6%」本身没错，但「费用拖累 13.4% → 58%」「佣金被抬 4.8×」出自规划期探针把**滑点写成 0.00005（5bps 应为 0.0005）**的笔误。M5c-1 实施期由报告曲线精确重算并订正为本条数字；探针结论（`P2-M5c.md` F7）同步留了沿革注。

**护栏**：`min_pool = 20`（实测该分支在可用窗口口径下**一次都不触发**——它是护栏不是过滤器，如实写进报告）；`start` / `end` 缺省 = 语料 ∩ 行情的自然窗口。

**端点**（v1.25 拍板：同步、公开、不落库、不走 SSE）：`GET /api/v1/factor/report`

| 参数 | 约束 | 缺省 |
|---|---|---|
| `source` | `event` \| `price` | `event` |
| `start` / `end` | `YYYY-MM-DD`，显式越界 → 400（同事件驱动窗口收口口径） | 自然窗口 |
| `direction` | `reversal` \| `momentum`（仅 `source=price` 有意义） | `reversal` |
| `costs` | `true` \| `false`（是否输出净曲线） | `true` |
| `adjust` | `qfq` \| `raw` | `qfq` |

**窗口护栏**：跨度 > **750 自然日** → 422（价格面板要按窗口读全市场行情，无上限即内存与长尾延迟的入口）；语料为空时缺省回看 365 天。窗口跨度在补窗之外**另行**计算——事件归属的日历含补窗，报告只认窗口内。

**响应形状**：`{params, window, universe, ic{per_day[], mean, std, icir, t_stat, positive_days, days}, groups[{quantile, label, turnover_avg, gross{curve[], metrics}, net{curve[], metrics}}], long_short{gross, net, t_stat, tradable:false}, notes[]}`。
`notes` 是**必填清单**（不是自由文案，逐条对应已知边界）：池子口径（有向且挂标的的事件池，不是全市场）· 有效样本天数 · **多空不可交易** ·
换手与费用口径（含最低佣金的规模事实）· **「读数在噪声区间内，不是因子有效的证据」** · 价格因子的样本期限定。
实测端到端（成品路径）：**事件源 ~0.6–0.7s / 价格源 ~0.9s**（价格行读取 0.26–0.47s + 面板构建 0.21s 占大头；事件行 62ms / 事件面板 11ms / 统计组装 16ms）⇒ **同步端点即可**；不建表、不落库（M5b 的 SSE 是为 7–13s 的网格准备的，本段不适用）。**规划期探针的「< 0.2s」是纯 Python 原型的读数**（只取四列、`fetchall`、无 dict 行），成品照抄不了那个数——如实改成上面这一组。

**验收**：参数网格任务跑通；策略曲线与基准叠加对比；一字涨停日买单被拒且拒单原因可见；Deflated Sharpe 与因子 IC 报告生成。

## 7. M6 模拟盘

> 竞品出处：QuantDinger（虚拟账户费用模型）/ vnpy（paperaccount）/ ai-hedge-fund（**逐交易日推进 + 执行前审批**）。
> 一句话形态：**模拟盘 = 回测引擎 + 审批闸门 + 账户结算**。撮合与费用复用同一批对象，闸门只决定「这张单发不发出去」。
> 2026-10-10 拍板 D1–D8，规划底稿与实测读数见 `docs/private/Pn-n/P2-Mn/P2-M6.md`（F1–F8）。

### 形态与时间模型（D1）

- **回放式**：会话有 `start` 与 `as_of`，按**冻结交易日历**逐日推进；成交用本地下一根 bar 的开盘价。
- **实时跟盘不做**：行情是年度整片重下载、不日更（§3 M2c 补口已记），「跟着真实日历滚」在当前数据通道上
  物理不成立（规划期实测：行情止于 2026-09-30 而事件已到 10-08）。调度器形态留 **P3-E1**，前置是行情自动刷新——那是数据层的独立待办，不塞进本段。
- 推进两粒度：`step`（推进**一个**交易日）与 `run`（跑到区间末端，审批口径 `all` / `none`）。
- 区间校验：交易日数 **2–250**（超出一律 422，不做隐式截断）；`end` 缺省取**池子最后一根 bar 的交易日**
  （数据到哪模拟到哪），且 `end` 不得晚于该日。

### 账户与撮合（D2 / D3）

- 标的池 **1–20 只**（沿用 §6 M5b 批量回测的上限，不另立数字），缺省取自选股，也可手输；**池子在创建时定死**。
- **等额配额**：`quota = initial_cash / 池子大小`。买入用「该标的配额剩余」与「账户可用现金」的较小者
  （`min` 是必要的：费用从现金出而配额只记持仓占用，两者会错开）；卖出后该标的配额回到全额。
  **单标的会话是 n=1 的特例**（配额 = 全部资金），因此它与回测的等价性可被直接断言。
- **创建期校验**：任一只 `quota < 最近收盘价 × 100` 一律 422 并**指出是哪一只**——否则那只标的一辈子不出手，
  而用户看不出为什么。（这是一次前检、不是保证：建仓后价格翻倍仍可能一手都买不起，那时落 `unfilled` + `REJECT_LOT`。）
- **撮合零复制**：`Broker` 与 `CostModel` 是同一个类的同一个对象语义。多标的账户通过 **`SymbolView` 适配器**
  把「该标的的配额现金 + 该标的的持仓」喂给同一个 `Broker`（它只读 `cash` 与 `position` 两个属性）。
  `Broker._affordable_qty` 上提为模块级 `affordable_qty()`，提案期预估与成交期定股数**共用同一个函数**。
- **数量口径**：决策单的 `est_qty` 是按**决策日收盘价**预估的整手数（界面标注「预计」）；成交股数在
  **次日开盘价**上按同一规则重算，`est_qty` 与 `fill.qty` **两个数都落库**。理由：回测的成交股数本就按开盘价现算，
  若提案时定死数量，本段最硬的那条验收（全批 ⇒ 与回测逐笔相等）立即不成立。
- **A 股规则**：一字涨跌停、停牌、整手走既有 `a_share_rules.order_reject` / `limit_band` / `limit_pct`；
  停牌顺延上限同引擎 `MAX_DEFER_BARS`（连续超限则记 `unfilled` + `REJECT_SUSPENDED`）。
- **估值**：每个交易日按收盘价；当日无 bar 的标的（停牌）按**最近一次已知收盘价**估值，**不塌成 0**。
- 复权口径同引擎：信号与成交用 `qfq`，涨跌停价从 **raw 前收**自算。除权日漏判、持仓不做分红送转处理，均为 M5a 已记的既有边界，本段不新引入。

### 决策审批闸门（D4）

**时机**：决策在 **T 日收盘后**生成，成交在 **T+1 开盘**。⇒ **审批时成交价还不知道**——这正是「闸门不改变价格口径」
的证明，也是它能与回测逐笔对齐的原因；界面必须把这句话说出来，否则用户会以为批准的那一刻就锁价了。

六态状态机（全部落库，**每个非法转移一律 409**）：

| 状态 | 含义 | 何时进入 |
|---|---|---|
| `pending` 待审批 | 已生成，等用户裁决 | T 日收盘 |
| `approved` 已批准 | 用户确认，等 T+1 开盘 | 用户动作（仅 `pending` 可批） |
| `filled` 已成交 | 按 T+1 开盘价成交，含费用/滑点明细 | T+1 开盘 |
| `rejected` 已驳回 | 用户拒绝 | 用户动作（仅 `pending` 可驳） |
| `expired` 未审批过期 | 用户直接推进过去了 —— **未审批不成交**（PRD 硬要求） | 推进越过 T+1 时 |
| `unfilled` 已批准未成交 | 批了但成交不了：一字板 / 停牌顺延超限 / 资金不足一手（带拒绝码与原因） | T+1 开盘 |

**明确不做**（可延）：审批时改数量 / 改价、部分成交、定时超时（**推进即过期**，不需要调度器）、急停开关、
防篡改账本（哈希链）——后两项是竞品调研里标「可延」的项，本轮都不做，理由写在规划底稿 D 表下。

### 决策单形状（M7 的消费契约）

```
{id, account_id, trade_date, symbol, side, est_qty, est_price, reason,
 sources{event_id?, title?, event_time?, available_at?, source?, original_source?, content_hash?, source_url?},
 status, decided_at?,
 fill{trade_date, qty, price, ref_price, commission, stamp_tax, slippage_cost, cash_delta}?,
 reject_code?, reject_reason?}
```

- `id` 由 **`uuid5(account_id | trade_date | symbol | side)`** 确定 —— 重放可复现，落库天然幂等（同 M3 的 point id 口径）。
- `sources` 来自 `Signal.event_id` → 事件行：**每张买入决策都能回链到驱动它的那条事件**（标题 / 双时间戳 / 来源三元组）；
  卖出（持有到期一类）无事件，如实留空，**不编**。
- `reason` 直接取 `Signal.reason`（引擎原样带出）。
- M7 的到期结算反思**只读这张表**；改它的形状要走 SPEC 变更。

### 端点（`/api/v1/paper`，全部纳入鉴权；越权与不存在同为 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts` | 建会话（含创建期校验）→ `{id, name, status, as_of, initial_cash, cash, symbols[], strategy{}, created_at}` |
| GET | `/accounts` | 本人的会话列表（摘要，按创建时间倒序） |
| GET | `/accounts/{id}` | 详情：账户卡（现金/市值/净值）+ 持仓 + **当日待审批决策** + 最近的决策流水 + 净值曲线 |
| POST | `/accounts/{id}/step` | 推进一个交易日 → `{as_of, filled[], expired[], unfilled[], account{cash, market_value, equity}, decisions[]}` |
| POST | `/decisions/{did}/approve` | 批准（仅 `pending`） |
| POST | `/decisions/{did}/reject` | 驳回（仅 `pending`） |
| POST | `/accounts/{id}/run` | 跑到末端，`{approve: "all"｜"none"}` → `{as_of, status: "finished", ...}` |

### 持久化（D6）：四张表 + 一个事务 + 乐观并发

- `paper_accounts`（`id` / `user_id` / `name` / `config` JSONB / `status` / `cash` / `as_of` / 时间戳）——
  `config` 存**创建时的全部参数**（池子 / 策略 / 参数 / 区间 / 费用开关），落库即契约。
- `paper_positions`（`account_id` + `symbol` 主键 / `shares` / `entry_price` / `entry_fees` / `entry_date` / `entry_reason`）
- `paper_decisions`（上表那个形状；**决策表同时是订单表与日志表**——决策与成交 1:1，不拆两张）
- `paper_equity`（`account_id` + `trade_date` 主键 / `cash` / `market_value` / `equity`）
- **推进是一个事务**：一次 `step` 要写成交、持仓、结算点、新决策与 `as_of`，全部落在一个 `conn.transaction()` 里
  （既有 `Database` 是 autocommit 单语句风格，本段为它加一个事务出口）。
- **乐观并发**：`UPDATE paper_accounts SET as_of = ? WHERE id = ? AND as_of = ?`，0 行受影响即 **409**（双击/并发推进）。
- 幂等建表走 `core/db.py` 既有机制（`CREATE TABLE IF NOT EXISTS` + 另起迁移语句的约定不变）。

### 数据访问：批量取数 + 窗口涨跌停价（规划期实测定的形态）

逐标的取数 20 只 **4,958ms**，一次批量 **480ms**（10×）；纯策略循环（20×181 根 bar）仅 **24.5ms**。
故模拟盘**自建批量取数**（`duckdb_client` 加全池 bars / raw 补窗 / events 三个查询），
**不复用引擎私有的 `_load_bars` / `_limit_bands`**（后者单标的就要 403ms：raw 全史 + `Decimal` 逐日）。
两条路径必须同源，用一条断言钉死：**窗口口径算出的涨跌停价 == 引擎全史口径算出的同一批日子**（逐日相等）。

### 推进的实现：重放是纯函数（D5）

`replay(config, 决策日志, through) -> ReplayResult` 是**纯函数**——同一份（config, 决策日志, 数据）必得同一个账户状态。
这条性质是「推进」可幂等、可对账、可测试的全部依据，用一条不变量断言钉死（两次重放逐字段相等）。

每次 `step` 都是**从会话起点重放到目标日**（不是增量推进）：这是用户策略能正确进入模拟盘的原因——
用户代码可以在模块级持状态（M4a 已记），而**跨 HTTP 请求保留不了实例**，只有重放能让它看到同一串 bar。
- 内置策略：进程内线程跑同一份 `replay()`（内置策略是无状态冻结 dataclass）。
- **用户策略：整段重放放进沙箱子进程**（父侧传决策日志、子侧回账户状态与当日决策，`kind="paper"`）——
  零 IPC 协议改造，三层配额继续生效。
- **过去的日子以决策日志为准**：策略在历史日子的输出被丢弃（它只负责推进自身状态），当日才用它的输出生成新决策。
  故「策略非确定性」（沙箱白名单里 `datetime` 是允许的，用户可写 `datetime.now()`）**不污染账本**，
  但同一会话两次推进可能给出不同决策 —— 如实记入已知边界。

### 前端（M6b）

`/paper` 页 + 页头第 7 项导航 + `/space` 加「我的模拟盘」页签。页面元素：会话创建表单 · 账户卡（现金 / 市值 / 净值 /
当日盈亏）· **待审批决策卡**（标的 / 方向 / 预计数量 / 理由 / 来源三元组 / 批准与驳回）· 持仓表 · 决策流水（六态可见）·
净值曲线 · 每日结算表。动手前先出 design brief（P1 口径），图表遵守 dataviz skill。

**验收**：策略信号 → 模拟成交 → 持仓变化全链路可查；费用扣减与手工样例一致；未审批决策不成交。
**本轮不做**：实时跟盘与调度器 · 手工下单与 Agent 下单 · 审批时改单 · 部分成交 · 多策略资金分配（P3-E6）·
急停开关 · 防篡改账本 · 分红送转的持仓处理 · 做空/融券 · 模拟盘的研报页（M7）。

## 8. M7 绩效分析

> 一句话形态：**M7 建「报告容器与信任层」，M8 建「生成器」**——报告一次生成后冻结落库，事实型结论全由
> 数据/规则生成且可回链事件，LLM 只写综述与逐笔反思（显式标为推断型）；M8 的深度研报接进同一个容器
> （容器只认 `blocks` 结构，不认来源）。
> 2026-10-10 拍板 D1–D4，规划底稿与 F1–F11 实测见 `docs/private/Pn-n/P2-Mn/P2-M7.md`。
> 切片：**M7a** 绩效层与报告容器（后端）→ **M7b** 决策记忆与到期结算反思（后端）→ **M7c** 研报页与证据追溯面板（前端）。

### 主体与生成（D1）

- 报告**主体 = 一个模拟盘账户**（`paper_accounts`）。回测 run 不出本段报告——它的 `trades` 没有 `event_id`，
  证据面板会空一半（规划期实测确认）。
- **冻结产物**：报告一次生成即落库（`research_reports`），同 `(account, snapshot_hash)` **幂等复用**
  （不新建行）——分享链接因此永远看到同一份。
- **claim 分级**：`blocks[].kind ∈ fact | inference`。事实型由数据/规则生成（数字可复算、事件可回链）；
  推断型只有两处：报告综述与逐笔反思（M7b）。**生成期校验闸门**：`kind="fact"` 的块必须带 `evidence`
  或 `numbers`（引用报告内指标键路径），否则拒收不落库——M8 的 LLM 结论也将过这道闸门。
- **块清单**（固定五块，顺序即渲染顺序）：① 概览（池子 / 策略 / 区间 / 基准口径 / **数据截止日**）·
  ② 绩效（指标 + 净值与全市场等权对照）· ③ 归因（标的级 + 事件级）· ④ 逐笔复盘（回合卡，M7b 追加）·
  ⑤ 综述（唯一的 inference 块）。M8 的深度研报按同一 `blocks` 结构追加，不改渲染层。
- **重放一致**：`snapshot_hash` 相同的报告重新生成时，事实层逐字段相等，综述与反思**复用已存文本**
  （缓存键 = `snapshot_hash | model_version | prompt_version`）；`report_hash` = 冻结产物正文（去易变字段）
  的规范化 JSON sha256 —— 同一快照重放 `report_hash` 必然相同（可断言）。

### 绩效与归因（M7a）

指标（**口径与回测同源**，复用 `backtest.metrics`）：总收益 / 年化 / 最大回撤 / 夏普 / 胜率 / 交易次数，
**补波动率**（日净值收益样本标准差 × √252）与**超额收益**（账户收益 − 全市场等权同窗口，`market_benchmark`）。
账户级「期末权益 / 已实现盈亏」取**账本**（`paper_equity` / `paper_accounts.realized_pnl`）。

归因（**如实收窄：本地无标的行业分类数据**——M2a 已核 `cn-daily` 无行业字段、sector 归档仅数日）：

| 维度 | 口径 | 来源 |
|---|---|---|
| 标的级 | 每只标的的已实现盈亏 + 未平仓浮盈 → 对初始资金的贡献 pp | 决策日志回合配对 + 账本 |
| 事件级 | 每笔买入决策的驱动事件**方向**分布与 **industries** 分布，按组给回合结果（组内 n 小如实标注） | `paper_decisions.sources.event_id` → 语料行 join（不新引数据源） |

因子归因不做（M5c 的因子面板是独立尺子，硬接会造一个假接口）；页面导流到 `/factor`。

### 决策记忆与到期结算反思（M7b）

- **「到期」= 持有期回合结束**（D2）：结算单位是**一次买→卖回合**（同账户同标的，买入成交 → 卖出成交）；
  期末仍未平仓的买入按**末根 bar 收盘**估值并标「未平仓」，alpha 算到数据末端；被驳回 / 过期 / 未成交的
  决策**不做反事实收益**（PRD 未要求），只记状态。
- **alpha 口径**：窗口 = [买入成交日, 卖出成交日]，与**同窗口全市场等权**比——与账户级基准同一把尺子
  （实测 181 天 24ms，逐笔可实时算）。
- **事实层可重算**：回合配对、pnl、alpha 都是纯函数（输入 = 决策日志 + 行情 + 日历），**不另落表**；
  落 Store 的只有「结算快照 + 反思文本」——反思是 LLM 产物、不可重算，才需要持久化。
  - 重算 pnl 用 `paper/account.py::settle` 的同一公式（`(卖价×量−卖费用) − (买价×量+买费用)`）。
    **已知舍入边界**：落库成交价/费用是 `NUMERIC(18,4)`，重算与该账户 `realized_pnl` 有**元级以下**差异
    （实测 2 个有回合的账户：0.01 元 / 0.53 元）——断言用容差，UI 不并列展示两数。
- **Store**（LangGraph，本段首用）：namespace = `("decisions", user_id, account_id)`，key = 决策 id，
  value = `{settled_at, outcome, pnl, return_pct, benchmark_pct, alpha_pp, window, reflection{text, model, at}}`；
  跨标的聚合走 `search(("decisions", user_id), filter=…)`，不上向量检索。
- **反思**：flash 一句话教训（输入 = 决策 + 结果 + 相对基准；输出 ≤120 字），**超时 20s、失败降级**
  （反思位留空并标注「反思不可用」，不挡报告与结算）。这是 `app/` 包内第一处直接 LLM 调用，
  统一经 `core/llm.py` 新增的 `ask_once()`。
- **触发**（D4）：结算核心是纯函数，入口两个——① 生成报告 / 查看复盘时**惰性结算**（幂等：已有结算即复用，
  不重复调 LLM）；② **独立调度 job**（`MEMORY_SETTLE_ENABLED` 开关，默认关）——与 `ETL_ENABLED` **解耦**，
  不寄生在 ETL 开关下（F6：唯一调度器在 `etl/scheduler.py`，ETL 关掉整个调度器不启）。
- **未到期如实标注**：行情止于 2026-09-30（X5 已知停更项），期末未平仓是**多数形态**（实测：14 笔买单里
  9 笔落在数据末端那天、未平仓）——报告与复盘页必须写「未到期（数据止于 2026-09-30）」，不得静默省略。

### 证据追溯面板（后端组装在 M7a，渲染在 M7c）

- 每个 `kind="fact"` 的块可展开 → 证据列表：**标题 / 摘要 / `event_time` 与 `available_at` 并列 /
  `source` / `original_source` / `content_hash`（截断）/ `source_url`**（字段形状与 M3 检索返回、M6 决策卡一致）。
- 证据 = **决策时的来源快照**（`paper_decisions.sources`，冻结）+ 语料行 join 补 `summary` / `industries`；
  join 键 = `(event_id, event_time::DATE)`（**`event_id` 不是全局唯一键**，M2b 已记）。
- **修订标注**：语料行 `content_hash` 与快照不一致时标「该事件已被平台修订」并给两个 hash 前 12 位——
  当前实测 22/22 一致（噪声为零），是廉价保险。

### 分享与导出

- `share_token = secrets.token_urlsafe(24)`（不可猜）、**可撤销**（撤销即公开端点 404）；公开页 `noindex`，
  响应**不含任何用户身份字段**（无 user_id / 邮箱）。
- **公开端点进 M1 的公开清单**：`GET /api/v1/public/reports/{token}`（JSON）+ `/markdown`（下载）。
  这是全站第一个匿名可达的用户数据出口——越权矩阵单列（token 猜不中、撤销即 404、不带 token 不泄露存在性）。
- **导出**（D4 拍板改 SPEC）：**Markdown 下载**（必做，含证据链）在受保护与公开两侧都可用；
  **PDF = 前端打印样式**（`window.print()` + `@media print`）替代原「Playwright 渲染」——复用 P1 capture
  链路是构建期脚本、不是产品功能；改为打印后未登录的分享页也能导出，且零新依赖。

### 端点（`/api/v1/reports` 受保护；越权与不存在同为 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/reports` | `{account_id}` → 生成并冻结报告（幂等复用）→ `{id, report}` |
| GET | `/api/v1/reports?account_id=` | 该账户的报告摘要列表（`/paper` 入口按钮状态用） |
| GET | `/api/v1/reports/{id}` | 完整报告 JSON（受保护） |
| POST | `/api/v1/reports/{id}/share` | 生成/取回分享 token → `{share_token, share_path}`（幂等）——**不给 `share_url`**：URL 由前端按当前 origin 拼（同 M1b「apiBase 跟随页面 host」的理由，部署换域名不用改后端配置） |
| DELETE | `/api/v1/reports/{id}/share` | 撤销 → 公开端点即 404 |
| GET | `/api/v1/reports/{id}/markdown` | Markdown 下载（含证据链） |
| GET | `/api/v1/public/reports/{token}` | **匿名只读**完整报告 |
| GET | `/api/v1/public/reports/{token}/markdown` | **匿名** Markdown 下载 |
| GET | `/api/v1/accounts/{id}/review` | 逐笔复盘（回合 + 未平仓 + 未成交 + 反思）；惰性结算（M7b） |
| POST | `/api/v1/accounts/{id}/settle` | 手动触发结算（登录必需、幂等；调度器走同一条路径）（M7b） |
| GET | `/api/v1/lessons` | 跨标的教训聚合（`symbol` / `direction` / `limit` 过滤）（M7b） |

### 持久化与快照

- 新表一张：`research_reports`（`id UUID PK` / `user_id FK` / `account_id UUID` / `created_at` /
  `snapshot JSONB` / `snapshot_hash TEXT` / `report JSONB` / `report_hash TEXT` / `share_token TEXT UNIQUE NULL` /
  `shared_at TIMESTAMPTZ NULL`）；幂等建表走 `core/db.py` 既有机制。
- `snapshot` = `{bars: {shards[{name, sha256}], digest}, events: {digest, days, last_day, rows},
  corpus: {start, end}, market_end, decisions_hash, account: {id, config_hash, as_of}}`——**顺带还上 M2c 的挂账**
  （`backtest_runs.request` 不含数据版本指纹；M7 起报告自带，`backtest_runs` 新写入补指纹列可延）。
  shards 的字段是 `{name, sha256}`（落盘文件名）：回执里的 `object_key` 属下载那一侧，
  本地拿不到也不采信（M2a 已记它连顶层 manifest_version 都对不上号）。
- 快照 digest **不采信清单元数据**：sha 从实际 Parquet 重算（体检 B1 同规）。实测：860MB 行情 0.93s、
  80MB 语料 0.28s —— 报告是点一次的动作（之后幂等复用），这个量级可接受。
- **冻结产物一律过 `plain_json`**：UUID / Decimal / date 一次性翻成纯 JSON 类型——落库（psycopg 的
  Jsonb 不认 UUID，而真库读回的 `account_id` / 决策 `id` 就是 UUID 对象）、分享、导出、算 hash
  用的一定是同一棵树。

### 前端（M7c）

- 两个路由：`(app)/research/[id]`（登录态，本人）与 `/r/[token]`（**公开只读，不在 `(app)` 守卫组内**），
  共用同一套渲染组件；`/paper` 账户卡旁加「生成研报」入口，`?id=` 深链。
- 页面元素：报告头（标的池 / 策略 / 区间 / 快照 digest 与 `report_hash` 短码 / **数据截止日**）· 指标卡
  （含波动率与超额，带口径标签）· 净值 vs 全市场等权（ECharts，沿用 ChartFrame / useEChart 约定）·
  归因表 · 逐笔决策复盘（回合卡：驱动事件证据 + 结算 + 反思）· 证据追溯面板（块内展开）·
  **页脚免责声明**「本报告由 QuantSage 生成，仅供研究用途，不构成投资建议」· 导出（打印 PDF / Markdown）·
  分享（复制链接 / 撤销，带确认）。动手前先出 design brief（P1 口径），图表遵守 dataviz skill。

**验收**：未登录浏览器打开分享链接可见完整研报（Playwright 无 cookie context 断言 + 截图）；真实账户跑结算
生成反思并落 Store（含「未到期」如实标注）；Markdown 导出含每条证据的双时间戳与来源三元组；同一
`snapshot_hash` 重放 → 事实层逐字段相等且 `report_hash` 相同（集成用例断言）；撤销分享后公开端 404。
**本轮不做**：研报生成器（多 Agent 深度研报，M8）· 回测 run 出报告 · 因子归因 · 持仓行业分类（无数据源，
留 M10 板块数据）· 反事实机会成本 · PDF 服务端渲染 · 报告版本历史（只保最新一份，重放由快照保证）·
研报评论/协作 · 手工下单与 Agent 下单。

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

- 登录后落地页。大盘总览：指数行情（覆盖验证见 §3）、涨跌家数、涨跌停家数、成交额（全市场日线 DuckDB 一次聚合，走 M5 那条 `cross-section`）；板块热力图**只能**用事件 `industries` 聚合——行情数据源没有行业字段（M2a 已核实），且该字段按事件类型覆盖不均（news 45% / policy 100% / announcement 0%），**热力图要如实标注口径与覆盖率**，不能画成一张看起来完整的图
- **标的浏览（2026-10-09 从「股票展示 / 信息查询」并入，不新开功能 ID）**：全市场列表（按涨跌幅 / 成交额排序、按代码**或名称**搜索）与个股页（K 线 + 事件时间线 + 加自选 / 去回测）。数据全部复用既有端点（`market/{symbol}/bars`、`events`、`market/{symbol}/probe`），新增的只有 M5 那两件地基（截面 + 名称字典）。**定位是「把已有能力摆出来」，不是独立产品**——公司资料、财务、权威行业分类一概没有，界面上不假装有
- 事件日历/快讯流：按日聚合事件，重要度排序；每条并列 `event_time` 与 `available_at`（PIT 语义展示位）；来源三元组可展开；日期筛选
- 导航入口：个人空间 / 研报 / 回测 / 模拟盘

**验收**：大盘总览与日线数据抽查一致；事件日历点击任意事件可见来源三元组与双时间戳。

## 12. 测试策略

- 单元：AST 检查器规则样例矩阵（M4）；A 股规则各拒绝码（M5）；Deflated Sharpe / 因子 IC 与分层（M5）；交易日历与体检脚本（M2）；到期结算与反思（结算用固定价格快照，M7）；证据链组装（M7）；鉴权纯函数（密码哈希 / JWT / cookie 属性）与归属过滤（M1：离线跑「未登录 401 门 + 内存 DB 的越权矩阵」，注入 `app.state.db` 与 `InMemorySaver`）
- 集成：auth 注册登录流与 A/B 隔离矩阵（M1，真实 Postgres；用唯一邮箱前缀 + teardown 清理，不污染 dev 库）；ETL 幂等重跑（M2）；模拟盘全链路（M6）；研报导出与重放一致（M7）；深路径降级（M8）
- 归属校验是**双跑**的：越权矩阵在离线（内存实现，快反馈）与集成（真实 SQL 过滤，验真）各跑一遍——过滤逻辑写在 SQL 里，离线实现无法证明真库行为
- M1c 增量：自选股离线用例注入内存业务库（未登录 401 门 / 重复 409 / 越权 404 / 分组 UPDATE 语义 / 涨幅与 NULL 口径 / 行情层不可用时的降级），**内存替身必须照抄 `UNIQUE(user_id, symbol)` 语义**，否则 409 用例是假的；回测落库用例验信封形状、列表只列本人、越权 404
- **M2c 补口增量（2026-10-09，四通道新鲜度）**：三条新检查各一组用例——**正常 / 落后 / 依赖不可达降级**（X4 的 Qdrant 不可达必须落 **info 而非 error**、X5 的日历越界不得抛、X6 的台账缺 `archive_last` 时按「无从判断」处理而不是当成停更）；**自动补嵌**断言三件事——「ETL 没有物化任何新日时不触发嵌入」「嵌入子进程失败**不改变 ETL 的成败**（台账里留痕即可）」「开关关闭时一次都不拉」。**不 mock 子进程本身**（要验的是真的拉得起来），但嵌入命令用 `--days-from/--days-to` 限到一天，别让用例真跑全量
- M1c 增量（2026-10-09，代码体检）：probe 的三态各一条——有数据 / 无数据（200 且 `has_data: false`，**不是 404**）/ 行情层不可用（503，用空数据目录）；外加一组**加自选表单提示口径**的前端纯函数用例（没输满六位不吭声 / 判重压过体检 / 四种体检状态各自的落点 / 结果属于旧代码时不贴到新输入上 / 列表未加载完不判重）
- M1c 回归：`POST /api/v1/backtest` 改信封与加鉴权后，P1 既有的 12 个离线回测用例统一挂 `signed_in` 夹具并改读 `body["report"]`；集成侧两个回测用例改为跑 lifespan + 真实注册（裸 `TestClient` 没有 `app.state.db`），账号在 teardown 里清（`users` 级联清 `watchlist` 与 `backtest_runs`）
- M2b 增量：交易日历纯函数（长假边界 + 与行情 `trade_date` 的全期双向对账脚本）；`symbols` 归一化矩阵（`code` 为 null / 带 `.SZ/.SH` 后缀 / 多标的 / 无标的）；归档分片合并的**幂等**（同日重跑逐字节一致、中途 kill 后重跑与干净运行逐字节一致）；`list_contains` 按标的过滤；事件驱动窗口收口（显式越界 400、缺省仍取语料起点）。集成侧对真实归档分片跑一次小窗口回填，回归 P1 的 **353 条包含性**
- M3 增量：**离线用例不下载模型**——编码器是协议，测试注入假实现（返回确定性向量）；文本构造（空字段不写标签）、point id 稳定、filter 构造（`as_of` 必带）、RRF 融合排序、NDCG 计算（对已知排序手算）、长度分桶、增量判据（sha 变 / 不变）、`RagNotReady` 降级各一组。检索层的 PIT 边界在**内存向量库替身**上离线跑（替身必须照抄「服务端过滤」语义，否则边界用例是假的，同 M1 内存业务库口径），另在集成侧对**真实 Qdrant** 跑 upsert → 检索 → 按日重建幂等
- M4 增量：**AST 规则样例矩阵**——每条规则正/负样例成对，断言行号与 severity 与 `message` 关键词；`PARAMS` 校验矩阵（未知键 / 类型不符 / 越界 / 跨字段）；入口解析（缺 `on_bar` / 语法错 / 运行期异常 / 返回值非 Signal 列表）；**沙箱故障注入**（死循环撞 CPU 与墙钟、超大分配撞内存看门狗、超长输出被截断、`sys.exit` 与 `os._exit`、导入白名单外模块）逐条断言「明确错误 + 父进程存活」；**模板等价性**——双均线与事件驱动模板经沙箱跑出的报告与注册表策略逐点一致（离线，真实 Parquet 小窗口）。集成侧对**真实 Postgres** 跑策略 CRUD 与归属矩阵（越权 404 / 撞名 409），并跑一条「保存 → 检查 → 沙箱回测 → 落库 → 重开」端到端
- M4b 增量：负样例必须**零命中**：`[-1]`、`i-1`、`ctx.history[a+1:b+1]` 切片（双均线模板原样）、`for i in ...: closes[i+1]` 相邻比较、字符串与注释里的 `shift(-1)`、名为 `bfill` 的变量、`fillna(method="ffill")`、生成器 `on_bar` 的裸 `return`、嵌套函数里的 `return 1`；**元测试**断言规则表每条规则都在样例矩阵里有正/负样例；**一致性测试**双跑——① R1 用的白名单与运行时 `api.ALLOWED_MODULES` 是同一对象 ② 同一批入口/签名样例上「R5 命中 ⟺ `api.load_strategy` 抛 `StrategyRejected`」（AST 与 `inspect` 两套判据不许漂移）；模板库 5 个各自 `check_source == []` + `load_strategy` 装载成功，3 个新模板各配一条**合成触发序列**断言至少 1 笔成交
- M4c 增量（离线）：**CRUD 矩阵**（未登录 401 门 / 建读改删 / 撞名 409 / 越权与不存在同为 404 / 草稿可存——语法错的 code 也 201 且 findings 随响应 / 列表按 `updated_at` 倒序）；`/check` 形状（`{findings, meta}`，`meta.params` 与 `parse_meta` 同源，`PARAMS` 坏掉时 `meta` 为 null 且 findings 有 R4）；**闸门**（`strategy="user"` 缺 `strategy_id` → 422、带 `strategy_id` 而 `strategy != "user"` → 422、库里含 `shift(-1)` 的策略跑 → 422 带 findings、参数越界 → 422、运行期异常 → 422 带 line）；**沙箱路径**（5 个模板各跑通并断言 `meta.strategy_kind == "user"`；`USES_EVENTS=True` 的模板在 `pit_mode="both"` 下真的产出 `pit_comparison`；落库 `request` 可 JSON 序列化且带 `strategy_id`；`code_sha256` = 源码 sha256）；**HTTP 层故障矩阵**（6 条坏法 × 明确错误码 + 每条之后 `/health` 200 且正常策略仍跑通；配额由 monkeypatch 压低，同 M4a 口径，不让用例真等 20s）；`FakeDatabase` 必须照抄 `UNIQUE(user_id, name)` 与归属过滤语义（否则矩阵是假的，同 M1c 口径）
- M4c 增量（集成，真实 Postgres）：建表与**增列的幂等**（连跑两次 lifespan 不报错）；策略 CRUD 与归属矩阵双跑；「保存 → 检查 → 沙箱回测 → 落库 → 重开」端到端一条；删除策略后旧回测记录仍可打开（`strategy_id` 悬空）
- M5a 增量（离线）：**A 股规则各拒绝码矩阵**（`REJECT_T1` / `REJECT_LIMIT_UP` / `REJECT_LIMIT_DOWN` / `REJECT_SUSPENDED` / `REJECT_LOT`）；**一字拒单与「开板日正常成交」成对样例**——防的是误拦，单侧的拒单用例证明不了不误伤；除权日与**缺 raw 序列**两条降级路径；名称归一化的 **11 例一名多写对照表逐条断言**（本段的负样例矩阵）；等权基准的剔除规则（含 +1942% 样例）与剔除条数入报告；**回归断言：既有报告 golden 分毫不动**——演示标的（600519 / 000001 / 300750）2026 年实测零一字板，规则接上后数值若有位移即为误拦
- M5a 增量（集成）：`cross-section` 端点的形状与排序、名称搜索、**ETL 刷新字典的幂等**（同日重跑逐字节一致）
- M5b 增量（离线）：网格展开与边界校验（非法组合 422 而非静默跳过；**逐条负样例**：轴数 3 / 轴参数不在参数集 / 与基座 `params` 撞键 / 轴值重复 / 某格 `fast≥slow` / 格数 >100）；**并发上限实测**——判据是「仪器化探针记录**同时在跑的格数峰值 ≤2** **且并发结果与串行逐格相等**」，**耗时比值如实记录、不设阈值**（v1.18 写的「≈串行 55%」经 2026-10-09 实测推翻：DuckDB 默认 `threads=8` 已吃满核心，实测只有 0.83 / 0.90，进程池亦然）；**单格失败隔离**（一格注入墙钟超时 → 该格记 `error{kind:"wall"}`，其余照跑）；**单格与既有回测同口径**（某格 `metrics` 与 `POST /backtest` 同配置报告逐键相等）；Deflated Sharpe **与手算对照**——三条正交验算：① `SR = SR₀ ⇒ DSR = Φ(0) = 0.5`（不含任何常数）；② **γ₃=0 且 γ₄=1** 时分母恰为 1（对称两点分布就是这种形态）⇒ 退化成 `DSR = Φ((SR−SR₀)·√(T−1))` 可闭式手算；③ 一组固定输入的期望值由 **scipy 独立实现**算得并钉成常数（**只钉常数、不引依赖**）。**外加四个退化分支**（有效格 <2 / 最优格 sharpe 为 `None` / 观测数 <2 / 分母非正）与「V=0 仍出数但标注 `sr_variance: 0.0`」
- M5b 增量（集成）：批量请求落库汇总 + 重开
- M5c 增量（离线）：**面板是纯函数**（输入是行，不是库），故边界用例喂**合成行**而不是替身——但「同一把尺子」必须**双跑断言**：同一批事件（含 `available_at` **恰为 15:00:00** 与 **15:00:01** 两例、周末事件、节假日事件、跨日滞后事件）分别喂 `EventFeed.advance` 与因子面板，**归属日逐条相等**。另：前向收益缺窗口 / 缺价各一条；`min_pool` 护栏（造一个 19 只的池断言跳过且计入 `skipped`）；IC 对**已知排序手算**（含并列名次）；分层与多空口径（**只做等权并写明**）；换手与费用的手算样例（进出名单 → 费率 → 净值）；价格面板的 lookback 边界（上市不足 20 日的标的剔除）
- M5c 增量（集成）：真实语料上全期跑通一次 IC（**当前读数：93 天语料 / 54 个有效信号日**，订正 v1.18 起写的「84 天」）；真实行情上价格因子跑通一次
- M5c 增量（HTTP 矩阵）：`source` / `direction` / `adjust` 非法取值 → 422；`start > end` 与显式越界 → 400/422；缺省窗口的 200 形状（`params` / `window` / `ic` / `groups` 五组 / `long_short.tradable = false` / `notes` 非空）；`costs=false` 时 `net` 为 null 而 `gross` 照常
- M5c 增量（回归判据）：**既有用例零改动 + 既有行为逐点不变**是硬判据（同 M5b 口径）。本段对既有文件的改动只有两处**纯搬移**：`backtest/metrics.py` 增 `EXCLUDE_ABS_CHANGE_PCT`（从 `benchmark.py` 挪来——纯口径常量住纯口径层，消费方不必为一个阈值 import 数据层）、`benchmark.py` 改为再导出；外加 `main.py` 注册路由一行——**行为零变化**
- M6 增量（离线）：**闸门六态转移矩阵**逐条（每个非法转移断言被拒、每个合法转移断言落对状态）；**未审批不成交**——推进越过成交日后记 `expired`，且此后批准一律被拒；**parity 断言**——单标的 + 全批 ⇒ 与 `run_backtest` 的 `fills` **逐笔相等**（含 `reason` / `event_id` / 费用 / 滑点 / 股数），驳回则轨迹确实改变（驳回一笔买入后，后续卖出信号落在空仓上）；**涨跌停价同源**——窗口口径算出的 band 与引擎全史口径逐日相等；**账户层单测**（配额：买入不越额度、卖出释放、`min(配额, 现金)` 的错开、跨标的不串账；停牌日按最近收盘价估值不塌成 0）；**顺延语义**（停牌日不成交、连续超 `MAX_DEFER_BARS` 记 `unfilled`）；**重放不变量**（同一决策日志重放两次逐字段相等）；`est_qty` 与 `fill.qty` 在跳空日的差异有据可查
- M6 增量（HTTP 矩阵）：未登录 401 · 越权与不存在同为 404 · 状态机 409（批已过期 / 批已成交 / 重复批准 / 越权批）· 创建期 422（池子为空 / 超 20 只 / 区间不足 2 个交易日 / 超 250 个交易日 / **配额不足一手**并指出是哪只 / 未知策略 / 用户策略未保存）· 推进到区间末端 409
- M6 增量（集成，真实 Postgres）：四表**幂等建表**（连跑两次 lifespan）；**推进是一个事务**（注入中途失败后 `as_of` 与持仓都不变）；**乐观并发**（两次并发 `step` 只生效一次，另一次 409）；**重放对账**（从决策日志重建的账户状态 == 库里存的账户状态）
- M6 沙箱增量：`kind="paper"` 路径下**用户源码版双均线的模拟盘结果 == 内置 ma_cross**（与 parity 同一条判据）；坏法矩阵（死循环 / 超大分配 / 抛异常）各给明确错误码且父进程存活（复用 M4a 口径，配额由 monkeypatch 压低）
- M7 增量（离线，M7a）：绩效指标**手工样例**（净值曲线与回合列表硬编码 → 波动率 / 超额 / 回合统计逐项断言，同 P1「不引第三方快照库」口径）；**回合配对重算 vs 账本**用**容差**断言（`NUMERIC(18,4)` 舍入，实测 0.01 / 0.53 元级，见 §8）；归因（标的级贡献之和 == 已实现盈亏 + 未平仓浮盈；事件级分组小样本如实标注）；**证据组装**（sources → 语料行 join 双键命中、修订标注在 hash 不一致时出现、缺行时如实留空不编）；**快照**（digest 从实际 Parquet 重算而非采信清单；`snapshot_hash` 对同一输入稳定、对任一字段变化敏感）；**claim 校验闸门**（`fact` 块无 evidence 且无 numbers → 拒收；`inference` 块放行）；Markdown 渲染（含双时间戳与来源三元组的逐条存在性）
- M7 增量（离线，M7b）：结算口径**固定价格快照**逐例（回合 / 未平仓按末根 bar / 未成交不结算三类；alpha 与 `market_benchmark` 同源）；**幂等**（同 `(决策, snapshot)` 二次结算不重复调 LLM，用假模型计次）；**反思降级**（假模型抛异常 / 超时 → 反思位留空且带「不可用」标注，结算结果照常落 Store）；Store 用 `InMemoryStore` 离线跑 namespace / filter 聚合
- M7 增量（HTTP 矩阵）：生成 / 查看 / 分享 / 撤销 / Markdown 五端点各自的未登录 401 · 越权与不存在同为 404 · 非 UUID 422；**匿名只读**（`/api/v1/public/reports/{token}` 无 cookie 200，**响应不含 user_id / 邮箱**）；**撤销即 404**；**不带 token 不泄露存在性**（随机 token 与已撤销 token 同响应）
- M7 增量（集成，真实 Postgres + 真实 Store）：`research_reports` 幂等建表与幂等复用（同 `(account, snapshot_hash)` 二次 POST 不新建行）；**重放一致**——同 `snapshot_hash` 重新生成，事实层逐字段相等且 `report_hash` 相同；**Store 真库** put / search 往返；`AsyncPostgresStore.setup()` 连跑两次不报错
- M7 增量（前端纯函数）：报告 JSON → 视图模型（指标卡口径标签、归因表、回合卡、`report_hash` 短码）；证据行展示序（**事发与可得并列**，沿用 `sourceRows` 口径不新写一套）；`fact`/`inference` 分级 → 徽章与展开态；公开页与登录页共用同一映射
- M7 界面验证：`capture-m7.mjs` 走真浏览器——**匿名 context**（无 cookie）打开分享链接断言报告正文可见并出截图；所有者侧生成 → 分享 → 复制链接 → 撤销后公开页 404；打印样式走查（人工，出 `logs/m7/print-*.pdf` 截图）；Markdown 下载内容含证据链（读文件断言）
- **界面验证必须验「动作的结果」，不能只走动作**（2026-10-10 补）：因子页 / 优化页的脚本**也走了**「看表格 → 看图」，但只验表格里的数、没验图回来没有，于是「切回来是一张空画布」一路全绿。补上的判据是**等画布宽 > 300 再数像素**（零尺寸容器会让 ECharts 按兜底 100×300 建画布，压扁的图也有像素）
- M5 界面验证（**不属于自动化层**，同 M4c 口径）：基准叠加出图、自选股按名称搜、网格跑到热力图 + 点一格重跑、因子页出图，各出一条真浏览器证据。**已落地部分（M5a）抓到一类新问题：旧数据形状**——`meta.benchmark` 这类新增字段在 M5a 之前落库的记录里不存在，直取属性会整页白屏；断言「图例出现某条线」也不能用 `text=` 去撞（ECharts 画在 canvas 上，DOM 里没有该文本，撞上了是撞到别处的同名文案）——**改读画布像素**数三个 series token
- 前端：Vitest 只测纯函数（日历日期映射、瀑布图数据映射、证据面板分组、自选股分组视图与 symbol 校验 + 加自选表单提示口径；M4c 增：findings → 编辑器标注映射、`PARAMS` schema → 表单初值与请求体、运行请求体只在非空时带区间、**错误体还原**（422 的 `findings` / 沙箱的 `kind`，畸形响应不当崩）、`strategy="user"` 旧记录的处置函数；M5c 增：请求体构造 → 查询串、报告 → IC 图 / 分层曲线 / 多空曲线的数据映射（含 `net` 缺失与空 `per_day` 不当崩）、`notes` 分组与「不显著」态判据（IC 的 t 绝对值 < 2 时页面文案口径）），不引组件测试框架（沿用 P1 口径）。**M5 增量**（M5b 为主）：网格输入 → 笛卡尔展开与边界校验、矩阵（`params → metrics`）→ 热力图数据映射、SSE 帧解析沿用 `lib/sse.ts` 既有纯函数（**不新写一套**）、名称搜索输入 → 请求参数的匹配口径
- M4c 界面验证（**不属于自动化层，单列一节**）：`frontend/scripts/capture-strategies.mjs` 驱动 Playwright 走一遍真浏览器——断言波浪线出现、面板报「会拦住运行」、点「保存并运行」被 422 拦下、参数表单按 schema 生成，并出四张截图（`logs/m4c/workbench-ui.md`）。**这一层抓到的问题自动化层结构上看不见**：CORS 预检（TestClient 不走 CORS）、浏览器路由时序（深链与列表选择竞争）、编辑器事件语义（`setValue` 的回声）、sticky 遮挡（只有窄屏真看一眼才发现）
- 离线用例继续走真实 Parquet，不 mock 查询层（沿用 P1 口径）

## 13. 变更记录

| 版本 | 日期 | 关联 | 变更 |
|---|---|---|---|
| v1.35 | 2026-10-10 | M7c | **M7c 落地回填 —— 研报页与证据追溯面板交付，M7 整段完成**。① **交付**：`(app)/research/[id]`（登录态）与 `/r/[token]`（**公开只读，不在守卫组**）**共用同一份 `ReportView`**（壳不同而已——分享出去的那份是别人唯一会看到的，写两份必然漂移）；渲染**只认 `blocks[]`**（未知块兜底渲染 `text`，M8 接进来不用改这页）；claim 分级徽章（事实 / 推断）；证据面板挂在引用它的块上（展开见事发与可得并列 + 来源三元组 + 修订/查无标注）；逐笔复盘卡（已到期带教训与模型署名 / 未到期标「还没有结果可总结」/ 定了没交易只列状态）；打印样式导出（`@media print`：页头与按钮不印、白底黑字、卡片不跨页）；`/paper` 账户卡旁「出研报」入口（**不加页头导航项**，M5b 记过窄屏溢出）。② **⚠ 配色被校验器改过一次**（dataviz 的纪律：算，不眼看）：原想沿用回测页的「全市场等权 = series3 紫」，`validate_palette.js` 判**硬失败**（蓝紫在 deutan 下 ΔE 1.4、正常视力也只有 12.5 < 15）；改 **账户 = series1 蓝实线 / 等权 = series2 橙虚线**（浅 24.7/33.6、深 26.8/31.8 全 PASS）——分类色按**固定顺序**分配，而不是按实体记忆（回测页那条紫是**三序列的第三位**）。③ **同一件事不写两遍**：净值图复用 `CurveChart`（给它加了可选 `formatValue`，研报喂金额走 `amount()`，不让 50 万显示成 `500000.0000`）；复盘卡的理由复用 `lib/paper.ts::reasonText`（`event_driven:news:123 score:88` → 中文）。④ **眼睛扫出的两处修正**（渲染出来看一眼是 dataviz 的最后一条）：概览块的两个数字**没有主语** ⇒ `numberLabel()` 补中文标签；买入理由还是引擎原文 ⇒ 接上既有可读化。⑤ **脚本自身的竞态**（如实记）：跳转后不等块渲染就断言，第一版当场假红两条——加等待后 17/17。⑥ **读数**：界面验证 **17/17**（`logs/m7/ui.md`），四条是自动化层结构上看不见的——**匿名 context** 看到完整正文（PRD 主验收）、曲线**真的画在画布上**（1066×298、非底色 13377 px）、证据展开后双时间戳与三元组都在、撤销后同一 context 见到失效页；前端 **308** 纯函数（新增 26）+ typecheck / lint / `pnpm build` 全绿（`/research/[id]` 848 B、`/r/[token]` 890 B，均动态）。设计推导见 `docs/private/Pn-n/P2-Mn/P2-M7c-design-brief.md` |
| v1.34 | 2026-10-10 | M7b | **M7b 落地回填 —— 决策记忆与到期结算反思交付**。① **交付**：`app/memory/` 五个模块（`settle.py` 增结算核心 / `reflection.py` 一句话教训 / `decision_store.py` Store 封装 + **按需持有者** / `service.py` 结算编排 / `scheduler.py` 独立定时）+ 三端点（`GET /accounts/{id}/review` · `POST /accounts/{id}/settle` · `GET /lessons`）+ 报告追加「逐笔复盘」块与 `body["review"]` + `snapshot.review_hash`；`MEMORY_SETTLE_ENABLED`（默认关，21:40）与 ETL **各起一个调度器**。② **⚠ 一个只有真库才现形的事件循环坑**：`AsyncBatchedBaseStore` 在**构造时**捕获 `asyncio.get_running_loop()`，此后所有 `aget/asearch` 都在那个 loop 上建 Future——在 lifespan 的 loop 里建 Store、在请求的 loop 上用，直接炸 `Future attached to a different loop`（uvicorn 单 loop 侥幸不炸，那是运气不是设计）。修法是 `MemoryHolder` **按需建 + 按 loop 缓存**。③ **幂等的两层**都已验：进程内（同账户连看两次复盘，第二次零新写）与**跨进程**（重跑取证脚本，首跑即全复用——比进程内更强）。④ **`freeze_review` 必须去掉运行读数**（`saved`/`reused`）：留着它们 `review_hash` 每次结算都变（首跑 saved=3、二跑 saved=0），报告就再也复用不上——被幂等用例当场逮到。⑤ **降级要留痕**：`MemoryHolder` 起初把建库异常静默吞成 503，诊断时无从下手（`NameError` 藏了两轮），补日志后一眼看见——「可以降级，但原因必须进日志」。⑥ **读数**：离线 **1113 passed** / 集成 **97 passed**；真实账户取证（`logs/m7/evidence.md`）——结算「已到期 3 / 未到期 1 / 教训 3」，首跑 0.38s（含 flash 反思）、二跑 0.15s 全复用，跨进程幂等成立；教训样本与综述均为真 flash 产出。⑦ **措辞订正**：§1 的 `memory/` 由 3 个文件改列 5 个（加 `service.py` / `scheduler.py`）。**M7c（研报页与证据面板）未开工** |
| v1.33 | 2026-10-10 | M7a | **M7a 落地回填 —— 绩效层与报告容器交付（后端整片）**。① **交付**：`app/report/`（performance / attribution / evidence / snapshot / narrative / builder / markdown 七个模块）+ `app/memory/settle.py`（回合配对与未平仓估值，纯函数）+ `app/api/reports.py` 八端点 + `research_reports` 表（幂等 DDL + 归属过滤 + `UNIQUE(share_token)`）+ `core/llm.py::ask_once`（超时 20s、失败降级）+ `duckdb_client` 两个批量查询（`events_by_ids` / `closes_through`）。② **claim 闸门实测拦下一处真问题**：纯价量账户（双均线）没有事件证据，归因块既无 `numbers` 也无 `evidence` ⇒ 拒收整份报告；修法是给归因块补**回合口径的合计数字**（`attribution.summary`），不放松闸门。③ **真数据取证逮到两个静默错**（`scripts/report_evidence.py`，读数为证）：**(a) 日期形状**——`paper.store.equity_from_row` 给的是 ISO 字符串，而 `market_benchmark` 按 `date` 查表，喂错了会**全数落空、基准静默变 0**（真账户跑赢 2.51%、基准却显示 0.00%），收口为 `report.builder.equity_dates()`；**(b) UUID 落库**——真库读回的 `account_id` / 决策 `id` 是 `uuid.UUID`，psycopg 的 Jsonb 适配器拒收，收口为 `assemble_report` 末尾的 `plain_json()`（落库 / 分享 / 导出 / 算 hash 用同一棵树）。④ **另外三处只有真库/真浏览器才现形**：`Content-Disposition` 头是 latin-1（中文文件名要走 RFC 5987 的 `filename*`）；`CASE WHEN %s IS NULL` 参数无类型上下文 ⇒ `IndeterminateDatatype`（要 `%s::text`）；综述 prompt 直接给原始浮点会被模型**原样复述**（`0.02509640439999994`）——数字在 prompt 里就格式化好。⑤ **内存替身的两个反向教训**（同 M1c/M4c 的「替身会骗人」）：token 撞号检查没排除**自己那一行**（比真库严，误报「重复点分享」）；UUID 未照抄（比真库松，放过了 ③(b)）。⑥ **读数**：离线 **1076 passed**（唯一红是 `test_etl` 那条既定日期定时炸弹，与 M7 无关）、集成 **96 passed**（新增 3 条：幂等建表 / 全链路往返 / 推进后出新报告）；真实账户取证 3 项复核全过——回合重算 vs 账本差 **0.0119 元**（容差内，SPEC §8 记的舍入边界在真数据上复现）、证据 4/4 可回链、同一综述重放 `report_hash` 相同；耗时快照 0.93s / flash 综述 4.6–7.0s。⑦ **措辞订正**：`share_url` → `share_token` + `share_path`（URL 由前端按 origin 拼）；snapshot 分片字段 `object_key` → `name`；`report/` 增列 `markdown.py`。**M7b（决策记忆与结算反思）/ M7c（研报页与证据面板）未开工** |
| v1.32 | 2026-10-10 | M7 | **M7 细化为可执行规格（开工前，用户已拍板 D1–D4）**。① **形态定为「容器与信任层」**：报告主体 = 一个模拟盘账户（回测 run 的 `trades` 无 `event_id`，证据面板会空一半，规划期实测确认）；报告**一次生成即冻结落库**（新表 `research_reports`），同 `(account, snapshot_hash)` 幂等复用——**M7 建容器、M8 建生成器**，深度研报接同一个 `blocks` 结构。② **claim 分级与闸门**：`kind ∈ fact｜inference`；事实型必带 `evidence` 或可复算的 `numbers`，生成期校验不过即拒收（M8 的 LLM 结论也过这道闸门）；LLM 只出综述与逐笔反思（flash），**超时 20s、失败降级留空并如实标注**。③ **重放一致是可断言的**：`snapshot_hash`（bars 逐片 sha + events 日分区 + 语料覆盖 + 决策日志 hash + 账户 config）相同 ⇒ 事实层逐字段相等、综述与反思**复用已存文本**（缓存键含 model/prompt 版本）、`report_hash` 相同。④ **「到期」= 持有期回合结束**：结算单位是买→卖回合，期末未平仓按末根 bar 收盘估值并标「未平仓」（**实测这是多数形态**：14 笔买单里 9 笔落在数据末端那天），alpha 对**同窗口全市场等权**（与账户级同一把尺子，实测 181 天 24ms）；被驳回/过期/未成交**不做反事实收益**。⑤ **事实层可重算、不落表**；落 **LangGraph Store**（本段首用）的只有结算快照 + 反思文本，namespace `("decisions", user_id, account_id)`，跨标的聚合走 `search` 前缀扫。⑥ **实测记下一条舍入边界**：落库成交价/费用是 `NUMERIC(18,4)`，回合重算与该账户 `realized_pnl` 有**元级以下**差异（0.01 / 0.53 元）——断言用容差、UI 不并列展示两数。⑦ **分享与导出**：`token_urlsafe(24)` 不可猜、可撤销（撤销即公开端 404）、`noindex`、**响应不含任何用户身份字段**；公开端点显式进 M1 公开清单（全站第一个匿名可达的用户数据出口，越权矩阵单列）；**PDF 改走前端打印样式**（替代原「Playwright 渲染」——capture 链路是构建期脚本、不是产品功能，打印后未登录页也能导出且零新依赖），Markdown 下载两侧都可用。⑧ **触发解耦**：惰性结算（报告/复盘时，幂等）+ 独立调度 job（`MEMORY_SETTLE_ENABLED` 默认关）——**不寄生 `ETL_ENABLED`**（唯一调度器在 `etl/scheduler.py`，ETL 关掉整个调度器不启，F6 实测）。⑨ **如实收窄归因**：本地无标的行业分类数据（M2a 已核），只做标的级 + 事件级（方向 / industries）；因子归因不做（导流 `/factor`）。⑩ §1 结构、§12 测试增量（离线 / HTTP / 集成 / 前端纯函数 / 界面验证）与验收同步。规划底稿与 F1–F11 实测：`docs/private/Pn-n/P2-Mn/P2-M7.md` |
| v1.31 | 2026-10-10 | M5b · M5c · M6 补口 | **图表基础设施补口：`useEChart` 的宿主从 `RefObject` 改为回调 ref。**① **现象**：因子页与优化页热力图的「看表格 ⇄ 看图」切回来之后**图永远不再出现**（实测 `初始 1366×238 → 切表格「无 canvas」→ 切回来仍「无 canvas」`）。② **根因**：建图 effect 依赖 `[hostRef]`，而 ref 物件身份永不变 ⇒ 它**只在组件挂载那一刻跑一次**；宿主是「数据到了才渲染」或「切走再切回」时，那一刻 `hostRef.current` 是 `null`，实例永远不会建出来（**静默无画布，控制台干净**）。③ **修法（修 hook，不逐个打补丁）**：宿主参数改为**回调 ref**（调用方 `<div ref={hostRef} />` 一字不改），宿主出现/消失都变成一次 state 变化，建图 effect 与 `setOption` effect 跟着重跑；**`option` 那条 effect 也必须把宿主写进依赖**——换回来的是一张新实例（空的），而 option 本身没变。四处调用点（因子 IC / 分层 / 热力图 / 分布图）同步切换，净值曲线组件退回最朴素的「图 ⇄ 表」写法。④ **两个既有界面验证脚本当时为什么没逮到**：它们**也走了**「看表格 → 看图」，但只验了表格里的数、**没验图回来没有**——动作做过不等于断言到位。补上后因子页 **15/15**、优化页 **14/14**，判据连画布**尺寸**一起看（零尺寸容器会让 ECharts 按兜底 100×300 建画布，压扁的图也有像素）。⑤ 回归：前端 **283** 纯函数 + typecheck / lint / `pnpm build` 全绿；模拟盘界面验证 **18/18** 不受影响 |
| v1.30 | 2026-10-10 | M6 | **M6b 前端落地回填 —— `/paper` 页交付，M6 整段完成**。① **页面**：会话条（切换 / ＋新建）+ 账户卡（净值英雄数字 + 已实现盈亏 / 现金 / 市值 + 进度条 + **数据截止日如实标注**）+ **决策闸门**（待审批卡：标的 / 买卖 / 预计数量与金额 / 理由 / **来源三元组可展开** / 批准与驳回）+ 推进控制（推进一天 / 跑到结束带**内联二次确认**）+ 净值曲线（单序列 + **成交日 ▲买 ▼卖标记**）+ 持仓表 + 决策流水（六态徽章）+ 每日结算表；页头第 7 项导航、`/space` 第 6 个页签、`?id=` 深链。② **`lib/paper.ts` 纯函数**（28 条 Vitest）：六态 → **语气**（`action`/`waiting`/`normal`/`void`/`alert`，语义与像素分开）+ 决策卡视图（**预估与成交两个数分开说**）+ 来源三元组展示序（**事发与可得并列**）+ 推进回执 + 进度映射 + 请求体构造。③ **界面验证 18/18**（`logs/m6/ui.md`），三条是自动化层结构上看不见的：净值曲线**真的画在画布上**（1366×298、非底色 13340 px）、**批准真的换了状态**（按 `data-status` 断言而非文案）、**推进后账户数字真的变了**（现金 500,000 → 3,816 且持仓库出现该标的）。④ **界面验证逮到四处、两处是真 bug**：**(a) 切了策略后旧参数照发**（`event_driven` 收到 `fast/slow` → 422）——M5b「界面上看不见的东西进了请求」的翻版，修法是同一条：只发该策略**可见**的参数键（`STRATEGY_PARAM_KEYS` 由表单与请求体共用）；**(b) 图宿主「数据到了才渲染」→ `useEChart` 在挂载时 ref 还是 null，实例永远建不出来**（ECharts 静默无画布）；用 `hidden` 藏到有数据为止也不行——它会按**零尺寸兜底 100×300** 建画布（界面验证读数当场露馅）——修法是宿主常驻 + 按真实尺寸 init（`ChartFrame` 的老写法）；**(c) 批准后卡片当场消失**（待审批区只渲染 pending），用户看不到回执——改为闸门区同时给「待审批」与「已批准（等次日开盘）」两态。⑤ 回归：前端 **282** 纯函数（新增 28）+ typecheck / lint / `pnpm build` 全绿（`/paper` 12.3 kB / 首屏 328 kB）；**后端零改动**。设计推导见 `docs/private/Pn-n/P2-Mn/P2-M6b-design-brief.md` |
| v1.29 | 2026-10-10 | M6 | **M6a 后端落地回填 —— 模拟盘从「回测 + 闸门 + 账户」建成（前端 M6b 待做）**。① **落地物**：`app/paper/{types,account,market,replay,store}.py` + `api/paper.py`（7 端点）+ `core/db.py` 四张表与事务出口 + `duckdb_client.{bars_multi,events_multi}` + 沙箱 `kind="paper"` 分支 + `scripts/run_paper.py`（取证 7/7）。② **头号断言成立**：单标的 + 全批 ⇒ 与 `run_backtest` 的 `fills` **逐笔相等**（真数据 600519：2 笔成交、期末净值 1,092,071.15 两边一致）。③ **「推进一天 = 全量重放」实测比规划期还便宜**：20 标的 × 250 个交易日，**每步 288–323ms**（50 日 288 / 250 日 323——成本几乎不随天数增长，说明它由**取数**而非循环主导，印证 F3 的结论②）；纯策略循环 20×181 根 bar 仅 24.5ms。④ **两处只有真数据/真库能验的**：真实**一字涨停**日（002058）批了也成交不了，拒绝码 `REJECT_LIMIT_UP`；真库上**重放对账**（按决策日志重建的账户状态 == 库里存的 `cash`/`as_of`/持仓）。⑤ **实施期逮到三个真 bug，都不是自动化层单测能发现的形状**：**(a) 日志里已成交的决策必须照原样记回账本**（否则「重放是纯函数」不成立——第二轮得到一条没有成交的曲线，决策却显示已成交）；**(b) 从日志种回来的「已批准」挂单会在决策日当天成交**（同一次重放天然不会撞上，带日志回来才会——铁律「T 日生成、T+1 成交」被悄悄改掉）；**(c) 已驳回的决策带着上一轮的成交明细**（决策单自相矛盾；逮到它的是取证 CLI 而非单测——单测里的日志是手搓的，真实调用方却是「拿已成交的结果改状态」）。三处修法与可复用判据已进踩坑记录。⑥ **驱动差异一条**：psycopg 的**异步连接没有 `executemany`**（那是同步 API），批量要自己开 cursor——离线替身全绿什么都不能说明，得有真库用例。**事务与并发怎么测**也有了定式：拿唯一约束当注入点验回滚、直接打 `Database.paper_advance(expected_as_of=旧值)` 验守卫（顺序调用永远撞不上）。⑦ §1 结构、§12 测试增量的落点与本文同步。**既有文件只动了五处，全是加法**：`broker.py`（`_affordable_qty` 上提为模块级 `affordable_qty`，行为不变）、`duckdb_client.py`（两个批量查询）、`events.py` 未改（来源三元组直接从取数行取）、`core/db.py`（+4 表 +9 方法）、`main.py`（注册路由 + 两个处理器）、`strategy/{sandbox,worker}.py`（`_run_worker` 抽出共用 + `kind="paper"` 分支）。**回归：离线 1004 passed**（既有 958 零改动 + 新增 46）、**集成 93 passed**（既有 86 + 新增 7）。证据：`logs/m6/{paper.md, paper.json}`。规划底稿与 F1–F8：`docs/private/Pn-n/P2-Mn/P2-M6.md` |
| v1.28 | 2026-10-10 | M6 | **M6 细化为可执行规格（开工前，用户已拍板 D1–D8）**。① **形态定为「回放式」**：会话按冻结交易日历逐日推进，成交用本地下一根 bar 的开盘价；**实时跟盘判死**——规划期实测行情止于 2026-09-30 且是年度整片重下载（事件已到 10-08），「跟着真实日历滚」在当前数据通道上物理不成立，调度器形态留 P3-E1。② **实测推翻「每步重放太贵」的直觉**：20 标的 × 181 个交易日，逐标的取数 **4,958ms** 而一次批量取数 **480ms（10×）**、纯策略循环仅 **24.5ms**；引擎里最大的单项是 `_limit_bands`（raw 全史 + `Decimal` 逐日）**403ms/标的**——故模拟盘**自建批量取数 + 窗口涨跌停价**，不复用引擎私有取数路径，并以「窗口口径 == 全史口径逐日相等」的断言钉死同源。③ **头号验收断言**：单标的 + 全批 ⇒ 与 `run_backtest` 的 `fills` **逐笔相等**——它成立的前提是股数在**成交时按开盘价**现算，故决策单上的数量定为「按决策日收盘价**预估**」、`est_qty` 与 `fill.qty` 两个数都落库。④ **闸门语义定死**：审批在收盘后、成交在次日开盘（**审批时成交价还不知道**——这是闸门不改价格口径的证明）；六态状态机（`pending`/`approved`/`filled`/`rejected`/`expired`/`unfilled`），非法转移一律 409，**未审批不成交**。⑤ **推进 = 从会话起点全量重放**（纯函数 `state = f(config, 决策日志, 数据)`），这是用户策略（可在模块级持状态、跨 HTTP 请求保不住实例）能正确进入模拟盘的唯一方式；用户策略整段重放放进**沙箱子进程**（`kind="paper"`，零 IPC 协议改造），内置策略在进程内跑同一份实现；**过去的日子以决策日志为准**，故策略非确定性不污染账本（但两次推进可能给出不同决策，如实记边界）。⑥ **账户形态**：标的池 1–20 只（沿用 M5b 上限）+ **等额配额**，单标的是 n=1 特例；**撮合零复制**——`Broker`/`CostModel` 同一个对象语义，多标的经 `SymbolView` 适配器喂「该标的配额现金 + 该标的持仓」，`_affordable_qty` 上提为模块级函数供提案与成交共用；**创建期校验**任一只配额不足一手即 422 并指出是哪只。⑦ **结算与落库**：四张表（账户 / 持仓 / 决策 / 净值），决策表兼订单表与日志表（M7 只读它；`id = uuid5(account|date|symbol|side)` 使重放可复现、落库幂等）；**推进落在一个事务里** + 乐观并发挡双击（0 行受影响即 409）；决策单带**来源三元组**（`Signal.event_id` → 事件行，买入可回链、卖出如实留空）。⑧ §1 结构、§12 三档测试增量与验收同步。**不做**：手工/Agent 下单、审批时改单、部分成交、急停开关、哈希链账本、分红送转处理。规划底稿与 F1–F8 实测：`docs/private/Pn-n/P2-Mn/P2-M6.md` |
| v1.27 | 2026-10-10 | M5c | **M5c-2 前端落地回填 —— `/factor` 页交付，M5 整段完成**。① **页面**：筛选行（因子源 / 方向 / 窗口 / 费用）+ 指标卡（英雄数字 RankIC 均值 + ICIR / t / 有效日 / 池子；`|t| < 2` 挂**「不显著」标记**，判据在纯函数里、组件不自己判）+ 逐日 IC 柱 + 分层净值 + 多空价差 + 如实标注；页头第 6 项导航；`?source=&direction=` 深链，进页面自动出报告。② **配色跑过校验器**：IC 的**离散极性对**用 `up`(红=正)/`series1`(蓝=负)（浅 CVD ΔE **23.8** / 深 **22.2**，六项全 PASS）——既有 `diverge` 极色**不能直接用**（它是给连续色阶插值到灰中点的：深色端 L 0.78 在分类带 0.48–0.67 之外、彩度 0.076 也在下限下），离散极性对按分类六项重选；分层 5 档用**单色相 ordinal 蓝阶**（分位是**有序**类别），浅 `#86b6ef…` / 深 `#184f95…` 各自 `--ordinal` 全 PASS（单调 L / 相邻 ΔL ≥ 0.06 / 光端 ≥ 2:1 / 单色相 3°），**深色不是翻转**（暗底上越亮越高，且刻意避开 IC 负极的 `#3987e5`）。③ **表格孪生**（dataviz 硬要求）：IC 逐日表 + 分层「日期 × (Q1…Q5 + 多空)」表，与图同一份数、缺值显示 `—` 不补 0；毛/净是**视图开关**（关费用时净为 null → 开关禁用并说明原因，不静默回落）。④ **界面验证 13/13**（`logs/m5c/ui.md`，含图例裁剪图那条），三条是自动化层结构上看不见的：IC 柱**按符号上色**（画布偏红 6347 / 偏蓝 7660 像素）、**五档蓝阶逐档都在**（Q1 2266 … Q5 2534 px）、**毛/净切换真的换了数**（同一格 净 0.8666 ≤ 毛 0.9602）。⑤ **一次环境坑**：8000 上的后端是改动前起的进程（无 `--reload`），新端点 404 → 界面验证超时；重启后 12/12——**排查界面验证超时先 `curl` 端点**。⑥ **交付后一处修正（用户三轮反馈，同日收口）**：多空图的毛/净两条线**同色、只靠线型区分**，结果在每个小承载面上都读不出来——30px 图例（ECharts 默认虚线节奏 8 实 2 空，1:1 下读作实线）与 8px tooltip 色块（画不下线型）先后被逮到。故按回测页既有约定收口：**颜色定身份 + 线型做二次编码**（净 = `series2` 橙实线 / 毛 = `series3` 紫虚线，CVD ΔE 27.0 / 26.2 六项全 PASS；**选紫不选蓝**——本页蓝已承载 IC 负值与分层两义）；虚线节奏取 `[9, 6]`、图例短线 30px 作为二次编码保留。`equity-chart` 的图例同样是 `icon: "rect"`（丢掉线型信息，但它靠颜色分辨序列、可读性打折而非对不上号），本轮不动既有页面、已记入 brief 备查。⑦ 回归：前端 **253** 纯函数（新增 20）+ typecheck / lint / `pnpm build` 全绿（`/factor` 16.8 kB / 首屏 339 kB）；**后端零改动**。设计推导见 `docs/private/Pn-n/P2-Mn/P2-M5c-design-brief.md` |
| v1.26 | 2026-10-09 | M5c | **M5c-1 后端落地回填 —— 因子分析管道交付（前端 M5c-2 待做）**。① **落地物**：`app/factor/panel.py`（PIT 归属、事件 / 价格面板、前向收益）+ `analysis.py`（RankIC·ICIR·t、分层、多空、换手与费用、tear sheet）+ `api/factor.py`（公开同步端点，不落库）+ `duckdb_client` 四条查询 + `scripts/run_factor.py`（真数据取证）。② **同尺子从口号变成断言**：`bucket_day` 复用 `bar_cutoff` 同一对象，取证脚本把**真实 30.7 万行语料**逐条喂 `EventFeed.advance` 比对归属日——**零不一致**（294,335 条被放行 / 102 个交易日）。③ **一个只有在显式窗口下才现形的 bug 被评审抓住**：`factor_price_rows` 只返窗口内行 ⇒ 归属日历缺「窗口之前」的日子 ⇒ `bucket_day` 的首日兜底把**历史事件整堆灌进窗口第一天**；默认窗口起点=语料起点正好把它藏住。修法是新增 `dc.factor_market_days()`（只取补窗内的交易日，几十行）——归属日历与取数各归其位；回归用例按「先证红（signal_days=1）再证绿（0）」验过。④ **规划期两个读数是错的，实施期订正**：探针把**滑点写成 0.00005（5bps 应为 0.0005）**，故「费用拖累 13.4%」实为 **19.3 bps/日 → 年化 48.7%**，「最低佣金被抬 4.8×」也要按实际组数看（事件因子组 57 只 → **1.1×**；价格因子组 1,100 只 → **22×**），§6 M5c 与规划文档 F7 均已留沿革注。⑤ **如实结论（对外不得反着写）**：事件信号因子 RankIC −0.0081 / t −0.61（**噪声区间内**）、分层不单调、多空 t 0.57；价格反转因子 RankIC +0.0696 / t +2.44 但样本期特定、日间不独立。**两个因子的多空都不可交易**，且 59 天年化是外推值。⑥ **回归**：离线 **958 passed**（既有零改动；新增 53）、集成 4 条真实数据用例通过、取证 7/7 断言过。既有文件只动了三处（`metrics.py` 增一个常量、`benchmark.py` 改为再导出、`main.py` 注册路由一行），**行为零变化**。证据：`logs/m5c/{factor.md, factor.json}` |
| v1.25 | 2026-10-09 | M5c | **M5c 细化为可执行规格（开工前，用户已拍板 D1–D6）**，并**订正 SPEC 自己的一条口径**。① **拍板六条**：因子日归属＝**可用窗口归属**（`前一交易日 15:00 < available_at ≤ 当日 15:00`，即 `EventFeed.advance` 的语义）；前向收益＝`open(t+1)→open(t+2)`（与引擎「信号 bar 收盘生成、next bar 开盘成交」同口径）；净值＝**毛/净双轨 + 费率口径**（**不含最低佣金**——它是「每笔订单 × 资金规模」的函数，规模事实进 `notes`）；范围＝**并入价格反转 / 动量因子**（`lookback=20` 固定、**不做扫描**、两因子全报）；端点＝**同步 `GET /api/v1/factor/report`**（公开、不落库、不走 SSE，实测端到端 <0.2s）；落点由 `backtest/factor_analysis.py` **订正为 `app/factor/{panel,analysis}.py`**（因子分析是独立数据管道，不属回测引擎）。② **订正 v1.18 起的 PIT 措辞**——「`available_at` 落在当日 15:00 之后的不进当日因子」照字面按事件日归日会丢弃 **48.7%** 的面板原料（实测按事件排除 30,568 / 57,403 = **53.3%**，周末与盘后新闻**整类出局**），而可用窗口归属只丢 **3.3%**；同尺子以「同批事件喂两侧、归属日逐条相等」的断言钉死（复用 `bar_cutoff` 同一对象，不复制常量）。③ **规划期实测揭出四件与设想不同的事实**：**有向事件 70.5% 未挂标的**（面板原料只有 21,318 个 (事件, 标的)；person / policy 几乎全无 ⇒ 面板实为 news 独家）；**`factor_value` 与 `factor_scores.score` 同源**（`corr(|fv|, score) = 1.0` ⇒ **没有第二个变体可挑**）；**换手 96.6% / 日**（费率口径年化费用拖累 **13.4%**——**此数在实施期被订正为 48.7%**：规划期探针把滑点写成 0.00005，见 §6 M5c 沿革注）；**事件因子测不出截面预测力**（六种口径 RankIC 均值全在 ±0.01 内、t 全 < 0.55、分层不单调、多空 t 0.64–1.64）——对照的价格反转因子 RankIC **+0.0764** / t≈2.44 但**样本期特定、日间不独立**。**M5c 的产出因此大概率是一份如实呈现「不显著」的报告**，`notes` 必填清单写死「读数在噪声区间内，不是因子有效的证据」。④ §12 补 M5c 三档测试增量（同尺子双跑断言含 **15:00:00 整点**与 15:00:01 两例、HTTP 矩阵、回归零改动判据）与前端纯函数增量；⑤ §1 结构、`验收`行与版本号同步。规划底稿：`docs/private/Pn-n/P2-Mn/P2-M5c.md` |
| v1.24 | 2026-10-09 | M2c 补口 | **四通道新鲜度：先让落后看得见，再让向量索引自己跟上**（用户在 M2c 收口后追问「以后每个交易日都会自动更新吗、嵌入会不会自动补嵌」——答案是一半自动一半不自动，而不自动的那半正是当天手工补了两次的）。① §3 M2c 增 **X4 / X5 / X6** 三条 warn：向量索引落后（本地日分区 ↔ Qdrant `day` 分区，缺任一天即报；**Qdrant 不可达降级 info 而非 error**——索引是派生物）、行情停更（落后 > 5 个交易日；detail 须写明行情是年度整片重下载而非日更）、**归档自己停更**（从 **ETL 台账**读 `archive_last`，**本地读不联网**——E7 的教训；与 E7 的「源侧有、本地无」互补）；② §3 M2c 补口写明四条通道各自的更新者与收口前状态（语料与名称字典已自动、**向量索引与行情没有**），并定 **自动补嵌**：ETL 成功且确有物化后触发增量嵌入，**走子进程不塞进 API 进程**——理由写进 SPEC：批量嵌入跑 CPU 而 API 检索用同一个 CPU 编码器，同一对象两线程并发调用就是 2026-10-09 那次 SIGABRT 的同类风险（`_run_on_device` 刻意不给 CPU 加锁），子进程还买到「崩了不拖垮 API」；护栏为独立锁 / 超时 / 失败只记台账 / 开关 `etl_embed_after_run` 默认关。**行情仍未自动化**（年度整片，重构后进调度器仍留待办）；③ §12 补该补口的测试增量（三条检查的正/落后/降级三态；自动补嵌三断言）；④ 本条沿用 **M1c 补口**的体例，不新开功能 ID |
| v1.23 | 2026-10-09 | M2c | **M2c 第三次核对（10-09 晚）+ 一条既有红断言的处置**。① **21:10 的定时 `daily` 到位**（`archive_last=2026-10-07`、7 天全 `skipped`、调度触发）——至此**三天各自都有 21:10 的定时触发**，SPEC 要求的触发方式如实标注齐了；台账 22 条；② 那次跑完复查，`gap` 又冒 1 个交易日 **10-08**——19:23 / 20:45 / 21:10 三次读到的 `archive_last` 都是 10-07，即该分片**在 21:10 之后才发布**，**不怪调度**；按同一条约定 `--backfill` 补回（3,817 行 / 716 只），语料 **93 天 / 307,704 行**，`gap` 归零、去重与指纹全过，RAG 索引随之增量补嵌（Qdrant 307,704 点 = 语料行数，93 个日分区）。**两次缺口的性质不同、值得分开记**：09-30 差一天就滑出 7 天窗口（结构性风险，补不回就只能靠人发现）；10-08 落在窗口正中，明天的日任务本会自愈。共同判据：**日任务的 `skipped` 只说明「源侧与我本地一致」，不说明源侧发全了——有没有缺口永远看 `gap`**；③ **处置 v1.21 记下的那条红**（`test_data_t2::test_events_cover_the_same_window_as_p1_and_more`，用户拍板「换成能成立且仍有用的形式」）：该测试三条断言里，真正的漏拉判据 `coverage["end"] == archive_last` **本来就是绿的**（回填后更是），红的是末条「样本标的 K 线末端 ≥ 语料末端」——它编码了「行情会追平事件」的假设，而**事件日更、行情是年度整片重下载**，该假设结构性不成立。改为「起点 ≤ 语料起点（可追溯到语料之前）+ **末端 == 本地行情末端**（这只历史没被截断）」，保护意图保留、不再跨通道比进度；停牌导致末条无价的极小概率在注释里写明（全期基线 49 只，体检 B4 盯着）。集成 **86 passed**（原 85 passed / 1 failed）——**本轮唯一一次「改断言」有据可查，不是为了让测试变绿**；④ **提交归属如实记**：M2c 的 SPEC v1.17 与 ROADMAP 改动**被 M5a 的 `bd211ae` 捎带提交**，没有独立的 `M2c` commit；内容完整、验证无碍，用户决定不改写历史（改写=force push）；⑤ 顺带修一处漂移：**头部版本号停在 v1.19 而变更记录已到 v1.22**，本次一并对齐 |
| v1.22 | 2026-10-09 | M5b | **M5b-2 落地回填 —— 前端 `/optimize` 页交付，M5b 整段完成**。① **页面**：模式页签（`?mode=`）+ 一行 filters + 逐格进度 + DSR 卡（英雄数字 + 计量条 + 输入清单）+ 夏普分布（**强调**形态：一片中性灰点 + 一个红点）+ 参数热力图（带**表格视图孪生**）+ 批量表 + 「最近的优化」；`?run=<id>` 重开**不重跑**；`/space` 加第 5 个页签、页头加第 5 项导航。② **色阶用数据定档**：热力图要 diverging（夏普有正负、0 有含义），候选里本项目既有的 A 股红绿（`up`/`down`）跑 CVD 模拟（Machado 2009, severity 1.0）只有 **ΔE 7.6**（deutan）——落在 6–8 的 floor 带（仅在有次级编码时合法）；**蓝↔红是 20.9（浅）/ 13.9（深）**，远高于目标线 8。色相指派取**红 = 正夏普**（与全站 A 股口径一致），7 档色阶的四个臂各自过 `validateOrdinal`（单调 L / 相邻 ΔL ≥ 0.06 / 最浅步对底色 ≥ 2:1 / 单色相 ≤ 40°）全 PASS，推导写在 design brief。③ **界面验证逮到三处只有真浏览器才现形的问题**：**最重的一处是整页白屏**——运行中 `cells` 数组**带空洞**（逐格填充），而热力图/分布/表格的纯函数直接解引用（`TypeError: ... reading 'ok'`），单测里我构造的永远是「跑完之后」的整齐数组、跑不到这个态，改法是遍历入口一律先 `filter(Boolean)` 并补一组空洞回归用例；其次**「界面上看不见的东西进了请求」**——参数被选作轴后基座输入框收起、但 state 里的默认值照发，后端按「轴参数与基座撞键」判 422，改为把「生效的基座参数」抽成 `effectiveBaseParams` 由校验与请求体共用，并从前端校验里删掉「撞键」那一条（界面上已结构性地不可能）；第三是**批量表的键会撞**——`(标的, 策略)` 里没有参数，同一策略带不同参数出现两次（合法请求）会互相覆盖，改为**按格序下标定位**（后端定死的「标的在外、策略在内」本就是契约，集成用例已钉）。④ 一处无障碍硬要求：连续色标必须有表格孪生，故「看表格」不是附加功能而是热力图组件的另一半。⑤ 顺带把 `Field`/`Select`/`Input`/`Check` 从 `backtest-form.tsx` 上提到 `components/ui/form-controls.tsx`（两处共用一套，不复制）；后端补 `overfit.reason_text`（原因码与文案同行，前端不自己拼一句）并去掉没人读的 `X-Optimize-Kind` 响应头。**界面验证 13/13**（`logs/m5b/ui.md`，含两条自动化层看不见的机械证据：热力图画布上 **38.3 万个非底色像素、偏红 8.4 万 / 偏蓝 25.1 万**——diverging 两端真的都画出来了；点格重跑真的跳到了 `/backtest?run=`）。回归：前端 **233** 纯函数 + typecheck / lint / `pnpm build` 全绿（`/optimize` 路由 **36.8 kB** / 首屏 350 kB）、后端离线 **905**。证据：`logs/m5b/`（ui.md / ui.json + 六张图）+ `docs/images/optimize-grid.png`；工具 `frontend/scripts/capture-m5b.mjs` |
| v1.21 | 2026-10-09 | M5b | **M5b 落地回填 —— 批量 / 网格 / 过拟合检验全段交付**。① **真实数据 5×5 网格跑通**（600519 / ma_cross / 2020-01-02 → 2026-09-30 / 1,636 bars）：**25/25 成功**，探针记录**同时在跑的格数峰值 = 2**，并发与串行**逐格 `metrics` / `moments` 相等**；耗时**三次实测：12.40s→11.47s（0.92）、13.08s→11.65s（0.89）、12.31s→11.72s（0.95）**——同一份输入比值逐次在 0.89~0.95 间波动，故判据里**不设阈值**，只如实记录（`logs/m5b/concurrency.md` 存的是当次读数，别把它当成一个固定常数）；最优格 `fast=8, slow=15` 夏普 **0.3027**、总收益 28.12%、回撤 43.55%（指标确定，逐次完全一致）。② **Deflated Sharpe 出数并被交叉验证**：同一网格 **DSR = 0.5183**——「看着不错的 0.30 夏普」在两两比较 25 组参数后有一半概率只是运气，这正是本检验存在的理由；与 **scipy 独立实现差 1.1e-16**，两条正交验算（`SR=SR₀ → 0.5`；γ₄=1 ⇒ 分母为 1 的闭式）在真实输入上复现。**25 格逐格**与独立 `build_report` 的 `metrics` 逐键相等（不止验最优格）。③ **并发口径按 v1.20 的订正落地并取证**：判据是「峰值 ≤2 + 逐格相等」，耗时比值如实记录、**不设阈值**——实测 0.89~0.95 与规划期的 0.83~0.91 落在同一区间（**都是「几乎没提速」**），① 的读数与之互为印证。④ **两处实施期定死的契约**：用户策略的参数归一器**返回填满 `PARAMS` 缺省值的字典**（内置策略原样返回、缺省由引擎 `from_params` 补）——两条路径的差别收在一个 `normalize` 签名后面，网格不各写一套；`summary` 增 `kind` / `cells_total` / `cells_ok`（列表页走 `summary - 'cells'` 投影）。⑤ **v1.20 行内那两处正交验算写错已在实施期订正**（见该行的沿革注），测试以反向断言钉死：把 γ₄ 写成正态的 3 会得到不同的数。⑥ 回归：后端离线 **904 passed**（既有 **823 零改动**，新增 81：overfit 27 / batch 27 / 端点 27）、集成 **85 passed / 1 failed**——新增 6 条（真库 JSONB 往返、SQL 投影 `summary - 'cells'`、归属过滤、级联删除、批量格序）**全绿**；唯一那条红是**既有的数据漂移**，与 M5b 无关：`test_data_t2::test_events_cover_the_same_window_as_p1_and_more` 断言「行情末端追平事件语料末端」，而**行情止于 2026-09-30、事件已推进到 2026-10-07**——这条不对称正是 v1.17（M2c 收口）记下的「须知悉不必处理」，只是断言在事件侧继续前移后翻红了。**已用 `git stash` 剥掉 M5b 全部改动复现同一条失败**，确认非本轮引入；处置（改断言还是刷新行情）单列，本段不动它。⑦ 一处实施期才发现的设计不一致：`run_cells` 的 `on_cell` 是 **await** 的，端点最初传了同步 lambda——7 条端到端用例同时红才暴露。证据：`logs/m5b/{grid.md, concurrency.md, deflated_sharpe.md, grid.json}`；CLI `scripts/run_grid.py`（退出码 0/1，可复跑） |
| v1.20 | 2026-10-09 | M5b | **M5b 细化为可执行规格（开工前，用户已拍板 D1–D4）**，并**订正两处 SPEC 自己的性能读数**。① **规划期实测推翻 v1.18 的并发锚点**：本机 8 核、**DuckDB 默认 `threads=8`**，一次查询就已吃满全部核心 → 25 格网格实测串行 **7.7s**（短窗 65 bars）/ **12.9s**（全期 1,636 bars），并发 2 仅 **0.83 / 0.90**，且**进程池与线程池几乎一样**（0.86 / 0.91）⇒ **瓶颈不是 GIL**。故 §12 的判据由「≈串行 55%」改成「**仪器化探针断言同时在跑的格数峰值 ≤2 且并发结果与串行逐格相等**，耗时比值如实记录不设阈值」；**并发上限 2 保留但价值重新定位**——不是提速，是单格互不牵连 / 不独占服务 / P3-E1 的前置形态，对外不得宣称接近翻倍。② **揭出更便宜的加速点并明确本轮不做**：单格 250–500ms 里约 2/3 是重复取数（`dc.bars(raw)` 全史扫描 **164ms** 最粗，M5a 为算涨跌停价刻意不带 `start`；qfq 窗口读 37ms、事件 30ms，真实计算仅 ~45ms），按 `(标的, 区间, 口径)` 记忆后 **25 格可降到 1.5–1.9s（≈5×）**且 `metrics` 逐格相等——**它是数据层的优化（受益面是整个 API），单独立项，不塞进 M5b**。③ **接口契约落定**：两个 SSE 端点（`/optimize/grid`、`/optimize/batch`）的请求形状与四种帧（`start` / `cell` / `done` / `error`）；**轴数 1–2**（>2 一律 422，不做隐式降维）、**展开后每格都过参数校验、任一格非法即整单 422** 并指出是哪一格、总格数 **2–100**、批量标的 1–20 × 策略 1–10、`pit_mode` 不收 `both`；用户策略的**归属 / 静态检查 / 参数 schema 只在开跑前做一次**；并发上限是**进程级全局 Semaphore**（SPEC 风险表写的是「网格把服务打满」，全局才是那道闸）。④ **`summary` 在「每格 `params → metrics`」上补三个必要字段**（`moments` / `window` / `duration_ms`）——`moments` 是重开时 DSR 能自证的前提，`window` 在批量里逐格不同，不记就分不清那格跑的是哪一段。⑤ **最优格判据固定为夏普最大**（`None` 排最后、总收益破平）——DSR 的「被选中者」必须与选取判据同源，换目标却不换口径会自相矛盾。⑥ **DSR 四条口径写进契约**：SR 一律每期口径（年化值须 `/√252`，混用会静默差 16 倍）、**N 取格数不做相关性校正（保守，如实标注）**、γ₃/γ₄ 取最优格自身日收益的**总体矩**（γ₄ 是原始峰度非超额）、**四个退化分支一律返 `None` + `reason` 绝不硬凑**；三条正交验算（`SR=SR₀ → 0.5`；**γ₄=1** 时分母为 1 可闭式手算；第三条由 scipy 独立实现钉常数）与 `statistics.NormalDist` 的可用性均已核。**本条在实施期被订正过一次**：初稿写「正态收益（γ₄=3）下分母为 1」是**错的**——正态时 `(γ₄−1)/4 = 0.5`，分母是 `√(1 + SR²/2)`；分母恰为 1 的条件是 `γ₄ = 1`（对称两点分布）。同处另一个笔误「`N=1 → SR₀=0`」也一并改正——`N=1` 时 `Φ⁻¹(0) = −∞`，**无定义**（返 `None`）；`SR₀=0` 对应的是 `V=0`。两处均在 `test_overfit.py` 里以反向断言钉死（写成正态反而会得到不同的数）。⑦ **取消语义**：断线后**停止派发新格、已在跑的最多 2 格自然跑完且不落库**（不取消 in-flight——`to_thread` 本就取消不了，硬取消还会在沙箱里漏子进程）。规划与实测底稿：`docs/private/Pn-n/P2-Mn/P2-M5b.md` |
| v1.19 | 2026-10-09 | M5a | **M5a 落地回填 —— 规则与地基全段验收通过**。① **A 股规则接进引擎**（`a_share_rules.py` 纯函数 + 五码）。涨跌停价 = `round(前收_raw × (1±幅度), 2)`，四舍五入走 `Decimal`——内置 `round()` 是银行家舍入且在二进制表示上失真（`round(2.675, 2)` 给 2.67），**算钱不能用**。**既有 golden 分毫不动**：真实数据上规则开 vs 强制关，`metrics` / `equity_curve` / `trades` 逐块相等（演示标的 2026 年零一字板，已实测）。② **三处降级一律往「判大 / 跳过」倒**——幅度判大只会漏判（真实涨停价低于算出来的，价格够不着），判小才会误拦；故未知板别返 `None` 而不猜 10%、缺名一律按非 ST 而不猜 ST。除权日（分红送转下调参考价 → 算出的价偏高）**漏判不误判**，识别它要比对 qfq/raw 比值跳变，成本高收益低，**不做**。③ **名称字典三重过滤后一名多写只剩 5 只**（`688111` / `600876` / `600778` / `000830` 的错配名连同事件的 `symbols` 都没进，直接消失；`920438` / `000002` 被归一化合并），众数选名与人工核对 **10/11 一致**，残留 `301699` 如实记着；覆盖率复核 **96.7%（5,610 / 5,799）——与 v1.16 同口径**（规划期一度误记为 99.2%，错在把 2026 分片的「无名 44 只」放进了全期分母）。④ **等权基准的剔除数与一条独立 SQL 精确吻合**（同为 102 条）；同区间三个数：策略 -6.53% / 买入持有 -9.72% / **全市场等权 -1.90%**——把代理写成「沪深 300」会让这个差异无从解释。⑤ 报告新增四块（`rejects` / `meta.benchmark` / `meta.a_share_rules` / `equity_curve[].market`）与两个端点（`cross-section` / `symbols`）；字典随 ETL 刷新，**建失败不拖垮 ETL**（状态进台账）。⑥ **界面验证 13/13**，抓到一类新问题——**旧数据形状**：新增字段在 M5a 之前落库的记录里不存在，直取属性会**整页白屏**（「我的回测」点开任何一条旧记录都会崩），已改为可选并按缺失处理。同批订正一处**错误断言**：ECharts 的图例与折线都画在 canvas 上，`text=` 定位器撞不到（撞上了是撞到别处的同名文案），改读画布像素数三个 series token。证据：`logs/m5a/`（limit / naming / benchmark / ui 四份 + `ui.json` + 四张图）+ `docs/images/backtest-benchmark.png`；回归：后端离线 **823 passed**（既有 665 零改动，新增 158）、前端 170 + typecheck / lint 全绿。**`pnpm build` 需在 dev 停掉后复跑**：它与 `next dev` 抢同一个 `.next/`，并行跑会把 dev 打成「页面出得来、CSS 一律 404」的混合态（本次已踩） |
| v1.18 | 2026-10-09 | M5 | **M5 细化为可执行规格并切片**（开工前，用户已拍板）。① **切 M5a 规则与地基 / M5b 批量·网格·过拟合 / M5c 因子分析**——a 是唯一动既有引擎的一段，独占并全量回归；b、c 不动既有链路。同批拍板三条：进度走 **SSE 流式逐格推送**（不建队列）、网格与批量**落汇总 `optimization_runs` 不落完整报告**、涨跌停**从 raw 前收自算**。② **规划期实测揭出五个与 v1.16 设想不同的事实**：**换手率源侧停更**（`turnover_pct` 2026-07 及以前 100% → 08 月 55% → **09 月 0%**，端点如实返 `null`、UI 不渲染该列）；**停牌是死代码**（`is_suspended=True` 全库仅 **7 行**，均 2020-06；无价 bar 已在查询层剔除，等价于顺延到下一根可交易 bar）；**一字板有真样本且演示标的干净**（2026 年 876 行近涨跌停的一字板，而 600519 / 000001 / 300750 **零行**，故规则接上后既有 golden 不应位移——这本身是回归判据）；**名称覆盖率复核为 96.7%**（5,610/5,799，与 v1.16 的 96.7% 同口径；**规划期一度误记为 99.2%，错在把 2026 分片的「无名 44 只」放进全期分母**，已在 §6 写明沿革）；**因子横截面很薄**（有向事件日均只覆盖 **199 只**，占全市场 3.6%）。③ **名称字典补三重过滤**（同事件 `symbols` 内 + 行情宇宙内 + 归一化众数）——归档 `stocks` 混着港股美股（`005930.KS`「SK海力士」），甚至外部代码与 A 股**撞号**（`000660`）——① 挡后缀型、② 挡宇宙外、③ 挡撞号型。**一名多写原始 11 只，三重过滤后只剩 5 只存别名**（4 例错配名连 `symbols` 都没进）；众数选名与人工核对 **10/11 一致**，唯一残留 `301699`（众数取到新股前缀期的截断名），字典带别名与冲突标记，**对外不宣称 100% 准确**。④ **涨跌停不能信 `change_pct`**：实测与 raw 自算日收益 **0.3% 偏差**（集中在除权日）+ **4.8% 缺失** → 改为 `round(前收_raw ×(1±幅度),2)`，ST 幅度只能来自名称字典（231 只）且历史区间无名称、如实按板块默认；**除权日漏判不误判**记入已知边界；缺 raw 序列降级标注。⑤ **基准剔除口径**：聚合前剔 `|change_pct|>30%`（2026 有 102 行新股首日/复牌，raw 极值 +1942%，单只即可把日均拉高 **0.36pp**），剔除条数入报告；**不写成「沪深 300」**。⑥ **订正「复用回测引擎跑分层组合」措辞**——引擎单标的、分层是日频截面再平衡，套不进去；改走独立小循环 + 复用 `CostModel` 与 `metrics.py`。⑦ §12 补 M5a/b/c 三段测试增量（含「一字拒单与开板正常成交**成对样例**」与「既有 golden 分毫不动」两条断言）与前端纯函数增量 |
| v1.17 | 2026-10-09 | M2c | **M2c 落地回填 —— M2 整段收口**（第二段：三天台账核验）。① §3 M2c 补收口读数：三天齐、无重复四层与无漏拉四层全过、`gap` 归零、体检 error 0 / warn 5（与第一段同一组挂账）、退出码 0；触发方式如实标注（两天定时 + 一天重启兜底 catchup + 一次手动 backfill）；② **当日归档停更 9 天后恢复发布**（09-29 → 10-07），积压 9 天撞 7 天回落窗口，**`2026-09-30` 这个交易日被滑窗漏掉**——第一段交付时预警的风险一天不差地发生，按同一份文档写下的约定 `--backfill` 补回（6,512 行 / 967 只）；语料 84 天 / 289,519 行 → **92 天 / 303,887 行**，RAG 索引随之增量补嵌 8 天；③ 两条可复用判据进档：**台账的 `failed` 不等于缺数据**（失败的是「对已有分区的重拉」，撞小石 CLI 版本门槛；真正的缺口判据只有本地算的 `gap`），以及「恢复发布那天先看 gap、>0 就 backfill」**不依赖知道平台何时恢复**、可当例行；④ 一处须知悉不必处理的口径不对称：行情仍停在 09-30 而事件到 10-07——`resolve_window` 的 `end` 取最后一根 bar，多出的 7 天永不被消费，冒烟实测照常（行情刷新已记待办，可延）。证据：`logs/m2c/{etl_three_days.md, health-after-backfill.json, embed_incremental.log}` |
| v1.16 | 2026-10-09 | M5 · M10 | **「股票展示 / 信息查询」定范围并回填实测**（用户提问「5000 多只股票要不要做展示」→ 决定**不新开功能 ID**，拆进 M5 前置与 M10）。① **名称拿得到**——先前记的「数据源没有名称数据集 ⇒ 按名称搜索做不了」只对**行情表**成立；事件语料的 `stocks` 列是 `[{code, name, reason}]`，实测覆盖 **5,608 / 5,798 = 96.7%**，同一份小石语料、不碰「不引第二数据源」的红线；② §6 M5 新增**横截面数据能力**作为选股/分层的前置——现引擎单标的（`BacktestConfig.symbol`），`cross-section` 端点实测全市场排序 **20ms**（5,572 只有价）；名称字典随 ETL 刷新（全量重建 74ms），并写明 A 股的三类一名多写（`N`/`C` 新股前缀、`XD`/`XR`/`DR` 除权前缀、全半角与改名）与**「名称带时点」**（改名有 `available_at`，字典不是静态的）；③ §11 M10 补**标的浏览**条目（复用既有端点，不假装有财务与权威行业分类），并订正板块热力图口径——行情源没有行业字段（M2a 已核实），只能走事件 `industries`，且其覆盖按类型不均（news 45% / policy 100% / announcement 0%，**M3 记的「0%」只对公告成立**），热力图必须标注覆盖率而不是画成完整的一张图 |
| v1.15 | 2026-10-09 | M1c | **M1c 补「加自选表单的即时反馈」**（用户反馈「现状是提交才知道」：重复加要等 409、代码打错一位也照样加得进、之后价格永远显示「—」）。① §2 M1c 新增 `GET /api/v1/market/{symbol}/probe`（路由在行情层、消费方是自选股表单）——**「没有」是答案不是错误**，无数据回 200 `has_data: false`，与 `/{symbol}/bars` 的 404 分开；复用 `latest_closes`（实测热 34ms / 冷 311ms），故支持**输满 6 位即查（防抖 300ms）**；② 定死四态界面口径与按钮行为表——**已在自选**（判据取前端已加载列表，不走服务端）按钮变「移出」且**要确认**（`added_price` 是加入时的事实，删掉再加重来会重置涨幅基点）；**本地无此代码**则禁加（覆盖面 5,798 只 A 股，提示带数据截止日）；**行情层不可用时不拦**——沿用既有降级口径；③ 写明前端判重只是**提示**，裁决权仍在 `POST` 的 409 与 `UNIQUE(user_id, symbol)`；④ §12 补 probe 三态与表单提示口径的纯函数矩阵。本条原写「**不做**按名称搜索（数据源无名称数据集）」——**当天即被 v1.16 推翻**：核查只看了行情表，名称在事件语料里（覆盖 96.7%） |
| v1.14 | 2026-10-08 | M4c | **M4c 落地回填**（M4c-1 后端 + M4c-2 前端两段）。① §5 前端补三处实现期定死的点：**Monaco 主题与全站 token 同源**（自定义 `quantsage-light/dark`，取值全来自既有 token；`editorWarning` 必须覆盖——Monaco 默认的 warning 是绿色，与本项目语义冲突）；**新增语义色 `--warn`**（浅 `#B45309` / 深 `#E0A030`，对比度实测 5.02 / 4.68 / 7.88 全过 AA）——此前没有 warning 色，检查面板若借 `--destructive` 会让用户分不出哪几条会拦运行；语法高亮是**第三层色**（与 UI 语义、图表序列并列，代码里的琥珀色不代表警告）。② `--warn` 是 M4c 唯一一处动全站配色的改动。③ 记录两处界面验收才暴露的问题：**CORS 的 `allow_methods` 漏了 `PUT`**（策略更新走 PUT，浏览器预检直接拦掉，TestClient 与 HTTP 矩阵都看不见——同类坑第二次，已补 `tests/test_api_endpoints.py` 的 PUT 预检断言）与**sticky 运行条遮住末段内容**（已加底部留白）。④ §12 补前端纯函数增量（`lib/strategy-form.ts`：findings → 标注、schema → 表单、运行请求体、错误体还原）。落地物：`app/api/strategies.py` + `app/core/db.py`（`strategies` 表 / `backtest_runs` 两列）+ `app/api/backtest.py` user 分支 + `app/main.py` 四个处理器 + `frontend/{components/strategies/*, lib/{strategy-form,monaco-theme}.ts, app/(app)/strategies/}`；证据：`logs/m4c/{http_fault_matrix,workshop,workbench-ui}.md` + `docs/images/{strategies,strategy-markers}.png`；回归：离线 652 / 集成 80 / 前端 148 + `pnpm build` 全绿 |
| v1.13 | 2026-10-08 | M4c | **M4c 细化为可执行规格（开工前，用户已拍板 D1–D6）**：① §5 持久化补 `strategies` 表约束（名字 1–60 字符、code ≤64KB、撞名 409）与 `backtest_runs` **两个**增列（`strategy_id` + `code_sha256`，不设外键）；② 写死 **HTTP 闸门顺序**（鉴权 → 请求体 → 互斥 → 归属 → 静态检查 error=0 → 参数 → 沙箱）与**错误码映射表**（检查未过 422 带 findings / 参数 422 / 运行期被拒 422 带 line / 沙箱终止 400 带 kind）；③ 七个端点补响应形状，`POST /strategies/check` 定为 **`{findings, meta}`**（meta = 参数 schema + `USES_EVENTS`，`PARAMS` 坏掉时为 null）——表单与标注同源；④ 记录 `POST /backtest` 的 additive 增量（`strategy_id` 互斥校验、user 分支走沙箱、`compare_pit` 判据泛化为 `USES_EVENTS`）与 runs 摘要/详情的两个新字段（`strategy_name` / `code_sha256`，后者由**前端比对**得出「已非当次代码」，服务端不造派生布尔）；⑤ 前端补齐：工作台结构、**Monaco spike 实测结论**（本地资源 + AMD loader，零外部请求；磁盘 24.4MB 不入库、路由包 3.26 kB、编辑器首屏 1.17MB gzip；两条实现期事实 + pnpm 装依赖）、「保存并运行」流程、运行条最小面、回测页**只跑内置**与 `"user"` 的显式处置（`formFromRequest` 查表会 TypeError）；⑥ §12 补 M4c 离线（CRUD / 检查 / 闸门 / 沙箱路径 / HTTP 故障矩阵）与集成（DDL 幂等 / 归属矩阵 / 全链路 / 删策略后旧记录可开）两段。规划与落地记录见 `docs/private/Pn-n/P2-Mn/P2-M4c.md` |
| v1.12 | 2026-10-08 | M4b | **M4b 落地回填**：§5 M4b 补三处实现期定死的契约——① `Rule` 增 `message` 字段（规则自身的一句话说明），并写明**同一条规则可拆成多条 `Rule`**（R3 双档、R1 两臂），`Finding.rule` 仍是同一个规则号；② **行号归属**（M4c 的编辑器标注直接吃）：节点规则指命中节点行，R4 的「读了事件却没声明」指**第一处**事件读取行（不刷屏）、「声明了却不读」指 `USES_EVENTS` 行；③ 模板**装载 API** 形状（`TEMPLATES` / `get_template` / `template_source`，未知 key 抛 `KeyError`；源码只读文本、不作模块导入）。落地物：`app/strategy/static_check.py`（规则表 R0–R5 + 单遍遍历器，`check_source` 从不抛异常）+ `app/strategy/templates/`（5 份源码 + 装载 API）+ `scripts/check_strategy.py`。**证据**（`logs/m4/`）：规则矩阵 35 正样例全命中本规则、29 负样例全零命中（含切片、`closes[i+1]` 相邻比较、生成器裸 `return`、变量名 `bfill`）；CLI 在一份「从别的框架搬来」的样例上一行一条拦下 6 个 error（R1/R2×2/R3/R5×2，带行号，退出码 1），干净样例零命中退出码 0；R3 双档实测——`ctx.history[i+1]` 只 warning 且退出码 0（不拦运行）；与运行时一致性（白名单同对象、禁用名与 `_SAFE_BUILTINS` 无交集、15 条入口形态上 R5 与 `load_strategy` 结论一致）；**模板等价性真数据逐点一致**（ma_cross 1,636 点 / 50 笔、event_driven 55 点 / 3 笔，去掉三个身份键后整份报告相等）；全量回归 616 passed（新增 109，既有 507 零改动） |
| v1.11 | 2026-10-08 | M4b | **M4b 细化为可执行规格**（开工前，用户已拍板）：§5 M4b 从 9 行扩为规则表——**新增 R0 语法**（`check_source` **从不抛异常**：语法错也是一条 finding，否则编辑器没得标）、R1 数据绕行（白名单**读运行时那一份** `api.ALLOWED_MODULES` 不复制，禁用名补 `__builtins__`；`os`/`subprocess`/`socket`/`duckdb`/`pandas` 由 import 臂自然覆盖）、R2 未来函数（`ffill`/`pad` 与 `.shift(正数)` 不拦）、**R3 定档为双档**（`error` 只留给「明说当前 + n」的 `ctx.index + 正数` / `len(ctx.history) + 正数`，`ctx.history[i+1]` 一类降 warning——理由：R3 拦不住真实泄漏，而 `error` 档会让**合法**策略在「运行前强制 error=0」下跑不起来；切片一律放行，自有列表不判，覆盖边界如实写进 docstring）、R4 兼收 `parse_meta` 的拒绝（`PARAMS` 非字面量等归**声明**类）、R5 订正为「形参必须是 `(ctx)` 或 `(ctx, p)`」并增拦 `async def on_bar` 与 `return Signal(...)`（生成器 `on_bar` 的裸 `return` 豁免）；findings 契约定为 `{rule, line, severity, message, snippet}`（**增 `message`**），`Rule.match(node, ctx) -> str \| None` 返回详情串、由遍历器统一组装；§5 模板库补 `Template{key,…,builtin}` 结构与**遍历 `TEMPLATES` 自动配对**的等价性测试（断言恰好 2 个带 `builtin`）、3 个新模板的合成触发序列证据、模板头注释三段（含「故意不给 pandas」的产品立场）、**模板不进内置注册表**（两处真源）；§12 补 M4b 测试增量（负样例清单 + 元测试 + 白名单同源与 AST/`inspect` 判据一致性双跑）；§5 验收写明 ③ 的检查器本体属 M4b、编辑器标注与 422 闸门属 M4c。规划与落地记录见 `docs/private/Pn-n/P2-Mn/P2-M4b.md` |
| v1.10 | 2026-10-08 | M4a | **M4a 落地回填**：§5「用户策略 API」补两处实现期定死的契约——① `on_bar` 的形参是 **(ctx) 或 (ctx, p)**，参数取值走第二个形参而非注入全局名（父侧校验为了不 spawn 给 422、子侧再确认保证字典完整）；② 内存配额**不按平台分叉**（Linux 上虚拟地址空间动辄 >2GB，照 512MB 设 `RLIMIT_AS` 会在 import 期自杀），一律走 `ru_maxrss` 看门狗。落地物：`app/strategy/{api,params,sandbox,worker}.py` + 引擎注入点 `run_backtest(config, strategy=None)` + 报告 `strategy_kind`/`strategy_name` 两键 + 内置策略 `validate_params`（**关闭待办「后端不校验策略参数的值」**）+ `scripts/{run_user_strategy,sandbox_fault_matrix}.py`；证据见 `logs/m4/`（故障矩阵 11 条全处置且每条之后服务可用、用户源码版双均线与内置 ma_cross 在 1,636 根 bar 上逐点一致） |
| v1.9 | 2026-10-08 | M4 | §5 从 9 行扩写为可执行规格并切 **M4a 沙箱与用户策略 API / M4b 静态检查器与模板库 / M4c 持久化与前端工作台**；开篇写明分工判断——**主闸门是结构（`ctx` 截断 + PIT 过滤 + 无 pandas/IO），import 白名单是正确性闸门，AST 是第二道门**，对外不得写反；用户策略 API 定稿（模块级 `on_bar` + `PARAMS` schema + `USES_EVENTS` + 可选 `validate_params`，白名单不含 pandas）；**沙箱配额按规划期实测定档**——CPU 走 `RLIMIT_CPU`（macOS 生效），**内存不能用 rlimit**（macOS `setrlimit`/`ulimit -v/-d` 实测均不可设，改 `ru_maxrss` 自监控线程），墙钟父进程兜底，威胁模型如实限定为可用性（容器隔离留 P3-E8）；规则库定稿 R1–R5 并**订正 `ffill` 不拦**（只用过去值，PRD 亦只写 shift(-n)/bfill）+ 负样例不误报为硬要求；模板库 5 个（含 2 个内置策略源码等价版，配等价性测试）；**运行契约＝先保存才能跑**（存 `strategy_id` + `code_sha256`），保存允许草稿、运行前强制 error=0；`strategies` 表 + `backtest_runs` 增列（须 `ADD COLUMN IF NOT EXISTS`）+ 7 个端点；前端新开 `/strategies` 工作台、Monaco 走本地资源（spike 不过回落 CodeMirror 6）、跑完跳 `/backtest?run=`；§1 落点与 §12 测试增量同步；**关闭待办「后端不校验策略参数的值」** |
| v1.8 | 2026-10-08 | M3 | **M3 落地回填**：§4 M3c 写入实测三档分数（dense 0.7764 / hybrid 0.7602 / rerank 0.8142，验收通过）与两条如实记录的发现——**稀疏腿在本语料无增益反而略损**（−0.0162，已核实稀疏向量非空）、**精排增益 +0.0540**；**精排候选数由端到端实测定档为 50**，并给出 10 / 20 / 50 三档的质量-延迟权衡表（关键读数：增益主要来自第 21–50 名，只精排 20 篇时与稠密档打平）；NFR 冲突的处置列三条出路交用户裁决 |
| v1.7 | 2026-10-07 | M3 | §4 M3a 回填 spike 实测：**fastembed 路线否决**（0.8.1 为 PyPI 最新版仍不支持 `BAAI/bge-m3` 的 dense 与 sparse，reranker 也无 `bge-reranker-v2-m3`）→ 定 FlagEmbedding + torch；**CPU vs MPS 用数据定档**（CPU batch=4 22.8 条/s、峰值 1.0GB；MPS 仅快 1.57 倍却吃 4.2GB 且有长跑泄漏 ⇒ 选 CPU），全量外推约 3 小时；**精排实测 30–34 ms/对**（50 候选 ≈1.6s），NFR「检索 p95 ≤500ms」的冲突留 M3c 报告后由用户裁决；补两条下载期实测坑（Xet 通道卡死须 `HF_HUB_DISABLE_XET=1`；`cache_dir` 必须指 `<模型目录>/hub` 否则白下 2.3GB）；§1 脚本落点改为 `build_rag_eval.py`（评测四步：生成/池化/标注/报告）与 `spike_rag_encoder.py` |
| v1.6 | 2026-10-07 | M3 | §4 从 5 行扩写为可执行规格并切 M3a / M3b / M3c：**三条实测约束**（289,519 条语料无长文本 ⇒ 分块与父子文档不落地为代码；公告 83% 摘要为空、`industries` 覆盖 0% ⇒ 过滤项一律可选；`dedup_key` 亦非唯一 ⇒ point id 用 `uuid5(day\|event_id)`）；落点 `app/rag/`（encoder / collection / embed / retrieve）与两个薄壳脚本；**嵌入运行时先 spike 后定**（FlagEmbedding vs fastembed，用吞吐/内存/sparse 支持定栈）、MPS 需分段重启规避内存泄漏；**RRF 的 k 显式传 61**（Qdrant 默认 k=2，论文 k=60 在其公式下等价 k=61，不显式传即换了算法）；payload 索引必建（`available_at` range 为 PIT 过滤的性能前提）；PIT 过滤**必须服务端**且验收含「高度相关但晚可得的事件不挤掉合法结果」的行为证据；评测集构建口径（LLM 生成 + LLM 分级 + 人工抽检）、基线口径（**相对消融 + NDCG@10 ≥ 0.5 绝对下限**）；§1 补 `app/rag/` 与脚本落点；§12 补 M3 测试分工（离线用假编码器 + 内存向量库替身，集成对真实 Qdrant） |
| v1.5 | 2026-10-07 | M2c | §3 M2c 细化为可执行规格：落点 `data_health.py` / `delisting.py` / `run_health_check.py` 与 `Check` 接口 + 三条硬规则（每检查自带 try/except、共用一条连接、集合式 SQL 禁逐标的 API）；检查项 B1–B8 / E1–E7 / X1–X3 逐条定级并写死基线；**E7 明确不得复用 `runner.status()`**（其 `archive.coverage()` 走子进程 CLI，不可达时会把「查不到归档」静默显示成「缺口 0」）；**B7 由「跨年边界」改为每日 bar 数包络**（原判据被 B2+B8 覆盖且会命中 500 只停牌股）；**B5 自洽性容差写死 0.011pp**（容差取 0 时命中 762 万条，判据不可复现）；**退市股核实口径由「对照全市场清单」改成数据内双证据链**（本地无标的清单、小石无证券主数据、归档无 symbol 维度），按 8 候选 / 2 确证 / 6 待解释如实呈现；「真实三天连跑」改为**分两段收口**；顺带修三处漂移——头部版本号、M2c 验收的 353 条回归锚点由 `(event_id, content_hash)` 改锚 `event_id`（与 M2b 口径自相矛盾）、events 列名 `original_url` / `lineage` 实测不存在 |
| v1.4 | 2026-10-07 | M2b | §3 M2b 细化为可执行规格：**事件语料通道改归档按日整片**（实测 MCP 单次 500 硬顶 + `cursor` 被 FastMCP 拒收 ⇒ 结构上不可翻页，单日任一主要类型即触顶；归档为 `date × event_type` 整市场分片，逐片 sha256，保留期为滚动窗口）——MCP 退回只服务对话实时查询；**日历只落冻结文件 + DuckDB 视图**（PG 表延后，无消费方）；日历验证由「抽查 1 年」升级为**与 21 片行情 `trade_date` 全期双向对账**；**落盘 schema 按 `quant-event-v2` 重定**（新增 `dedup_key`/`revision_id`/`is_corrected` 等 9 列，去重键以平台 `dedup_key` 为准）；**新增事件驱动策略的时间收口**——判据为本地语料覆盖区间（起点固化、终点随日增前移，不写死「最近 3 个月」），显式越界 400、报告带 `event_coverage`；日任务改「拉最近 N 个交易日」的回落窗口 |
| v1.3 | 2026-10-07 | M2a | §3 拆成 M2a / M2b / M2c 三段并把 M2a 细化为可执行规格：全市场 21 片与扁平命名（glob 不变）、**P1 按标的文件必须清掉**（唯一静默错误点）、物化先暂存后换入、数据版本指纹锚定逐片 `object_key` + sha256（顶层 manifest_version 不可用作锚点）、**无价 bar 不进定价路径**（`no_turnover_observed` 整行价量为空，全市场 271 行/复权）、满规模查询实测；**关闭两项待验证**（小石不覆盖指数、`cn-daily` 不含行业字段）并写明对 M5 基准与 M10 热力图的影响；事件长历史口径复核（归档 2026-07 起才有量，P1 结论成立）；M2b 记入交易日历来源的已拍板方案与事件语料归一化要求 |
| v1.0 | 2026-10-06 | M1–M10 | 初版：P2-Mn 技术规格（含竞品调研后扩容的 15 项新功能与 M10 研究首页） |
| v1.2 | 2026-10-07 | M1c | §2 M1c 细化为可执行规格：自选股六端点与分组名/「默认分组」约束、行情层不可用时的降级口径；回测落库（`request` 存解析后 config、`runs` 摘要取 metrics 整块、落库时机）、`POST /backtest` 纳入鉴权与信封化、**鉴权先于参数校验（未登录 401 覆盖 422）**；个人空间四页签与两条深链、会话列表增补 `last_active_at`；§1 自选股不单开路由（`watchlist/` → `space/`）；§12 补 M1c 增量与 P1 回测用例的回归口径 |
| v1.1 | 2026-10-06 | M1 | §2 细化为可执行规格并切 M1a / M1b / M1c：四张自有表与幂等建表机制（不引 Alembic）；**会话归属真源改用自有表，不再直读 checkpointer 内部表**；auth 端点、cookie 属性、密码 72 字节上限、`JWT_SECRET` 缺失的降级姿态；越权 404 规则与公开/受保护边界；`POST /backtest` 响应改信封 `{run_id, report}`（M1c）；§1 补 `core/db.py` 与前端路由组 `(app)/`；§12 补 M1 测试分工与双跑口径 |
