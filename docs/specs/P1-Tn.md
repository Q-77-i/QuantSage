# QuantSage SPEC · P1-Tn（跑通 demo）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，已通过）→ 本文（技术规格）→ 代码
>
> 版本 v0.12 ｜ 2026-10-06 ｜ 状态：已通过
>
> 本 SPEC 覆盖 PRD §2.1 的 T1–T7。**正文章节按功能 ID 排序**（§2–§8 对应 T1–T7）。
> 注：实际实现顺序为 T1 → T2 → T4 → T5 → T3 → T6 → T7（先打通数据与回测，再做对话与前端），故变更记录的版本号先后与功能 ID 不同序。

## 目录

| 节 | 内容 |
|---|---|
| [§1](#1-仓库结构) | 仓库结构 |
| [§2](#2-t1-地基) · [§3](#3-t2-样例数据) · [§4](#4-t3-agent-对话) · [§5](#5-t4-最小回测引擎) · [§6](#6-t5-分析输出) · [§7](#7-t6-极简前端) · [§8](#8-t7-示例策略教程) | T1–T7 规格 |
| [§9](#9-测试策略) | 测试策略 |
| [§10](#10-变更记录) | 变更记录 |

## 1. 仓库结构

```
QuantSage/
├── backend/
│   ├── pyproject.toml              # uv 管理，Python 3.12；依赖显式 pin（防 uv lock --upgrade 静默漂移）
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口、lifespan 装配、异常映射
│   │   ├── api/                    # chat.py / backtest.py / market.py / events.py
│   │   ├── agent/                  # graph.py / tools.py / prompts.py / history.py
│   │   ├── backtest/               # types.py / events.py（PIT 闸门）/ engine.py / portfolio.py
│   │   │                           #   / broker.py / costs.py / metrics.py / report.py / strategies/
│   │   ├── data/                   # duckdb_client.py / xiaoshi.py（CLI 与 MCP 封装）
│   │   └── core/                   # config.py / checkpoint.py / llm.py / langfuse.py / logging.py
│   ├── scripts/                    # download_bars.py / download_events.py / init_checkpoint_db.py
│   │                               #   / run_backtest.py / run_report.py / run_chat.py
│   │                               #   / verify_xiaoshi_mcp.py
│   └── tests/                      # 离线用例 + tests/integration/
├── frontend/
│   ├── app/                        # page.tsx（对话页）/ backtest/page.tsx（回测页）
│   ├── components/                 # ui/（shadcn）+ chat/ + backtest/
│   ├── lib/                        # api.ts / sse.ts / markers.ts / chat-state.ts / backtest-form.ts
│   │                               #   / chart-theme.ts / format.ts / types.ts（纯函数，附单测）
│   └── scripts/                    # capture-screenshots.mjs（README 截图，Playwright）
├── docker/postgres/init/           # 01-create-langfuse-db.sql
├── docs/                           # PRD / specs / tutorials / images
├── data/                           # 行情 Parquet / 事件语料（gitignore）
├── logs/                           # 验收证据（gitignore）
├── .tools/xiaoshi/                 # 小石工具包托管安装（gitignore）
├── docker-compose.yml
└── .env
```

## 2. T1 地基

- Python 3.12 + uv。依赖：fastapi / uvicorn[standard] / pydantic / pydantic-settings / langgraph / langgraph-checkpoint / langgraph-checkpoint-postgres / langchain / langchain-core / langchain-mcp-adapters / langchain-litellm / mcp / litellm / duckdb / pyarrow / psycopg[binary,pool] / psycopg-pool / httpx / langfuse / pytest / pytest-asyncio
- Docker Compose 服务：postgres:18 / redis / qdrant（默认启动；postgres 宿主端口 5433，本机 5432 已被占用）
- Langfuse 自托管全套（web/worker + clickhouse + minio + 独立 redis）挂 `profiles: [observability]`，默认不启动，T3 接入 trace 时开启；v4 起必须 ClickHouse + 对象存储，无法只在 postgres 上跑
- 小石 CLI 装入独立 venv（不动系统环境）；MCP 以本地 stdio 接入；密钥只读 `.env`
- LangGraph checkpointer 使用 Postgres

**验收**：`docker compose up` 全绿；MCP 完成 initialize + tools/list + 一次有界认证查询；pytest 空跑通过。

## 3. T2 样例数据

- 标的：贵州茅台 `600519`、宁德时代 `300750`、招商银行 `600036`（symbol 格式以落盘实测为准）
- 日线：`cn-daily` 年度分片（year=2025/2026 × adjust=raw/qfq，共 4 片）下载后本地过滤 3 标的至 `data/bars/{symbol}.{adjust}.parquet`，保留分片全字段（含 `available_at`、`adj_factor`）
- 事件语料：经 MCP `get_event_timeline` 按标的拉取至 `data/events/{symbol}.parquet`；在线 PIT 窗口上限约 3 个月，本期取 2026-07-05 → 2026-09-30
  - ⚠️ **M2b 起本条已被取代**（P2 SPEC §3 M2b）：语料改「全市场按日」落盘，通道改归档按日整片（MCP 保留给对话实时查询）。P1 的 `{symbol}.parquet` 布局与字段集均已作废，仅作历史记录
  - 保留字段：`symbol` / `event_id` / `event_type` / `title` / `summary` / `event_time` / `available_at` / `observed_at` / `direction` / `direction_norm` / `confidence` / `importance_score` / `factor_value` / `factor_scores` / `industries` / `stocks` / `source` / `original_source` / `content_hash` / `quality_status` / `source_time_quality`
  - `direction` 原值中英混用，派生 `direction_norm` 归一到 `bullish` / `bearish` / `neutral`；`高管人事` 这类非情绪标签归 NULL（无方向语义），不臆造情感极性
  - `industries` / `stocks` 落原生嵌套类型，`factor_scores` 键集随事件类型变化故存 JSON 文本
  - 溯源标注用 `source` + `original_source` + `content_hash` + `quality_status`；数据源无 `source_verified` 字段
- 原始分片留档 `data/raw/`；下载清单（manifest 版本 / 时间 / sha256 / 行数 / 区间）落 `data/_meta/*.json`
- DuckDB 查询层：对 `data/bars/*.parquet` 与 `data/events/*.parquet` 建只读视图

**验收**：SQL 可查任意标的日线；事件 `available_at` 无空值且 `available_at >= event_time`；单标的 1 年日线查询 < 1s。

## 4. T3 Agent 对话

- 图：START → agent（LLM + tools）→ END；Postgres checkpointer；模型 `deepseek-flash`（LiteLLM SDK 统一调用）
  - 模型接入经 `langchain-litellm` 的 `ChatLiteLLM`，模型名必须带 provider 前缀（`deepseek/deepseek-flash`）——litellm 不接受裸模型名；密钥由 `Settings` **显式传入**，不依赖 `os.environ`
  - 图在 lifespan 内构建：checkpointer 在 compile 期固化，导入期建图会永久拿不到真 saver。MCP 或数据库不可用时按既有降级姿态启动，不阻断服务
- 工具：`query_market_bars`（DuckDB）、`query_events`（小石 MCP，经 langchain-mcp-adapters 注册，按 tools/list 实际结果绑定）
  - 小石工具**按白名单暴露**：只放只读查询类，动作型（`plan_history_download` / `prepare_local_research` 一类）不进 LLM 工具集——误调用会触发昂贵下载；白名单以 tools/list 实际结果为准，缺项时启动告警
  - 工具异常（含 MCP 传输错误）转成错误结果交模型自纠，不穿出图炸断整条流；MCP 单次调用设超时上限（适配器本身没有超时参数，须自行包裹）
  - 本地 DuckDB 查询是同步 IO，须卸载到线程执行，避免阻塞事件循环
- 流式：SSE 事件类型 `token` / `tool_call` / `tool_result` / `done` / `error`
  - `tool_call.data` = `{id, name, args}`；`tool_result.data` = `{id, name, content, is_error}`，其中 `content` 为**截断预览**——MCP 返回可达上百 KB，不整包塞进事件流
  - `data` 一律单行 JSON（`ensure_ascii=False`）；客户端按 `\n\n` 切帧，不得假设「一个网络分片 = 一个事件」
  - 图内异常转 `error` 帧（`{code, message, request_id}`）而非裸抛断连；静默期发注释帧保活
  - **客户端断连后图继续跑完**（2026-10-09 改，原为「断连即取消」）：取消点若落在「模型节点已写出 `tool_calls`、工具节点尚未执行」之间，checkpoint 会留下悬空调用，此后**每一轮**都被模型 400 拒掉——会话永久不可用（实测复现）。现在断连只是不再收帧，图跑完照常落 checkpoint，重新进会话就能看到完整回答；起新一轮前还会先体检历史、给悬空调用补一条错误结果（`heal_dangling_tool_calls`），治的是进程被杀一类留下的存量会话
- API：`POST /api/v1/chat`，请求 `{thread_id?, message}`；`thread_id` 缺省时由服务端生成 UUID，经响应头 `X-Thread-Id` 与 `done` 事件**双通道**回传（客户端中途断连也能拿到会话号）
- API：`GET /api/v1/chat/threads` → 会话列表（T6 对话页需要；补齐 SPEC 原有的契约缺口）
  - 取数：只读 SQL 从 `checkpoints` 表按 `thread_id` 聚合（限 `checkpoint_ns = ''`）；消息正文落在 `checkpoint_blobs`（msgpack），**不能手写 JSONB 路径取**，须经 checkpointer 官方读接口反序列化；标题由服务端从首条人类消息派生，不接受客户端传入
  - 暂无归属校验：单用户 demo 可接受，**P2 引入登录后必须按用户过滤**（记为 M1 阻塞项）
- API：`GET /api/v1/chat/threads/{thread_id}/messages` → 单个会话的历史消息（T6b 对话页回看需要）
  - 路径参数非 UUID → 422；thread 不存在 → 404；checkpointer 不可用 → 503
  - 响应 `{thread_id, messages: [{role: "user" | "assistant", content, tools: [{id, name, args, content, is_error}]}]}`；user 消息的 `tools` 恒为 `[]`
  - 取数与会话列表同源：经 checkpointer 官方读接口反序列化最新 checkpoint 的 `channel_values.messages`，不手写 msgpack / JSONB 解析
  - **合并语义**：一个 human 之后的全部 AIMessage 合并为**一条** assistant（正文按序拼接、工具步骤按调用顺序累计）。真实模型常在**同一条** AIMessage 里既给正文又给 tool_calls，一次提问也常产出多条 AIMessage（说话 → 调工具 → 再说话），而实时 SSE 里它们是同一个助手气泡；直到序列末尾或下一条 human 仍无正文时只输出工具步骤（回合中断形态）。合并后与实时 SSE 产出**同一种消息结构**，前端共用一套渲染
  - `tool.content` 为 null 表示该步没有结果；有结果时与 SSE `tool_result` **同口径截断**（复用 `TOOL_RESULT_PREVIEW`）
  - 转换逻辑为纯函数（`app/agent/history.py`），离线单测；`system` 与未知消息类型跳过
- API：`DELETE /api/v1/chat/threads/{thread_id}` → 删除会话及其全部 checkpoint（T6b 会话管理需要）
  - 路径参数非 UUID → 422；thread 不存在 → 404；checkpointer 不可用 → 503
  - 走 checkpointer 官方删除接口（`adelete_thread`），不手写 SQL 删表；CORS 须放行 `DELETE`
  - 响应 `{thread_id, deleted: true}`；删除当前打开的会话后前端回到空态
- Langfuse 记录完整 trace：须**显式构造客户端**后再取 `CallbackHandler`——`.env` 只进 pydantic Settings、不进 `os.environ`，零参构造会静默降级为 NoOpTracer（表现为「无报错但没有任何 trace」）
- 本轮不做（记录待议）：同 thread 并发写入的串行化、请求幂等去重、历史消息裁剪与上下文超限策略

**验收**：问「贵州茅台最近行情」Agent 自主调工具并流式作答；Langfuse 可查看完整 trace（含工具调用与成本）。

## 5. T4 最小回测引擎

- 事件循环：按 bar 时间排序驱动；信号在 bar 收盘生成，成交在下一 bar 开盘（防日内前视）
- 撮合：市价单。成本模型：佣金双边万 2.5（单笔最低 5 元）+ 印花税卖出 0.05% + 滑点（bps）；买入按 100 股整手向下取整。费用与滑点各为独立开关，便于分别验证影响
- PIT 与行情的关系：**PIT 闸门只作用于事件语料**；`bars.available_at` 是整表下载快照而非逐 bar 可得时间，对 bars 做逐 bar 设卡会把历史全滤空，故日线 OHLC 按「当日收盘已知」处理
- 策略接口：

```python
class Strategy(Protocol):
    name: str
    def on_bar(self, ctx: BarContext) -> list[Signal]: ...
```

- 内置策略 ×2：
  - `ma_cross` 双均线：MA5/MA20 金叉全仓买入、死叉全仓卖出
  - `event_driven` 事件驱动：利多事件（`direction_norm == 'bullish'` 且 `factor_scores.score >= 50`）触发买入，持有 5 个交易日卖出。信号过滤两种模式——**收盘时刻级**口径：PIT 模式按 `available_at <= 当日 15:00（Asia/Shanghai）` 过滤，非 PIT 模式按 `event_time <= 当日 15:00` 过滤（模拟穿越未来）；两者唯一差别是拿哪个时间戳设卡

**验收**：两策略均可跑通；手续费/滑点开关对结果有可见影响；PIT 与非 PIT 结果存在差异。

## 6. T5 分析输出

- 指标：总收益 / 年化 / 最大回撤 / 夏普 / 胜率 / 交易次数；基准 = 同标的买入持有
- 指标口径：年化按 252 交易日折算；夏普 rf=0，用日净值收益的样本标准差（ddof=1）年化，样本不足或标准差为 0 时为 `null`；最大回撤取**正值**幅度；胜率与交易次数只统计**已平仓**交易
- 基准口径：首根 bar 收盘价**份额化**全额买入（不受 100 股整手限制，避免整手取整造成的现金拖累压低基准），按同一 `CostModel` 扣一次买入成本，持有到期末按收盘价估值（不卖出故不计印花税）；成本全关时退化为纯价格曲线
- 输出结构：

```json
{
  "meta": {"symbol": "", "strategy": "", "mode": "", "start": "", "end": "", "bars": 0,
           "initial_cash": 0, "adjust": "", "costs": "", "cutoff_field": "", "warnings": []},
  "metrics": {"total_return": 0, "annual_return": 0, "max_drawdown": 0, "sharpe": 0,
              "win_rate": 0, "trade_count": 0, "final_equity": 0,
              "benchmark_return": 0, "excess_return": 0},
  "equity_curve": [{"date": "", "equity": 0, "benchmark": 0}],
  "trades": [{"entry_date": "", "exit_date": "", "pnl": 0, "reason": "",
              "entry_reason": "", "return_pct": 0, "hold_bars": 0, "qty": 0}],
  "open_position": null,
  "pit_comparison": {
    "pit_metrics": {}, "non_pit_metrics": {},
    "delta": {"final_equity_abs": 0, "final_equity_pct": 0, "total_return_pp": 0,
              "annual_return_pp": 0, "max_drawdown_pp": 0, "sharpe_abs": 0,
              "win_rate_pp": 0, "trade_count": 0},
    "entry_dates": {"pit": [], "non_pit": []}
  }
}
```

- `trades.reason` 是**出场**原因（入场原因另列 `entry_reason`）
- `pit_comparison` 为 `null` 表示本次未做对比：未请求，或该策略不消费事件（`ma_cross` 不读事件语料，两模式必然同结果，不做无意义的二次回测）。`delta` 的核心量化值是 `final_equity_pct`（期末权益**差异**比例：非 PIT 相对 PIT，分母恒为正，不会除零，**方向不预设、可正可负**）；`entry_dates` 给出两模式入场日序列，是差异的最直接证据
- `open_position` 非空时给出期末持仓的浮动盈亏（`unrealized_pnl` / `unrealized_return`）。它不进 `trades`，故不影响胜率——报告必须单列，否则「胜率」会失真
- 样本量提示：`bars < 120` 时在 `meta.warnings` 标注。本期事件窗口仅约 55 个交易日，年化与夏普按 252 折算会放大噪声约 √(252/55)≈2.1 倍，报告与 UI 需如实标注

**验收**：对比报告能量化两口径差异（方向不预设，如实呈现）；指标与手工计算样例一致（pytest 硬编码期望值，不引第三方快照库）。

## 7. T6 极简前端

- Next.js 15 + TS + Tailwind + shadcn/ui，包管理 pnpm
- 端口：前端 **3001**（3000 已被 Langfuse 自托管 UI 占用）；后端 8000
- 跨域：后端挂 `CORSMiddleware`，允许来源由 `Settings.cors_origins` 配置（默认 `http://127.0.0.1:3001` 与 `http://localhost:3001`）
- 前置：design brief 评审通过后才实现页面（视觉规范见 PRD §5）
- 页面 ×2：
  - `/` 对话页：消息列表、SSE 流式渲染（含停止生成）、工具调用步骤可视化、会话列表（含删除，二次确认）、历史会话回看（含工具步骤）
  - `/backtest` 回测页：参数表单（策略/标的/区间/成本开关/模式）、指标卡、净值曲线（ECharts）、K 线（TradingView Lightweight Charts，标注买卖点）、交易明细表、PIT 对比表
    - lightweight-charts **v5 的 markers 是独立图元**：`chart.addSeries(CandlestickSeries, …)` 建序列后，用 `createSeriesMarkers(series, [])` 取**常驻句柄**再 `handle.setMarkers(...)`；重复调用工厂会叠加图元而非替换。**marker 的 `time` 必须与某根 bar 的 `time` 精确相等且按时间升序，否则静默丢弃**——「买卖点与明细一致」这条验收就靠它成立，映射逻辑须是**可单测的纯函数**
    - `meta.warnings`（`bars < 120` 的样本量提示：年化/夏普按 252 折算，本期事件窗仅约 55 个交易日，噪声放大 √(252/55)≈2.1 倍）必须在 UI 常驻展示，不能只留在 JSON 里
    - 第 6 块**事件表**：该标的在回测窗口（`meta.start` → `meta.end`）内的全部事件，`event_time` 与 `available_at` **并列成列**（两者之差即 PIT 语义最直观的展示位），行尾「来源」单元格显示 `original_source`，展开见 `source` 与 `content_hash` 前 12 位——PRD §5「来源标注必须可见」在回测页的落点
    - 主指标区的口径标签**必须以响应体为准**：`pit_mode=both` 时 `meta.mode` 恒为 `"pit"`（主指标跑的就是 PIT），不得回显表单值；`pit_comparison` 为 `null` 时（策略不消费事件语料）须给「两模式必然同结果，不做无意义的二次回测」的解释，不得留空表
    - K 线的取数窗口与复权口径一律取自响应：`meta.start` / `meta.end` + `adjust=qfq`（缺省区间由 `resolve_window` 代决策，用表单值或全量取数会让 K 线范围与净值曲线不一致）
- **图表交互口径**（T6d）：
  - 净值曲线与 K 线**均可缩放平移**，且**两图各自独立**——缩放期间不再保证「同一交易日在两图水平位置一致」，回到全览即恢复
  - **竖直滚轮一律归还页面**，不改变图表；横向滚轮在 K 线上平移
  - 缩放方式：两图均支持**捏合**（macOS 触控板捏合在浏览器里即 `ctrl + wheel`）与**拖拽平移**；净值曲线另有常驻的底部 slider 作为可发现的缩放控件，K 线另有双击时间轴复位（库默认行为）
  - 缩放下限：可视跨度不少于 `MIN_VISIBLE_BARS`（8 根）——再少既无意义，也算不出蜡烛宽度
  - **重置入口**：每图各有一个「重置缩放」，仅在该图已缩放时出现在分节标题右侧；不写「捏合可缩放」一类文字提示（slider 是控件，不是提示）
  - 缩放数学抽为纯函数（`lib/chart-gesture.ts`）并单测；触控板手势本身不做自动化测试，由人工走查确认
- API 契约：
  - `POST /api/v1/chat`（SSE，见 §4）
    - 浏览器 `EventSource` 只支持 GET，前端须 `fetch` + `ReadableStream` 手解帧（按 `\n\n` 切帧，忽略 `: keepalive` 注释帧，不得假设「一个网络分片 = 一个事件」）
  - `POST /api/v1/backtest`，请求 `{strategy, symbol, start?, end?, costs?, pit_mode?, params?}`，同步返回 §6 输出结构
    - **P2-M1c 起**：响应外套信封 `{run_id, report}`（`report` 即 §6 结构，本身未动），且端点纳入鉴权（未登录 401）。本行其余口径不变，详见 P2 SPEC `P2-Mn.md` §2 M1c
    - **P2-M4c 起**：请求体加可选 `strategy_id`（**additive**，信封与报告结构不动）——`strategy="user"` 时必带它并改走沙箱子进程执行，缺一或与非 user 策略同给即 422；报告 `meta` 自 M4a 起增 `strategy_kind` / `strategy_name` 两个标识键（详见 P2 SPEC `P2-Mn.md` §5 M4）
    - `costs` = `{fees: bool = true, slippage: bool = true, slippage_bps: number = 5.0}`；`fees=false` 关佣金与印花税，`slippage=false` 关滑点，两者皆 false 等价 `CostModel.disabled()`
    - `pit_mode` ∈ `pit` / `non_pit` / `both`（默认 `pit`）；`both` 时返回的 `pit_comparison` 非空。只有 `event_driven` 消费事件语料，`ma_cross` 传 `both` 时 `pit_comparison` 仍为 `null`（两模式必然同结果，不做无意义的二次回测）
    - `start` / `end` 缺省：`end` = 该标的最后一根 bar；`start` = `event_driven` 取该标的事件窗口起点、其余策略取第一根 bar（与 `scripts/run_report.py` 同口径，两处必须共用同一段解析逻辑）
    - `params` 为策略参数（`ma_cross`: `fast` / `slow`；`event_driven`: `min_score` / `hold_days`）；**键必须落在该策略的已知字段内，未知键返回 422**——`from_params` 会静默忽略拼错的键，API 层不能跟着沉默
    - 执行：`build_report` 是同步 CPU + DuckDB IO，须卸载到线程（`asyncio.to_thread`），不得阻塞事件循环
    - 错误映射：`DataNotReady` → 503（样例数据未落盘）；标的不存在或区间内无 bar → 404；参数非法 → 422；其余 `BacktestError` → 400
  - `GET /api/v1/market/{symbol}/bars?start&end&adjust=qfq` → `{symbol, adjust, count, bars: [{time, open, high, low, close, volume, is_suspended}]}`
    - `time` 用 `YYYY-MM-DD`（Lightweight Charts 的 business day 口径），与回测取数同复权口径（默认 `qfq`）
    - 缺省区间 = 全部可得数据；`symbol` 须为 6 位数字，否则 422；无数据 → 404
  - `GET /api/v1/events?symbol&start&end` → 裁剪后的字段列表，**必须含来源三元组** `source` / `original_source` / `content_hash`（PRD §5 要求来源标注在 UI 可见），并同时给出 `event_time` 与 `available_at` 两列——两者并列是 PIT 语义最直观的展示位
    - **不做 PIT 过滤**：与 `duckdb_client.events()` 一致，按可得时间设卡是消费方（回测 / 对话工具）的职责
    - `factor_scores` 落盘是双重编码的 JSON 文本，出接口前解析出 `score` 数值，不把转义字符串丢给前端

**验收**：浏览器内完成一次对话与一次回测查看，无手动改代码；K 线买卖点与交易明细一致。

## 8. T7 示例策略教程

- `docs/tutorials/ma_cross.md`、`docs/tutorials/event_driven.md`：策略逻辑、参数含义、结果解读

**验收**：照文档可复现回测结果。

## 9. 测试策略

- 单元：引擎撮合/成本/PIT 过滤（T4/T5）、指标计算（T5）、DuckDB 查询层（T2）、SSE 帧编码与事件映射、工具白名单过滤（T3）
- 集成：chat 端点 SSE、backtest / market / events 端点（离线的端点用例同样写真实 Parquet，不 mock 查询层）
- 前端：Vitest 只测纯函数（SSE 帧解析、`trades → markers` 的 time 对齐与升序、对话状态机 reducer 与历史消息映射、指标格式化与表单↔请求映射），不引组件测试框架
- T3 的离线流式测试需自备假模型：现成的 `FakeMessagesListChatModel` 没实现 `bind_tools`（`create_agent` 运行时会调用），`GenericFakeChatModel` 不产出 `tool_calls`——两者都不足以单独驱动「先调工具、再逐 token 作答」的两轮
- Langfuse trace 断言：每次集成测试产生 trace

## 10. 变更记录

> 按版本升序；次版本为整数计数器（`v0.10` 晚于 `v0.9`）。下表 `§` 引用按当前编号（§2–§8 = T1–T7）。

| 版本 | 日期 | 关联 | 变更 |
|---|---|---|---|
| v0.1 | 2026-10-04 | T1–T7 | 初版：技术规格 |
| v0.2 | 2026-10-05 | T1 | compose 默认三服务；postgres 宿主端口 5433；Langfuse 自托管改挂 `observability` profile（v4 依赖 ClickHouse + 对象存储） |
| v0.3 | 2026-10-05 | T2 | `cn-daily` 无按标的维度 → 整市场年度分片 + 本地过滤；事件走 MCP（在线窗口上限约 3 个月）；`source_verified` 字段不存在 → 改真实溯源字段组；`direction` 中英混用 → 增派生列 `direction_norm` |
| v0.4 | 2026-10-05 | T4 | PIT 口径定为**收盘时刻级**（当天 15:00）；增设 `types.py`（避免策略↔引擎循环依赖）与 `events.py`（PIT 闸门单列）；A 股撮合规则（整手 + 佣金最低 5 元）；费用与滑点拆成独立开关；明确 **PIT 只作用于事件语料** |
| v0.5 | 2026-10-05 | T5 | 基准改**份额化买入 + 扣一次成本**；增设 `report.py`；明确指标口径（252 日年化 / rf=0 / 回撤取正值 / 胜率只算已平仓）；补 `meta` 与 `open_position`；`bars < 120` 提示样本量 |
| v0.6 | 2026-10-06 | T3 | 补会话列表端点与 `thread_id` 缺省规则（服务端生成 + 双通道回传）；SSE 事件补字段定义与保活帧；小石工具改**白名单**暴露；记录三条静默失败陷阱；写明本轮不做的四项 |
| v0.7 | 2026-10-06 | T6 | 钉死 §7 五处留白（`costs` 结构 / `start`·`end` 缺省 / 未知键 422 / bars·events 响应结构 / CORS 与 **3001** 端口）；写入 markers 图元与「`time` 必须精确匹配且升序」踩坑预警；样本量 `warnings` 要求进 UI |
| v0.8 | 2026-10-06 | T6b | 新增会话历史端点（按回合合并、与实时 SSE 同构）与会话删除端点（CORS 放行 `DELETE`）；§7 对话页补停止生成、历史回看与会话删除 |
| v0.9 | 2026-10-06 | T6c | §7 回测页补第 6 块**事件表**（`event_time` / `available_at` 并列 + 来源三元组）；补「口径标签以响应体为准」与「K 线窗口与复权口径取自响应」；§6 措辞校正：`final_equity_pct` 由「虚高比例」改「**差异比例（可正可负）**」 |
| v0.10 | 2026-10-06 | 收尾 | §6 与 §7 样本量订正 约 54 / √(252/54)≈2.2 倍 → **约 55 / √(252/55)≈2.1 倍**（实际由 bars 数动态计算）；§2 T1 依赖清单补齐 6 项实际依赖 |
| v0.11 | 2026-10-06 | 收尾 | 正文章节改为**按功能 ID 排序**（§2–§8 对应 T1–T7，此前 §4 曾是 T4、§6 曾是 T3）；§1 仓库结构补齐实际模块 |
| v0.12 | 2026-10-06 | T6d | §7 新增**图表交互口径**：两图均可缩放平移且**各自独立**；**竖直滚轮一律归还页面**；缩放下限 `MIN_VISIBLE_BARS` = 8 根；每图一个「重置缩放」 |
| v0.13 | 2026-10-07 | P2-M1c | §7 的 `POST /api/v1/backtest` 契约加注：响应自 P2-M1c 起外套 `{run_id, report}` 并纳入鉴权（§6 报告结构未动）——回测落库后归属成为必要信息 |
| v0.14 | 2026-10-08 | P2-M4c | §7 的 `POST /api/v1/backtest` 契约加注：请求体自 P2-M4c 起加可选 `strategy_id`（**additive**），`strategy="user"` 时必带并改走沙箱子进程；信封与 §6 报告结构不动。用户策略参数由源码里的 `PARAMS` schema 校验（非注册表 `from_params`），详见 P2 SPEC §5 M4c |
| v0.15 | 2026-10-09 | chat 修复 | §4 的 SSE 契约订正一处：**客户端断连后图继续跑完**（原「断连即取消」）——取消点落在「模型已写出 tool_calls、工具未执行」之间会在 checkpoint 留下悬空调用，令**该会话此后每一轮都被模型 400 拒掉**（用户报的「刷新之后这个会话就死了」，已实测复现）。另增「起新一轮前体检历史、给悬空调用补错误结果」的修复路径（治进程被杀留下的存量会话）。前端同轮加「重新生成」入口（末尾停在用户消息时出现） |
