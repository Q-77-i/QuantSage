# 代码评审记录

> 读者：工程团队、面试官。
> 本文记录一个**单人 + AI 协作**项目怎么做代码评审：流程、历史台账、本轮定向评审的 findings 与处置。
>
> 结论按「证据 + 用例」采信，不按「评审意见」采信；AI 评审不是安全证明，也不冒充第三方审计。

## 1. 为什么单人项目也要评审

单人项目的评审不是为了「走流程」，而是为了引入**第二种视角**：

- **Standards 轴**：这段代码合不合仓库既有标准？命名、错误分类、纯函数边界、注释口径、测试形态。
- **Spec 轴**：这段代码合不合规格？判据、边界、失败语义、与文档的一致性。

AI 互审的真实收益在 Spec 轴与「跨文件一致性」：它能同时读完实现、SPEC 与测试，发现「代码看起来对、但与另一处口径打架」的问题。历史台账里最有价值的两条——M5c 的窗口归属 bug、E8a 的 SSE 续期静默失效——都是这种跨文件/跨形态的问题。

## 2. 评审流程

| 阶段 | 做法 |
|---|---|
| **触发** | 功能交付前；安全相关变更；跨模块重构；测试出现「改弱断言才能过」的迹象 |
| **范围** | 固定基线（本次改动 / 某个接缝），不跑无边界全仓评审 |
| **两轴** | Standards 与 Spec 并行；每条 finding 必须带文件、证据、失败场景 |
| **分级** | **必须修复**（正确性/安全/契约）· **建议修改**（可维护性/一致性）· **仅供参考**（不确定或超出本次范围） |
| **处置** | 必须修复项带回归用例；建议项记录不修理由；不重构、不改契约 |
| **留痕** | 台账（日期/范围/findings/处置/证据）+ 本次记录；修复 commit 与用例名可追 |

**硬规矩**：

- 只报能用代码/规格证明的问题；不确定的标「仅供参考」。
- 不报风格偏好；不报与本次范围无关的既有问题（除非是安全/正确性）。
- 一轮不超过 1 会话；每个接缝按严重度排序，最多 8 条。
- 修复必须带回归用例；没有用例的「修好了」不算完成。

## 3. 历史台账

| 日期 | 范围 | 发现 | 处置 |
|---|---|---|---|
| 2026-10-10 | M5c 因子分析（两轴并行） | Standards 5 条：常量错位、统计实现重复、写死 20 日、参数标签各拼一份、无人用的参数。Spec 1 条真 bug：显式窗口时窗口之前可得的事件被整堆归到窗口首日；另 3 条补口（换手序列、拖累口径、非法取值用例） | Standards 5 条全修；Spec 真 bug 修复并加回归用例（先证红 `signal_days=1`、再证绿 `0`）；3 条补口全修 |
| 2026-10-15 | E7a lint / CI | 存量 152 条 lint；ruff 逮到 `etl/archive.py` 用了 `subprocess.CompletedProcess` 却从未 import；两处裸 `Exception` 断言收窄；CI 首跑与第二跑共 27 个问题（工作流错、测试对宿主环境的隐藏依赖、时区/内存阈值/配置缺失） | 152 条清零；真缺陷修复；断言收窄；CI 三 job 跑绿；27 个问题全部处置 |
| 2026-10-15 | E8a 安全审计 | 后端 166 包 0 漏洞；前端 7 条传递依赖告警（postcss 含 2 高危、dompurify 2 低危），1 条 `braces` 上游无补丁；另发现 **SSE 续期静默失效**（见 [事故复盘](postmortem.md)） | 2 条 override 顶掉；1 条上游无补丁如实记录；续期改纯 ASGI 中间件 + 真 SSE 路由守卫用例 |
| 2026-10-15 | E8c 注入面侦察 | 比原稿多找出 2 处注入面（辩论 / 综合的证据块也在渲语料标题）；另 4 处核查后刻意不包并写依据（行业名是平台受控词表、路由 prompt 只有用户问题、反思 prompt 只有我们自己的决策事实、用户自己的话不算外部数据） | 2 处补包；4 处写排除依据；小石 MCP 结果做形状感知（只包 content，artifact 原样留给下游） |
| 2026-10-15/16 | CI 三跑 | 首跑 4 个、第二跑 23 个：审计 job 没装 pnpm（exit 127）；`uv run` 默认同步把跳装的 torch 装回来；离线套件隐含依赖真实 `data/`；时区/内存阈值/缺 `.env` 配置 | 全部处置；CI 三 job 跑绿；「本地复现 CI」的手法进踩坑记录 |
| 2026-10-17 | 收尾项 6（CSP / 容器化 / 取消 / MQ） | CSP 生产验证 33/33，修复 `next-themes` 主题脚本未带 nonce；容器化 spike 证明 bwrap 可解但 torch 在 linux/arm64 拉 CUDA 13；多 worker 维持单实例；MQ 不引入 | CSP 修复并加可复用验证脚本；全量容器化暂缓；running 取消改 DB 标志；MQ 只落文档 |
| 2026-10-31 | P2 收尾（A12 / O9 / O15 / O8） | 配额 `active_tasks` 死分支；Langfuse 建库非幂等；CI 缺只读权限/超时/SHA 钉版；`dev.sh` 端口占用仍打印就绪、PID 不校验身份 | 全部修复并带用例：`quota.enforce` 误用改明确报错、建库 `\gexec` 幂等、CI 四 job 钉版、PID 记启动时间/命令行；集成 155 项单进程 154 passed / 1 skipped |

### 3.1 当前基线（2026-10-31 快照）

| 项 | 读数 | 口径 |
|---|---|---|
| 后端离线 | 1,933 passed / 155 deselected | `pytest -q` |
| 前端 | 515 tests | `pnpm test` |
| 集成 | 155 项；单进程 154 passed / 1 skipped（20:46） | 真容器 + 真密钥 + 真 `data/`；默认关 Langfuse tracing |
| 覆盖率 | 行 89.34% / 分支 80.13%；行门槛 88% | `check_coverage.py` |
| CI | 四 job：backend / frontend / prometheus / audit | `.github/workflows/ci.yml` |

## 4. 本轮定向评审（E9）

### 4.1 范围与方法

四个接缝，两轴并行，只读评审；发现的问题按「必须修复 / 建议修改 / 仅供参考」分级：

1. **PIT 闸门 + RAG 检索**：`backtest/events.py`、`backtest/types.py`、`backtest/engine.py`、`backtest/report.py`、`rag/retrieve.py`、`rag/correct.py`、`rag/collection.py`、`rag/embed.py`
2. **交易风控四闸门 + 处置 + 审计 + Agent 提案**：`paper/risk.py`、`paper/replay.py`、`paper/store.py`、`paper/proposal.py`、`paper/types.py`、`paper/account.py`、`agent/tools.py`（提案工具）
3. **鉴权 / 会话 / 配额 / 限流 / 幂等**：`core/auth.py`、`core/session.py`、`core/quota.py`、`core/security.py`、`core/db_users.py`、`core/db_tasks.py`、`api/auth.py`、`api/limits.py`、`api/idempotency.py`、`core/body_limit.py`
4. **沙箱 / AST 静态检查 / 注入边界 / 对外 MCP**：`strategy/sandbox.py`、`strategy/isolation.py`、`strategy/static_check.py`、`strategy/worker.py`、`strategy/api.py`、`core/untrusted.py`、`agent/tools.py`、`agent/deep/roles/`、`mcp/server.py`

### 4.2 Findings

**26 条**：2 条必须修复（已修）；建议修改 13 条（修 8、记录 5）；仅供参考 11 条（修 2、记录 9）。
「已修」均带回归用例；「记录」保留理由与后续触发条件。

#### PIT / RAG（5）

| # | 分级 | 位置 | 缺陷 | 处置 |
|---|---|---|---|---|
| P1 | 建议修改 | `backtest/types.py::EventView.stamp` | 用 `is` 比较 `Mode`；传字符串会静默退化为 `event_time`，而 `cutoff_field` 仍报 `available_at` | **记录**：当前调用点均已 `Mode(...)`；改 `==` 会静默接受字符串、改变输入契约，留待边界归一 |
| P2 | 建议修改 | `tests/integration/test_rag_m3.py::test_pit_filter_runs_on_server` | 未来事件只有 5 条，远小于单路召回上限 100，无法区分「服务端过滤」与「取回后过滤」 | **已修**：改为 120 条未来事件；真库用例通过 |
| P3 | 仅供参考 | `rag/retrieve.py::build_filter` | `as_of` 在 filter 内才 resolve；MCP 回显与两次纠错检索的 cutoff 可能不一致 | **记录**：本地语料日更，窗口极小；后续在边界只 resolve 一次 |
| P4 | 仅供参考 | `rag/retrieve.py::parse_as_of` | 仅日期解析为北京时间 00:00，不是收盘 15:00；当日事件不可见 | **记录**：PIT 收盘口径待拍板；当前偏保守、不泄漏 |
| P5 | 仅供参考 | `rag/retrieve.py::search` | 精排只捕 `RagNotReady`；运行期 OOM / 计算异常会整条检索失败 | **已修**：捕获 `Exception` 并降级；新增 `test_reranker_runtime_failure_also_degrades` |

#### 交易风控（8）

| # | 分级 | 位置 | 缺陷 | 处置 |
|---|---|---|---|---|
| R1 | **必须修复** | `paper/replay.py` 总仓位闸门 | 停牌 / 缺 bar 顺延的已批准买单不计入 `pending_notional`，多笔可越过 `max_gross_exposure` | **已修**：顺延 BUY 的名义金额并入当日 pending；**先证红（无 `gross_exposure` 事件）再证绿**；新增 `test_carried_over_buy_counts_toward_gross_exposure` |
| R2 | 建议修改 | `paper/replay.py::_propose` | 用户策略可自填 `Signal.origin`，伪装成 Agent 提案 / 风控自动单 | **已修**：来源由调用方给定（`replay(origin=...)`），Agent 路径传 `agent`；新增 `test_user_strategy_cannot_forge_origin` |
| R3 | 建议修改 | `paper/types.py::risk_event_id` | 审计 id 不含 `origin`，同日同标的策略 block 与 Agent block 撞 id，`ON CONFLICT DO NOTHING` 丢后一条 | **记录**：涉及审计 id 契约与既有行；R2 已让 origin 可信，单列后续 |
| R4 | 建议修改 | `api/paper.py::DECISION_LIMIT` | 500 同时是展示与重放输入上限；超长会话静默截断最老决策，权益/持仓失真 | **记录**：需分页或建会话时显式校验；超出本次文档范围 |
| R5 | 建议修改 | `docs/enterprise/risk.md` · `paper/replay.py` docstring | 一天顺序漏掉止损 / 回撤段 | **已修**：risk.md §3.1 与 replay.py docstring 改为四步 |
| R6 | 建议修改 | `tests/test_paper_risk_replay.py` | 回撤恢复的 `clear` 分支没有用例 | **记录**：测试缺口；补「跌破后再回升」的脚本用例 |
| R7 | 建议修改 | `paper/proposal.py` | `account_id` 不做 UUID 规范化，非规范写法参与 uuid5 | **记录**：合法大写 UUID 才触发、低影响；校验后统一小写 |
| R8 | 仅供参考 | `paper/proposal.py` | Agent 提案重放未过 `replay_gate()`，并发配额可绕过 | **记录**：闸门下沉到 `paper/core` 后复用 |

#### 鉴权 / 会话 / 配额（7）

| # | 分级 | 位置 | 缺陷 | 处置 |
|---|---|---|---|---|
| A1 | **必须修复** | `core/db_users.py::find_user_by_email` | 不 SELECT `role`，admin 登录响应回 `role: "user"` | **已修**：SELECT 加 `role`；真库用例 `test_login_returns_admin_role`（离线 fake 整行返回会掩盖投影错误） |
| A2 | 建议修改 | `core/session.py::_renewal_cookie` | 续期不带 `remember`，会话 cookie 被静默升级为持久 cookie | **已修**：token 带 `rem`，续期原样带回；新增 `test_renewal_preserves_session_cookie` |
| A3 | 建议修改 | `core/auth.py::decode_session` | 空 `JWT_SECRET` 抛 `InvalidKeyError`（非 `InvalidTokenError`），解码 500；register 先建用户再 500 | **已修**：捕获 `PyJWTError`；register/login 前置 503；扩展 `test_auth_endpoints_are_503_without_secret` |
| A4 | 建议修改 | `README.md` | 鉴权例外清单漏 `factor/report` 与 `/metrics` | **已修**：公开面逐项写明 |
| A5 | 仅供参考 | `api/idempotency.py` | 配额拒绝时事务外重算 `used`，429 文案与拒绝记录可能自相矛盾 | **记录**：竞态窗口确定、影响低；可 `max(used, limit)` |
| A6 | 仅供参考 | `core/reliability.py::Bucket` | 容量配 0 时除零，命中该桶的请求 500 | **记录**：0 非受支持取值；后续加 >0 校验或定义为不限额 |
| A7 | 仅供参考 | `docs/specs/P3-En.md` §3.2 | review / settle 仍写「不计数」，与 §8.10 冲突 | **已修**：§3.2 改为「按反思次数计 Agent 配额」 |

#### 沙箱 / 注入（6）

| # | 分级 | 位置 | 缺陷 | 处置 |
|---|---|---|---|---|
| S1 | 建议修改 | `strategy/sandbox.py` | 墙钟只包住 pipe pumps，`proc.wait()` 无超时；子进程关掉 stdout/stderr 后可挂死父进程 | **已修**：按剩余墙钟等 `proc.wait()`；新增 `test_wall_timeout_covers_a_child_that_closes_its_pipes` |
| S2 | 建议修改 | `mcp/server.py::_fit` | 12KB 硬上限只弹 hits；超长 query 回显可突破上限 | **已修**：query 回显 500 字上限 + `_fit` 兜底；新增 `test_long_query_echo_is_clipped_to_keep_payload_cap` |
| S3 | 仅供参考 | `strategy/api.py` | 预注入真实模块的 `__builtins__` 可绕 R1 与受限 builtins；`auto` 下可读 `.env`，`require` 下读数据自索引未来 bar | **记录**：命名空间已声明非安全边界，OS 隔离兜底；建议后续把 `module.__builtins__` 与 `__subclasses__` 并列写明 |
| S4 | 仅供参考 | `strategy/sandbox.py::_parse_envelope` | 父进程信任子进程「最后一行 JSON」，报告可被伪造 | **记录**：影响限于用户自己的报告；信任假设需在文档写明 |
| S5 | 仅供参考 | `core/untrusted.py::escape_delimiters` | 只转义精确字面量；`</untrusted_data >` 等空白 / 大小写变体不转义 | **记录**：低置信度、模型依赖；可改正则 |
| S6 | 仅供参考 | `agent/tools.py::query_market_bars` | 外部行情返回未包 `untrusted_data`，SPEC §8.5 排除表未列 | **记录**：纯数值无自由文本；建议 §8.5 补一行依据 |

### 4.3 无发现的面

以下是评审检查过、未发现问题的关键点（每条一句证据，避免「没写就是没查」）：

**PIT / RAG**

- PIT 过滤确在服务端：`build_filter` 无条件追加 `available_at <= resolve_as_of(as_of)`，并以 `query_filter=` 下推；RAG 路径没有取回后的本地过滤。
- RRF 的 `k` 显式传 61；精排失败按档位如实标 `score_kind`；零命中改写只换 query，`as_of` / 标的 / 事件类型 / `top_k` 逐字保留。
- 回测 PIT 用 `available_at`、非 PIT 用 `event_time`；两口径差异有符号、方向不预设；`BarContext.history` 是 `bars[:index+1]`，物理上拿不到未来 bar。

**交易风控**

- 四闸门判据与处置与 SPEC §6.1 一致：硬拦截 → 缩量 → 不足一手拦截；缩量公式逐字对应。
- 止损出生即 `approved`、审批端点只接受 `pending`；回撤只禁新开仓、恢复自动解禁；只有买入路径过闸门。
- 审计覆盖放行 / 缩量 / 拦截 / 触发 / breach / clear，`decision_audit` 是生成期事实的权威记录；风控关闭时「全部批准 ⇔ 一次回测」有逐笔相等用例。
- Agent 只写 `pending`、`origin=agent`；`approve="all"` 在有 Agent 待审批时 409；批量 SQL 再排除 `origin='agent'`（双保险）。

**鉴权 / 会话 / 配额**

- 越权与不存在同为 404；集中矩阵 23 例；admin 面只认角色且改库立即生效，角色不进 JWT。
- 续期三判据（成功响应 / 通过 `require_user` / 响应未自带 `Set-Cookie`）有真 SSE 路由用例；登出撤销只认当前 `token_version`。
- 配额两维语义正确：在飞任务直查 `tasks` 表（gauge），Agent 请求/日为 counter 且「检查 + 递增」同一条语句；429 契约与 `kind` 一致。
- L0 限流进程内令牌桶、429 带 `Retry-After`、SSE 开流前拒；请求体 1MB 覆盖 chunked；幂等键 `(user_id, key)` 无跨用户冲突；17 个路由逐一核对，无假鉴权旁路。

**沙箱 / 注入**

- `build_namespace()` 未被当作安全边界（四处声明一致）；`STRATEGY_ISOLATION=require` fail closed；环境变量清洗且不读仓库根 `.env`。
- 用户代码只在 worker 子进程执行；R2 未来函数有正/负样例；R3/R5 边界与文档一致；提交闸门顺序为静态检查 → 参数 → 入队/沙箱。
- 注入边界覆盖 SPEC §8.5 的五个面；只包 content、artifact 原样；块内内容一字不改。
- 对外 MCP 的 PIT 过滤在服务端、零命中改写保留 `as_of`、无网络暴露面（stdio 单工具、未被 `app.main` 挂载）。

## 5. 边界（AI 评审不能替代什么）

1. **不是安全证明**：注入边界、沙箱、会话这些面，评审只能证明「我们检查过的路径」；不能证明「没有未知路径」。
2. **不是全仓评审**：本轮只覆盖四个接缝；其余模块靠测试、CI 与日常交付中的两轴评审兜底。
3. **评审者与被评审者同源**：单人项目的 AI 评审没有独立第三方；结论按证据与用例采信，不按「评审意见」采信。
4. **不替代运行验证**：静态评审发现不了所有运行期问题；关键路径仍要真机取证、集成测试、演练。

## 6. 评审清单（可复用）

- [ ] 改动的「横切面」是否在所有端点形态上生效？（尤其是直出 `Response` 的 SSE）
- [ ] 错误分类是否只看结构属性，不 import 上游 SDK？
- [ ] 纯函数边界是否被破坏（读库 / 调模型 / 写状态）？
- [ ] 降级路径是否如实标记（`score_kind`、`degraded`、`errors`），而不是静默？
- [ ] 是否有「看起来过滤了、实际没过滤」的路径？
- [ ] 权限/越权：别人的资源与不存在是否同码？
- [ ] 安全：外部文本进 prompt 是否包边界？用户代码是否在沙箱？响应头是否走中间件？
- [ ] 测试：修复是否带回归用例？断言是否被改弱？
- [ ] 文档：结构性断言是否有 `文件 + 符号` 或 SPEC 节号？

---

**延伸阅读**：[架构设计](architecture.md) · [风控专题](risk.md) · [事故复盘](postmortem.md) · [P3 SPEC §5.7（故障注入）](../specs/P3-En.md)
