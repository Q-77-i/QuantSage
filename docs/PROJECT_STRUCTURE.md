# 项目结构

> 从 README 移出：这棵树是**导航辅助**，漂移率却是全仓库最高的——它挂在门面上时，
> 每加一条路由、一个目录都要回来改，漏一次读者就会照着找不到东西。放在这里，读者按需查。
>
> 与本文件同步的责任方是**结构与路由的改动**，不是每次功能交付。

```
QuantSage/
├── backend/                      # FastAPI + LangGraph
│   ├── app/
│   │   ├── agent/                # 快路径：ReAct 图、工具白名单、prompt、历史回合合并
│   │   │   └── deep/             # 深路径：路由 / 五角色取证 / 归并 / 辩论 / 综合 / 风控闸门 / 预算
│   │   ├── api/                  # 13 个挂路由的模块：auth · chat · backtest · watchlist · market · events
│   │   │                         #   strategies · optimize · factor · paper · reports · research · etl
│   │   │                         #   （sse.py 是帧编码共用件，不挂路由）
│   │   ├── backtest/             # 引擎、PIT 闸门、撮合、成本、指标、报告、A 股规则、批量、过拟合
│   │   ├── strategy/             # 用户策略沙箱、前视静态检查（R0–R5）、模板库
│   │   ├── factor/               # 因子面板与分析（独立于回测引擎的数据管道）
│   │   ├── paper/                # 模拟盘：账户 / 撮合 / 回放 / 决策六态
│   │   ├── report/               # 研报容器：组装 / claim 闸门 / Markdown / PDF / 深研报
│   │   ├── memory/               # 决策记忆：到期结算、教训、跨账户聚合（LangGraph Store）
│   │   ├── rag/                  # 嵌入 / 检索 / PIT 硬过滤 / 重排
│   │   ├── etl/                  # 调度器与日增量通道（两条：事件语料、行情按需刷新）
│   │   ├── data/                 # DuckDB 客户端、交易日历、行情刷新、小石 CLI/MCP 装配
│   │   └── core/                 # 配置、checkpointer、LLM 与 Langfuse 入口、日志脱敏
│   ├── scripts/                  # 下载、建库、回测与走查取证脚本（含 check_docs.py）
│   └── tests/                    # 离线 + tests/integration
├── frontend/                     # Next.js 15（App Router）
│   ├── app/                      # 17 条路由：
│   │   │                         #   / 研究首页 · /stocks 标的浏览 · /stocks/[symbol] 个股页
│   │   │                         #   /chat 对话 · /backtest 回测 · /strategies 工作台
│   │   │                         #   /optimize 网格 · /factor 因子 · /paper 模拟盘
│   │   │                         #   /research 深研入口 · /research/[id] 研报 · /research/runs/[id] 运行页
│   │   │                         #   /space 个人空间 · /login · /register
│   │   │                         #   /r/[token] 公开分享页 · /print/[id] 打印页
│   │   └── (app)/                # 受保护路由组（登录守卫与页头都挂在这一层）
│   ├── components/               # agent-run · auth · backtest · chat · events · factor · home
│   │                             #   optimize · paper · research · space · stocks · strategies · ui
│   └── lib/                      # SSE、图表主题与手势、对话状态机、分页、格式化
│                                 #   （纯函数 + 单测；组件本身不做单测，靠走查脚本）
├── docs/
│   ├── PRD.md                    # 需求与验收
│   ├── specs/                    # 技术规格（逐阶段滚动：P1-Tn.md · P2-Mn.md）
│   ├── tutorials/                # 使用教程与示例策略教程
│   └── images/                   # README 用截图（由 frontend/scripts/capture-*.mjs 生成）
├── docker/postgres/init/
└── docker-compose.yml
```

`data/`（行情分片与事件语料）与 `.tools/`（小石 CLI/MCP）**不入 Git**——量大且可重新下载，
取数命令见 README「数据来源与复现边界」。
