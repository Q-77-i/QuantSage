# QuantSage SPEC · P3-En（企业级思维）

> 文档链：规划报告（调研底稿，docs/private/）→ CLAUDE.md（定稿摘要）→ PRD（需求，v0.10）→ 本文（技术规格）→ 代码
>
> 版本 v2.1 ｜ 2026-10-14 ｜ 状态：**规划已评估拍板**（四处裁剪 + E6 砍，PRD v0.10 已同步）；**E1 规格已定稿（E1a + E1c 先做，E1b 后延）**，其余节待滚动。正文按功能 ID 滚动填写（同 P2 SPEC 的滚动方式）；每个功能的规格、实测读数与拍板记录随交付追加到 §11 变更记录。
>
> 本 SPEC 覆盖 PRD §2.3 的 E1–E9（**E6 组合已砍**，不设节，留待 P3 之后独立评估）。规划评估底稿见 `docs/private/Pn-n/P3-En/P3-En-规划评估.md`（现状基线核对与逐项评估，本文件只收技术事实）。

## 目录

| 节 | 内容 |
|---|---|
| [§1](#1-规划基线) | 规划基线（P3 起笔时点的现状核对） |
| [§2](#2-e1-架构分层) | E1 架构分层（**E1a 任务队列 + E1c 接缝已定稿**；E1b 后延） |
| §3–§9 | E2–E9 规格（正文待滚动填写；E6 已砍不设节） |
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

## 10. 测试策略

（待随各功能滚动补充。E4 故障注入双轨已定：pytest 确定性注入（进 CI）+ 手动演练脚本（真拔数据源/杀 LLM，日志留档作验收证据）。）

### E1（E1a + E1c）

- **离线后端**：`tests/test_tasks_api.py`（提交 202 / 轮询到终态 / 越权与不存在同为 404 / 非法请求仍同步 422 / 取消语义：queued 可取消、running 与终态 409）、`tests/test_task_worker.py`（FIFO 串行、失败捕获且不落 run、取件复核已取消状态、启动把 `running` 扫成 `interrupted`）。
- **测试驱动方式**：离线用例**不起 worker**——用 `app/tasks/worker.py::run_once(db)` 由夹具手动驱动。理由：模块级 `TestClient` 每请求一个事件循环，后台常驻协程在那种用法下活不过一次请求；集成用例 `real_stack` 走 `with TestClient` 才有常驻 loop。
- **假库**：`tests/fakes.py::FakeDatabase` 补任务方法（与 `Database` 同方法面）。
- **集成**：`tests/integration/test_tasks_e1.py`（真库 + 真回测走队列；`running` 扫尾语义）。
- **接缝**：一条用例 patch `app.core.llm.complete` / `ask_once` 后，deep 角色 / reflection / narrative 三路都吃到假实现——证明单点注入成立。
- **前端**：`lib/task.test.ts`（终态判定 / 进度文案 / 失败映射 / `pollTask` 的取数与等待全注入，**不 sleep**；首轮 400ms、其后 1s 也钉死）；既有 423 例全绿。

## 11. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v2.0 | 2026-10-13 | 规划评估拍板（底稿 docs/private/Pn-n/P3-En/P3-En-规划评估.md）：四处裁剪（E1 定向接缝 / E4 L0+L4 进程内 / E7 eval gate 只跑确定性断言 / E8 清单库直用化）+ E6 砍；PRD v0.10 / CLAUDE.md / ROADMAP 同步。正文待按功能 ID 滚动填写 |
| v2.1 | 2026-10-14 | §2（E1）正文定稿（底稿 docs/private/Pn-n/P3-En/P3-E1-规划评估.md）：**E1a（任务队列 + 单次回测异步）+ E1c（LLM 调用面取用式）先做，E1b（网格/批量迁入）后延**。三处现状订正——API 版本化已是基线、数据侧接缝已存在（零代码只登记）、LLM 侧绑定导入是真缺口；痛点订正——单次回测实测 0.3–2s，E1a 不得声称解决长任务丢失。队列实体 = 任务表（理由：`asyncio.Queue` 绑事件循环，与模块级 `TestClient` 的每请求 loop 冲突） |
| v2.2 | 2026-10-14 | **E1a + E1c 交付**（执行计划与落地记录 `docs/private/Pn-n/P3-En/P3-E1a.md`）。新增 `tasks` 表（DDL 进 `core/db.py::SCHEMA`，两个索引）、`app/tasks/{executors,worker}.py`（执行体 + 单协程取件循环，取件间隔 200ms，lifespan 起停）、`app/api/tasks.py`（列表/详情/取消）；`POST /api/v1/backtest` 改 202 + `{task_id}`，落 `backtest_runs` 与报告取回路径不变。**实测（本机真栈，2026-10-14）**：ma_cross/600519——旧同步 `build_report` 627–653ms、提交 427–439ms、取件 552–616ms、端到端 1220–1427ms；event_driven/300750 `both`——1168–1668ms / 414–453ms / 451–564ms / 1855–2073ms。读法：**提交 ≈ 0.4s（闸门里 `resolve_window` 真读一次行情），端到端比旧同步多 0.6–0.9s**——E1a 换的是任务契约与状态可查，不是速度（§2.7 边界⑤）。错误通道分界（§2.3 表）：前置闸门仍同步 4xx，运行期错误落任务 `error`（`StrategyRejected` 另带 `line`，与旧 422 响应体同形）。E1c：7 处 `from app.core.llm import complete` 改取用式（常量与异常类保持直接导入），一条用例证明单点注入成立；**不做 `LLMGateway` 类**。回归：离线 1345 passed（+22）、前端 432 passed（+9），16 处既有调用点全部改写 |
