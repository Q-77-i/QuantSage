# QuantSage SPEC · P3-En（企业级思维）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.10）→ 本文（技术规格）→ 代码
>
> 版本 v2.6 ｜ 2026-10-14 ｜ 状态：**规划已评估拍板**（四处裁剪 + E6 砍，PRD v0.10 已同步）；**E1 全节交付完毕**——E1a（任务队列）/ E1c（接缝）/ E1b（网格批量迁入队列）均已落地；**E4a（统一 policy + LLM 侧）已交付**（§5，2026-10-14），E4b（限流/数据源熔断/幂等）与 E4c（演练与告知面）待做；其余节待滚动。正文按功能 ID 滚动填写（同 P2 SPEC 的滚动方式）；每个功能的规格、实测读数与拍板记录随交付追加到 §11 变更记录。
>
> 本 SPEC 覆盖 PRD §2.3 的 E1–E9（**E6 组合已砍**，不设节，留待 P3 之后独立评估）。规划评估底稿见 `docs/private/Pn-n/P3-En/P3-En-规划评估.md`（现状基线核对与逐项评估，本文件只收技术事实）。

## 目录

| 节 | 内容 |
|---|---|
| [§1](#1-规划基线) | 规划基线（P3 起笔时点的现状核对） |
| [§2](#2-e1-架构分层) | E1 架构分层（E1a 任务队列 · E1c 接缝 · [§2.8](#28-e1b网格--批量迁入队列2026-10-14-定稿) E1b 网格批量迁入——**全节已交付**） |
| [§5](#5-e4-可靠性-l0l7) | E4 可靠性 L0–L7（**规格已定稿，待实施**） |
| §3–§9 其余 | E2 / E3 / E5 / E7 / E8 / E9 规格（正文待滚动填写；E6 已砍不设节） |
| [§10](#10-测试策略) | 测试策略（待随各功能滚动补充） |
| [§11](#11-变更记录) | 变更记录 |

## 1. 规划基线

P3 起笔时点（2026-10-13）的现状核对——各 E 节的增量以本表为起点，已交付的不重做、只声明基线：

| PRD 条目 | 事实 |
|---|---|
| E1 API 版本化 | ✅ `/api/v1` 全端点已用（P2 落地）——仅声明基线 |
| E1 异步回测 | 同步执行：`api/backtest.py` 用 `asyncio.to_thread` 卸载线程，HTTP 请求挂到跑完——真实缺口 |
| E2 资源隔离 | ✅ M1 全量按用户归属过滤，越权与不存在同为 404 |
| E2 RBAC / 配额 | 无 role 字段、无配额——增量 |
| E3 数据体检 | M2c 脚本级已有（缺失/异常/`available_at` 倒挂）——升级为定时 + 告警 + 看板 |
| E3 Redis | compose 就位、代码零使用（仅 `config.py` 有 url） |
| E3 数据快照 | M7 报告级快照已有；数据层按日分片（Parquet 直读）——数据快照借分片架构 |
| E4 L1–L3、L5、L6 | LiteLLM 已用（LLM 侧 429/fallback 未做）；RetryPolicy/TimeoutPolicy 未配；双档模型已用（无失败切换链）；M8 节点内 try/except 降级已有；M8 已有步数/轮数/预算硬上限 |
| E4 L0 / L4 / L7 | 均无（限流仅有小石数据源的 429 停止信号，非用户侧） |
| E5 风控 | M6 下单审批闸门、M8 内容风控闸门已有；仓位/止损/集中度/回撤无 |
| E7 CI/CD | 完全无 CI（无 `.github/workflows`）——从零 |
| E7 测试 | 后端 1323 例 + 前端 423 例；覆盖率门槛无；后端无 lint |
| E7 Langfuse | 已全面接入；待补字段规范化（模型版本/prompt hash/工具序列/成本/延迟/用户） |
| E7 评测集 | M3c 已有 200 条中文财经评测集 + NDCG@10 基线 |
| E8 checkpointer | ✅ Postgres checkpointer 已用 |
| E8 对外 MCP | 消费侧 stdio（小石）已有；对外 Server 无——新 |

## 2. E1 架构分层

> 规格底稿：`docs/private/Pn-n/P3-En/P3-E1-规划评估.md`（现状核对与甲/乙/丙三案）。
> **拍板（2026-10-14）：先做 E1a（任务队列 + 单次回测异步）+ E1c（LLM 调用面取用式）；E1b（网格/批量迁入队列）后延**——E1a 是独立可停的止损位，停下不留半成品。

### 2.0 与 PRD 假设的核对订正

| PRD 条目（v0.10） | 2026-10-14 代码核对 | 本 SPEC 的处理 |
|---|---|---|
| E1 API 版本化 `/api/v1` | P2 已全端点落地 | 基线，不重做 |
| E1 接缝——数据源访问 | **已经在了**：`xiaoshi_cli.run()` 是 CLI 唯一入口（超时与限流语义在模块内统一）；`duckdb_client.connect()` 是本地数据入口；14 个模块清一色 `dc.<fn>` **属性访问** ⇒ 打桩天然单点。3 处刻意绕过各有注释写明理由 | **零代码**，只登记（§2.5），不新建转发层 |
| E1 接缝——LLM 网关 | `core/llm.py` 是唯一**构造**入口（`ChatLiteLLM` 全仓仅此一处 import），但 7 个调用方 `from app.core.llm import complete/ask_once/build_chat_model` **直接绑定函数** ⇒ patch `app.core.llm.complete` 打不中调用点 | E1c：取用式改造 |
| E1 异步回测任务队列 | 同步：`asyncio.to_thread(build_report)`，HTTP 挂到跑完 | E1a |

**痛点位置的订正**（影响对外叙述）：单次回测实测 0.3–2s（`build_report` 全期 1,636 bars 0.25s，SPEC §5 M4a；用户策略含解释器启动 1.0s），**异步化它不省时间**；真正的损失在网格/批量那条路径断线即丢（SPEC §6 M5b 原文「落库在 done，流没走完就不留记录」）。E1a 落单次回测是为**统一任务契约与状态可查**——**E1a 对外不得声称解决了长任务丢失**（那是 E1b）。

### 2.1 任务模型

`tasks` 表（DDL 进 `core/db.py::SCHEMA`，幂等建表与索引，与既有九张表同约定）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 提交时生成 |
| `user_id` | BIGINT REFERENCES users ON DELETE CASCADE | 归属；越权与不存在同为 404 |
| `kind` | TEXT | `backtest`（E1a 唯一已注册；E1b 加 `grid` / `batch`） |
| `status` | TEXT | `queued → running → done｜failed`；`queued → cancelled` |
| `request` | JSONB NOT NULL | 用户可见入参 **+ 冻结的执行材料**（见下） |
| `progress` | JSONB | `{done, total}`；E1a 恒 1 格（E1b 的网格逐格推进） |
| `result` | JSONB | 完成时 `{run_id}`——**不复制报告**，重开走既有端点 |
| `error` | JSONB | `{kind, message}`（用户代码的运行期异常另带 `line`）——与 `backtest.batch.error_of` / 沙箱 `kind` 同一套词汇 |
| `created_at` / `started_at` / `finished_at` / `updated_at` | TIMESTAMPTZ | |

索引：`(user_id, created_at DESC)`、`(status)`。

**`request` 冻结执行材料**（与 `backtest_runs.request` 的 `_stored_request` 同风格：请求 + 派生字段）：

- 通用：`symbol` / `strategy` / `strategy_id` / `params` / `costs` / `pit_mode` / `start` / `end` / `adjust`（区间取 `resolve_window` 解析后的值）/ `compare_pit`
- 用户策略额外：`source`（**源码全文**）、`strategy_name`、`uses_events`

源码必须随任务冻结：同步路径里「读源码 → 跑」在同一请求内，异步化后出现「提交后被改源码」的窗口，不冻结就会跑出与提交时不同的策略。

### 2.2 执行路径

```
提交（端点内，同步做完全部闸门）
  归属 404 → 静态检查 error=0 → 参数校验 → resolve_window → 建任务行(queued) → 202
worker（进程内单协程，lifespan 起停）
  取件：UPDATE tasks SET status='running' … WHERE id = (
          SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1) RETURNING *
  执行：按 kind 派发；成功 → 落 backtest_runs + finish_task(result={run_id})
                     失败 → fail_task(error)（**不落 run**，与今天异常路径不落库同语义）
```

**队列实体就是任务表，不另设 `asyncio.Queue`**：`asyncio.Queue` 绑事件循环，而本仓离线用例用模块级 `TestClient`（每请求一个 portal/loop，只有集成用例 `with TestClient` 才跑 lifespan）⇒ 跨 loop 的队列在离线用例里直接坏掉。表即队列还顺带把「重启续跑」变成零代码（worker 起来接着取 `queued` 行）。取件间隔 200ms：单次回测 0.3–2s，这点启动延迟可忽略，换来零跨 loop 状态。

**闸门必须在入队前**（不可退让）：错误若挪进 worker，HTTP 就只剩 202 一种回应，M4c「运行前带行号给话术」（422 + findings）当场失效，既有测试矩阵随之变红。

**单 worker 串行**（PRD 定）。E1a 只有亚秒级任务，排队延迟可忽略；E1b 迁入网格后才有「满格网格挡住单次回测」的代价，届时再拍板（可延：worker 数可配）。

### 2.3 端点契约

| 端点 | 说明 |
|---|---|
| `POST /api/v1/backtest` | **改**：202 + `{task_id, status: "queued"}`，不再内联报告；**能静态判的闸门照旧在提交时判**（401/404/422/503 语义一字不变） |
| `GET /api/v1/tasks?limit=` | 我的任务摘要，倒序 |
| `GET /api/v1/tasks/{task_id}` | `{id, kind, status, progress, result, error, created_at, started_at, finished_at}`；越权与不存在同为 404 |
| `POST /api/v1/tasks/{task_id}/cancel` | `queued` → **条件更新**置 `cancelled`（`WHERE status='queued'`）；已在跑 / 已结束 → **409**（单次回测跑在子线程/子进程里，中止不了——如实拒绝，不假装能停）。与 worker 之间无需加锁：`cancel` 只改 `queued` 行、`claim` 只选 `queued` 行，两条语句各自原子，谁先命中谁生效。E1b 的网格改用 `run_cells(stop=)` 做协作式中止 |

报告取回走**既有** `GET /api/v1/backtest/runs/{run_id}`（回测页深链 `?run=` 已在用这条路），不新开报告通道。

**错误通道的分界**（异步化的必然结果，写死在这里防漂移）：

| 类别 | 何时知道 | 通道 |
|---|---|---|
| 归属 404 / 静态检查 422（带 findings）/ 参数 422 / 窗口 404 / 数据未就绪 503 | 提交期（`resolve_window` 会真读一次行情） | **同步 4xx**，与 P1–P2 逐字同形 |
| 沙箱配额（wall/cpu/memory/output/crash）、用户代码运行期异常（`StrategyRejected`）、执行期数据错误 | 跑起来才知道 | 任务的 `error`：`{kind, message}`，`StrategyRejected` 另带 `line`（**与旧 422 响应体同形**，前端不写第二套映射） |

M4c「运行前带行号给话术」的承诺因此**分两段兑现**：静态检查仍在提交期给 422 + findings，
运行期异常在任务 `error` 里给 `line`。

### 2.4 重启与失败语义（如实）

- 启动时 `status='running'` 的行 → 标 `failed`，`error.kind='interrupted'`（文案「服务重启中断」）。**不自动重跑**：静默重跑比明说失败更让人困惑。
- `status='queued'` 的行由 worker 自然接续（表即队列）。
- 对外**只能说**「排队中的任务重启后续跑」——**不得宣称「重启不丢任务」**（P3 规划底稿那句是粗口径）。
- 已知代价（E1a 独有，E1b 会消掉）：重启会打断正在跑的**单次回测**（0.3–2s 窗口，概率极低）——失败可查、可重跑。
- **单实例假设**（实测撞到过）：扫尾的判据是全局的 `status='running'`，所以同一库上**同时跑第二个进程**（本机并发跑集成套件、或起第二个 uvicorn）时，后起者的启动扫尾会把前者正在跑的任务标成 `failed/interrupted`。单机单进程是本项目的既定形态；真要多实例，任务行得带实例号并按实例扫尾（P4 规模，不在此做）。

### 2.5 接缝（E1c）

**改造（LLM 侧）**：7 处调用点从「绑定函数」改为「取用式」——`from app.core import llm` + `llm.complete(...)` / `llm.ask_once(...)` / `llm.build_chat_model()`：

| 文件 | 现在的用法 |
|---|---|
| `app/main.py` | `build_chat_model()` |
| `app/agent/deep/roles/base.py` | `complete(...)` |
| `app/agent/deep/debate.py` / `router.py` / `synthesize.py` | `complete(...)` |
| `app/memory/reflection.py` | `ask_once(...)` |
| `app/report/narrative.py` | `ask_once(...)` |

`DEFAULT_MODEL`（常量）与 `LLMNotConfigured`（异常类）**保持直接导入**——打桩不需要它们，改成属性访问只是噪音。深度角色既有的 `RoleContext.chat` 与 `ask_once(chat=...)` 注入**原样不动**（它们服务离线测试，是比网关更局部的既有设计）。

**不做 `LLMGateway` 类**：模块级函数 + 模块属性访问已给出单点注入；E4 的熔断/降级链用模块全局状态即可，等真需要「多份互相独立的状态」时再对象化。

**登记（数据侧，零代码）**——E4 故障注入的三个注入面：

| 注入面 | 实体 |
|---|---|
| 小石 CLI | `app/data/xiaoshi_cli.run()`（超时/限流语义在模块内统一） |
| 小石 MCP | `app/data/xiaoshi.adapter_connection()` + `app/agent/tools.load_xiaoshi_tools()` |
| 本地数据 | `app/data/duckdb_client.connect()` |

3 处**刻意绕过**（`etl/store.py:163`、`rag/embed.py:185` 各自的物化/建索引连接；`bars.py` 两处——注释写明「connect() 要求 bars/ 与 events/ 都有分片」）**不动**，理由已在代码里。

### 2.6 前端

| 件 | 内容 |
|---|---|
| `lib/api.ts` | `submitBacktest`（202 拿 `task_id`）、`task(id)`、`tasks(limit)`、`cancelTask(id)`；既有 `run(id)` 复用为报告取回 |
| `components/tasks/use-task.ts` | 提交 → 轮询 → 终态：首次 400ms、其后 1s、上限 3 分钟（与 `use-chat.ts` 同族口径）。**结果由返回值带出**（`{ok, runId}` / `{ok: false, failure}`），不从 state 回读——`await` 之后读 hook 返回对象拿到的是本次渲染的快照 |
| `lib/task.ts` | 纯逻辑（终态判定 / 进度文案 / 失败映射 / `pollTask`）：取数与等待可注入，vitest 直接测（不 sleep） |
| 三个调用点 | `use-backtest.ts`（轮询到 `run_id` 后走既有 `api.run` 装配）、`optimize/page.tsx`「点一格重跑」、`use-workbench.ts`「运行」——后两处仍是「等终态 → 跳转 `/backtest?run=`」 |
| 不动 | `use-chat.ts` / `use-run.ts` 的既有轮询**不重构**（各自终止条件与副作用不同，合并收益薄） |

### 2.7 回归面与已知边界

- 16 处既有用例直接打 `POST /api/v1/backtest` 并断言内联报告（离线 8 + 集成 8），**全部按新契约改写**为「提交 → 驱动 worker / 等任务 → 取 run」，断言语义不降级（报告仍逐项比对）。
- **语义迁移一并写进用例**：`test_backtest_user.py` 的故障矩阵按「提交期拦 / 运行期落任务」两栏重排（`BAD_CODES` 的 stage 字段），跨字段参数约束（`fast < slow`，跑在沙箱里）与运行期异常改断言任务的 `error` + `line`。
- `test_api_endpoints.py` 的鉴权/越权矩阵加任务三端点。
- 已知边界：① 单 worker 串行；② `running` 任务重启即中断（如实标失败）；③ 任务不设保留期与清理（单人项目量小，只加索引）；④ 无幂等键与重试（E4 的 L7/L2 落地）；⑤ **异步化对单次回测是「契约统一」不是提速**——端到端比旧同步路径多 0.6–0.9s（闸门里 `resolve_window` 那次读盘 + 取件轮询 + 落库，实测见 §11 v2.2）；⑥ 单实例假设（见 §2.4）。


### 2.8 E1b：网格 / 批量迁入队列（2026-10-14 定稿）

**要收掉的痛点**（SPEC §2.0 点明的那个）：网格/批量那条路径**断线即丢**——M5b 原文「落库在 done，流没走完就不留记录」，一次满格网格跑到 80% 刷新就是 30 秒白跑、零记录。E1b 把执行迁进 E1a 的 worker，**SSE 从「执行载体」降为「进度订阅」**。

**后端**

| 件 | 内容 |
|---|---|
| 提交 | `POST /optimize/{grid,batch}` → **202 + `{task_id}`**（实施期订正，见下「与草案的偏差」）；闸门（`_prepare` / `grid_cells` / 窗口解析 / 参数）照旧同步做完，展开结果 `CellSpec[]` + `RunOptions` + `start` 帧**冻结进任务行**的 `request.exec` 子键，落 `optimization_runs.request` 时由执行体剔掉（与 `_EXECUTION_ONLY` 同一手法） |
| 执行 | worker 的 `EXECUTORS` 加 `grid` / `batch`：复用**既有** `run_cells`（连 `CONCURRENCY=2` 的进程级闸门一起——它本就是「E1 异步队列的前置形态」）；每完成一格 `bus.publish(task_id, cell)` 并推一格 `progress`；收尾落 `optimization_runs` **一次**（语义与今天一字不变）→ `finish_task(result=done 帧载荷)` → **广播终态帧**（`done` / `error` / `cancelled`，三态都发——漏了它在线订阅者会一直停在「运行中」，见下「实施期逮到的两个 bug」） |
| 进度总线 | 新 `app/tasks/bus.py`：按 `task_id` 的**累积缓冲 + 订阅者队列**。订阅者先收回放（断线重连补齐已有格）再收新帧；缓冲在**最后一个订阅者离开且任务已终结**时释放。进程内（单实例假设同 E1a——服务重启则任务标 failed、缓冲消失，如实） |
| 重连 | 新 `GET /api/v1/tasks/{task_id}/events`（SSE，受保护，越权/不存在 404）：**已终结** → 从 `optimization_runs.summary.cells` 回放 + 补一帧 `done`（无记录 = 失败/取消 → `error` 帧）；**仍在跑** → 订阅总线回放 + 续流。帧形状与 `POST /optimize/*` 的流**逐字同形**（同一编码件），前端两条路共用一个消费器 |
| 兜底通道 | **不需要**：提交回的是 202 + `{task_id}` JSON，任务号在响应体里就拿到了——不存在「流断了还没拿到号」的窗口（把提交与订阅分开的副产品） |
| 取消 | `running` 的网格/批量 → **协作式中止**（`run_cells(stop=)` 已有：停止派发新格，已在跑的最多 2 格自然跑完）→ 任务标 `cancelled`。**中止的结果不落库**（与 M5b「中止的结果不是一份可引用的记录」一致），前端保留本地已收到的格并标注「未落库」 |

**与草案的偏差（实施期订正，2026-10-14）**

草案写的是「`POST` 保持 SSE 形态、前端零改动」。动手后发现那条不成立：执行搬进 worker 之后，
**请求内开流会一直挂到终态**——客户端不读响应体就占着连接，离线用例也没法驱动它
（没有 worker 就没有终态，流永不结束）。改成 `202 + {task_id}`，与 `POST /backtest` 同一副形态：
**每个提交都回任务号**。前端因此多一步「提交完再订阅」，但 `use-optimize` 本来就要改
（断线语义、取消），边际成本小；`X-Task-Id` 兜底头随之取消。

**实施期逮到的两个 bug**（都是走查/真机验出来的，记在这里防复现）

1. **worker 从不广播终态帧**：在线订阅者收得到 `cell`、收不到 `done`，页面永远停在「运行中」。
   回放那条路是 `stream._replay` 从库里拼的 `done`，所以「断线后重连」的集成用例照样绿——
   **只有在线跑完并且在看**才暴露。修法：`done` / `error` / `cancelled` 三态都在 worker 里广播，
   载荷与回放共用 `stream.done_payload`（一处构造，两处用，防漂移）。
2. **`?task=` 自动重连在 StrictMode 下失效**：去重若用 `useRef` 记「已接过的号」，开发模式的
   effect 双跑（挂载 → 清理 → 再挂载）会让第二遍直接跳过，而第一遍那条订阅已被清理函数 abort
   ——**一条订阅都没有**，页面永远空白。修法：去重改用 `taskId` 这个 state（第二遍照样订上，
   重复订阅由 `subscribe` 里的 `abort` 兜住）。这条是走查脚本 `capture-e1b.mjs` 逮到的。

**竞态**：handler 必须**先订阅、再查状态**——反过来的话，任务在两次调用之间结束，那一格会落进空档（客户端什么都收不到）。订阅拿到 `(回放, 队列)` 之后再查任务行：已终结就从库里回放并收尾，未终结就续流。两条路覆盖全部时序。

**前端**

- `use-optimize.ts`：`start` 帧带 `task_id`；**断线不再清空本地状态**——改为「任务仍在后台运行」+「继续查看」（走重连端点，帧形状同形所以消费器共用）；`stop()` 从「掐流 + 清空」改成**真正取消任务**（`POST /tasks/{id}/cancel`）。
- 页面：去掉 `beforeunload` 拦截（「跑着的时候离开会丢掉整次」这句随 E1b 失效——正是本片要收掉的东西）；运行号之外把**任务号也写进地址栏**（`?task=<id>`），刷新后自动重连（去重**用 state 不用 ref**——见上面第 2 个 bug）。
- `lib/optimize-stream.ts`（新）：帧 → 视图模型的**纯函数**（按 `index` 落位、`detached` / `cancelled` / `error` 三态分明、`arrivedCount` 不把空洞算进去），vitest 直接测。
- 「排队中」在优化页也要显示：提交到 worker 取件之间有 ≤200ms，前面压着别的任务时更久——如实说出来，别让进度条空转。

**测试**

- 离线：总线的发布/回放/订阅者清理（含「先订阅后查状态」那条竞态用例）；worker 跑 `grid` 任务（逐格进度、单格失败不牵连、取消走协作式中止）；`GET /tasks/{id}/events` 的三态（running 回放 / done 直接收尾 / failed 给 error）。
- 集成：真库 + 真 lifespan——提交网格 → **断开流** → 任务照跑完 → `optimization_runs` 有记录、cells 与流里收到的逐格相等（**今天的行为是零记录**）；重连端点回放同一份 cells。
- 前端：`lib/optimize-stream.test.ts`（10 条：按 index 落位 / 半截 JSON / 未知事件 / 断线保留已收到的格 / 终态后断线不改状态）；走查脚本 `capture-e1b.mjs`（刷新自动接回 / 停止不落库，见 §11 v2.4）。

**已知代价（如实）**

- **单 worker 串行**：满格网格（30s–2min）会把随后的单次回测挡在队列里——前端显示「排队中」，代价可见（E2 配额会把它显式化）。**不引多 worker**（PRD 定「单 worker」）。
- 服务重启仍丢正在跑的网格（任务标 `failed/interrupted`，无部分结果）——与 E1a 同一条边界，不粉饰。
- 取消的结果不落库：那是「中止」，不是「一份可以引用的记录」。

## 5. E4 可靠性 L0–L7

> 规格底稿：`docs/private/Pn-n/P3-En/P3-E4-规划评估.md`（2026-10-14 现状核对与甲/乙/丙三案）。
> **拍板（2026-10-14）：乙案**——按真实缺口做满，不做「每层最小形态」。LLM 侧接入点取 `ChatLiteLLM` 子类；降级链单向 pro→flash；幂等键只覆盖 tasks 三个提交端点；演练的「杀 LLM」用新增的 `DEEPSEEK_API_BASE` 指向不可达端口（不动密钥）。
> **E4a 已交付**（2026-10-14，落地记录 `docs/private/Pn-n/P3-En/P3-E4a.md` 与 §11 v2.6）；E4b / E4c 待做。

### 5.0 与 PRD 假设的核对订正

| PRD 条目（v0.10） | 2026-10-14 代码/实测核对 | 本 SPEC 的处理 |
|---|---|---|
| L1 LiteLLM 网关（配额 / fallback / 429 切换） | 网关已在（`core/llm.py` 唯一构造入口，E1c 已保证）；但库自带的 tenacity 装饰器写的是 `stop_after_attempt(max_retries)`，默认 `max_retries=1` ⇒ **1 次尝试 = 实际不重试**（实测：1→1 次调用、3→3 次）；等待固定 4–10s 指数，**不读 `Retry-After`**；重试不进预算、不可观测 | §5.2 把失败语义收口成一条策略；库内层显式关掉 |
| L2 LangGraph RetryPolicy + TimeoutPolicy | 深路径**所有会调 LLM 的节点内部已全量 catch**（`roles/base.py:124-129`、`debate.py:217-228`、`synthesize.py:203-208`）⇒ 节点级 RetryPolicy 是**死配置**；`create_agent` 那条 chat 路径不暴露节点配置。节点级 `timeout` 实测**节点内接不住**（节点里捕获 `CancelledError` 后整图照样抛 `NodeTimeoutError`） | §5.2：重试落**调用层**；节点级 `timeout` 只作兜底（命中即整跑失败） |
| L5 降级（部分结果 + 明确告知） | **已是既有资产**：角色降级 → `gaps` → run `degraded` → 报告块 → 前端徽章全线已通 | §5.6：不重造，只补三类缺口 |
| L0 / L4 / L7 | 均无（无令牌桶、无熔断、无幂等键；提交即 `uuid4()` 建行，`create_task` 是纯 INSERT） | §5.3 / §5.4 / §5.5 新做 |

### 5.1 统一 policy 对象

`app/core/reliability.py`。三层生命周期分明——这是本节的枢纽，写死防漂移：

| 层 | 生命周期 | 内容 | 持有者 |
|---|---|---|---|
| 进程级 | 进程内单例 | 熔断状态机（每依赖一份）、限流桶 | 模块注册表 `get_dep(name)` |
| 运行级 | 每次 run 新建 | `Budget`（四计数器） | 深路径 `RoleContext`（既有），policy **引用**不复制 |
| 调用级 | 每次调用 | 尝试次数、退避时长 | `guard()` 栈上局部量 |

```python
@dataclass(frozen=True)
class Retry:
    max_attempts: int = 3            # **总尝试次数**（不是「重试次数」——库内层那个坑就在这）
    base: float = 0.5
    factor: float = 2.0
    cap: float = 8.0
    jitter: float = 0.25
    max_total_seconds: float = 90.0  # 尝试累计时长的硬顶——防「超时 × 次数」相乘爆掉节点预算
    respect_retry_after: bool = True
    max_retry_after: float = 30.0    # 超出就放弃重试、直接走降级

@dataclass(frozen=True)
class Circuit:
    threshold: int = 5               # 连续失败几次开路
    cooldown: float = 30.0           # 开路后多久允许探测
    probes: int = 1                  # 半开放几个探测

@dataclass
class Dependency:
    name: str                        # "llm" / "xiaoshi-cli" / "xiaoshi-mcp" / "duckdb"
    retry: Retry
    circuit: Circuit
    clock: Callable[[], float]       # 可注入——测试不 sleep
    sleep: Callable[[float], Awaitable[None]]
    # 活状态：state / failures / opened_at / probes
```

- **时钟与睡眠可注入**是硬要求：熔断的「冷却后半开恢复」要么真等待（测试慢且脆）、要么假时钟（确定性）。选后者。
- **不引依赖**：退避循环手写（~20 行）——`tenacity` 表达不了「读 `Retry-After` 并对其封顶」这条语义。
- **不做插件框架**：四个依赖、两个策略类、一个 `async def guard(name, fn)`——成功原样返回；失败把最后一个异常抛出；熔断打开时抛 `CircuitOpen(name, retry_after)`。
- **错误分类是纯函数** `classify(exc) -> retryable｜permanent｜rate_limited｜cancelled`（规则见 §5.2）。**`rate_limited` 永远不重试**（小石红线，见 §5.3）。

### 5.2 LLM 侧（L1 / L2 / L3）

**接入点**：`core/llm.py` 内新增 `PolicyChatModel(ChatLiteLLM)`，`build_chat_model()` 返回它。**重写 `completion_with_retry` / `acompletion_with_retry` 两个方法**——实测四条执行路径（`_generate` / `_stream` / `_astream` / `_agenerate`）**全部经由这两个方法**，故 chat 流式、工具调用、深路径一并受管（含 `create_agent` 那条够不着节点的路）。

| 项 | 规格 |
|---|---|
| 库内层 | `max_retries=0` 显式关掉（实测 0 与 1 都是 1 次调用，写法取 0 表明「这层不要」）——避免双重重试与「重试不进预算」 |
| 超时 | `request_timeout=60.0`（`build_chat_model` 传入）——**chat 主链路今天完全无超时**（该参数默认 None），这条必补 |
| 错误分类 | `RateLimitError` → `rate_limited`（读 `Retry-After`：≤ `max_retry_after` 则等待后重试，超出则不重试、直接降级）；`Timeout` / `APIConnectionError` / 5xx → `retryable`；4xx（鉴权/参数/内容）→ `permanent`；`CancelledError` → `cancelled`（不重试） |
| 降级链 | **单向 pro → flash**。触发点在「重试耗尽之后」，不是首次失败就换（换档也要花一次完整调用）。反向升档**不做**——那是把故障掩盖成「更贵的成功」，且污染预算口径 |
| 换档实现 | 在重写的方法内改 `kwargs["model"]` 后调 `super()`——**不改实例状态**（chat 的模型实例在 lifespan 建一次、跨请求共享，改 `self.model` 会并发污染） |
| 如实记录 | `Completion.model` 已是实际模型；**`RoleResult.model` 现在填的是「请求的」模型**（`roles/base.py:112`）——降级链上线那一刻这句就成假话，必须改成实际；降级发生时 run 的 `notes`/`gaps` 补一条「本段由 flash 降级产出」 |

**超时补点（收敛为两条调用级 + 兜底）**：调用级补「chat 主链路」（上表）与「小石 MCP 工具调用」（`adapter_connection()` 加 `session_kwargs={"read_timeout_seconds": timedelta(...)}`——实测该键是 `langchain-mcp-adapters` 连接 TypedDict 的受支持字段、直通 `ClientSession`）；embedding/精排与 DuckDB 读**不加调用级包装**——本地调用、挂死风险低，且 `wait_for(to_thread(...))` 只收回控制权、线程照跑（是假安全），由下面的节点级兜底覆盖。

**节点级 timeout（兜底，不是降级路径）**：深路径每个节点挂 `timeout=TimeoutPolicy(run_timeout=...)`（settings `research_node_timeout_seconds`，默认 120.0——须大于「`Retry.max_total_seconds` + 取数时长」的最坏值）。**命中即整跑失败**（实测节点内接不住），故 `runner.py` 必须捕获 `NodeTimeoutError` 并转成 run `failed`（`error` 写明哪个节点超时），**不静默挂死**。与「一个角色失败不连坐」不冲突：那条讲**可预期的**降级，这条是**兜底**，SPEC 分开写、测试分开验。

### 5.3 L4 熔断

依赖各一份状态：**`llm` 按档位分**（`llm:pro` / `llm:flash`——降级链要能绕过已熔断的那一档；单份 `llm` 状态会让链在熔断后报废，实施期订正）/ `xiaoshi-cli` / `xiaoshi-mcp` / `duckdb`。三态标准：`closed →（连续失败 ≥ threshold）→ open →（cooldown 后第一个调用放行）→ half_open →（成功 → closed 清零 / 失败 → 回 open 重置冷却）`。

打开时的行为（**绝不静默**）：

| 依赖 | 熔断打开后 |
|---|---|
| `llm` | 直接走降级链（不再耗超时）；两档都熔断 → **快速失败**（秒回 503 + 明确文案，不挂满超时） |
| 数据源 | 该角色/该步就地降级，`errors` 写明「数据源熔断中（连续 N 次失败）」 |

**`rate_limited` 的专门规则**：不参与「连续失败」计数（它是平台在叫我们退后，不是依赖故障），但它**立刻开路**——「429 按 `Retry-After` 停」这条红线由此在策略层强制成立，而不是靠每个调用点自觉（既有 `test_bars_refresh.py` 的 429 用例与 `test_etl.py` 的中止用例**一字不改**，继续全绿）。

### 5.4 L0 限流

进程内令牌桶，键 `(user_id, bucket)`；桶名 `llm-chat` / `llm-research` / `task-submit`。**其余读端点不限**（本地 DuckDB 便宜，限了只是自找麻烦）。

| 桶 | 默认额度 | 挂载点 |
|---|---|---|
| `llm-chat` | 10/分钟 | `POST /api/v1/chat` |
| `llm-research` | 3/10 分钟 | `POST /api/v1/research/runs` |
| `task-submit` | 20/分钟 | `POST /backtest`、`POST /optimize/{grid,batch}` |

- 拒绝契约：**429 + `Retry-After`** + 结构化体 `{kind: "rate_limited", message, retry_after}`。
- **必须在开流之前拒**：chat / research 是 SSE，首字节发出就改不了状态码（与 `api/chat.py` 既有注释同源）。
- 额度全部进 settings（`rate_limit_*`），默认取「演示友好」档。
- **与 E2 配额的边界写死**：L0 管**速率**（每分钟多少，进程内、重启清零），E2 管**累计用量**（跨重启需持久化）。E4 只实现前者，桶的计数件暴露成可复用对象，E2 届时配置而不是另起一套。

### 5.5 L7 幂等键

- 客户端头 `Idempotency-Key`（前端每次**用户动作**生成一个 UUID，重试复用同一个）。
- `tasks` 表加 `idempotency_key TEXT` + 部分唯一索引 `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL`（幂等 ALTER，与既有九张表同约定）；`request` 里存 body 指纹。
- 命中：**202 + 同一个 `task_id`**；**同 key 不同 body → 409**（宁可吵一声，不静默跑错的那份）。
- 顺序：查 key 在闸门之前（重复提交不再走一遍静态检查的算力），body 指纹比对在命中之后。
- 覆盖 `POST /backtest`、`POST /optimize/{grid,batch}` 三个端点；`POST /research/runs` 不做（要动 `research_runs` 表且该端点带 HITL 语义，留给 E2 那轮）。
- **不做任务级自动重试**：E1a 已定「重启不自动重跑」。幂等键防**重复提交**，不防**失败重跑**；任务**内部**的瞬时故障由 §5.1 的调用层重试兜住，两者不冲突。

### 5.6 L5 告知与 L6 护栏

L5 只补三类缺口（既有链路不重造）：

| 缺口 | 补法 |
|---|---|
| chat 主链路失败只有「内部错误，请重试」 | 按分类给文案：上游不可用 / 熔断中 / 超时 / 内部错误——**词汇与深路径一致**，不新增第二套 |
| 数据源侧降级未与熔断、超时打通 | §5.3 的 `errors` 文案统一由此产出 |
| 「本次由降级模型产出」无告知 | run `notes`/`gaps` + 报告块，前端照抄服务端文案（既有纪律） |

L6：深路径的 `Budget` 并进 policy 的「运行级」半边（**定义不搬家**，只在 policy 里引用同一对象）。chat 路径补三项：token/成本累计（`astream_chat` 现在只留最后一次 `usage`，需改成累加）→ 到限硬停并给明确文案；`RECURSION_LIMIT=12` 命中时同理（今天是裸的 `GraphRecursionError` → 「内部错误」）。

### 5.7 故障注入双轨

**轨一（pytest，离线、进 CI）**：注入面用 E1c 登记的接缝（`core.llm` 模块属性 / `xiaoshi_cli.run` / `xiaoshi.adapter_connection` / `duckdb_client.connect`）；故障由**测试夹具**制造（monkeypatch + 假时钟），**生产代码不留故障开关**。矩阵 12 条：

| # | 注入 | 期望 |
|---|---|---|
| 1 | LLM 单次超时 | 重试后成功；尝试次数与退避可断言 |
| 2 | LLM 持续超时 | 重试耗尽 → 降级链 pro→flash 顶上；`model` 字段记录**实际**模型 |
| 3 | LLM 429 带 `Retry-After` | 按 `Retry-After` 等待（假时钟推进），不盲重；超 `max_retry_after` → 直接降级 |
| 4 | LLM 连续失败 ≥ 阈值 | 熔断打开 → 后续调用**不耗超时**即失败 |
| 5 | 冷却后半开 | 探测成功 → 恢复；探测失败 → 重新开路 |
| 6 | 小石 CLI 挂 | 角色降级 + run `degraded` + 报告照出（既有语义不降级） |
| 7 | 小石 429 | **不重试**（停止信号）+ 熔断打开 + 明确告知 |
| 8 | 数据源熔断中 | 后续角色**跳过取数**并写明「熔断中」，不逐个去撞 |
| 9 | 连打 chat 超限 | 第 N+1 次 429 + `Retry-After`，**未开流** |
| 10 | 同幂等键重复提交 | 只跑一次；同 key 不同 body → 409 |
| 11 | chat 到 token/成本上限 | 硬停 + 明确文案（不是「内部错误」） |
| 12 | 节点挂死 | 节点级 timeout 命中 → 整跑 `failed` 且写明节点 |

**轨二（演练脚本，真环境、留档）**：`scripts/drill_reliability.py` 四幕——① 拔数据源（`XIAOSHI_CLI_COMMAND` 指向不存在的命令）② 杀 LLM（`DEEPSEEK_API_BASE=http://127.0.0.1:9`）③ 触发限流（连打 chat）④ 熔断恢复（冷却后再跑通）。产物落 `logs/e4-drill/<时间戳>/`，作验收证据兼 E9 事故复盘素材。

**「进 CI」依赖 E7**（本项目至今无 `.github/workflows`）：E4 交付用例与入口命令，E7 落地时挂上——如实写明，不假称已进 CI。

### 5.8 已知边界（如实）

- **子类重写了包内方法**（`completion_with_retry` / `acompletion_with_retry`，非 LangChain 接口契约）：包版本已精确 pin（`langchain-litellm==0.11.0`），**升级该包时必须复核这两个方法仍在**。
- **进程内状态**：限流桶与熔断状态重启清零——单机单进程的既定形态（同 E1a 的单实例假设）。真要多实例，这两处都要外部化（P4 规模，不在此做）。
- **节点级 timeout 命中 = 整跑失败**（无部分结果）——它是兜底不是降级；常规超时由调用级负责。
- **降级链不保证质量等价**：flash 顶上的段落如实标注，报告读者自行权衡。
- **幂等键不覆盖 research runs**（§5.5），该端点的重复提交仍会各跑一遍。

## 10. 测试策略

（待随各功能滚动补充。E4 故障注入双轨的 12 条矩阵与演练四幕见 §5.7——本节只登记用例落在哪些文件。）

### E1（E1a + E1c + E1b）

- **离线后端**：`tests/test_tasks_api.py`（提交 202 / 轮询到终态 / 越权与不存在同为 404 / 非法请求仍同步 422 / 取消语义：queued 可取消、running 与终态 409）、`tests/test_task_worker.py`（FIFO 串行、失败捕获且不落 run、取件复核已取消状态、启动把 `running` 扫成 `interrupted`）。
- **测试驱动方式**：离线用例**不起 worker**——用 `app/tasks/worker.py::run_once(db)` 由夹具手动驱动。理由：模块级 `TestClient` 每请求一个事件循环，后台常驻协程在那种用法下活不过一次请求；集成用例 `real_stack` 走 `with TestClient` 才有常驻 loop。
- **假库**：`tests/fakes.py::FakeDatabase` 补任务方法（与 `Database` 同方法面）。
- **集成**：`tests/integration/test_tasks_e1.py`（真库 + 真回测走队列；`running` 扫尾语义）。
- **接缝**：一条用例 patch `app.core.llm.complete` / `ask_once` 后，deep 角色 / reflection / narrative 三路都吃到假实现——证明单点注入成立。
- **E1b 离线**：`tests/test_tasks_stream.py`（总线三条时序：先发布后订阅靠回放、先订阅后发布靠推送、订阅者走了照常发布；事件流三态：running 回放续流 / 已终结从库回放 / 失败与取消各一帧）、`test_optimize_api.py` 补「202 + 冻结执行材料」「逐格进度」「协作式中止不落库」；`run_optimize` 辅助件把「提交 → 驱 worker → 订阅」收成一处（任务已终结时订阅走回放，**不需要并发**）。
- **E1b 集成**（`tests/integration/test_optimize_e1b.py`）：**断开订阅后任务照跑完并落库**（M5b 时代这里是零记录）+ 中止不落库。
- **E1b 走查**：`frontend/scripts/capture-e1b.mjs` 5 条（刷新自动接回 / 停止不落库）；`capture-m5b.mjs` 14 条为回归。
- **前端**：`lib/task.test.ts`（终态判定 / 进度文案 / 失败映射 / `pollTask` 的取数与等待全注入，**不 sleep**；首轮 400ms、其后 1s 也钉死）；既有 423 例全绿。

### E4（计划，待实施）

- **LLM 侧（E4a）**：`tests/test_reliability.py`（分类纯函数、退避与 `Retry-After` 封顶、熔断三态与半开——**假时钟，不 sleep**）；`tests/test_llm_policy.py`（`PolicyChatModel`：重试后成功、降级链换档且 `kwargs["model"]` 真变、4xx 不重试、`CancelledError` 不重试、库内层为 0）；`test_deep_graph.py` 增补节点 `timeout` 与 `NodeTimeoutError` → run `failed`；`test_deep_roles.py` 增补 `RoleResult.model` 记的是**实际**模型。
- **服务侧（E4b）**：`tests/test_rate_limit.py`（三桶额度 / `Retry-After` / SSE 端点**未开流**即 429）；`tests/test_idempotency.py`（同 key 同 body 返同一 `task_id` / 同 key 异 body 409 / 不同用户同 key 各跑各的）；数据源熔断与 `rate_limited` 立刻开路（接在 `test_bars_refresh.py`、`test_etl.py` 既有红线用例**上方**，那两条不改）。
- **集成**：`tests/integration/test_reliability_e4.py`——真库 + 真 lifespan 下的 429 与幂等键端到端。
- **前端**：`lib/idempotency.test.ts`（同一次用户动作复用 key、新动作换 key）；限流与降级提示**照抄服务端文案**。
- **演练**：`scripts/drill_reliability.py` 四幕，产物 `logs/e4-drill/`。

## 11. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v2.0 | 2026-10-13 | 规划评估拍板（底稿 docs/private/Pn-n/P3-En/P3-En-规划评估.md）：四处裁剪（E1 定向接缝 / E4 L0+L4 进程内 / E7 eval gate 只跑确定性断言 / E8 清单库直用化）+ E6 砍；PRD v0.10 / CLAUDE.md / ROADMAP 同步。正文待按功能 ID 滚动填写 |
| v2.1 | 2026-10-14 | §2（E1）正文定稿（底稿 docs/private/Pn-n/P3-En/P3-E1-规划评估.md）：**E1a（任务队列 + 单次回测异步）+ E1c（LLM 调用面取用式）先做，E1b（网格/批量迁入）后延**。三处现状订正——API 版本化已是基线、数据侧接缝已存在（零代码只登记）、LLM 侧绑定导入是真缺口；痛点订正——单次回测实测 0.3–2s，E1a 不得声称解决长任务丢失。队列实体 = 任务表（理由：`asyncio.Queue` 绑事件循环，与模块级 `TestClient` 的每请求 loop 冲突） |
| v2.6 | 2026-10-14 | **E4a 交付 —— 统一 policy + LLM 侧可靠性**（落地记录 `docs/private/Pn-n/P3-En/P3-E4a.md`）。① **交付**：新 `app/core/reliability.py`（`Retry`/`Circuit`/`Dependency` + `classify()` 纯函数 + `guard()`/`guard_sync()`，三层生命周期、时钟与睡眠可注入）；`core/llm.py` 新 `PolicyChatModel(ChatLiteLLM)` 重写两个 `*_with_retry`（`max_retries=0` 关掉库内层、`request_timeout=60` 补上 chat 主链路**此前完全没有的**超时、单向降级链 pro→flash、`on_model_used` 回执实际模型）；模型如实——角色/综合/辩论的 `model` 字段与**预算计价**都改用实际模型，降级时补 note；节点级墙钟兜底（新 settings `research_node_timeout_seconds=120`，`runner.py` 单独捕获 `NodeTimeoutError` 转**点名节点**的失败文案）。② **实施期订正**：熔断状态**按档位分**（`llm:pro`/`llm:flash`）——单份 `llm` 状态会让降级链在熔断后报废，SPEC §5.3 已同步。③ **实施期发现三点**：库自带「重试」的 `stop_after_attempt(max_retries)` 默认 1 = **1 次尝试**（实测 1→1、3→3，即一直是关的）；`CircuitOpen` 身上也有 `retry_after` 属性，会被 `classify` 结构化成 `rate_limited`（判断须排在 `retry_after_of` 前）；`asyncio.wait_for` 把调用放进子任务，**子任务里写的 contextvar 父上下文看不见**——实际模型回执改用回调。④ **回归**：后端离线 **1403 passed**（基线 1358，+45）、集成 **106 passed**（真库，含真 pro/flash 与真流式工具调用）；真机核对 `build_chat_model()` → `PolicyChatModel`（`max_retries=0` / `request_timeout=60`），一次真实 flash 调用 1.84s 且 `Completion.model` 报实际模型 |
| v2.5 | 2026-10-14 | **§5（E4 可靠性 L0–L7）规格定稿**（底稿 `docs/private/Pn-n/P3-En/P3-E4-规划评估.md`）：拍板**乙案**（按真实缺口做满）。三处现状订正——① **L1 网关已在，但库自带的 tenacity 重试是死的**：装饰器写 `stop_after_attempt(max_retries)`，默认 `max_retries=1` ⇒ 1 次尝试 = 不重试（实测 1→1 次调用、3→3 次），且等待固定 4–10s、不读 `Retry-After`；② **L2 的节点级 `RetryPolicy` 在本仓是死配置**（所有会调 LLM 的节点内部已全量 catch），且节点级 `timeout` 实测节点内接不住（整图抛 `NodeTimeoutError`）⇒ 重试落**调用层**、节点 timeout 只作兜底；③ **L5 是既有资产不是缺口**（角色降级 → gaps → run degraded → 报告块 → 前端徽章已全线通）。设计：`app/core/reliability.py`（`Retry`/`Circuit`/`Dependency` + 三层生命周期 + 可注入时钟）、`PolicyChatModel(ChatLiteLLM)` 重写两个 `*_with_retry`（实测四条执行路径全部经由它们，含 chat 流式与工具调用）、降级链单向 pro→flash 且**模型如实记录**、四依赖熔断（`rate_limited` 不计数但**立刻开路**）、三桶令牌桶 + 429 契约（SSE 端点未开流即拒）、tasks 三端点幂等键、chat 主链路 `request_timeout=60` 与 MCP `session_kwargs` 两处调用级超时。故障注入双轨：pytest 12 条矩阵（夹具注入，**生产不留故障开关**）+ 四幕演练脚本（新增 `DEEPSEEK_API_BASE` 作「杀 LLM」开关，不动密钥）。**待用户确认后开工** |
| v2.4 | 2026-10-14 | **E1b 交付 —— 网格/批量迁入任务队列，M5b 的「断线即丢」收掉**。① **交付**：`app/tasks/bus.py`（按 task_id 的累积缓冲 + 订阅者）、`app/tasks/stream.py`（`task_frames`：先订阅再查状态；终态从 `optimization_runs` 回放）、`app/tasks/executors.py` 加 `grid`/`batch`（复用 `run_cells`，逐格 `publish` + `progress`，中止则**不落库**）、`app/api/optimize.py` 两个 POST 改 **202 + `{task_id}`**（执行材料冻结进 `request.exec`）、`app/api/tasks.py` 加 `GET /{id}/events` 与**协作式中止**的取消分支、`worker.request_stop` 中止投递口 + 三态终态帧广播。前端：`lib/optimize-stream.ts`（纯函数视图模型）+ `use-optimize` 重写（提交与订阅分开、`detached` 保留已收到的格、`stop()` 改成真取消）+ 页面去 `beforeunload` 拦截、`?task=` 自动重连。② **与草案的偏差**：`POST` 不再是 SSE 而是 202（理由见 §2.8「与草案的偏差」——请求内开流会挂到终态，离线用例无法驱动），`X-Task-Id` 兜底头随之取消。③ **走查与真机**：`capture-e1b.mjs` **5/5**（`?task=` 入地址栏 / **刷新后结果照常出现** / 跑完照常落库 / 停止后说明「未落库」/ **中止不落库**——记录数 4→4）；`capture-m5b.mjs` 回归 **14/14**（含「点一格重跑」）。④ **实施期逮到两个 bug**：worker 从不广播终态帧（在线订阅者永远停在「运行中」，只有「在线跑完并且在看」才暴露）；`?task=` 重连去重用 `useRef` 在 StrictMode 下失效（effect 双跑 → 一条订阅都没有）。两条都写进 §2.8 防复现。⑤ **回归**：后端离线 **1358 passed**（+12）、集成**全量 106 passed / 0 failed**（含新 `test_optimize_e1b.py` 两条：断开订阅后照跑完并落库 / 中止不落库；`test_optimize_m5b.py` 的辅助件按新契约改写，断言一字未改）、前端 **454 passed**（+10）、typecheck/lint/build 全绿。同轮 P2 SPEC 的 M5b 断线条款划掉并指路（v1.59） |
| v2.3 | 2026-10-14 | **§2.8（E1b）定稿**：网格/批量迁入 E1a 的 worker，SSE 从执行载体降为进度订阅；新 `app/tasks/bus.py`（按 task_id 的累积缓冲 + 订阅者）、新 `GET /tasks/{id}/events`（三态重连）、`X-Task-Id` 兜底头、取消走 `run_cells(stop=)` 的协作式中止且**结果不落库**；前端去 `beforeunload` 拦截、`?task=` 自动重连。三条已知代价如实写明（单 worker 串行 / 重启仍丢在跑的 / 取消不落库）。**待用户确认后开工** |
| v2.2 | 2026-10-14 | **E1a + E1c 交付**（执行计划与落地记录 `docs/private/Pn-n/P3-En/P3-E1a.md`）。新增 `tasks` 表（DDL 进 `core/db.py::SCHEMA`，两个索引）、`app/tasks/{executors,worker}.py`（执行体 + 单协程取件循环，取件间隔 200ms，lifespan 起停）、`app/api/tasks.py`（列表/详情/取消）；`POST /api/v1/backtest` 改 202 + `{task_id}`，落 `backtest_runs` 与报告取回路径不变。**实测（本机真栈，2026-10-14）**：ma_cross/600519——旧同步 `build_report` 627–653ms、提交 427–439ms、取件 552–616ms、端到端 1220–1427ms；event_driven/300750 `both`——1168–1668ms / 414–453ms / 451–564ms / 1855–2073ms。读法：**提交 ≈ 0.4s（闸门里 `resolve_window` 真读一次行情），端到端比旧同步多 0.6–0.9s**——E1a 换的是任务契约与状态可查，不是速度（§2.7 边界⑤）。错误通道分界（§2.3 表）：前置闸门仍同步 4xx，运行期错误落任务 `error`（`StrategyRejected` 另带 `line`，与旧 422 响应体同形）。E1c：7 处 `from app.core.llm import complete` 改取用式（常量与异常类保持直接导入），一条用例证明单点注入成立；**不做 `LLMGateway` 类**。回归：离线 1345 passed（+22）、前端 432 passed（+9），16 处既有调用点全部改写 |
