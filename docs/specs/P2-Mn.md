# QuantSage SPEC · P2-Mn（MVP）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.6 待评审）→ 本文（技术规格）→ 代码
>
> 版本 v1.12 ｜ 2026-10-08 ｜ 状态：M1 已落地（M1a / M1b / M1c）；M2a / M2b 已落地；M2c 第一段已交付（待 10-09/10 回填三天台账证据）；**M3 已落地并验收通过（M3a / M3b / M3c）**；**M4a 已落地（沙箱与用户策略 API）、M4b 已落地（静态检查器与模板库），M4c 待开工**；M5–M10 随各功能开工滚动过审
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
├── agent/                  # 深路径 fan-out 角色 A 股化（PIT 基本面 / 政策面）
├── backtest/               # + a_share_rules.py / overfit.py / factor_analysis.py
├── data/                   # + calendar.py（冻结交易日历）/ trading_calendar.json（冻结文件）
│                           #   data_health.py（体检检查器，M2c）/ delisting.py（退市覆盖核实，M2c）
├── etl/                    # 事件语料接入（M2b）：archive.py（归档通道）/ store.py（日分区落盘）
│                           #   runner.py（回填与日增量编排）/ scheduler.py（APScheduler 定时）
├── rag/                    # RAG（M3）：encoder.py（编码器协议 + 懒加载）/ collection.py（Qdrant 索引）
│                           #   embed.py（逐日分区批处理 + 断点续跑）/ retrieve.py（过滤 → 双路 → RRF → rerank）
├── paper/                  # 模拟盘：account.py / broker.py / settlement.py / decisions.py
├── memory/                 # 决策记忆：decision_store.py / settle.py / reflection.py
├── strategy/               # 用户策略与沙箱（M4）：sandbox.py（子进程执行器：配额/超时/输出上限）
│                           #   worker.py（沙箱子进程入口）/ static_check.py（AST 前视检查，纯函数 + 规则表）
│                           #   params.py（PARAMS schema 校验，纯函数）/ templates/（模板源码，服务端为唯一真源）
├── core/                   # + auth.py（哈希 / JWT / 依赖）/ db.py（自有连接池与幂等建表）
│                           #   scheduler.py（ETL 与到期结算定时）
scripts/                    # + generate_calendar.py（生成冻结日历）/ audit_calendar.py（日历×行情对账）
│                           #   download_events.py（改薄壳：回填 / 日增量 / 状态）
│                           #   run_health_check.py / embed_events.py（嵌入薄壳）
│                           #   spike_rag_encoder.py（M3 运行时实测，结论见 §4）/ build_rag_eval.py（评测集：生成/池化/标注/报告）
frontend/app/               # + (app)/（受保护路由组：守卫 + 页头）login/ register/ space/（个人空间）paper/
│                           #   strategies/（策略工作台，M4）dashboard/（研究首页）research/[id]/（研报页）
│                           #   自选股不单开路由，是 space/ 的一个页签（M1c 定）
frontend/components/        # + auth-provider.tsx space/ strategies/（编辑器/参数表单/检查面板）dashboard/ evidence/ agent-run/ calendar/
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
- 深链回原页完整恢复：会话历史 → `/?thread=<id>`；我的回测 → `/backtest?run=<id>`（载入存下来的完整报告，**并把表单回填成该次请求**；跑完一次把地址更新为 `?run=`）
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

**只报告不修的两条挂账**（各出一条 info Check 明示，否则「只报」没有落地）：`DataNotReady` 判据仍是「目录里有没有 parquet」；`backtest_runs.request` 不含数据版本指纹与快照版本（M2a 遗留①，留 M5）。

**退市股覆盖确认（生存者偏差核实）**

口径必须是**本地可得清单**，不是「全市场清单」——本地无标的清单文件，小石 10 个数据集里没有证券主数据 / 退市清单，归档是 `(date, event_type)` 分区、**无 symbol 维度**。故走数据内双证据链：

1. **全期结构证据**：按 `max(trade_date)` 早于数据末端的标的清单与年度分布（基线 226 只 = 16 / 20 / 43 / 45 / 52 / 30 / 20）。完整清单只进 `--json`，人读只出一行
2. **窗口内逐只实证**：事件语料覆盖区间内，末日落在区间内的标的中，有退市类事件（`direction ∈ {退市风险, 停复牌}` 或标题含「退市」）者**确证**，无公告者列为**待解释**（长期停牌与退市在纯行情数据里不可辨）。基线：**5 只候选 / 2 只确证 / 3 只待解释**（`002898`、`920305` 均有「进入退市整理期」公告；`000004`/`002808`/`300029` 窗口内无公告）——**如实写，不得表述为「逐只实证通过」**。候选的判据是「末日落在**语料窗口内**」：早于窗口起点结束的标的，语料里本来就不可能有它的事件，拿它当候选是把「无从取证」混进「取了证」

**验收**（§3 共享，M2c 结束后整段可签）：全市场日线 DuckDB 秒级查询；**注入脏数据时体检脚本报警**（三步证据：离线单测逐项断言 → 脚本级用例 `main([...])` 退 1 且 JSON 里能定位到对应 error → 一次真实 CLI 调用留终端输出）；**交易日历与全市场行情 `trade_date` 全期双向对账一致**（例外逐条解释）；全市场语料对 P1 三标的的 **353 条锚点包含性回归通过——锚 `event_id`，不锚 `content_hash`**（平台一天内修订过 255/353 条，锚 hash 会把「平台改内容」误判成「我们丢数据」；既有用例在 `tests/integration/test_data_t2.py`）；**事件驱动回测在语料覆盖区间外显式报错，不空转**（`app/backtest/report.py` 已实现，本轮补一条断言该 400 的用例）；**ETL 连续运行 3 天无重复、无漏拉**——本片**分两段收口**：第一段交付脚本与核实，第二段（10-09/10）核对 `_meta/etl_runs.jsonl` 出现 3 个自然日的运行记录并逐日核验无重复、无漏拉。触发方式须写进交付文档：调度器是**进程内** APScheduler，只在 uvicorn 存活时 21:10 才跑；进程不在则该日补跑一次日增量并在文档里如实标注是调度还是手动；不足 3 天如实记录并顺延。

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

| 表 | 要点 |
|---|---|
| `strategies` | `id UUID PK` / `user_id FK→users ON DELETE CASCADE` / `name TEXT` / `code TEXT` / `params JSONB`（最近一次保存的参数值） / `created_at` / `updated_at`；`UNIQUE(user_id, name)` |
| `backtest_runs` | **增列** `strategy_id UUID NULL`——`CREATE TABLE IF NOT EXISTS` 不会给既有表加列，须 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（沿用 M1a 幂等 DDL 机制） |

- **运行契约（2026-10-08 拍板）：先保存才能跑**。回测记录存 `strategy_id` + `code_sha256`；「我的回测」重开与 M7 复现成立，改码后 hash 不匹配即如实标注「已非当次运行的代码」。不做内联源码试跑
- **保存不因 findings 被拒**（存的是草稿，响应随带 findings），**运行前强制 `error`=0**——闸门设在回测提交前（PRD 口径），不在保存时
- `POST /api/v1/backtest` 增量：`strategy="user"` 时必带 `strategy_id`（互斥校验，缺一或同给即 422）；报告 `meta` **增** `strategy_kind`（`builtin|user`）与 `strategy_name`，其余结构不动（M1c 信封 `{run_id, report}` 不变）

**端点**（`app/api/strategies.py`，全部 `require_user` + 归属 404，同 M1c 口径）：

| 端点 | 行为 |
|---|---|
| `GET /api/v1/strategies` | 本人全部策略（列表摘要） |
| `POST /api/v1/strategies` | 建；`UNIQUE(user_id, name)` 撞名 409 |
| `GET /api/v1/strategies/{id}` | 单条（含 code） |
| `PUT /api/v1/strategies/{id}` | 改（同样允许草稿）；越权 / 不存在 404 |
| `DELETE /api/v1/strategies/{id}` | 200；不存在 404；已跑过的回测记录不受影响（报告 JSONB 自足，`strategy_id` 悬空即可，不级联删） |
| `POST /api/v1/strategies/check` | 源码 → findings（编辑器标注用；纯函数，不落库） |
| `GET /api/v1/strategies/templates` | 5 个模板源码 |

### 前端（M4c）

- 新开工作台 `app/(app)/strategies/`：左列表（我的策略 + 模板，模板「另存为我的」）｜ 右编辑器 + 参数表单 + 检查结果面板 + 「保存并运行」
- 编辑器 **Monaco**，**走本地资源**（拷贝 `monaco-editor/min/vs` 到 `public/` + loader 指本地，**不用 CDN**——与弃用 `next/font/google` 同一条理由）；开工前 spike 定集成方式，**不过则回落 CodeMirror 6**（届时同步改 PRD 措辞）
- findings → 编辑器行号标注（`setModelMarkers`）；参数表单由 `PARAMS` 生成，前端只做即时反馈，**值域规则不重复造**（后端 M4 起已校验）
- 跑完跳 `/backtest?run=<id>` 复用整套报告 UI；`/space?tab=strategies` 从占位改为列表 + 入口
- 开工前出 **design brief**（沿用 T6 口径，与 P2-M4c 同目录）

**验收**：① 在线写策略可成功回测（模板与自写各一条端到端证据）；② 坏代码不拖垮服务（故障注入矩阵：死循环 / 大内存 / 语法错 / 缺 `on_bar` / 超长输出 / 数据绕行——各给明确错误码且 API 存活）；③ 注入含 `shift(-1)` / `bfill` 的策略被拦截并提示行号（编辑器标注截图 + API 422 findings）。**分工**：③ 的检查器本体与 CLI/矩阵证据属 **M4b**，编辑器标注与回测提交前的 422 闸门属 **M4c**。

**本轮不做**：并发队列与批量回测（M5）；在线装包；策略分享/市场；用户策略调 RAG（M8）；容器级隔离（P3-E8）；编辑器高级能力（补全 / 跳转 / 实时 lint 之外的特性）。

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
- M1c 增量：自选股离线用例注入内存业务库（未登录 401 门 / 重复 409 / 越权 404 / 分组 UPDATE 语义 / 涨幅与 NULL 口径 / 行情层不可用时的降级），**内存替身必须照抄 `UNIQUE(user_id, symbol)` 语义**，否则 409 用例是假的；回测落库用例验信封形状、列表只列本人、越权 404
- M1c 回归：`POST /api/v1/backtest` 改信封与加鉴权后，P1 既有的 12 个离线回测用例统一挂 `signed_in` 夹具并改读 `body["report"]`；集成侧两个回测用例改为跑 lifespan + 真实注册（裸 `TestClient` 没有 `app.state.db`），账号在 teardown 里清（`users` 级联清 `watchlist` 与 `backtest_runs`）
- M2b 增量：交易日历纯函数（长假边界 + 与行情 `trade_date` 的全期双向对账脚本）；`symbols` 归一化矩阵（`code` 为 null / 带 `.SZ/.SH` 后缀 / 多标的 / 无标的）；归档分片合并的**幂等**（同日重跑逐字节一致、中途 kill 后重跑与干净运行逐字节一致）；`list_contains` 按标的过滤；事件驱动窗口收口（显式越界 400、缺省仍取语料起点）。集成侧对真实归档分片跑一次小窗口回填，回归 P1 的 **353 条包含性**
- M3 增量：**离线用例不下载模型**——编码器是协议，测试注入假实现（返回确定性向量）；文本构造（空字段不写标签）、point id 稳定、filter 构造（`as_of` 必带）、RRF 融合排序、NDCG 计算（对已知排序手算）、长度分桶、增量判据（sha 变 / 不变）、`RagNotReady` 降级各一组。检索层的 PIT 边界在**内存向量库替身**上离线跑（替身必须照抄「服务端过滤」语义，否则边界用例是假的，同 M1 内存业务库口径），另在集成侧对**真实 Qdrant** 跑 upsert → 检索 → 按日重建幂等
- M4 增量：**AST 规则样例矩阵**——每条规则正/负样例成对，断言行号与 severity 与 `message` 关键词；`PARAMS` 校验矩阵（未知键 / 类型不符 / 越界 / 跨字段）；入口解析（缺 `on_bar` / 语法错 / 运行期异常 / 返回值非 Signal 列表）；**沙箱故障注入**（死循环撞 CPU 与墙钟、超大分配撞内存看门狗、超长输出被截断、`sys.exit` 与 `os._exit`、导入白名单外模块）逐条断言「明确错误 + 父进程存活」；**模板等价性**——双均线与事件驱动模板经沙箱跑出的报告与注册表策略逐点一致（离线，真实 Parquet 小窗口）。集成侧对**真实 Postgres** 跑策略 CRUD 与归属矩阵（越权 404 / 撞名 409），并跑一条「保存 → 检查 → 沙箱回测 → 落库 → 重开」端到端
- M4b 增量：负样例必须**零命中**：`[-1]`、`i-1`、`ctx.history[a+1:b+1]` 切片（双均线模板原样）、`for i in ...: closes[i+1]` 相邻比较、字符串与注释里的 `shift(-1)`、名为 `bfill` 的变量、`fillna(method="ffill")`、生成器 `on_bar` 的裸 `return`、嵌套函数里的 `return 1`；**元测试**断言规则表每条规则都在样例矩阵里有正/负样例；**一致性测试**双跑——① R1 用的白名单与运行时 `api.ALLOWED_MODULES` 是同一对象 ② 同一批入口/签名样例上「R5 命中 ⟺ `api.load_strategy` 抛 `StrategyRejected`」（AST 与 `inspect` 两套判据不许漂移）；模板库 5 个各自 `check_source == []` + `load_strategy` 装载成功，3 个新模板各配一条**合成触发序列**断言至少 1 笔成交
- 前端：Vitest 只测纯函数（日历日期映射、瀑布图数据映射、证据面板分组、自选股分组视图与 symbol 校验；M4 增：findings → 编辑器标注映射、`PARAMS` schema → 表单状态与请求体），不引组件测试框架（沿用 P1 口径）
- 离线用例继续走真实 Parquet，不 mock 查询层（沿用 P1 口径）

## 13. 变更记录

| 版本 | 日期 | 关联 | 变更 |
|---|---|---|---|
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
