# QuantSage SPEC · P2-Mn（MVP）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.21）→ 本文（技术规格）→ 代码
>
> 版本 v1.62 ｜ 2026-10-31 ｜ 状态：**M1–M10 全部交付并验收**；M4c-3（回测页可选「我的策略」）已交付，见 §5 M4c。2026-10-31 追加 P0 修复：模拟盘重放读全量决策日志、`run` 批量裁决与推进同事务、深研报报告落库失败固定 `failed`（见 §13 v1.61）；追加 P1 修复：研报部分唯一索引、停机 drain 与 research 启动扫尾（见 §13 v1.62）。逐段决策、实测读数与验收见 §13。
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

> P2 在 P1 结构上新增的模块边界；**当前完整目录与职责见 [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md)**。本节只留增量索引，不再维护逐目录树。

| 层 | P2 增量 |
|---|---|
| API | `auth` / `watchlist` / `etl` / `paper` / `dashboard` / `calendar` / `optimize` / `factor` / `reports` / `research`；`sse` 为共用帧编码 |
| Agent | `agent/deep/`：router / roles / merge / debate / synthesize / risk_gate / budget / graph / runner |
| 回测与因子 | `a_share_rules` / `benchmark` / `batch` / `overfit`；`factor/` 为独立面板与分析管道 |
| 数据与 ETL | `calendar` + 冻结 JSON / `data_health` / `delisting` / `naming` / `bars` / `xiaoshi_cli`；`etl/` archive · store · runner · scheduler |
| RAG / 模拟盘 / 记忆 / 报告 / 策略 | `rag/` · `paper/` · `memory/` · `report/` · `strategy/`（契约见各节） |
| 前端 | 受保护路由组 `(app)/`；`space` / `paper` / `strategies` / `optimize` / `factor` / `research` / `r/[token]`；组件按域分目录 |

## 2. M1 用户系统

> 本功能同时清理 P1 遗留阻塞项（会话无归属校验，P1 SPEC §4 记录），是 P2 的第一道门。
> 切片：**M1a** 后端地基与归属校验 → **M1b** 前端认证闭环 → **M1c** 自选股 · 个人空间 · 我的回测（三段均已交付，逐段见 §13）。

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
**本轮不做（记录待议）**：密码重置、邮箱验证、refresh token、记住我、第三方登录、资料页（改密 / 改邮箱）、全局 401 拦截（守卫层已覆盖）、旧会话认领脚本、多设备会话管理（**不做**——P3-E8 已拍板撤销粒度按用户、不做按设备，理由见 P3 SPEC §8.3；欠账于 P3-E2 轮关闭）。

## 3. M2 数据层

> 切片：**M2a** 全市场行情扩容 → **M2b** 交易日历 + 日增量 ETL → **M2c** 数据体检 + 退市股覆盖核实 → **M2d** 数据新鲜度自动化（行情刷新进调度器）。

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

### M2d 数据新鲜度自动化（行情刷新进调度器）

> 定位：M2 数据层的**补口**（与 M1c / M2c 同级）。不改架构、不引新依赖，只在既有调度器上加一路——让「数据落后」不再需要人记得手敲。
> **形态先纠一条口径**：行情**没有日增量可拉**——`cn-daily` 的源侧维度是 `{year, adjust}`（`xiaoshi_client/plans.py`，**无 `date`**），发布形态就是「按年整片、内容寻址」。所以本片做的是**「便宜探测 → 按需整片重下」**：把「落后几天」压到「落后一天」。**不是实时**（实时形态是 P3-E1 的事）。

**口径（开工前拍板 + 实测依据，2026-10-12）**

- **源侧节奏**（口径与沿革）：`cn-daily` 按年整片发布、没有日增量可拉 ⇒ 本片的上限就是「落后若干交易日」。**规划时写的「源侧节奏是 T+1」是 10-12 单次观测下早了，10-13 实测订正**：平台有 **canonical（`quality=complete`，我们在用的）** 与 **available** 两条轨道——10-13 21:42 探测时 canonical 仍停在 **2026-10-09**（连缺 10-12、10-13 两个交易日），而 available 的 `available_last` 已到 **2026-10-12**（但 `quality_status=partial`、有缺口，且平台字段明写 `fill_or_forward_fill_allowed=false`）。**滞后是观测值、不是契约**，对外不得承诺「落后一天」；我们固定用 canonical（宁慢不脏，M2a 的取舍），两条轨道**不许混进同一个数据目录**（CLI 会报 `quality_track_conflict`）。**待决**：要不要把 available 轨道用于「提前量可见性」（只显示、不喂回测），属 P3 的可见性话题
- **成本三档**（实测本机分片）：日常一个交易日只动**当前年 3 片**（raw 22.6 + qfq 33.8 + hfq 42.4 = **约 99MB**，历史年分片是冻结的，2025 片止于 12-31）；除权触发 qfq 全量重算时才接近**整片 860MB**（21 片，实测全量 236s）；源侧无更新时 **0 字节**——未变分片由 CLI 本地校验复用（`_download_verified` 命中同 sha 即返回「复用」）。**整轮耗时见本节末「落地实测」——不是「1–1.5 分钟」**（那个数只算了下载，没算 CLI 对未变分片的重算 sha）。
- **门禁是两级的**（判据不能只看交易日）：
  - **一级（1 次 API 调用）**：`coverage` 返回的发布标识 `manifest_version` 与本地上次同步记录比对，不同才往下走。实测该字段与回执里的**同一个值**（`_cmd_coverage` 打印 `manifest['version']`），且探测当天得 `history-public-v1-20261012T130505Z` 而本地记的是 `…20261005T160503Z` ⇒ **判据当场跑通**。
  - **二级（零网络）**：跑满 21 次 `download` 后，把回执里 21 片的 `object_key → sha256` 与 `_meta/bars.json` 的 `fingerprint.shards` 比——**全同 ⇒ 状态 `up_to_date`、不动物化**（`data/bars/` 零写入）；有异 ⇒ 走既有 verify + materialize + write_meta，**一字不改**。
  - **为什么不只比交易日**：**原地内容修订不改交易日却换数据**——实测同一批分片来自多个 release（2020–2022 的 raw/hfq 出自 `20260908`、qfq 出自 `20260930`），前复权在新的除权除息后会整体重算。只比 `coverage.last` 会漏掉这一类，而它正是护城河要防的口径漂移。`coverage.last` **只进台账与展示，不作判据**。
- **门禁记忆落 `data/_meta/bars_sync.json`**（原子写：`last_probe_at` / `manifest_version` / `coverage_last` / `last_change_at` / `status`）。**不写进回执**——回执由两个通道共用、按最新 publication 被重写（实测：10-12 夜里的语料那轮把行情的 21 条条目**冲掉了**，`same_publication` 为假时 `files_state` 清空重填）；也**不塞进 `bars.json`**——那是数据指纹契约，体检 B1 逐片重算它。
- **本地早退（零 API 调用）**：触发时先看 `cal.last_session_on_or_before(today)` 是否已被本地行情覆盖，覆盖则记 `skipped_no_gap` 直接收工（周末与长假自然命中，且不误伤「停机几天后周末补拉」）。**日历越界不影响刷新**——`CalendarOutOfRange` 时跳过该检查、照常探测：日历到期这颗炸弹不该由 M2d 引爆（X1 已在报 warn，本片再加一条台账 note）。
- **回执互踩的三条处置**：① 门禁**绝不读回执**；② 每轮**跑满 21 个 spec**，不做「只补变了的那年」的优化（回执里可能已经没有它们）；③ materialize 前的 21 片齐全核对是唯一兜底，保留不动。
- **整轮预算是硬的**：per-call `timeout = min(300, 剩余预算)`，超时杀子进程 → `incomplete`。理由不是纸面的：`download_bars._run_cli` **原本没有 timeout**，实测 10-12 手敲那轮卡在网络 20 分钟零写入（`logs/bars-refresh.txt`，卡在 `2020/qfq`），而它一直握着 `runner._LOCK` ⇒ **两个通道一起冻死到重启**。
- **两个 job 显式互斥**：复用 `runner._LOCK`（两个通道共用 `data/raw`：同一份回执 + 同一个 `.xiaoshi-execution.lock`），撞上就**跳过并留痕**（不排队等、不撞 `data_directory_busy`）。
- **429 / `bulk_download_required` 是停止信号**：整轮中止、留台账（`retry_after_seconds` + 已下片数），**不重试**，次日再来（沿用 M2b 口径）。其余异常只记日志与台账，不带崩调度器。

**落点**

- `app/data/xiaoshi_cli.py`（新）：收口 `_run` / `_load_json` / `_retry_after`，`app/etl/archive.py` 与 `app/data/bars.py` 共用——**纯搬移、行为零变化**（archive 的 300s 超时与 429 识别原样保留）。
- `app/data/bars.py`（新）：M2a 的下载 / 校验 / 物化 / 清单四段搬进来，加 `refresh() -> BarsReport`（`status ∈ updated / up_to_date / skipped_no_gap / rate_limited / incomplete / failed`）。对齐既有范式：`scripts/download_events.py` 只是 `app.etl.runner` 的薄壳。
- `scripts/download_bars.py` 改薄壳（只负责参数与打印）；**`--skip-download` 路径行为一字不变**是硬判据。
- `app/etl/scheduler.py` 加第二路 job（**不新起第三个调度器**：同一运行域、同一把锁、同一 status 出口），独立开关 `BARS_REFRESH_ENABLED`；代价如实记——`ETL_ENABLED=false` 时两个 job 都不起。
- 配置项：`bars_refresh_enabled`（默认关，与 `etl_enabled` 同风格）、`bars_refresh_hour/minute`（**21:40**）、`bars_refresh_budget_seconds`（1200）、`bars_call_timeout_seconds`（300）、`bars_refresh_catchup_on_start`（默认开，延迟 180s——语料 catchup 在 +20s、实测约 1 分钟）。
- **时段 21:40 的依据（含 10-13 的沿革）**：10-12 实测发布时刻在 21:05–21:29（版本戳 `…20261012T130505Z` = 21:05:05+08、回执 `remote_generated_at` = 21:29），语料 job 在 21:10 ⇒ 21:40 在两者之后，既不踩发布也不撞锁。**但发布时刻并不固定**：10-13 21:40 那一跑看到的最新版本戳是 `…20261012T200508Z`（= 10-13 **04:05**），当晚并没有 21:05 那次发布 ⇒ 这个时点的准确表述是「**典型发布之后**」，不是「一定在发布之后」。**错过一次发布不会丢数据**：门禁是内容驱动的（发布标识 + 分片 sha），下一次跑照样追得上，代价只是数据多停一晚。
- `/api/v1/etl/status` 的 `scheduler` 块加 `bars`（enabled / hour / minute / budget / next_run_at），并加 `last_bars_run`（读台账里 `scope=bars-refresh` 的最后一行）。
- **可见性（用户 2026-10-12 选做）**：`GET /api/v1/market/freshness` 增加**源侧边界**（`source_bars_last` / `source_checked_at`，取自 `bars_sync.json`，**本地读、不联网**——沿用 E6/X6 的规矩），页头 tooltip 显示「源侧止于 X」；X5 的 detail 文案删掉「见待办『行情刷新进调度器』」（落地后即过期）。**动机是实测**：10-12 首页显示行情止于 09-30，而本地其实**只落后源侧 2 个交易日**（源侧 10-09），体检 X5 却只报 `[info]`——「落后是谁的锅」当时无处可查。

**不做（本段明确排除）**：实时跟盘（P3-E1）；多 worker 分布式调度（跨进程靠 CLI 文件锁兜底，如实记）；自动重生成日历（要升级库，做不到自动）；改调度器架构（仍是进程内 APScheduler，只在 uvicorn 存活时跑）；改下载形态（仍整片，不做按标的或按日切片的增量）；语料通道的任何改动。
（顺带排除：契约里另有 `daily-date`（按日切片）与 `adjustment-factors`（全量仅 26.6MB），看上去能拼出「日增量 + 本地复权」；**不采用**——qfq 在除权后全量重算历史，按日累出来的是不同 vintage 的拼盘，回测复现性当场作废。另实测：`daily-stock` / `daily-date` 在契约里有、**manifest 里没有** ⇒ 平台当前未发布。）

**验收（逐条给证据）**

1. **门禁真省调用**：本地已追平最近交易日时跑一轮 ⇒ `skipped_no_gap` 且**零 API 调用**（判据：台账 + 日志里没有 `download` 调用）；
2. **一级变、二级不变**：造发布标识变化而分片全同的场景 ⇒ 状态 `up_to_date`、**零网络下载、`data/bars/` 零写入**（mtime 不变）；
3. **有更新时真下**：造「分片 sha 变化」的场景（`--force`，或把某片本地产物挪走）⇒ 21 片下齐、原子换入、`_meta/bars.json` 指纹更新；
4. **两个 job 不并发**：events 持锁时触发行情 job ⇒ 后者**跳过并留痕**（不排队、不撞 `data_directory_busy`）；
5. **429 整轮中止**：注入 `Retry-After` ⇒ 台账 `rate_limited` + `retry_after_seconds`，且**没有重试**；
6. **整轮预算是硬的**：把预算压到 1 秒 ⇒ `incomplete` + 已下片数被记录，且子进程**真的被杀掉**；
7. **零回归**：后端离线基线全绿；`download_bars.py --skip-download` 行为一字不变；`_meta/bars.json` 的 `fingerprint.shards` 与 21 片 sha 一字不变。

**测试策略**：门禁判据是纯函数（`should_refresh(local_state, probe, today, sessions) -> bool`）⇒ 逐条手搓断言，不碰网络；CLI 调用**不 mock 子进程本身**（要验的是真拉得起来），但走 `--skip-download` 与既有本地分片，别真下 99MB；调度断言（互斥 / 早退 / 429 / 预算）全在**假时钟 + 假锁**上跑，不真等 cron。

**落地实测（2026-10-13，真联网四跑）**

| 场景 | 实测读数 | 判据 |
|---|---|---|
| 真刷新（9 片变：**qfq 2020–2026 七年全重算** + raw/hfq 当年） | **518.7s / 395MB**；本地 **09-30 → 10-09** | 换入 9 片、指纹更新、`status=updated` |
| 发布标识未变 | **8.3s / 1 次调用 / 零下载会话** | `status=up_to_date` |
| 伪造发布标识逼过一级门禁（= 二级门禁单跑） | **192.9s / 21 次会话 / 零下载 / `data/bars/` 的 mtime 一字未动** | `status=up_to_date`，note「分片 sha 与本地指纹全同，未动物化」 |
| `--skip-download`（零回归硬判据） | 21 片**指纹、行数、日期区间逐字不变**；合计 **24,006,374 行**——与源侧 `coverage` 的 `rows` 相等 | 重构未改行为 |

- **成本口径订正**：本节先前写的「日常 1–1.5 分钟」**低估了本地校验**——CLI 对未变分片也要逐个重算 sha（888MB），21 次会话本身约 **190s**；再加上物化（复制 888MB + 逐片 sha + DuckDB 计数）与下载，**一个交易日约 6–7 分钟**。整轮预算 1200s 对常见日有 ~2.8× 余量；某天若 21 片全变（冷启动、大规模修订）会判 `incomplete`，次日按 sha 续跑——**这是设计好的行为，不是故障**。若日后嫌那 190s 白花，可把一级门禁从「顶层发布标识」收窄成「`cn-daily` 的 `coverage.last` 有没有前移」；代价是**同日期修订要等下一个交易日才被发现**（现在不会漏）。
- **两处落点 delta（都写了理由，不是随手挪）**：① 互斥锁与台账分别从 `runner` 抽到 `app/data/{data_lock,ledger}.py`——`app/data/bars.py` 要用它们，而 data 层反向 import etl 会成环；**同一把锁、同一个台账文件**，语料侧的调用面一字未改（`runner.EtlBusy` / `runner.is_running()` 照旧）。② `xiaoshi_cli.run()` 超时统一抛 `CliTimeout`，`archive._run` 把它翻成 `ArchiveError`——于是 `status()` 那条**既有承诺**（「归档不可达时状态仍要能看」，E7 的教训）第一次真的覆盖超时；此前 `TimeoutExpired` 会穿透 `except ArchiveError` 变成 500。这是**唯一的**行为变化，如实记。
- **顺带落地**：`/api/v1/etl/status` 的 `scheduler` 块加 `bars` 子块与 `last_bars_run`（台账按 scope 分路，语料的 `last_run` 不再可能被行情那路顶掉）；体检 **X5** 的 detail 增列源侧边界与上一轮结果、**X1** 的余量写进每轮台账的 note（`cal.runway_note()`，阈值单一来源）。

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
- **威胁模型（2026-10-17 安全修复后）**：命名空间仍只挡无意 / 半有意绕行；**真正的边界在 `isolation.py`**——macOS `sandbox-exec` / Linux `bwrap`，只读挂载代码与数据、写只允许本次运行临时目录、禁网络与任意 exec、环境变量清洗且不读仓库根 `.env`；`STRATEGY_ISOLATION=require` 默认 fail closed。**残余风险**：`fork` 炸弹（CPU 配额是单进程的）与 C 扩展长跑仍按可用性处理；完整容器化 / 独立用户属部署层

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

> 切片：**M4c-1** Monaco spike + 持久化 + 端点 + 闸门（本段）→ **M4c-2** design brief + 前端工作台 + 证据 → **M4c-3** 回测页可选「我的策略」（2026-10-14 追加，P3 期间修订）。

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
- **回测页可跑内置策略与「我的策略」（M4c-3，2026-10-14 修订）**。原条款为「回测页仍只跑内置策略」（2026-10-08 拍板）——修订理由：M4c 之后平台已有用户策略、优化页的策略选择器早已是「内置 + 我的策略」，而回测页**读得到、跑不了**（能打开一条用户策略的回测记录却不能跑一条），同一页面的能力不对称；且「入口唯一」早已名不副实（工作台与优化页「点一格重跑」走同一端点跑用户策略）。规格：
  - 策略下拉分两组：「内置」两条（与后端注册表 `available_strategies()` 对齐）、「我的策略」按 `GET /strategies` 的既有顺序（`updated_at` 倒序）；**一条用户策略都没有时不显示空分组**
  - 选中用户策略时**仍守「先保存才能跑」**：请求带 `strategy_id`（`BacktestRequest` 与 `Strategy` 联合类型**无需改动**，`Strategy = BuiltinStrategy | "user"` 早已就位），跑的是库里那条源码；**回测页不提供内联源码试跑**
  - 参数表单按**源码声明的 `PARAMS`** 渲染：`GET /strategies/{id}` 取 code → `POST /strategies/check` 拿 `meta.params` → `userDescriptors()`（与工作台、优化页同一条通路，纯函数件直接复用）。**`meta` 为 null（`PARAMS` 解析不出来）时不放行**，提示去工作台检查——「这条策略现在跑不了」与「这条策略没有参数」是两回事，不许编成同一句话（优化页已按此写）
  - **静态检查仍在提交期 422 + findings**（闸门顺序一字不改，沿用既有异常映射）；回测页没有编辑器，findings 以「规则 · 行号 · 消息」列表渲染在错误区下方——**不搬 Monaco 标注**（那是工作台的形态）
  - `?run=` 打开一条用户策略的旧回测：报告照常渲染，表单回填到**当次**的策略位与参数（按 `request.strategy_id` 取当前策略比对两个 hash，「已非当次运行的代码」提示条照旧）；**策略已删（404）则退化为改动前的行为**——只回填标的 / 区间 / 费用 + 提示条 + 「去工作台重跑」
- **`lib/backtest-form.ts` 的字面量联合必须显式处置 `"user"`**：`formFromRequest` 里的 `PARAMS[strategy]` 查表会 TypeError（打开一条用户策略的旧回测即触发）——M4c-3 之后它从「防御分支」变成「真分支」：用户策略的参数由 `meta` 给，查表只在内置那一支

**M4c-3 落地落点**（2026-10-14）：`lib/strategy-params.ts`（`ParamDescriptor` + 两个转换件，从 `lib/optimize-form.ts` 搬来并加 `type`——回测页与优化页现在共用同一张参数表）；`lib/backtest-form.ts` 的六处签名改为 descriptor 驱动（`defaultParams(descriptors)` / `strategySelection` / `paramsMatch` / `validateForm(state, descriptors)` / `buildRequest(state, descriptors)` / `formFromRequest(..., descriptors, strategyId)`）+ `strategyOptions` 分组；`components/strategies/use-catalog.ts`（`useUserStrategies` / `useDescriptors` 从优化页目录搬来，两页共用）；`components/backtest/backtest-form.tsx` 的 `<optgroup>` 分组与 bool/数字两种控件；回测页的 schema 到位后补参、重开回填、findings 文本列表。**走查**：`frontend/scripts/capture-m4c3.mjs`（**11/11 通过**，证据 `logs/m4c3/` 三张图）。
- **主题与全站 token 同源**（M4c-2 落地）：自定义 `quantsage-light` / `quantsage-dark` 两个 Monaco 主题，底/前景/行号/选区全部取自既有 token（`vs-dark` 的 `#1E1E1E` 与面板 `#131722` 并排一眼不同套）；**`editorWarning` 必须覆盖**——Monaco 默认的 warning 是**绿色**，与本项目语义冲突。语法高亮（关键字 / 字符串 / 数字 / 注释）是**第三层色**，与 UI 语义 token 和图表序列色并列：代码里的琥珀色**不代表警告**
- **新增一支语义色 `--warn`**（M4c 唯一一处动全站配色的改动）：浅 `#B45309` / 深 `#E0A030`，对比度实测 5.02（面板底）/ 4.68（页面底）/ 7.88（深色面板底），全过 AA。理由：此前没有 warning 色，检查面板若借 `--destructive`，用户看到一屏同色**分不出哪几条会拦住运行**（error 才拦），而那正是本页的核心交互
- 开工前出 **design brief**（沿用 T6 口径，与 P2-M4c 同目录；**已出并落地**，含两处如实记录的偏离）

**验收**：① 在线写策略可成功回测（模板与自写各一条端到端证据）；② 坏代码不拖垮服务（**HTTP 层**故障注入矩阵：死循环 / 大内存 / 语法错 / 缺 `on_bar` / 超长输出 / 数据绕行——各给明确错误码，且每条之后 `/health` 200、正常策略仍跑通）；③ 注入含 `shift(-1)` / `bfill` 的策略被拦截并提示行号（编辑器标注截图 + API 422 findings）。**分工**：③ 的检查器本体与 CLI/矩阵证据属 **M4b**，编辑器标注与回测提交前的 422 闸门属 **M4c**。

**本轮不做**：并发队列与批量回测（M5）；在线装包；策略分享/市场；用户策略调 RAG（M8）；完整容器化 / 独立用户（P3 收尾期已落 OS 级 `sandbox-exec` / `bwrap`，见 P3 SPEC §8.7）；编辑器高级能力（补全 / 跳转 / 实时 lint 之外的特性）。

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

- ~~**断线即断、不续传**，刷新等于重跑~~ ——**2026-10-14 由 P3-E1b 取代**（见 §13 v1.59 与 `docs/specs/P3-En.md` §2.8）：执行搬进任务队列，SSE 从「执行载体」降为「进度订阅」，**断开只是没人听了**——任务照跑完并落库，回来看得见（`?run=` 重开或 `GET /tasks/{id}/events` 续流）。当时的理由与本条一并作废：不再是「停止派发新格」而是**订阅者离开**，中止改由 `POST /tasks/{id}/cancel` 显式触发（协作式，且中止的结果不落库）。原口径（供对照）：停止派发新格、已在跑的最多 2 格自然跑完且不落库——不取消 in-flight 的理由至今成立（`to_thread` 取消不了，硬取消会在沙箱里漏子进程）
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
- **`run` 的批量裁决与推进同事务**：`approve=all/none` 对 pending 决策的状态变更**不单独落库**；它在内存中应用到决策日志，并与重放结果一起在 `paper_advance` 的事务里写回。重放失败或乐观锁 409 时，不得留下任何已批准的决策。`origin='agent'` 的提案仍排除在批量裁决之外。
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
- **重放读全量日志**：`step` / `run` 的重放必须读取该会话的**全量**决策日志（账户上限 20 标的 × 250 交易日 ⇒ 至多 5,000 条）；`GET /accounts/{id}` 的展示列表仍可截断（当前 500 条），但**展示截断不得复用于重放**。日志读取失败时拒绝推进并明确报错；若未来改为分页读取，必须校验读取条数与账户决策总数一致，不得用策略输出补齐历史。

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
- **报告幂等唯一索引（2026-10-31 P1 修复）**：`(account_id, snapshot_hash)` 建**部分唯一索引**（`WHERE account_id IS NOT NULL`）；`create_research_report` 用 `ON CONFLICT DO NOTHING`，撞上就回查既有行并标 `reused=true`。并发生成同一份数据只出一行；深研报 `account_id IS NULL` 不受该索引约束。迁移前若发现历史重复，**不自动删除**，明确报错交人工处理。
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

> 一句话形态：**M7 建容器、M8 建生成器**——把「一次提问」变成一份 **PIT 锁定**的深度研报，产物接 M7 的
> `blocks` 容器（渲染 / 分享 / 导出 / 快照 / claim 闸门**全复用，不得重做**）。
> 2026-10-11 用户拍板 D1–D4；底稿 D5–D12、12 条代码测绘事实与 F1–F7 实测清单见
> `docs/private/Pn-n/P2-Mn/P2-M8.md`。
> 切片：**M8a** 路由 + 并行取证 + 归并 + 降级 → **M8b** 辩论 + 综合 + 风控闸门 + HITL + 落库 → **M8c** 过程可视化。

### 图与状态

- **独立 StateGraph**（不挂 chat 图）：五个取证节点同 superstep 并行 fan-out（**每节点自带 try/except**，
  同 superstep 内任一节点抛异常会取消全部兄弟节点）→ `merge`（纯代码）→ `bull ⇄ bear` 条件边 →
  `synthesize` → `risk_gate` → `END` / 回 `synthesize`（至多一次）/ `interrupt`。
- **取证角色 = 代码取数 + LLM 解读，LLM 不持工具**：取数直调 `rag.retrieve.search()` / `duckdb_client` / `factor`
  面板，**不得解析 `agent/tools.py` 给模型看的文本**（那份文本是给模型看的，结构化解析即假设）。一并收窄注入面：
  语料文本无法触发工具调用。
- 角色返回 `RoleResult{role, findings[], evidence_refs[], numbers{}, notes[], errors[], usage}`；**LLM 输出的每个
  数字必须能在 `numbers` 里找到同源项**（claim 闸门在角色层的预演）。节点名即过程事件的键——**token 采集不得
  沿用 `agent/graph.py` 里硬编码的 `"model"` 节点名**（照抄会静默丢 token）。
- 深图用**独立 thread_id 命名空间**（= `run_id`），与 chat 共用同一个 Postgres checkpointer（两图结构不同，
  同 thread 会互相污染）。

### 取证角色（五个，A 股化）

| 角色 | 取数（代码） | 解读 |
|---|---|---|
| 行情技术面 | `duckdb_client.bars()`：区间收益 / 波动率 / 均线位置 / 成交额 | flash |
| 新闻事件 | `rag.retrieve.search(as_of=…)`（**PIT 服务端硬过滤**） | flash |
| PIT 基本面 | `announcement` 语料按 `event_type` 过滤 + **类别→方向映射表** | flash |
| 政策面 | `policy` 语料（`industries` 覆盖 100%） | flash |
| 因子量化 | `app/factor/` 既有读数（IC / 分层） | flash（**不显著就写不显著**） |

**公告类别 → 方向映射（D3，应用层，草案待终审）**：表进 Git，只在基本面角色解读时使用，**不写进
`etl/store.py::DIRECTION_MAP`**——改语料会牵动既有回测与体检口径，M5a 的「golden 分毫不动」是硬判据。
草案：业绩预增 / 中标 / 回购 → 利多；减持 / 诉讼 / 预亏 → 利空；融资定增 / 资产重组 → 不映射（方向取决于方案）。
报告里署名「按 QuantSage 规则映射」，**不得写成平台标注**。

### 辩论（≤3 轮硬上限）

双方拿**同一份归并证据**，产出 `{stance, arguments[], citations[]}`；轮次由 `debate_round` + 条件边控制，
**第 3 轮后无条件收敛**；默认 `MAX_DEBATE_ROUNDS=2`（省钱且仍能演示）。提前收敛判据必须**可断言**
（双向新增论据为空，或引用集不再变化），不得是「感觉收敛了」。

### 综合与风控闸门

- 综合（**pro 档**）单点产出正文：结论 / 分歧点 / 风险提示，**只允许引用归并证据集里的 `event_id|day`**
  （RAG 返回的 `day` 是字符串，与 claim 闸门的键形状要对齐）；失败（超时 / 报错）→ 降级为「代码版结论」
  （证据汇总 + 缺口清单）并如实标注「本次未能生成综述」。
- **闸门是纯函数，LLM 不可决定合规**（CLAUDE.md 关键决策 1），三类：
  ① 正文数字无来源 → 回综合**重写一次**，仍不过则该段降级为纯文字并标注；
  ② 绝对化 / 承诺性表述（词表）或具体买卖指令 → `interrupt()`；
  ③ 预算超限（token / 时间 / 成本任一）→ `interrupt()`（人决定「加预算继续 / 用部分结果收尾」）。

### HITL 与恢复

`interrupt()` payload = `{kind, detail, options[]}`，运行状态转 `awaiting_approval`；
`POST /research/runs/{id}/resume`（SSE）携 `Command(resume=…)` 续跑。**断连不取消**（沿用 P1 SPEC v0.15 的订正）：
断开只是不再收帧，图跑完或停在 interrupt 照常落 checkpoint，重连从 `research_runs.events` 回放。

### 模型与思考开关（F1–F1d 实测，2026-10-11）

- **一律关思考**：`model_kwargs={"reasoning_effort": "none"}`（配置项 `RESEARCH_THINKING`，默认 `none`）。
  同一条取证 prompt 实测：flash 开 **8.99s**/2,180 tok、**flash 关 0.84s**/66 tok、
  pro 开 **107.03s**/10,783 tok、**pro 关 1.48s**/73 tok——**四条正文信息一致**、关闭档没有编造数字
  （**单样本，非统计结论**，如实标注）。**深度由「多角色取证 + 对抗辩论 + 证据回链」的结构承担，
  不由思维链长度承担**。
  ⚠ **`reasoning_effort="minimal"` 不生效**（实测 12.78s / 2,973 tokens）——别拿它当「少想一点」。
- 双档分工不变（取证 / 路由 flash，辩论 / 综合 pro），差别在**模型能力**而非思考档。
- **API 侧并发几乎免费**：5 条 flash 并发墙钟 **2.6s**（单条 2.25s）⇒ fan-out 的瓶颈**只在本地 RAG 那条**
  （rerank 稳态 ~1.8s 且**并发完全串行**：2 条 3.36s / 4 条 6.98s，MPS 锁如实生效）。

### 意图路由与预算

- 路由 = 一次 flash 结构化输出 `{path, symbols[], as_of?}`；**解析失败 / 超时（8s）一律回落 quick**；
  请求可显式带 `mode=deep` 跳过判定；问题里的标的名走 M5a 名称字典解析，**不新造、也不需要 LLM**
  （F7：字典加载 0.21s / 5,611 只 / **零重名**；六位码走正则、全名精确、简称包含匹配，多命中给候选）。
- **快路径 = 现有 ReAct 图一行不动**，路由在端点前置。
- 预算 `{llm_calls, tokens_in, tokens_out, cost_usd, elapsed_s}` 四计数器 + 硬上限；关思考后一次深路径
  ≈ **$0.03–0.05（0.2–0.4 元）**，成本由**输入**（每次喂证据 2–4K tokens）主导；
  **到限即在节点边界停**，不在 LLM 调用中途。

### 过程可视化（M8c）

- SSE 帧新增 `node_start` / `node_end{node, duration_ms, tokens, cost_usd}` / `debate` / `budget` / `interrupt` /
  `report`（复用 `error` / `done`）；**帧编码抽到 `app/api/sse.py` 共用**——现有两处手写
  （`api/chat.py` / `api/optimize.py`），只抽编码、不改既有行为。
- 运行页 `(app)/research/runs/[id]`：阶段环 + **并行瀑布**（五条取证条的长度即真实耗时——MPS 精排是进程级锁
  串行，**新闻那条最长，如实显示，不画成等长**）+ 预算条 + 审批卡 + 事件时间线。
- 对话页只给「已启动深度研究」卡片（**不加新帧到 chat 流**，reducer 的未知帧本就原样放行）。
- 研报页新增**运行摘要块**（冻结在 `meta.run`，分享页同样可见）：节点耗时 / token / 成本 / 模型档 / 降级与缺口清单。
- **深路径每次 LLM 调用都要挂 Langfuse handler**——现状 handler 只挂 `main.py` 与 `chat.py` 两处，
  M7 的报告 LLM 调用根本没被追踪；这是「过程与 trace 一致」这条验收的对账前提。

### 端点（`/api/v1/research`，受保护；越权与不存在同为 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/research/runs` | 建运行并启动 → **SSE** |
| POST | `/api/v1/research/runs/{id}/resume` | HITL 恢复 → SSE |
| GET | `/api/v1/research/runs` | 本人的运行列表（摘要） |
| GET | `/api/v1/research/runs/{id}` | 运行详情（事件回放：断连重连与事后回看都走它） |

研报本体沿用 M7 端点（`/reports/{id}` / 分享 / Markdown / PDF **自动可用**），不新开端点。

### 持久化

新表 `research_runs`（`id` / `user_id` FK / `question` / `subject JSONB` / `status`
（`running`｜`awaiting_approval`｜`done`｜`degraded`｜`failed`）/ `events JSONB`（节点事件——过程可视化与
Langfuse 对账的**真源**）/ `budget JSONB` / `report_id UUID` / `error` / `created_at` / `updated_at`）。
`research_reports` 三个幂等 ALTER：`kind TEXT NOT NULL DEFAULT 'paper_account_report'`、
`account_id DROP NOT NULL`、`subject JSONB`。深研报用另一个 `REPORT_KIND`（`deep_research_report`）并
递增 `REPORT_VERSION`；`validate_claims` / `snapshot` / `plain_json` 全部复用。

- **报告落库与运行状态一致**：`report` 帧的落库失败必须把 `research_runs.status` 置为 `failed` 并写 `error`；随后的 `done` 帧不得把失败覆盖成 `done/degraded`。`status in (done, degraded)` 且 `report_id IS NULL` 的组合不允许出现（图本身 `failed` / `awaiting_approval` 的运行不受此条约束）。
- **停机与重启（2026-10-31 P1 修复）**：lifespan 收尾对 chat / research 的 `_DETACHED` 做限时 cancel/await（默认 5s），再关连接池；后台主实例启动时把本实例遗留的 `research_runs.status='running'` 标为 `failed` + `error.kind='interrupted'`，`awaiting_approval` 保持不动（合法暂停）。

### 前端（M8c）

运行页 + `components/agent-run/`（run-panel / stage-ring / waterfall / budget-bar / approval-card）+ 研报页
运行摘要块 + `lib/agent-run.ts`（帧 → 面板状态的纯 reducer，Vitest）。动手前先出 design brief（P1 口径）；
瀑布与阶段环按 dataviz skill（单色相 ordinal，**不借 `--up/--down`**——那是涨跌 / 买卖专用色）。

**验收**：完整研报生成（真实数据 + 真实模型；研报页与公开分享页各一证）；辩论严格 ≤3 轮；token 成本在预算内
（含**压低上限触发一次 interrupt** 的全过程证据）；拔掉任一数据源降级而非崩溃（五个角色各注入一次故障，
报告仍产出且缺口如实标注）；基本面结论均源自带 `available_at` 的公告事件（脚本断言 `event_type='announcement'`
且 `available_at <= as_of`）；过程面板与 Langfuse trace 一致（节点名集合 + token 总数对账，
`scripts/research_evidence.py`）。**claim 闸门对 M8 产物同样生效**（不过即拒收不落库）——这是 M7 对 M8 的硬约束。

**F1–F7 已实测（2026-10-11，读数见底稿 §六）**：pro 档可调通；**思考是延迟与成本的绝对主导项且关掉不损质量**
（见「模型与思考开关」）· **跨进程 resume 通过**——进程 A 停在 `interrupt()`、进程 B 重开 checkpointer 用
`Command(resume=…)` 续跑到底，故 `research_runs` **不必自建恢复状态机** · `astream_events` 的节点事件
**要自己埋**（`on_chain_start/end` 带 `metadata.langgraph_node` 可配对，但**被 interrupt 的节点只有 start
没有 end**，且 interrupt 的 payload **不在事件流里**，要读 `aget_state().tasks[].interrupts[].value`）·
MPS 精排**并发完全串行**（如实画瀑布）· 标的名解析不需要 LLM。

**F2 口径（PRD「深路径 p95 ≤ 15s」）**：M8b 端到端实测 **40.5s（冷启动）/ ≈23s（预热）**——**15s 守不住**，
已按用户 2026-10-11 签署改为「**冷启动首问 ≤ 60s、预热 ≤ 30s**」（PRD v0.8，SPEC v1.44 ③）。
**最大单项是检索精排**（22.3s，冷启动载模型 ~18s），与模型档无关——关思考后 10 次 LLM 调用墙钟合计仅 29.3s。

**本轮不做**：Agent 下单接模拟盘 · RAGAS 生成质量指标 · 多专家视角并置 · DCF/comps 估值 · 定时自动研报与推送 ·
A2A（只留 Agent 能力元数据这一零成本预留）· 报告版本历史 · 深研报的 PDF 服务端渲染（M7d 的通用能力**自动可用**，不另做）。

## 10. M9 轻量教学

- 使用教程 + 示例策略文档 + 常见问题；覆盖 P2 新增流程（自选股/模拟盘/研报导出）
- **落点**：`docs/tutorials/getting-started.md` 与 `faq.md`（**不新增前端页面**——规划报告已定「教学平台不做，仅保留教程文档」；README 的文档表本就指向 `docs/tutorials/`，新增即零改链）。示例策略文档 = 既有两篇（`ma_cross.md` / `event_driven.md`，T7 交付，**本轮不重写**：它们的数字有快照指纹、54 项断言对齐）
- **读者模型三条**（决定每一句怎么写）：A 纯使用者（只有一个浏览器，无仓库无数据）→ 使用教程主体；B 本地跑起来的人 → 使用教程 §「本地跑起来之后」+ 两篇策略教程；C 想复现数字的人 → 两篇策略教程。**A 与 C 必须分开写**：策略教程头部写的是「已克隆本仓库、样例数据已落盘的读者」，把 A 挡在门外
- **数字口径三档**（T7 的快照纪律只覆盖得住 C 那一档）：静态规则（佣金万 2.5 / 100 股整手 / 密码 8–72 字节）照写并注明出处文件；**UI 交互流程写「该看到什么 + 判据」，不抄实测数字**（模拟盘净值、网格收益随数据滚动）；**LLM 生成不承诺可复现**，示例标日期与模型
- **文档机检**：`backend/scripts/check_docs.py` **只做三件**——文档内相对链接存在、出现的环境变量都在 `.env.example`、引用的 `backend/scripts/*.py` 存在。**刻意不查** CLI 参数、API 端点、界面文案（逐版本漂移，查了是噪声源）；界面文案的漂移由人工走查兜
- **验收口径**：`docs/private/Pn-n/P2-Mn/P2-M9.md` 的「流程 → 文档锚点对照表」11 个流程全部覆盖 + 机检退出码 0 + **按教程逐条走查**（`frontend/scripts/capture-m9.mjs`，证据落 `logs/m9/`）+ 零回归

**验收**：文档覆盖全部核心流程，新用户可独立走通。（**已达成**：走查 **54/54**，含真跑一次深度研究与一次真实 PDF 下载；每条断言都能对回教程小节号）

## 11. M10 研究首页

**形态**：登录后落地页（`/`），四块组成——**大盘总览与板块**（M10a）、**事件日历/快讯流**（M10b）、**标的浏览**（M10c）、**事件详情**（M10d，补口）。各段可独立验收。

### 11.1 M10a · 落地页 + 大盘总览 + 板块块（2026-10-12 细化并拍板 D1–D4）

**D1 落点**：M10 **占用 `/`**（§11 字面「登录后落地页」），**对话页迁到 `/chat`**，页头导航 **9 项**（「首页」排首位）。
搬迁共七处落点：`(app)/page.tsx` → `(app)/chat/page.tsx`；`lib/nav.ts` 的 `NAV_ITEMS`（新增 `home`、`chat` 改指 `/chat`）与 `activeHref`；
对话页 `ThreadUrlSync` 的 `router.replace` 目标；`lib/nav.test.ts` 用例；`docs/tutorials/getting-started.md` 的「页头八项」表与界面地图。
**刻意不改**：`register/page.tsx` 的 `router.replace("/")` 与 `lib/auth-form.ts` 的 `safeNextPath` 默认值——注册后落到研究首页正是 D1 的意图。
**既有证据会失效**：`frontend/scripts/capture-m9.mjs` 断言「页头八项」。**不回头改它**（那是 M9 的历史证据），由 M10 走查接替，并在 M10a 的验收记录里写明。

**D2 切分**：M10a 落地页 + 大盘总览 + 板块块 / M10b 事件日历 / M10c 标的浏览。
**D3 板块归属口径**：**PIT 累积**——行业 → 标的映射 = 截至该交易日收盘（`available_at <= 当日 15:00`）的**全部历史事件**累积。实测 16–260ms、覆盖率 **87.7%**（4,873 / 5,556）。**不用当日事件**：当天的新闻标签本身被当天涨跌驱动（当天大涨才被打标签），当日归属的「板块涨跌幅」是**选出涨得好的再算均值**（实测医药生物 +3.69% 即此），且样本薄（金融仅 127 只）。
**D4 指数代理口径**：**全市场等权，复用 M5 的同一把尺子**（`benchmark._daily_rows` + `EXCLUDE_ABS_CHANGE_PCT`），全期 1,636 个交易日实测 **329ms**。

**两条新端点**（沿用 `market` 路由的公开性，无 `require_user`）：

| 端点 | 返回 |
|---|---|
| `GET /api/v1/market/overview?date=&adjust=` | `trade_date` / 涨跌家数（`up`/`down`/`flat`/`total`）/ 涨跌停家数（`limit_up`/`limit_down`）/ `amount` 成交额 / 代理指数（`level` 点位、`change_pct` 当日、`base_date` + `base_level`、`kept`/`dropped` 样本数）/ 两个「截至」/ `st_source`（ST 字典是否可用） |
| `GET /api/v1/market/sectors?date=&limit=&min_symbols=&adjust=` | 按行业聚合的 `名称/成分数/成交额/平均涨跌幅/中位数涨跌幅`，附 `coverage`（`covered`/`total`）与 `note` |

**涨跌停口径（本段新增，实测逮到三件事）**：`raw` 的 `change_pct` 自 **2026-09-21 起全空**（2026 分片覆盖 83.2%，2020–2025 近 100%），**不可读**。判定走 **raw 前收自算**：前一交易日 raw 收盘 → `a_share_rules.limit_pct(symbol)` → `round_to_cent` → 与当日 raw 收盘比。

1. **封板 = 收盘价等于涨跌停价**，不是「够得着」。判据必须**对称**（`|close − 价| ≤ 半分`），并单独认出「收盘跑到区间外面」——那在物理上不可能，只可能是**当日除权而 raw 前收没反映除权参考价**，或**档位假设本身错**。实测 2026-09-30 的 `300980 祥源新材`：raw 前收 22.90 → 收盘 15.98（−30%，超出创业板 20% 的上限），而 qfq 口径当日只跌 2.31% —— 它根本没封板，单侧比较却会把它数成跌停。**这类一律跳过并计数，不数成任何一档。**
2. **主机板 ST 的幅度实测是 10%，不是 5%** ⇒ `_ST_MAIN_BOARD` 从 0.05 改为 0.10。取证：全市场 168 只主板 ST 标的逐日算「收盘是否精确等于某一档算出的涨跌停价」，**前收 ≥10 元的 1,341 个样本里命中 10% 档 57 个、命中 5% 档仅 3 个**（前收 ≥5 元档是 111 : 22）；加价格下限是为排掉「低价股两档同价」的假命中（前收 0.10 元时 5% 与 10% 都进位到 0.11）。名称侧已排除字典过期（语料里这几只从头到尾都叫 `*ST广糖` 一类，90–122 次全一致）。⚠ 改后这一档与主板档同值，`is_st` 在数学上成了空操作——保留是因为规则本身有这一档、且本数据集分不出「退市整理期 10%」与「风险警示 10%」；合并是一次跨 engine / paper / rules 的清理，不塞进 M10。
3. **复算与端点必须同口径**：走查脚本用 `/cross-section` 分页自己数涨跌家数与成交额、自己算等权均值（**不复用 `/overview` 的实现**），涨跌停则按同一份口径在 JS 里重算。三处易错点各有教训：**候选集必须取涨跌幅的两端**（只取涨幅榜会把跌停算成 0）；**必须含没有 `change_pct` 的标的**（判据根本不读它，按涨跌幅筛会漏）；**门槛不能取 4.9%**（低价股四舍五入会压低涨跌幅）。

实测 2026-09-30（修正后）：**56 涨停 / 11 跌停 / 判定 5,555 / 判不了 6**。ST 档 5% 时代是 58/16/5——多出来的那几只里，**5 只是两档都不命中的纯误报**、1 只是除权误判。ST 档走 `naming.st_symbols`（字典覆盖 96.7%，缺名按非 ST），字典缺失时按板块默认幅度并在响应里标注。

**板块块的形态（偏离 PRD/SPEC 的「热力图」字面，附数据理由）**：**不用 treemap / 堆叠条 / 饼图**——PIT 累积归属是**多对多**的（一只标的可同时挂「电子」「半导体」「AI算力」），按行业汇总成交额，38 个行业合计 **68,017 亿 = 全市场的 469%**；部分-整体形态会画出一个「加起来是市场四倍多」的假整体。改用**排序的横向 diverging 条**（按平均涨跌幅降序，条上标数值，右侧带成分数与成交额），依据 dataviz `choosing-a-form` 的「above/below a baseline → diverging bar」。**成分数必须显示**——它是读者判断该均值可信度的唯一线索。行业标签池含**概念题材**（844 个标签里既有「有色金属」也有「AI算力」「宏观政策」），故标题写「行业 / 概念」而非「行业」。

**配色（算过，非挑选）**：新开 token `divergePct`（语义 = **涨跌幅**）。**不复用 `diverge`**——那支是红↔蓝、语义是夏普式极性，而同一页的指标行用红绿显示涨跌，同屏两套色编码同一个量是「一色二义」的镜像错误。

| | 涨极 | 跌极 | CVD ΔE（deutan 最差） | 常视 ΔE |
|---|---|---|---|---|
| 浅色 | `#b32d2d` | `#0e8f6b` | **8.8** ✅ 过目标线 8 | 28.0 |
| 深色 | `#ef5350` | `#26a69a` | **11.6** ✅ | 29.6 |

浅色跌极与深色两极**就是站点既有的 `--up` / `--down`**；只有浅色涨极从 `#d03b3b` 微调为 `#b32d2d`（原对实测 ΔE **7.6**，落在 6–8 的 floor 带）。两者常视 ΔE 仅 **6.8**，读数上是同一个红——**收益全部来自明度差**（CVD 主要靠明度分辨）。七档数组**按数据升序存**（`[0]` = 跌极，`[6]` = 涨极）：**ECharts 的 `inRange.color[0]` 映射到 min**（读 `VisualMapping.js` 的 `doMapToArray` 确认），顺序写反会让色阶整体反向，而「两端像素俱在」的断言看不出来。两臂各自过 `validateOrdinal` 四项，中性中点复用 M5b 的同一对。

**顺带修（M5b 遗留，2026-10-12 用户拍板）**：`/optimize` 上两张图对 `diverge` 方向的理解相反，且有一处注释声称一致——`grid-heatmap.tsx` 用 `inRange.color`（数组 `[0]` = 红落在 **min** = 最负夏普），`sharpe-distribution.tsx` 用 `diverge[0]` 标**最优格**。文档化的意图是「**红 = 正夏普**」（M5b brief §二正文 + M5c 的 `up`(红=正)），故**把两支 `diverge` 数组按数据升序重排**（`[0]` = 蓝 = 负、`[6]` = 红 = 正），两处消费方的下标随之改，并把方向**钉进 `chart-theme.test.ts`**（原测试只断言「两端色相相反」，两个方向都能过，所以这条从未被抓到）。

**如实呈现（硬要求，逐条落进 UI）**：代理指数卡上写明「**不是指数**」（基点 1000 锚定行情首个交易日 2020-01-02，实测点位 1968.1，与回测基准同源同规则）；两个「截至」并列（行情停在更早的日子是数据通道的真相——年度整片重下载、不日更）；板块块的覆盖率照登服务端并写明标签来自事件、非权威行业分类；涨跌停口径写明；缺值一律 `—`，**不补 0**。

**验收（M10a）**：① 大盘四个读数与**独立复算一致**——走查脚本打后端**另一套 SQL** 对账，**不复用实现**；② 板块条按涨跌幅降序，且画布上**红端与绿端像素同时存在**；③ 代理指数卡上能读到「非指数」口径字、两个「截至」都在页面上；④ 搬迁回归——`/` 是研究首页、`/chat` 可发消息、页头「首页」高亮且 `aria-current` 正确；⑤ 窄屏 390px 无横向溢出（页头已 9 项）、深浅两色各截一张、零回归。

**不做（M10a 明确排除）**：treemap（见上）；行业 × 时间热力图（可延，那才是严格意义的 heatmap）；指数真行情 / 权威行业分类 / 财务数据（数据源没有，**界面上不假装有**）；大盘的近 N 日走势序列（可延）；事件日历的占位骨架（M10b 直接加真块）；**不改数据层**——`raw` 的 `change_pct` 断档只在本文记一笔（数据层是 M2c 的地盘），顺带确认 `/cross-section?adjust=raw` 的既有行为是否受影响。

### 11.2 M10b · 事件日历 / 快讯流（2026-10-12 细化）

**落点**：研究首页（`/`）的第三块，接在板块块之后（`app/(app)/page.tsx` 的 `mt-8 flex flex-col gap-6` 容器）。

**两条新端点**（沿用 `events` 路由的公开性）：

| 端点 | 返回 |
|---|---|
| `GET /api/v1/events/calendar?start=&end=` | `{start, end, days: [{date, total, by_type: {...}, high}]}`——日期带的每日计数。实测近 30 个自然日 **74ms** |
| `GET /api/v1/events/feed?date=&types=&min_importance=&limit=&offset=` | `{date, total, count, items: [...]}`——该日的分页快讯流。信封抄 `/market/cross-section` 的 `{total, count, items}` |

**条目字段与 M7 的证据行**同形（`event_id` / `event_type` / `title` / `summary` / `event_time` / `available_at` / `direction_norm` / `source` / `original_source` / `content_hash` / `source_url` / `industries`，外加 `importance_score` 与 `symbols` + 名称），**直接复用 `lib/research.ts` 的 `evidenceRows`**——它就是「事发与可得并列 + 来源三元组」的那一支，不新写一套。
> ⚠ 沿革：本节先前写的是「M7 的 **`sourceRows`**」——那个名字其实是 **M6 模拟盘**的（`lib/paper.ts`，行序与语义都不同）。M7 的那支叫 `evidenceRows`（`lib/research.ts:340`）。已订正。

**排序与分页**：`importance_score DESC, event_id`（稳定序）。实测分页无退化——第 1 页 14ms、第 20 页（`OFFSET 380`）15ms、带类型+分数筛选 6ms。分页 UI **与标的列表共用 `components/ui/pager.tsx`**（见下条的沿革注）。

**列表键用 `event_id@available_at`**（沿用 `events-table` 的实测判据：同题事件按月复用 id，84 天里 18 例——单用 `event_id` 会撞键）。

> **沿革（2026-10-12 用户反馈后订正）**：本节先前写「M10b 要新写『加载更多』」。用户看了 M10c 的列表后指出**排序表该用真分页而不是追加**——单日 6,512 条 / 20 一页 = **326 页**，靠点一百多次翻到中段不现实。**「加载更多」适合按时间顺着读的流，不适合按名次排的榜**；两处都改成真分页，判据抽到 `lib/paging.ts`（总页数 / 页码↔offset / 夹取），UI 共用 `components/ui/pager.tsx`。

**⚠ 一处实测发现改变了默认视图的设计**：`importance_score` **不跨类型可比**。2026-09-30 按分数降序的**前 200 名全是 `announcement`**（该日公告 2,226 条、均分 30.9 但满分 100；news 4,246 条、均分 48.5、满分 92）——验资报告一类常规公告拿到 100 分，把新闻整段压下去。所以「按重要度排序」在**不筛类型**时交出的是**一张公告列表**，不是「快讯流」。默认类型口径见 §11.2 的拍板项。

**时间显示**：条内用 `eventStamp`（`MM-DD HH:mm`，无年份）；**日期分组头需要「只到日」的格式，`lib/format.ts` 目前没有**（`dayStamp` 一定带时分）⇒ 补一个 `dayOnly`。

**如实呈现**：来源三元组与双时间戳是 PRD §5 的硬要求，**必须可见**（`source` / `original_source` / `content_hash` 实测填充率 **100%**，可以无条件展示）；`availability` 滞后是真实存在的（实测 `available_at − event_time`：0–1 小时 104,829 条、1–24 小时 87,806 条、**超过 7 天 8,264 条**），差值本身就是这个项目的护城河展示位，条内应当能一眼读到。

**验收**：① PRD 硬条款——**点击/展开任意事件可见来源三元组与双时间戳**（走查断言 DOM 里三件都在）；② 分页真的能翻且不重不漏（同一页两次请求逐条相等，第 N 页与第 N+1 页无交集）；③ 日期带与端点计数一致（脚本独立复算当日条数）；④ 零回归 + 窄屏不溢出。

**拍板项（用户，2026-10-12）**：

| # | 决策 | 内容 |
|---|---|---|
| **D1** | 默认类型 | **默认排除 `announcement`**（「快讯流」语义），类型筛选第一档是「公告」可单独选；口径说明里写明「公告评分上限高、会霸榜，故默认不混排」。理由：实测前 200 名全是公告，不筛就没有「流」 |
| **D2** | 日期控件 | **日期带（近 30 日每日计数，可点选）+ 原生 `<input type="date">` 兜底任意日**。不引第三方日期库（全站 11 处都是原生 input，既定取舍） |
| **D3** | PIT 开关 | **不做**（可延）：加一个「截至该日收盘可见」的过滤器是护城河的直接展示位，但 SPEC 只要求双时间戳并列；实测当日收盘后才可得的比例见 §11.2 的滞后分布，留作后续 |

**不做**：不做全文检索（M3 的 RAG 是那条路）、不做订阅/推送、不做跨日时间轴视图（按日分页已够）、不改 `MarketEvent` 的既有形状（回测页不受影响）。

### 11.3 M10c · 标的浏览（2026-10-12 细化）

**（2026-10-09 从「股票展示 / 信息查询」并入，不新开功能 ID）**。**定位是「把已有能力摆出来」，不是独立产品**——公司资料、财务、权威行业分类一概没有，**界面上不假装有**。

**两条路由**（**不进页头**——页头已 9 项，M5b 记过窄屏溢出、M7c / M8c 两次因此放弃加项）：

| 路由 | 内容 | 数据来源 |
|---|---|---|
| `(app)/stocks` | 全市场列表：排序（涨跌幅 / 换手 / 成交额 / 收盘）+ 按代码**或名称**搜索 + 分页 | **全复用** `GET /market/cross-section`（`sort` / `order` / `q` / `limit` / `offset` 已齐，实测全市场排序 6–12ms） |
| `(app)/stocks/[symbol]` | K 线 + 事件时间线 + 加自选 / 去回测 | `GET /market/{symbol}/bars`（全史 1,636 根 / 230KB / **268ms**）、`GET /events?symbol=`（600519 全期 **91 条**）、`GET /market/{symbol}/probe` |

**入口三处**：首页快捷入口行、板块条点一条 → 该行业的成分列表（见下面的拍板项）、自选股面板的既有名称搜索。

**`换手率` 不进列表**：源侧 2026-08 起逐步停更、2026-09 起**整列为空**（M5a 已核），列一栏恒为 `—` 是噪声。口径说明里写明它为什么不在。

**列表用真分页，不是「加载更多」**（用户 2026-10-12 反馈后改）：全市场 5,561 只、50 一页 = **112 页**，靠点 112 次翻到中段不现实，而且离开页面再回来位置全丢。**排序表要的是随机访问，不是流水账**——「加载更多」适合按时间读的快讯流，不适合按名次看的榜单。<br>页码住在地址栏（`?page=N`，第 1 页不带参数），故「翻到第 37 页 → 点进个股 → 浏览器后退」位置还在；**换筛选自动回第 1 页**（否则「第 5 页的茅台搜索结果」多半是空白），但**首次渲染不重置**——否则深链里的 `?page=5` 会被自己夹成第 1 页。`clampPage` 在没有数据时**只保下界不猜上界**，同理。列表键用 `symbol`（截面里唯一）。

**个股页的 K 线**：复用回测页的 `candlestick-chart`（不再写一套图表配置）；**买卖点标记不传**——个股页没有策略信号，硬塞一个会变成「假装这里有结论」。

**加自选的轻量版**：复用 `lib/watchlist.ts` 的全部纯函数 + `api.addWatchlist`，个股页只做「加入默认分组 + 本地判重」。**不抽共享表单组件**——`WatchlistPanel` 里的 probe 防抖、名称搜索、移出二次确认都内联在那一处，抽出来是一次重构，超出本段。

**小改 `/backtest?symbol=`**：回测表单支持 `symbol` 预填（现只支持 `?run=`）。不加则「去回测」点过去等于没带上下文。

**拍板项（用户，2026-10-12）**：

| # | 决策 | 内容 |
|---|---|---|
| **D1** | 板块钻取 | **做**（用户 2026-10-12 拍板）。**落点不是新端点，而是给 `/market/cross-section` 加一个 `industry` 查询参数**——那样「全市场列表」与「某行业成分列表」在前后端都走同一条代码路径（同一套 sort / q / 分页），不必再维护第二份截面逻辑。过滤走**与板块块同一支 PIT 累积归属**（否则点进去的成分与条上那个数字对不上）。<br>⚠ 沿革：本节先前把这条写成「新增一条按行业取成分的端点」，实现时改成了加参数——**同一件事，但少一条要维护的路径**。 |

**钻取的一致性判据（写进验收）**：点板块条进去后，列表页的 `total` 必须**等于**该条上显示的成分数。两个数走的是 SQL 里同一支归属、同一个当日截面，所以必须逐字相等；不等就是两处口径漂了。

**验收**：① 列表按所选键排序正确、搜索按代码与**名称**都能命中（「茅台」→ 600519）；② **翻页**——`?page=2` 进地址栏、换一整批且与前后页零交集、「上一页」逐行回到原位、跳到第 N 页、换筛选回第 1 页；③ 个股页 K 线**真的画出来**（画布像素，非 DOM 文案）且事件时间线与 `events` 端点的条数一致；④ 「加自选」写进默认分组后自选股页能看到，重复点不产生第二条；⑤ 「去回测」带过去的标的与当前页一致；⑥ 零回归 + 窄屏不溢出。

**不做**：公司资料 / 财务 / 权威行业分类（数据源没有，**界面上不假装有**）；不做自选股的「加自选表单」全量移植（见上）；不做 K 线的指标叠加（均线以外的东西没有数据支撑）；不做全市场列表的导出。

### 11.4 M10d · 事件详情（2026-10-13 细化并拍板）

> 补口：用户 2026-10-13 看首页快讯流后问「新闻/语料有没有必要做点进去看详情」，拍板**①+② 一起做、排在 P2 里程碑收尾之前**（好让 11 张截图重拍一次带上）。规划底稿与 D1–D5：`docs/private/Pn-n/P2-Mn/P2-M10d.md`。

**形态**：快讯流与个股页事件时间线的**每一条**都可点开右侧抽屉，给出这一条的**完整证据链**，七组——头部（类型/重要度/方向）· 正文（`title` + `summary` 全文）· 来源与版本（`source` / `original_source` / **`source_url` 外链** / `content_hash` / `quality_status`）· 关联标的（每只带 **`reason`**）· 人物（`person`）· 因子（`factor_value` / `factor_scores` / `confidence`）· **修订**（`record_version` / `revision_id` / `revision_time` / `is_corrected` / `correction_count`）。

**为什么值得做**（三条实测依据）：① 快讯流**只取 14 列**（`FEED_COLUMNS`），上面那批**大多已躺在 parquet 里没出库**；② 其中 `summary` 与 `source_url` 更冤——**已经在响应里**，只是 `feedItemView()` 只渲染 `title`、`SourceCell` 只显示来源三元组（「取了不用」）；③ PRD 里 M10 的验收原文就写着「**点击任意事件**可见来源三元组与双时间戳」——本片把「点击」做成真抽屉。价值落点：**修订史与打标理由是可追溯性的可见化**，与 M7 的证据面板同一条主线。

**端点**：`GET /api/v1/events/{event_id}?day=YYYY-MM-DD`（公开，与既有 events 端点一致——那三个都没有 `require_user`）。**列集单列** `DETAIL_COLUMNS`，**不 `SELECT *`**（别把 `payload_json` 一类原始字段塞进响应）。`day` **可选**：`event_id` 只在日分区内唯一（平台对同题事件复用 id，实测 19 例、最多跨 8 天）⇒ 省略时按 `event_time` 取**最新**那条并**回显 `date`**，给了则精确匹配；查不到 **404**（走既有 `NoDataError` 映射），不返回空壳。

**拍板（用户 2026-10-13）**

| # | 决策 | 理由 |
|---|---|---|
| D1 | 行内**不再**展开 `summary`——正文只从抽屉出，并把它移出 `FEED_COLUMNS` | 这个项目吃过「同一件事两份定义」的亏（M10c 逮到过截面的两个定义）；顺带让 feed payload 变小。行内现有的「来源」三元组展开不动 |
| D2 | 抽屉**不进地址栏**（v1 不做深链） | 页码是「位置」，抽屉是「看一眼就关的浮层」；本项目的深链纪律针对前者。省掉一整套「首次渲染不重置 / 深链被自己夹掉」的坑 |
| D3 | 个股页的事件时间线**共用同一个抽屉** | 同一组件同一端点，两处入口的一致性比省下的接线成本值 |
| D4 | `source_url` 为空时**不渲染链接元素**（显示「—」） | 不编 URL；公告类恒有（巨潮），新闻类常常没有 |
| D5 | 修订史**照原样列出**，不加「已修订 N 次」这类加工文案 | 平台会重发修订版、我们存的是拉取当刻那一版（M2b），数字本身就是证据 |

**验收**：① 七个分组齐全（走查按 `data-detail="<组>"`）；② 修订字段与库里**逐字一致**（拿一条真被修订过的）；③ 标的数与 `reason` 数相等、空 reason 显示「—」；④ `source_url` 为空时**没有链接元素**（不是空 `href`）；⑤ 省略 `day` 取最新并回显、带 `day` 精确命中（用跨日复用的真实样本验——F5）；⑥ 不存在的 id → **404**；⑦ **零回归**：`/events/feed` 去掉 `summary` 后 M10b 的既有断言全绿（`EventFeedItem` 类型同步收窄）。

**不做**：相关事件推荐 / 同标的其它事件（那是检索活，归 P3）；抽屉内翻上一条下一条（浮层不是阅读器）；不动 RAG 与 Agent 的证据链（它们已有自己的证据面板）；不做评论 / 收藏 / 分享。

**落地实测（2026-10-13）**

- **交付**：后端 `DETAIL_COLUMNS` + `duckdb_client.event_detail()` + `GET /api/v1/events/{event_id}`（公开、`day` 可选、查不到 404）；前端 `lib/event-detail.ts`（七组纯函数）+ `components/events/event-detail-sheet.tsx`（右侧抽屉，`aria-modal` + Esc/遮罩/按钮三种关法）+ 两处入口（快讯流每条、个股页事件表每行——后者与回测页共用 `EventsTable`）；`EventFeedItem` 收窄、`EvidenceFields.summary` 改可选。
- **走查 35/35**（`frontend/scripts/capture-m10d.mjs`，证据 `logs/m10d/`）：正文/修订/理由/原文链接**逐字对 API**、空值显示「—」、无 url 时**不出链接元素**（政策档真样本 `policy:4375`）、三种关法都关得掉、个股页同抽屉、快讯流的来源展开里**没有摘要行**（D1）。
- **三条实测发现（都改了口径或写法）**：
  1. **D1 的依据订正**：`summary` 与 `source_url` 并非「取了不用」——它们**在快讯流的「来源」展开里显示过**（复用 M7 的 `evidenceRows`，它带「摘要」「原文」两行）。D1 的实质因此是**把正文收敛到一处**（抽屉），顺带让 feed payload 变小；M10b 那条「展开后三件齐」的断言不受影响（走查第 10 步复验过）。
  2. **平台的 `stocks` 里混着脏条目**：实测 `news:2286536` 的 20 条提及里 **19 条 `code` 是空串**，还有一条 `code="数据缺失"`（理由偏偏挂在它上面）⇒ 关联标的**只列六位码的**，被滤掉的**明说条数**（「另有 N 条提及没有六位代码」，不静默隐藏）。走查第一次跑挂掉正是这个：脚本拿 `stocks[0].code` 跳个股页，跳到了 `/stocks/`。
  3. **修订标记在本地语料里全是默认值**：30.7 万行 `record_version=1` / `is_corrected=false` / `correction_count=0` ⇒ 「平台修订标记」那组当前恒显示「未修订」。**留着**（它是平台自己的声明，而 M2b 记录过「内容确实变过、标记不动」这件事本身有价值），但**验收 ② 改成「与库里逐字一致」**——「被修订过」那条渲染分支由夹具守住（`record_version=3` / `is_corrected=True`）。
- **回归**：后端离线 **1323 passed**（+5，唯一红仍是既有的 `test_etl` 日期定时炸弹）；前端 typecheck / lint / **423** 纯函数全绿（+17）。

**验收（M10 整段）**：大盘总览与日线数据抽查一致；事件日历点击任意事件可见来源三元组与双时间戳；**M10d 的抽屉七组齐全、修订字段与库里逐字一致、`source_url` 为空时不出链接**。

## 12. 测试策略

> 本节只登记「用例落在哪些文件、验什么」；逐次交付的读数与证据在 §13（`logs/` 不入库，公开文档不把日志当读者可打开的材料）。

| 范围 | 落点与关键断言 |
|---|---|
| M1 用户与归属 | 离线：未登录 401 门 + 内存 DB 越权矩阵；集成：真实 Postgres 注册/登录与 A/B 隔离（唯一邮箱 + teardown）。归属矩阵**双跑**：内存快反馈 + 真 SQL 验真。M1c 自选股/回测落库：401 / 409 / 404 / 分组语义 / 涨幅 NULL / 行情降级；内存替身必须照抄 `UNIQUE(user_id, symbol)`。 |
| M2 数据层 | 日历纯函数与全期双向对账；`symbols` 归一化；归档分片幂等（重跑逐字节一致）；`list_contains`；事件窗口越界 400；体检 20 项三态；退市覆盖；刷新门禁与两路锁互斥；集成跑真实归档小窗口 + P1 包含性回归。 |
| M3 RAG | 离线不下载模型：编码器协议注入假实现、文本构造、point id、filter（`as_of` 必带）、RRF、NDCG 手算、长度分桶、增量判据、`RagNotReady`；PIT 边界在内存向量库替身（照抄服务端过滤语义）；集成对真实 Qdrant 跑 upsert → 检索 → 按日重建幂等。 |
| M4 策略工作台 | AST 每条规则正/负样例成对（行号 / severity / 关键词）；`PARAMS` 校验矩阵；入口解析；沙箱故障注入（死循环 / 超大分配 / 超长输出 / `sys.exit` / 白名单外 import）逐条「明确错误 + 父进程存活」；模板与注册表策略逐点一致；集成 CRUD + 归属 + 保存→检查→回测→落库→重开。 |
| M5 回测增强 | A 股规则各拒绝码矩阵 + 开板日正常成交成对样例；名称归一化 11 例；等权基准剔除规则；既有 golden 分毫不动。网格：展开与边界负样例、并发峰值 ≤2 且逐格与串行相等、单格失败隔离、DSR 三条正交验算 + 四个退化分支。因子：面板纯函数 + 与 `EventFeed.advance` 归属日逐条相等、IC 手算、分层/多空/费用、HTTP 422/400 矩阵。 |
| M6 模拟盘 | 六态转移矩阵；未审批不成交；单标的 + 全批 ⇒ 与回测 `fills` 逐笔相等；涨跌停价同源；账户配额/停牌估值；顺延与 `unfilled`；重放不变量；HTTP 401/404/409/422 矩阵；集成四表幂等、推进事务、乐观并发、重放对账；沙箱版与内置 `ma_cross` 同结果。 |
| M7 研报 | 指标手工样例；回合配对容差；归因闭合；证据 join 与修订标注；快照 digest 重算；claim 闸门；Markdown；结算固定价格快照 / 幂等 / 反思降级；五端点 401/404/422、匿名只读不含用户信息、撤销即 404；集成幂等复用与重放一致；前端视图映射与公开页共用。 |
| M8 深研报 | 路由回落；五角色降级矩阵；辩论第 3 轮强制收敛；综合只引用归并证据；风控三类命中/放行成对；预算到限即停；数字须在同源 `numbers`；HTTP 401/404/409；集成 `interrupt → resume → 落库` 与进程重启后 resume；证据脚本对账 Langfuse。 |
| 前端与走查 | Vitest 只测纯函数（SSE 帧、图表映射、表单↔请求、错误体还原）；页面级走 Playwright 走查脚本人工核对。**走查必须验动作的结果**（例：等画布宽 >300 再数像素，否则「空画布」也会全绿）。 |
| 跨节口径 | 离线用例走真实 Parquet，不 mock 查询层；内存替身必须照抄关键约束（`UNIQUE` / 服务端过滤 / 归属），否则边界用例是假的；集成套件不进 CI。 |

## 13. 变更记录

> 按版本**降序**（最新的在前）。每条只留「决策 / 关键读数 / 验收结论」，过程铺陈与文件级流水账已收；完整证据仍在 git 历史与 `docs/private/`。

| 版本 | 日期 | 关联 | 变更 |
|---|---|---|---|
| v1.62 | 2026-10-31 | P1 修复 | **审计 P1**：① `research_reports(account_id, snapshot_hash)` 加部分唯一索引（`account_id IS NOT NULL`），`create_research_report` 改 `ON CONFLICT DO NOTHING` 回查；并发只出一份，历史重复不自动删、迁移明确报错。② lifespan 收尾对 chat/research 的 `_DETACHED` 限时 cancel/await（5s）；后台主实例启动把遗留 `research_runs.status='running'` 标 `failed/interrupted`，`awaiting_approval` 保持不动。详见 §8/§9。 |
| v1.61 | 2026-10-31 | P0 修复 | **审计 P0 三项**：① 模拟盘 `step` / `run` 的重放改读**全量**决策日志（`paper_decisions_all`，≤5,000 条），详情展示仍 500 条——修复 >500 条时旧 `filled` 决策被重生成覆盖；② `run` 的批量裁决不再单独落库，pending（非 `agent`）在内存改成 approved/rejected，随 `paper_advance` 同一事务写回——重放失败/409 零副作用；③ 深研报报告落库失败固定 `status=failed` + `error`，禁止 `done` + `report_id=NULL`。详见 §7.5/§7.6/§9；本机离线 **1903**（新增 15 例）/ 覆盖率 **88.95%**（门槛 88）。 |
| v1.60 | 2026-10-21 | 收口 | **文档收口**：§1 目录树改为模块边界指针；§12 测试策略压成分组表；§13 逐条压缩叙述，保留版本、决策、实测读数与验收结论；不放宽任何技术口径。 |
| v1.59 | 2026-10-14 | M5b（E1b 修订） | M5b 的「断线即断、不续传」由 P3-E1b 取代：`POST /optimize/{grid,batch}` 改 202 + `{task_id}`，进度由 `GET /api/v1/tasks/{id}/events` 订阅，断开只影响订阅；中止改 `POST /tasks/{id}/cancel`（协作式，结果不落库）。网格展开、闸门、DSR 判据与 `optimization_runs` 结构未动。详见 [P3 SPEC §2.8](P3-En.md#28-e1b网格--批量迁入队列2026-10-14-定稿)。 |
| v1.58 | 2026-10-14 | M4c-3 | 回测页可选「我的策略」落地：下拉分内置/我的两组，参数按源码 `PARAMS` 渲染，先保存才能跑；走查 11/11。修掉一个真 bug：`BacktestRequest.params` 把 `false` 收成 `0.0`，两处改 `float \| bool` 并补回归用例。后端离线 1346 / 前端 444。 |
| v1.57 | 2026-10-14 | M4c-3 | 规格修订：回测页策略选择从写死两项改为「内置 + 我的策略」，与优化页/工作台同源；仍守「先保存才能跑」，静态检查仍在提交期 422 返回 findings。后端零改动，PRD 不动（M4 验收未规定入口）。 |
| v1.56 | 2026-10-13 | 收尾 | 抬头状态行瘦身：原 1481 字逐段罗列 M1–M10 交付状态，与 §13 重复；改为一句现行状态 + 指向本节。 |
| v1.55 | 2026-10-13 | M10d | 事件详情抽屉落地，M10 五段完成：`GET /api/v1/events/{event_id}`（公开、`day` 可选、404）+ 七组纯函数 + 右侧抽屉 + 快讯流/个股页两处入口；走查 35/35。三条实测：正文收敛到抽屉一处；平台 `stocks` 混脏条目（只列六位码并明说滤掉几条）；本地修订标记全是默认值，验收改为「与库里逐字一致」。 |
| v1.54 | 2026-10-13 | M10d | 规格细化并拍板 D1–D5：抽屉七组（头部/正文/来源与版本/关联标的含 reason/人物/因子/修订史），价值是可追溯性的可见化；端点列集单列、`day` 可省略、查不到 404；行内不再展开 `summary`、抽屉不进地址栏、个股页共用、`source_url` 为空不出链接、修订史照原样。PRD v0.9。 |
| v1.53 | 2026-10-13 | M2d | 行情按需自动刷新落地，M2 四条通道全部自动化：`bars.refresh()` + `xiaoshi_cli` 收口 + 数据锁/台账 + 第二路调度 job。真联网四跑：真刷新 518.7s/395MB（9 片变，qfq 七年全重算）、发布标识未变 8.3s 零下载、二级门禁 192.9s 且 `data/bars/` mtime 未动、`--skip-download` 指纹与 24,006,374 行不变。成本口径订正：一个交易日约 6–7 分钟。后端离线 1316 / 前端 406。 |
| v1.52 | 2026-10-12 | M2d | 规格细化：行情无日增量，源侧按年整片、实测 T+1；门禁两级（`coverage` 发布标识 → 21 片 sha 全比），只比交易日会漏原地修订；门禁记忆落 `_meta/bars_sync.json`；加固 timeout 与两路锁；调度改 21:40（在发布会与语料 job 之后）；`/freshness` 加源侧边界。验收七条。 |
| v1.51 | 2026-10-12 | M10b · M10c | 翻页口径订正：标的榜与事件榜从「加载更多」改真分页（用户反馈）。判据抽 `lib/paging.ts`、UI 抽 `pager.tsx`；总页数至少 1、当前页由服务端 `offset` 反推、`clampPage` 总数未知时只保下界；标的页码进地址栏、换筛选回第 1 页、首次渲染不重置。走查 M10b 20/20、M10c 24/24。 |
| v1.50 | 2026-10-12 | M10c | 标的浏览落地，M10 整段完成：`/market/cross-section` 加 `industry` 并回显分页；`/stocks` 列表与个股页、板块钻取、深链。修掉「同一当日截面两个定义」（786 vs 787，池子不因筛选而变）；成交额改 2 位小数（1 位会让 93 只显示成 0）；保留 `?symbol=`。走查 18/18。 |
| v1.49 | 2026-10-12 | M10b | 事件日历与快讯流落地：`/events/calendar` + `/events/feed`；`importance_score` 不跨类型可比（前 200 名全是公告）⇒ 默认「快讯（除公告）」，公告单列；分页 `offset` 恒指下一页位置；列表键 `event_id@available_at`。走查 20/20。 |
| v1.48 | 2026-10-12 | M10a | 研究首页落地，`/` 归首页、对话迁 `/chat`：`/market/overview` + `/market/sectors`，等权代理与回测基准同一把尺子。实测三个硬事实：DuckDB 多 `unnest()` 按位置 zip（行业成交额之和 274% → 469%，treemap 被否）；涨跌停单侧比较在除权日误判，改对称；主板 ST 幅度 10% 不是 5%。新增 `divergePct` 配色。走查 25/25。 |
| v1.47 | 2026-10-12 | M10a | 规格细化并拍板 D1–D4：M10 占 `/`、对话迁 `/chat`；两条新端点；`raw.change_pct` 自 2026-09-21 起全空 ⇒ 涨跌停用 raw 前收自算；PIT 行业归属多对多（成交额合计 469%）⇒ 板块用排序横向 diverging 条，不用 treemap；配色 CVD ΔE 8.8；ECharts `inRange.color[0]` 映射 min，色阶按数据升序。验收五条。 |
| v1.46 | 2026-10-12 | M9 | 轻量教学三件套交付：使用教程（默认读者只有浏览器）+ FAQ（按症状、提示语原文）+ README 五处同步；数字口径三档（静态照写 / UI 只写判据 / LLM 不承诺复现）；新增 `check_docs.py` 三件机检；走查 54/54，逮到「教程写改名→保存，界面只有保存并运行」等四处。 |
| v1.45 | 2026-10-11 | M8 | M8c 过程可视化落地，M8 整段完成：运行页（阶段带/并行瀑布/预算条/审批卡/事件时间线）、`/space` 页签、报告 `run_summary`。事件带 `at` 偏移秒，帧与落库事件同形。三条只有真浏览器才现形的问题已修：`ReportView` 无条件读 `body.account`、未知块只渲染 `text`、`data-*` 为 undefined 不渲染。界验 15/15。 |
| v1.44 | 2026-10-11 | M8 | M8b 落地：辩论、综合、三类风控闸门、HITL、接 M7 容器。真数据端到端：done、11 块、16 证据、辩论 2 轮、10 次 LLM 调用、$0.0156、40.5s。PRD「深路径 p95 ≤15s」守不住，用户签改冷启动 ≤60s / 预热 ≤30s。两条真库缺陷：子链污染节点耗时；`AsyncPostgresSaver` 不能调同步 `get_state`。离线 1251。 |
| v1.43 | 2026-10-11 | M8 | M8a 落地：路由、五角色并行取证、归并、降级。真数据：0 缺口、并行墙钟 20.94s（五条之和 34.95s）、预算 $0.0015。降级分两层（可预期记 errors，意外上抛由 fan-out 兜）。数字可追溯判据修正：允许集扩到材料全文 + numbers + 标题摘要 + 标的代码，按数字主体核。离线 1190。 |
| v1.42 | 2026-10-11 | M8 | 开工前 F1–F7 实测：思考是延迟/成本主导项（flash 开 8.99s vs 关 0.84s；pro 开 107.03s vs 关 1.48s）⇒ M8 一律关思考；跨进程 resume 通过；节点事件需自埋；MPS 精排并发串行；API 并发几乎免费；标的名解析不需要 LLM。成本重估 $0.03–0.05/次。 |
| v1.41 | 2026-10-11 | M8 | M8 规格细化并拍板 D1–D4：M7 建容器、M8 建生成器；深路径独立 StateGraph，独立 thread 命名空间；四入口/三类闸门/公告方向映射走应用层/三段切片；取证=代码取数+LLM 解读、LLM 不持工具；过程可视化真源 `research_runs.events`；四条代码测绘事实决定写法。 |
| v1.40 | 2026-10-10 | 全局 UI | 页头导航加当前项指示：品牌色文字+浅底+中字重、`aria-current`；判据抽 `lib/nav.ts`（`/` 精确匹配、子路径归一、研报页归模拟盘、公开页不点亮）；七处手写 Link 改清单驱动。界验 23/23。 |
| v1.39 | 2026-10-10 | M7d | 一键导出 PDF（服务端渲染）落地，取代前端打印样式：两个端点直接回 `application/pdf`，子进程渲染、本机 Chrome、一次性令牌打印页；前端拆「导出 PDF / Markdown / 打印」；附件名 `{账户名}-{指纹前8}.pdf`。实测 500KB / 4.2s；界验 22/22。 |
| v1.38 | 2026-10-10 | M7c 修订 | 打印版式定向恢复：卡片永不跨页，只退化会跨页的竖排容器，卡片内部按标记恢复栅格。分页不变量 20/20。 |
| v1.37 | 2026-10-10 | M7c 修订 | 打印缺陷第二轮：打印时 flex/grid 一律退化 block，块级分页只会整行下移。代价是卡片内三列指标在纸上摊成一列；要既保真又一键出文件需服务端渲染（v1.39 已做）。另记：Chrome 打印对话框与 CDP `page.pdf()` 分页不等价。 |
| v1.36 | 2026-10-10 | M7c 修订 | 打印缺陷修复：容器 `break-inside: auto`，只对原子单元 `avoid`；段落 orphans/widows、thead 跨页重印、dev 覆盖层不印。两条不变量进界面验证；A4/Letter 复现不到用户那两处，如实记录。界验 20/20。 |
| v1.35 | 2026-10-10 | M7c | 研报页与证据追溯落地，M7 整段完成：登录页与公开页共用 `ReportView`，只认 `blocks[]`；claim 分级、证据面板、逐笔复盘、打印样式。配色经校验器改正（蓝紫 deutan ΔE 1.4 → 蓝/橙虚线）。匿名 context 可看完整正文。界验 17/17。 |
| v1.34 | 2026-10-10 | M7b | 决策记忆与到期结算反思落地：`memory/` 五模块 + 三端点 + 报告「逐笔复盘」块；`MemoryHolder` 按需建并按 loop 缓存（真库事件循环坑）；幂等进程内/跨进程都验；`freeze_review` 去掉运行读数；降级原因进日志。离线 1113 / 集成 97。 |
| v1.33 | 2026-10-10 | M7a | 绩效层与报告容器落地：`report/` 七模块 + `settle.py` + 八端点 + `research_reports` 表。claim 闸门拦下纯价量账户无证据（补回合合计数字，不放松闸门）；真数据逮到日期形状与 UUID 落库两个静默错；中文文件名走 RFC 5987。离线 1076 / 集成 96。 |
| v1.32 | 2026-10-10 | M7 | M7 规格细化并拍板 D1–D4：报告主体=模拟盘账户，一次生成即冻结、同 `(account, snapshot_hash)` 幂等；claim 分 fact/inference，事实必带 evidence 或 numbers；重放一致可断言；到期=买→卖回合；事实层可重算不落表，Store 只存结算快照与反思；分享 token 不可猜可撤销、响应不含身份；PDF 先走前端打印样式（后被 v1.39 取代）；结算与 ETL 调度解耦；归因只做标的级+事件级。 |
| v1.31 | 2026-10-10 | M5b · M5c · M6 补口 | `useEChart` 宿主改回调 ref：原 `[hostRef]` 身份不变，宿主「数据到了才渲染」或切走再切回时实例建不出来、静默无画布。四处调用点同步；option effect 也把宿主写进依赖。因子页 15/15、优化页 14/14。 |
| v1.30 | 2026-10-10 | M6 | M6b 前端 `/paper` 落地，M6 整段完成：会话条/账户卡/决策闸门/推进控制/净值曲线/持仓/流水/结算表。`lib/paper.ts` 28 条纯函数。界验 18/18；逮到切策略旧参数照发、图宿主零尺寸、批准后卡片消失三处（前两处是真 bug）。 |
| v1.29 | 2026-10-10 | M6 | M6a 后端落地：`paper/` 五模块 + 7 端点 + 四表 + 沙箱 `kind="paper"`。头号断言成立：单标的+全批与回测 `fills` 逐笔相等。推进一天=全量重放，每步 288–323ms。三个自动化层难发现的 bug：已成交决策要照原样记回账本、种回的挂单不得当日成交、已驳回决策不得带成交明细。离线 1004 / 集成 93。 |
| v1.28 | 2026-10-10 | M6 | M6 规格细化并拍板 D1–D8：回放式、六态闸门、未审批不成交；推进=从起点全量重放（纯函数）；用户策略进沙箱子进程；账户等额配额、撮合零复制；四表 + 单事务 + 乐观并发；决策单带来源三元组。不做手工/Agent 下单、改单、部分成交、急停、哈希链、分红送转。 |
| v1.27 | 2026-10-10 | M5c | M5c-2 `/factor` 前端落地，M5 整段完成：筛选行 + 指标卡（\|t\|<2 挂「不显著」）+ 逐日 IC + 分层 + 多空 + 表格孪生。配色经校验器：IC 离散极性对 CVD ΔE 23.8/22.2，分层单色相 ordinal 蓝阶。界验 13/13。用户三轮反馈后收口毛/净颜色+线型双编码。 |
| v1.26 | 2026-10-09 | M5c | M5c-1 因子管道落地：`factor/panel.py` + `analysis.py` + 公开 `GET /factor/report` + 四条查询。同尺子变成断言：真实 30.7 万行逐条喂 `EventFeed.advance`，归属日零不一致。评审抓住窗口外历史事件灌首日的 bug，补 `factor_market_days()`。规划期滑点写错（5bps→0.0005），费用拖累订正为 48.7%。离线 958。 |
| v1.25 | 2026-10-09 | M5c | M5c 规格细化并拍板 D1–D6：可用窗口归属、前向收益 `open(t+1)→open(t+2)`、毛/净双轨不含最低佣金、并入价格反转/动量、同步公开端点、落点改 `app/factor/`。订正 PIT 措辞（按事件日归日会丢 48.7% 原料）。实测：有向事件 70.5% 未挂标的；`factor_value` 与 `factor_scores.score` 同源；换手 96.6%/日；事件因子 RankIC 在噪声区间。 |
| v1.24 | 2026-10-09 | M2c 补口 | 四通道新鲜度：新增 X4 向量索引落后、X5 行情停更、X6 归档停更三条 warn；ETL 成功且确有物化后触发增量嵌入（子进程，独立锁/超时/失败只记台账/开关默认关）；行情仍未自动化。沿用 M1c 补口体例。 |
| v1.23 | 2026-10-09 | M2c | M2c 第三次核对：21:10 定时 daily 到位；10-08 缺口在 21:10 后发布，按 `--backfill` 补回（3,817 行/716 只，语料 93 天/307,704 行，gap 归零）。判据：`skipped` 只说明源侧与本地一致，缺口永远看 `gap`。处置一条红断言：改为「起点 ≤ 语料起点 + 末端 == 本地行情末端」；集成 86 passed。顺带同步抬头版本号。 |
| v1.22 | 2026-10-09 | M5b | M5b-2 `/optimize` 前端落地，M5b 整段完成：模式页签、逐格进度、DSR 卡、夏普分布、热力图+表格孪生、批量表、重开不重跑。色阶用数据定档（蓝↔红 ΔE 20.9/13.9，红=正夏普）。界验逮到三处：`cells` 空洞导致整页白屏、基座参数收起仍照发、批量表键撞。界验 13/13。 |
| v1.21 | 2026-10-09 | M5b | 批量/网格/过拟合全段交付：真实 5×5 网格 25/25，并发峰值 2 且逐格相等，耗时比值 0.89–0.95 如实记录不设阈值；最优格夏普 0.3027；DSR 0.5183，与 scipy 差 1.1e-16；25 格逐键与独立报告相等。两处契约：用户策略参数归一器填满缺省；`summary` 增 `kind/cells_total/cells_ok`。离线 904。 |
| v1.20 | 2026-10-09 | M5b | M5b 规格细化并订正两处性能读数：本机 8 核、DuckDB `threads=8`，并发 2 仅 0.83/0.90，瓶颈不是 GIL ⇒ 判据改「峰值 ≤2 + 逐格相等」，不设耗时阈值；更便宜的加速点是取数记忆化（25 格可降到 1.5–1.9s），单独立项不塞 M5b。定接口契约、轴数/格数/批量上限、全局 Semaphore、DSR 四条口径、取消语义。 |
| v1.19 | 2026-10-09 | M5a | M5a 落地：A 股规则五码接进引擎，涨跌停用 `Decimal` 自算；既有 golden 分毫不动。三处降级往「判大/跳过」倒；名称字典三重过滤后一名多写剩 5 只，覆盖率 96.7%；等权基准剔除数与独立 SQL 吻合，同区间策略 -6.53% / 买入持有 -9.72% / 全市场等权 -1.90%。报告新增四块两端点。界验 13/13，抓到旧数据形状白屏。 |
| v1.18 | 2026-10-09 | M5 | M5 规格细化并切片 M5a/b/c；拍板 SSE 逐格推送、网格落汇总不落完整报告、涨跌停 raw 前收自算。实测五事实：换手率源侧停更；停牌是死代码；一字板有真样本但演示标的干净；名称覆盖 96.7%（不是 99.2%）；因子横截面薄（日均 199 只/3.6%）。名称字典三重过滤；基准剔除涨跌幅绝对值 >30% 的样本；订正「复用回测引擎跑分层」措辞。 |
| v1.17 | 2026-10-09 | M2c | M2c 落地回填（第二段）：三天台账齐、无重复无漏拉、gap 归零、体检 error 0/warn 5。归档停更 9 天后恢复，09-30 被 7 天滑窗漏掉，按约定 `--backfill` 补回（6,512 行/967 只），语料 84 天/289,519 行 → 92 天/303,887 行。两条判据：`failed` 不等于缺数据；恢复发布先看 gap。行情仍停 09-30，事件到 10-07，多出 7 天不被消费。 |
| v1.16 | 2026-10-09 | M5 · M10 | 「股票展示/查询」定范围：不新开功能 ID，拆进 M5 前置与 M10。名称在事件语料 `stocks` 列（覆盖 5,608/5,798=96.7%），不引第二数据源；新增横截面能力（全市场排序 20ms）；M10 补标的浏览，热力图走事件 `industries` 并标注覆盖率。 |
| v1.15 | 2026-10-09 | M1c | 加自选表单即时反馈：新增 `GET /api/v1/market/{symbol}/probe`，「本地没有」是 200 `has_data:false` 不是 404；输满 6 位防抖 300ms 查；四态界面口径（已在自选要确认、本地无代码禁加、行情层不可用不拦）；前端判重只是提示，裁决权在 POST 409。 |
| v1.14 | 2026-10-08 | M4c | M4c 落地（后端+前端）：Monaco 主题与全站 token 同源，新增语义色 `--warn`；CORS 漏 PUT（同类坑第二次）与 sticky 运行条遮挡已修。离线 652 / 集成 80 / 前端 148。 |
| v1.13 | 2026-10-08 | M4c | M4c 规格细化并拍板 D1–D6：`strategies` 表约束与 `backtest_runs` 两增列；HTTP 闸门顺序与错误码映射；七端点响应形状、`/check` 返回 `{findings, meta}`；回测 additive 增量；Monaco spike 结论（本地资源零外部请求）；前端工作台与回测页处置。 |
| v1.12 | 2026-10-08 | M4b | M4b 落地：规则 `message` 字段、同一规则可拆多条、行号归属、模板装载 API。规则矩阵 35 正样例全命中、29 负样例零命中；CLI 样例拦下 6 个 error；模板与注册表策略真数据逐点一致。回归 616。 |
| v1.11 | 2026-10-08 | M4b | M4b 规格细化：新增 R0 语法（`check_source` 从不抛）、R1 数据绕行（白名单读运行时同一份）、R2 未来函数（`ffill`/`pad`/`.shift(正数)` 不拦）、R3 双档（`error` 只留「明说当前+n」，切片放行）、R4 兼收 `parse_meta` 拒绝、R5 形参校验；findings 契约增 `message`；模板库与等价性测试。 |
| v1.10 | 2026-10-08 | M4a | M4a 落地：`on_bar` 形参 `(ctx)` 或 `(ctx,p)`；内存配额不按平台分叉（Linux `RLIMIT_AS` 会 import 期自杀），一律 `ru_maxrss` 看门狗。落地物含沙箱、参数校验、注入点、故障矩阵 11 条全处置。 |
| v1.9 | 2026-10-08 | M4 | M4 规格扩写并切 M4a/b/c：主闸门是结构（ctx 截断 + PIT + 无 pandas/IO），import 白名单是正确性闸门，AST 是第二道门；沙箱配额实测定档（CPU rlimit、内存 `ru_maxrss`、墙钟父进程兜底）；R1–R5 定稿并订正 `ffill` 不拦；模板 5 个；运行契约「先保存才能跑」；`strategies` 表 + 7 端点；前端 `/strategies`。 |
| v1.8 | 2026-10-08 | M3 | M3 落地回填：三档 NDCG@10 dense 0.7764 / hybrid 0.7602 / rerank 0.8142；稀疏腿无增益（−0.0162），精排 +0.0540；精排候选数定 50（增益主要来自 21–50 名）；NFR 冲突三条出路交用户裁决。 |
| v1.7 | 2026-10-07 | M3 | M3a spike 实测：fastembed 不支持 BGE-M3 dense+sparse 与 reranker ⇒ 定 FlagEmbedding + torch；CPU vs MPS 用数据定档（CPU 22.8 条/s、1.0GB；MPS 快 1.57× 但 4.2GB 且有泄漏）⇒ 选 CPU；精排 30–34ms/对；两条下载坑（Xet 卡死、`cache_dir` 指 hub）。 |
| v1.6 | 2026-10-07 | M3 | M3 规格扩写并切 M3a/b/c：三条实测约束（无长文本、公告摘要空 83%、`industries` 覆盖 0%）；`dedup_key` 非唯一 ⇒ point id 用 `uuid5(day\|event_id)`；嵌入先 spike；RRF 显式 k=61；payload 索引必建；PIT 过滤必须服务端；评测集口径与 NDCG@10 ≥0.5 下限。 |
| v1.5 | 2026-10-07 | M2c | M2c 规格细化：`data_health.py`/`delisting.py` + 三条硬规则；检查项 B1–B8/E1–E7/X1–X3 定级；E7 不得复用 `runner.status()`；B7 改每日 bar 数包络；B5 容差 0.011pp；退市核实改数据内双证据链（8 候选/2 确证/6 待解释）；三天连跑分两段收口。 |
| v1.4 | 2026-10-07 | M2b | M2b 规格细化：事件语料改归档按日整片（MCP 500 硬顶且不可翻页）；MCP 只服务对话实时查询；日历冻结文件 + DuckDB 视图，全期双向对账；落盘 schema `quant-event-v2`；事件驱动策略时间收口；日任务改回落窗口。 |
| v1.3 | 2026-10-07 | M2a | M2a 规格细化：全市场 21 片扁平命名、清掉 P1 按标的文件、先暂存后换入、指纹锚 `object_key`+sha256、无价 bar 不进定价路径；关闭指数/行业两项待验证；事件长历史口径复核；M2b 记日历与归一化。 |
| v1.2 | 2026-10-07 | M1c | M1c 规格细化：自选股六端点与分组约束、行情降级；回测落库、`POST /backtest` 鉴权+信封、鉴权先于参数校验；个人空间四页签与深链；自选股不单开路由；§12 补增量。 |
| v1.1 | 2026-10-06 | M1 | M1 规格细化并切 M1a/b/c：四张自有表与幂等建表；会话归属改自有表；auth 端点/cookie/密码 72 字节/JWT 降级；越权 404 与公开边界；回测信封；`core/db.py` 与 `(app)/` 路由组；测试双跑口径。 |
| v1.0 | 2026-10-06 | M1–M10 | 初版：P2-Mn 技术规格（含竞品调研后扩容的 15 项与 M10）。 |
