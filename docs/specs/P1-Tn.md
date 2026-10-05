# QuantSage SPEC · P1-Tn（跑通 demo）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，已通过）→ 本文（技术规格）→ 代码
>
> 版本 v0.2 ｜ 2026-10-05 ｜ 状态：已通过
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
│   │   ├── backtest/               # engine.py / portfolio.py / broker.py / costs.py / metrics.py / strategies/
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

- 标的：贵州茅台、宁德时代、招商银行（代码格式以数据源为准）；日线 Parquet 下载至 `data/bars/`
- 事件语料：小批量拉取至 `data/events/`，保留字段：`event_id` / `title` / `summary` / `event_time` / `available_at` / `direction` / `confidence` / `industries` / `stocks` / `factor_scores` / `source_verified`
- DuckDB 查询层：对 `data/bars/*.parquet` 与 `data/events/*.parquet` 建只读视图

**验收**：SQL 可查任意标的日线；事件 `available_at` 无空值；单标的 1 年日线查询 < 1s。

## 4. T4 最小回测引擎

- 事件循环：按 bar 时间排序驱动；信号在 bar 收盘生成，成交在下一 bar 开盘（防日内前视）
- 撮合：市价单。成本模型：佣金双边万 2.5 + 印花税卖出 0.05% + 滑点（bps，可开关）
- 策略接口：

```python
class Strategy(Protocol):
    name: str
    def on_bar(self, ctx: BarContext) -> list[Signal]: ...
```

- 内置策略 ×2：
  - `ma_cross` 双均线：MA5/MA20 金叉全仓买入、死叉全仓卖出
  - `event_driven` 事件驱动：利多事件（`direction` + `factor_scores.score` 阈值）触发买入，持有 N 天卖出。信号过滤两种模式——PIT 模式按 `available_at <= 当日` 过滤，非 PIT 模式按 `event_time` 过滤（模拟穿越未来）

**验收**：两策略均可跑通；手续费/滑点开关对结果有可见影响；PIT 与非 PIT 结果存在差异。

## 5. T5 分析输出

- 指标：总收益 / 年化 / 最大回撤 / 夏普 / 胜率 / 交易次数；基准 = 同标的买入持有
- 输出结构：

```json
{
  "metrics": {},
  "equity_curve": [{"date": "", "equity": 0, "benchmark": 0}],
  "trades": [{"entry_date": "", "exit_date": "", "pnl": 0, "reason": ""}],
  "pit_comparison": {"pit_metrics": {}, "non_pit_metrics": {}, "delta": {}}
}
```

**验收**：对比报告能量化虚高幅度；指标与手工计算样例一致（pytest 快照）。

## 6. T3 Agent 对话

- 图：START → agent（LLM + tools）→ END；Postgres checkpointer；模型 `deepseek-flash`（LiteLLM SDK 统一调用）
- 工具：`query_market_bars`（DuckDB）、`query_events`（小石 MCP，经 langchain-mcp-adapters 注册，按 tools/list 实际结果绑定）
- 流式：SSE 事件类型 `token` / `tool_call` / `tool_result` / `done` / `error`
- API：`POST /api/v1/chat`，请求 `{thread_id, message}`；Langfuse 记录完整 trace

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

- 单元：引擎撮合/成本/PIT 过滤（T4/T5）、指标计算（T5）、DuckDB 查询层（T2）
- 集成：chat 端点 SSE（mock 模型）、backtest 端点
- Langfuse trace 断言：每次集成测试产生 trace

## 10. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-04 | v0.1 | 初版：T1–T7 技术规格 |
| 2026-10-05 | v0.2 | T1 落地澄清：compose 默认三服务、postgres 宿主端口 5433；langfuse 自托管改挂 `observability` profile（v4 依赖 ClickHouse + 对象存储） |
