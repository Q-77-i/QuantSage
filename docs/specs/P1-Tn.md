# QuantSage SPEC · P1-Tn（跑通 demo）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，已通过）→ 本文（技术规格）→ 代码
>
> 版本 v0.6 ｜ 2026-10-06 ｜ 状态：已通过
>
> 本 SPEC 覆盖 PRD §2.1 的 T1–T7。实现顺序：T1 → T2 → T4 → T5 → T3 → T6 → T7。

## 1. 仓库结构

```
QuantSage/
├── backend/
│   ├── pyproject.toml              # uv 管理，Python 3.12
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口与路由挂载
│   │   ├── api/                    # chat.py / backtest.py / market.py
│   │   ├── agent/                  # graph.py / tools.py / prompts.py
│   │   ├── backtest/               # types.py / events.py / engine.py / portfolio.py / broker.py / costs.py / metrics.py / report.py / strategies/
│   │   ├── data/                   # duckdb_client.py / xiaoshi.py（CLI 与 MCP 封装）
│   │   └── core/                   # config.py / llm.py / langfuse.py
│   ├── scripts/                    # download_bars.py / download_events.py
│   └── tests/
├── frontend/
│   ├── app/                        # page.tsx（对话页）/ backtest/page.tsx（回测页）
│   ├── components/
│   └── lib/                        # api.ts（SSE 客户端）
├── data/                           # 行情 Parquet / 事件语料（gitignore）
├── docker-compose.yml
└── .env
```

## 2. T1 地基

- Python 3.12 + uv。依赖：fastapi / uvicorn[standard] / pydantic / pydantic-settings / langgraph / langgraph-checkpoint-postgres / langchain / langchain-core / langchain-mcp-adapters / litellm / duckdb / pyarrow / langfuse / pytest / pytest-asyncio
- Docker Compose 服务：postgres:18 / redis / qdrant（默认启动；postgres 宿主端口 5433，本机 5432 已被占用）
- Langfuse 自托管全套（web/worker + clickhouse + minio + 独立 redis）挂 `profiles: [observability]`，默认不启动，T3 接入 trace 时开启；v4 起必须 ClickHouse + 对象存储，无法只在 postgres 上跑
- 小石 CLI 装入独立 venv（不动系统环境）；MCP 以本地 stdio 接入；密钥只读 `.env`
- LangGraph checkpointer 使用 Postgres

**验收**：`docker compose up` 全绿；MCP 完成 initialize + tools/list + 一次有界认证查询；pytest 空跑通过。

## 3. T2 样例数据

- 标的：贵州茅台 `600519`、宁德时代 `300750`、招商银行 `600036`（symbol 格式以落盘实测为准）
- 日线：`cn-daily` 年度分片（year=2025/2026 × adjust=raw/qfq，共 4 片）下载后本地过滤 3 标的至 `data/bars/{symbol}.{adjust}.parquet`，保留分片全字段（含 `available_at`、`adj_factor`）
- 事件语料：经 MCP `get_event_timeline` 按标的拉取至 `data/events/{symbol}.parquet`；在线 PIT 窗口上限约 3 个月，本期取 2026-07-05 → 2026-09-30
  - 保留字段：`symbol` / `event_id` / `event_type` / `title` / `summary` / `event_time` / `available_at` / `observed_at` / `direction` / `direction_norm` / `confidence` / `importance_score` / `factor_value` / `factor_scores` / `industries` / `stocks` / `source` / `original_source` / `content_hash` / `quality_status` / `source_time_quality`
  - `direction` 原值中英混用，派生 `direction_norm` 归一到 `bullish` / `bearish` / `neutral`；`高管人事` 这类非情绪标签归 NULL（无方向语义），不臆造情感极性
  - `industries` / `stocks` 落原生嵌套类型，`factor_scores` 键集随事件类型变化故存 JSON 文本
  - 溯源标注用 `source` + `original_source` + `content_hash` + `quality_status`；数据源无 `source_verified` 字段
- 原始分片留档 `data/raw/`；下载清单（manifest 版本 / 时间 / sha256 / 行数 / 区间）落 `data/_meta/*.json`
- DuckDB 查询层：对 `data/bars/*.parquet` 与 `data/events/*.parquet` 建只读视图

**验收**：SQL 可查任意标的日线；事件 `available_at` 无空值且 `available_at >= event_time`；单标的 1 年日线查询 < 1s。

## 4. T4 最小回测引擎

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

## 5. T5 分析输出

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
- `pit_comparison` 为 `null` 表示本次未做对比：未请求，或该策略不消费事件（`ma_cross` 不读事件语料，两模式必然同结果，不做无意义的二次回测）。`delta` 的核心量化值是 `final_equity_pct`（期末权益虚高比例，分母恒为正，不会除零）；`entry_dates` 给出两模式入场日序列，是差异的最直接证据
- `open_position` 非空时给出期末持仓的浮动盈亏（`unrealized_pnl` / `unrealized_return`）。它不进 `trades`，故不影响胜率——报告必须单列，否则「胜率」会失真
- 样本量提示：`bars < 120` 时在 `meta.warnings` 标注。本期事件窗口仅约 54 个交易日，年化与夏普按 252 折算会放大噪声约 √(252/54)≈2.2 倍，报告与 UI 需如实标注

**验收**：对比报告能量化虚高幅度；指标与手工计算样例一致（pytest 硬编码期望值，不引第三方快照库）。

## 6. T3 Agent 对话

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
- API：`POST /api/v1/chat`，请求 `{thread_id?, message}`；`thread_id` 缺省时由服务端生成 UUID，经响应头 `X-Thread-Id` 与 `done` 事件**双通道**回传（客户端中途断连也能拿到会话号）
- API：`GET /api/v1/chat/threads` → 会话列表（T6 对话页需要；补齐 SPEC 原有的契约缺口）
  - 取数：只读 SQL 从 `checkpoints` 表按 `thread_id` 聚合（限 `checkpoint_ns = ''`）；消息正文落在 `checkpoint_blobs`（msgpack），**不能手写 JSONB 路径取**，须经 checkpointer 官方读接口反序列化；标题由服务端从首条人类消息派生，不接受客户端传入
  - 暂无归属校验：单用户 demo 可接受，**P2 引入登录后必须按用户过滤**（记为 M1 阻塞项）
- Langfuse 记录完整 trace：须**显式构造客户端**后再取 `CallbackHandler`——`.env` 只进 pydantic Settings、不进 `os.environ`，零参构造会静默降级为 NoOpTracer（表现为「无报错但没有任何 trace」）
- 本轮不做（记录待议）：同 thread 并发写入的串行化、请求幂等去重、历史消息裁剪与上下文超限策略

**验收**：问「贵州茅台最近行情」Agent 自主调工具并流式作答；Langfuse 可查看完整 trace（含工具调用与成本）。

## 7. T6 极简前端

- Next.js 15 + TS + Tailwind + shadcn/ui
- 前置：design brief 评审通过后才实现页面（视觉规范见 PRD §5）
- 页面 ×2：
  - `/` 对话页：消息列表、SSE 流式渲染、工具调用步骤可视化、会话列表
  - `/backtest` 回测页：参数表单（策略/标的/区间/成本开关/模式）、指标卡、净值曲线（ECharts）、K 线（TradingView Lightweight Charts，标注买卖点）、交易明细表、PIT 对比表
- API 契约：
  - `POST /api/v1/chat`（SSE，见 §6）
  - `POST /api/v1/backtest`，请求 `{strategy, symbol, start, end, costs, pit_mode}`，同步返回 §5 输出结构
  - `GET /api/v1/market/{symbol}/bars?start&end`
  - `GET /api/v1/events?symbol&start&end`

**验收**：浏览器内完成一次对话与一次回测查看，无手动改代码；K 线买卖点与交易明细一致。

## 8. T7 示例策略教程

- `docs/tutorials/ma_cross.md`、`docs/tutorials/event_driven.md`：策略逻辑、参数含义、结果解读

**验收**：照文档可复现回测结果。

## 9. 测试策略

- 单元：引擎撮合/成本/PIT 过滤（T4/T5）、指标计算（T5）、DuckDB 查询层（T2）、SSE 帧编码与事件映射、工具白名单过滤（T3）
- 集成：chat 端点 SSE、backtest 端点
- T3 的离线流式测试需自备假模型：现成的 `FakeMessagesListChatModel` 没实现 `bind_tools`（`create_agent` 运行时会调用），`GenericFakeChatModel` 不产出 `tool_calls`——两者都不足以单独驱动「先调工具、再逐 token 作答」的两轮
- Langfuse trace 断言：每次集成测试产生 trace

## 10. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-04 | v0.1 | 初版：T1–T7 技术规格 |
| 2026-10-05 | v0.2 | T1 落地澄清：compose 默认三服务、postgres 宿主端口 5433；langfuse 自托管改挂 `observability` profile（v4 依赖 ClickHouse + 对象存储） |
| 2026-10-05 | v0.3 | T2 落地澄清：cn-daily 无按标的维度，改整市场年度分片 + 本地过滤；事件走 MCP，在线 PIT 窗口上限约 3 个月；`source_verified` 字段不存在，改用真实溯源字段组；`direction` 中英混用，增派生列 `direction_norm` |
| 2026-10-06 | v0.6 | T3 落地澄清：新增 `GET /api/v1/chat/threads` 会话列表与 `thread_id` 缺省规则（服务端生成 + 响应头/`done` 双通道回传）；SSE 事件补字段定义、`tool_result` 只发截断预览、补保活帧；小石工具改**白名单**暴露（动作型工具有昂贵副作用）；记录三条静默失败陷阱（litellm 裸模型名被拒、`.env` 不进 `os.environ` 致 trace 静默丢失、checkpointer 在 compile 期固化），并写明本轮不做的四项（并发串行化 / 幂等 / 历史裁剪 / 上下文超限） |
| 2026-10-05 | v0.5 | T5 口径确认：基准改**份额化买入 + 扣一次成本**（整手取整会造成现金拖累、系统性压低基准）；`backtest/` 增设 `report.py`（metrics.py 保持纯函数）；明确指标口径（252 日年化 / rf=0 / 回撤取正值 / 胜率只算已平仓）、`reason` 为出场原因、`pit_comparison` 可为 `null`、`delta` 以 `final_equity_pct` 为核心量化值；补 `meta` 与 `open_position` 两个可读性字段；`bars < 120` 时提示样本量 |
| 2026-10-05 | v0.4 | T4 落地澄清：PIT 口径定为**收盘时刻级**（当天 15:00）；`backtest/` 增设 `types.py`（`BarContext` 独立成层避免策略↔引擎循环依赖）与 `events.py`（PIT 闸门单列，护城河一眼可见）；撮合加 A 股实盘规则（100 股整手 + 佣金最低 5 元），费用与滑点拆成两个独立开关；`event_driven` 默认 `score >= 50`、持有 5 日；明确 **PIT 只作用于事件语料**，bars 不做逐 bar 设卡（`available_at` 为整表快照） |
