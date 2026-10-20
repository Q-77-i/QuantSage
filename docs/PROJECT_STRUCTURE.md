# 项目结构

> **本文只管一件事：目录与模块职责。** 端点、参数与表结构见 [SPEC](specs/)，架构理由与运行时拓扑见[企业级架构设计](enterprise/architecture.md)，功能验收见 [PRD](PRD.md)。
>
> 这棵树是**导航辅助**，漂移率全仓库最高——放在门面之外，读者按需查。与本文件同步的责任方是**结构与路由的改动**，不是每次功能交付。

```
QuantSage/
├── backend/                      # FastAPI + LangGraph
│   ├── app/
│   │   ├── agent/                # 快路径：ReAct 图、工具白名单、prompt、历史回合合并、请求上下文（5a）
│   │   │   └── deep/             # 深路径：路由 / 五角色取证 / 归并 / 辩论 / 综合 / 风控闸门 / 预算
│   │   ├── api/                  # 17 个挂路由的模块：auth · chat · backtest · watchlist · market · events
│   │   │                         #   strategies · optimize · factor · paper · reports · research · etl · tasks
│   │   │                         #   · admin（E2b）· data（E3 体检/快照）· capabilities（5b 静态能力卡）
│   │   │                         #   （idempotency / limits / sse 是共用件，不挂路由）
│   │   ├── backtest/             # 引擎、PIT 闸门、撮合、成本、指标、报告、A 股规则、批量、过拟合
│   │   ├── strategy/             # 用户策略沙箱、前视静态检查（R0–R5）、模板库、版本（5d）
│   │   ├── factor/               # 因子面板与分析（独立于回测引擎的数据管道）
│   │   ├── paper/                # 模拟盘：账户 / 撮合 / 回放 / 决策六态 / 风控四闸门与审计（E5）
│   │   │                         #   + Agent 下单提案（5a：只创建待审批决策）
│   │   ├── report/               # 研报容器：组装 / claim 闸门 / Markdown / PDF / 深研报 / 深研报评测（5c）
│   │   ├── memory/               # 决策记忆：到期结算、教训、跨账户聚合（LangGraph Store）
│   │   ├── rag/                  # 嵌入 / 检索 / PIT 硬过滤 / 重排 / 离线评测 / 零命中一轮纠错（5b）
│   │   ├── etl/                  # 调度器与日增量通道（两条：事件语料、行情按需刷新；只在后台主实例启动）；归档 / 物化 / 落库
│   │   ├── tasks/                # 异步任务：执行体（backtest/grid/batch）+ 单 worker 取件循环 + 后台主实例锁
│   │   │                         #   + 进度总线与事件流（断线重连）、进程内协作式中止
│   │   ├── data/                 # DuckDB 客户端、交易日历、行情刷新、小石 CLI/MCP 装配
│   │   │                         #   + 数据体检/落档、快照/回滚、热点缓存（E3）
│   │   ├── mcp/                  # 对外 MCP Server（E8b）：把 PIT 语料检索亮给外部 AI 客户端
│   │   └── core/                 # 配置、checkpointer、LLM/Langfuse、日志脱敏与 JSON 日志
│   │                             #   + 鉴权/会话/配额/安全头/注入边界、metrics/observability（E7e）
│   ├── scripts/                  # 下载、建库、回测与走查取证脚本（含 check_docs.py、build_deep_eval.py）
│   └── tests/                    # 离线 + tests/integration
├── frontend/                     # Next.js 15（App Router）
│   ├── app/                      # 18 条路由：
│   │   │                         #   / 研究首页 · /stocks 标的浏览 · /stocks/[symbol] 个股页
│   │   │                         #   /chat 对话 · /backtest 回测 · /strategies 工作台
│   │   │                         #   /optimize 网格 · /factor 因子 · /paper 模拟盘
│   │   │                         #   /research 深研入口 · /research/[id] 研报 · /research/runs/[id] 运行页
│   │   │                         #   /space 个人空间 · /admin 管理视图（E2b）· /login · /register
│   │   │                         #   /r/[token] 公开分享页 · /print/[id] 打印页
│   │   └── (app)/                # 受保护路由组（登录守卫与页头都挂在这一层）
│   ├── components/               # admin · agent-run · auth · backtest · chat · events · factor · home
│   │                             #   optimize · paper · research · space · stocks · strategies · tasks · ui
│   └── lib/                      # SSE（POST/GET 两条）、图表主题与手势、对话状态机、分页、格式化
│                                 #   + 任务轮询与网格事件流的纯函数视图模型
│                                 #   （纯函数 + 单测；组件本身不做单测，靠走查脚本）
├── scripts/                      # 跨端运维脚本：dev.sh 一键起停（基础设施 + 后端 + 前端）
├── docs/
│   ├── PRD.md                    # 需求与验收
│   ├── PROJECT_STRUCTURE.md      # 本文（结构导航）
│   ├── specs/                    # 技术规格（逐阶段滚动：P1-Tn.md · P2-Mn.md · P3-En.md）
│   ├── tutorials/                # 使用教程与示例策略教程
│   └── images/                   # README 用截图（由 frontend/scripts/capture-*.mjs 生成）
├── .mcp.json.example             # MCP 客户端配置模板（本地 .mcp.json 是绝对路径，不入库）
├── docker/postgres/init/
└── docker-compose.yml
```

`data/`（行情分片与事件语料）与 `.tools/`（小石 CLI/MCP）**不入 Git**——量大且可重新下载，
取数命令见 README「数据来源与复现边界」。
