# Changelog

> 按里程碑记录交付内容，版本号 = git tag（`m-p1`、`m-p2`…）。格式：新增 / 调整 / 修复。

## [未发布]

### 新增

- **M1a 用户地基与归属校验**：注册 / 登录 / 退出 / 当前用户四端点（bcrypt + HS256 JWT，httpOnly + SameSite=Lax cookie）；自建业务表（`users` / `chat_threads` / `watchlist` / `backtest_runs`）与幂等建表机制；**会话归属真源改为自有表**——列表不再直读 langgraph 的 `checkpoints` 内部表，越权与不存在同返 404、未登录一律 401（P1 遗留阻塞项清零）
- **M1b 前端认证闭环**：`app/(app)/` 受保护路由组（守卫 + 页头唯一落点）、登录 / 注册页（含开放重定向白名单）、`AuthProvider` 经 `/auth/me` 探测登录态、全部请求与 SSE 带 `credentials: "include"`；页头显示账号与退出
- 后端 `app/core/{auth,db}.py`、`app/api/auth.py`；前端 `lib/auth-form.ts` 纯函数（密码按字节判长、`next` 回跳白名单）
- **M1c 自选股**：`GET|POST /api/v1/watchlist`、`PATCH|DELETE /api/v1/watchlist/{symbol}`、`PATCH|DELETE /api/v1/watchlist/groups/{name}`；分组是 `group_name` 字符串列（无分组实体表），重命名撞名即合并、删组时标的回落「默认分组」；**加自选以来涨幅**取加入时最近可得收盘价（qfq），**取不到即留空**（显示「—」，不编数）；行情层不可用时列表照常返回（价格降级 + warning），不被行情依赖拖死
- **M1c 我的回测**：每次回测落库（`backtest_runs`，request 存解析后的 config），`GET /api/v1/backtest/runs` 摘要列表（SQL 抽 JSONB 子集）+ `GET /api/v1/backtest/runs/{id}` 完整报告（越权 404）
- **M2a 全市场行情扩容**：行情从 3 个标的扩到**全市场 A 股**——`cn-daily` 21 片 = 2020–2026 × raw/qfq/hfq，约 5500 只 / **2397 万行**，DuckDB 直读 Parquet 零 ETL（单标的七年全区间 151ms、单日全市场聚合 5ms）；落盘改扁平命名 `cn-daily_CN_{复权}_{年}.parquet`（查询层 glob 语义不变），物化改「先落 `_staging/`、逐片核 sha 与行数后再整体换入」；清单升 v2，`fingerprint` 锚定**逐片 `object_key` + sha256**（回执顶层的 `manifest_version` 不是数据版本，实测同一批分片来自多个 release）；**无价 bar 不进定价路径**（`trading_status='no_turnover_observed'` 的日子可能整行价量为空，留进去会被读成 0 元、期末权益静默塌到现金）；回测页标的控件从三选一下拉改为六位代码输入框 + 示例快捷项
- **M2b 交易日历**：`exchange_calendars` 的 XSHG 日历**一次性生成并冻结为仓库内数据文件**（`app/data/trading_calendar.json`，2916 个交易日，运行期零第三方依赖）；与 21 片行情的 `trade_date` **全期双向对账零例外**（2020-01-02 → 2026-09-30 共 1636 个交易日）；`calendar.py` 对越界日期**显式报错**（「不是交易日」与「我不知道」是两件事），DuckDB 增 `calendar` 视图
- **M2b 事件语料改全市场按日 + 日增量 ETL**：语料从「3 只示例标的」扩到**全市场按日落盘**（`data/events/cn-events_{日期}.parquet`，一条事件一行、标的是数组 `symbols`），按 `quant-event-v2` 重定列集（新增 `dedup_key` / `revision_id` / `is_corrected` / `correction_count` / `original_url` 等）；通道从 MCP 改为**归档按日整片**（`xiaoshi-data` CLI，逐片 sha256 校验）；进程内 APScheduler 定时（默认不启用）+ 启动补缺口 + `POST /api/v1/etl/run`（后台执行、并发锁）+ `GET /api/v1/etl/status`（覆盖 / 缺口 / 台账）；幂等：同一份归档分片重跑**逐字节一致**
- **M1c 个人空间页** `/space`：四页签「我的自选 / 我的回测 / 会话历史 / 我的策略（M4 前占位）」；**深链回原页完整恢复**——会话历史 → `/?thread=<id>`、我的回测 → `/backtest?run=<id>`（载入完整报告并回填表单）；跑完一次把地址更新为 `?run=`
- 前端 `lib/watchlist.ts`（分组视图与 symbol 校验，纯函数 + 单测）、`lib/api.ts` 的 `describeError`、`components/space/*`
- **T6d 图表交互**：净值曲线与 K 线的手势统一为「捏合缩放 / 横向滚轮平移 / 拖拽平移 / 竖直滚动归还页面」，两图各自独立缩放并各带「重置缩放」；缩放数学抽为纯函数 `lib/chart-gesture.ts`（附单测）
- 根 `README.md`：项目门面（定位与护城河、架构图、界面截图、快速开始、API、已知边界）
- `docs/images/`：三张界面截图，配 Playwright 截图脚本（`pnpm screenshots`，可重跑）

### 调整

- **登录 / 注册页改版**：整屏连续画布（网格／光晕／双净值曲线装饰铺满全屏，CSS+SVG，零图片素材）+ 浮起的半透明表单卡，左右不再割裂；左栏为字标、定位标语、三条能力点与一句钩子（Point-in-time：不让未来的信息，参与过去的决策）；主题切换移到登录页，深浅与登录后同源（`ThemeProvider` 共用）；登录页外壳提到 Suspense 边界之外，生产构建下品牌面直接 SSR
- **标题字体由宋体改为无衬线**（`--font-heading`：PingFang SC 栈 + 字重），全站标题层级改由字重与字距拉开——宋体在 macOS 上只有 Regular/Bold 两档，正文尺寸下发虚观感旧
- **对话页空态引导**：三条示例问题按能力分类（行情 / 对比 / 事件），点选灌进输入框并聚焦（不直接发送）；页头显示「数据截至 X」，值由 `GET /api/v1/market/freshness` 查询得出（M2 的日增量 ETL 接上后自动前移）
- **「记住我」**：登录页默认勾选，勾选=持久 cookie（7 天），取消=会话 cookie（关浏览器失效）；只改 cookie 存活方式，不动 JWT 有效期、不建服务端会话表
- `POST /api/v1/chat` 与三个会话端点要求登录（会话列表口径改为自有表 `last_active_at`）；`/api/v1/market`、`/api/v1/events` 保持公开（非用户资产）
- **`POST /api/v1/backtest` 纳入鉴权并改信封 `{run_id, report}`**（M1c 落库使归属成为必要信息）：报告结构本身未动，契约变更同步记入 P1 SPEC §7 与 §10（v0.13）；**鉴权先于参数校验**——未登录 + 非法体返回 401 而非 422
- `GET /api/v1/chat/threads` 增补 `last_active_at`（排序依据顺带带出，供个人空间显示「最近活动」）；`Database.list_thread_ids` 改为 `list_threads`
- CORS `allow_methods` 补 `PATCH`（自选股改分组用；漏了浏览器直接拦掉且报错难懂，T6b 漏 DELETE 同款）
- CORS 开 `allow_credentials=True`（凭据模式要求显式 origin 列表，已是）
- 依赖新增 `bcrypt==5.0.0`、`pyjwt==2.15.1`（与 checkpoint 链同等显式 pin）
- 文档链同步：PRD 升 v0.5（状态行改「已通过」、§6 待确认①证伪②③转 M2）；SPEC 升 v0.12（样本量订正 55 / 2.1 倍、T1 依赖清单补齐、章节按功能 ID 重排、§7 补图表交互口径）；CLAUDE.md 技术栈表增「落地状态」列、数据源通道对齐 SPEC
- 文档链同步（M1c）：P2 SPEC 升 v1.2（§2 M1c 细化为可执行规格、§1 自选股不单开路由改 `space/`、§12 补 M1c 增量与回归口径）；P1 SPEC 升 v0.13（§7 给 `POST /backtest` 加注、§10 记契约变更）
- **政策立场纳入方向映射**（M2b）：`dovish → 利多`、`hawkish → 利空`——实测这 385 条原被整批归成「无方向」；公告的类别标签（融资定增/业绩预告…）**不映射**，那是业务判断，留 M8 的类别→方向表。注：政策事件不带标的，故这张表当前不改变任何个股回测结果，修的是数据语义
- **新增「用本地分片重物化」**（M2b）：`download_events.py --rematerialize`，零网络会话，改口径后让历史数据与新口径对齐（实测 84 天约 90 秒、只改受影响的日子）
- **事件语料通道改归档，MCP 退回只服务对话实时查询**（M2b）：实测 MCP 单次返回硬顶 500 行、`cursor` 参数被 FastMCP 拒收（结构上不可翻页），单日任一主要类型都已触顶——装不下「全市场按日」；归档是 `date × event_type` 的整市场分片，逐片带 sha256
- **事件驱动策略的时间收口**（M2b）：判据是**本地语料覆盖区间**（起点固化、终点随日增前移），不是写死的「最近 3 个月」；显式把 `start` 放在覆盖起点之前一律 400 并给出路（改起点或换 `ma_cross`），报告 `meta` 增 `event_coverage` 与 `events_in_window`，回测页事件表显示本次依据的语料区间
- `dc.events()` 的标的过滤改**数组包含**（`list_contains(symbols, ?)`）：一条事件可挂多只股票，等值比较会永远匹配不到；`EventView.symbol` → `symbols`；`/api/v1/market/freshness` 增补 `event_coverage`
- **查询层对 `data/events/*.parquet` 是严格 glob**：日分区列集必须一致，加列要整体重物化——用「混合 schema 直接报错」换掉「静默并集」
- 事件语义上只收事件、不收状态快照（M2b）：`sector_constituent` / `future_dynamic` 实测占归档两天体量的 **91.7%** 且 `title` 为空，明确排除并写进清单
- 文档链同步（M2a/M2b）：P2 SPEC 升 v1.3 / v1.4；P1 SPEC §3 加注「事件语料条目已被 M2b 取代」；CLAUDE.md §四 通道与保留期口径同步；README 数据源、API、已知边界三处同步

### 修复

- **登录守卫会吃掉深链**：`(app)/layout.tsx` 重定向登录页时只带 `pathname`，`/?thread=x`、`/backtest?run=y` 在会话失效重登后丢失。改为带上查询串（`window.location.search`，该层不能用 `useSearchParams`）
- **从 `localhost` 打开时登录/注册看似失败**：API 基址原写死 `127.0.0.1:8000`，页面在 `localhost` 时两者跨站，`SameSite=Lax` 的会话 cookie 被浏览器静默丢弃（后端其实已注册成功）。改为 `apiBase()` 跟随页面 host，两个 hostname 下都跑通端到端
- 定下 SPEC 版本编号规则：主版本 = 阶段序号（P1 → `0`、P2 → `1`、P3 → `2`）
- 后端前视偏差措辞中性化（注释与 docstring 7 处，无行为变更）
- `frontend/README.md` 脚手架原文改为指向根 README 的短说明（原端口指引为 3000，实际为 3001）

## [m-p1] — 2026-10-06

> P1-Tn 跑通 demo：双闭环（数据 → 策略 → 回测 → 结果 / 对话 → 工具 → 回答）均可在浏览器内走完，
> 前视偏差两口径差异可量化，示例策略教程可复现。

### 新增

- **T1 地基**：uv + Python 3.12 monorepo（`backend/` + `frontend/`）；Docker Compose（PostgreSQL 18 宿主端口 5433 / Redis / Qdrant；Langfuse 自托管挂 `observability` profile）；小石 CLI 独立 venv + stdio MCP；LangGraph 用 Postgres checkpointer
- **T2 样例数据**：cn-daily 年度分片 → 本地过滤 3 标的（600519 / 300750 / 600036）× raw / qfq 落 Parquet；事件语料经 MCP 拉取（窗口 2026-07-05 → 2026-09-30，落盘即冻结，保留 `source` / `original_source` / `content_hash` 溯源三元组）；DuckDB 只读视图直读；下载清单与校验证据落 `data/_meta/`
- **T3 Agent 对话**：LangGraph 单 Agent（`deepseek-flash` 经 LiteLLM），DuckDB 行情工具 + 小石 MCP 事件工具（白名单暴露，动作型工具不进 LLM 工具集）；SSE 流式（`token` / `tool_call` / `tool_result` / `done` / `error`）；会话列表、历史消息、删除三端点；Langfuse 全链路 trace（含工具调用与成本）
- **T4 最小回测**：事件循环驱动，信号当根收盘生成、次根开盘成交；A 股撮合（100 股整手、佣金万 2.5 最低 5 元、卖出印花税 0.05%）、费用与滑点独立开关；策略 `ma_cross` / `event_driven`
- **T5 分析输出**：指标（总收益 / 年化 / 最大回撤 / 夏普 / 胜率 / 交易次数）+ 份额化买入持有基准；`pit_comparison` 量化前视偏差两口径差异（方向不预设）；`bars < 120` 时样本量提示进报告与 UI
- **T6 极简前端**（T6a / T6b / T6c）：Next.js 15 + TS + Tailwind + shadcn/ui（端口 3001）；对话页（SSE 流式渲染、工具步骤可视化、停止生成、会话列表与删除、历史回看）；回测页（参数表单、指标卡、净值曲线、K 线买卖点、PIT 对比表、交易明细、事件表含来源标注）；前端纯函数 85 项单测
- **T7 示例策略教程**：`docs/tutorials/ma_cross.md`、`docs/tutorials/event_driven.md`——策略逻辑、参数含义、结果解读、复现命令与期望数字、参数实证、回测页路径

### 调整

- PIT 口径定为**收盘时刻级**（每根 bar 的 15:00，Asia/Shanghai），闸门只作用于事件语料；日线 OHLC 按「当日收盘已知」处理（`bars.available_at` 是整表下载快照，逐 bar 设卡会把历史全滤空）
- 前视偏差表述统一中性化：写「两口径差异」，不写「虚高」——T5 实测非 PIT 期末权益均未高于 PIT，方向不预设（PRD / SPEC / CLAUDE.md 同步）
