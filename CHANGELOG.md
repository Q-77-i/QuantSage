# Changelog

> 按里程碑记录交付内容，版本号 = git tag（`m-p1`、`m-p2`…）。格式：新增 / 调整 / 修复。

## [未发布]

### 新增

- 根 `README.md`：项目门面（定位与护城河、架构图、界面截图、快速开始、API、已知边界）
- `docs/images/`：对话页 / 回测页 / PIT 对比区三张截图，由 `frontend/scripts/capture-screenshots.mjs`（Playwright，`pnpm screenshots`）驱动真实交互生成，可重跑

### 调整

- P1 收尾的全链同步：PRD 升 v0.5（状态行改「已通过」；§6 待确认①证伪、②③转 M2）；SPEC 升 v0.10（样本量订正 55 / 2.1 倍、T1 依赖清单补齐 6 项）；CLAUDE.md 技术栈表增「落地状态」列、数据源通道措辞对齐 SPEC v0.3
- SPEC 升 v0.11：正文章节改为**按功能 ID 排序**（§2–§8 对应 T1–T7，此前按实现顺序、§4 曾是 T4、§6 曾是 T3）；§1 仓库结构补齐实际模块；变更记录由三列表格改为分版本条目；新增目录。同步修正正文与 `ROADMAP.md` 的交叉引用
- 后端注释与 docstring 的前视偏差措辞中性化（7 处，与 SPEC v0.9 的既有校正对齐，无行为变更）
- `frontend/README.md` 脚手架原文改为指向根 README 的短说明（原文档端口指引为 3000，实际为 3001）

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
