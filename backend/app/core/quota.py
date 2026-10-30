"""用户配额（E2）：两个维度的记账与拒绝。

与 L0 限流（`core/reliability.py` 的令牌桶）的边界**写死在 SPEC §5.4 / §3.1**：
L0 管**速率**（每分钟多少，进程内、重启清零），本模块管**累计用量**（Postgres、跨重启）。

两个维度、两种语义（不硬凑成一套）：

  * **在飞任务数 = gauge**——直查 `tasks` 表，进终态**自动**释放。不另立计数器：终结有五条
    路径（成功 / 失败 / 取消 / 启动扫尾 / 进程被杀），计数器漏减一处就是永久占额，
    而直查以任务表为唯一真值、天然不漂；
  * **Agent 请求数 / 日 = counter**——`usage_counters` 表 + 「检查与递增同一条语句」
    （见 `Database.bump_usage_counter`：并发下才不会超发）。

口径三条（SPEC §3.1，都是**有意**的，别当 bug 修）：

  * **计「发起」不计「成功」**——后续失败不退额。退还要么在多条失败出口各加一处回滚、
    要么引入对账；而配额防的是刷量，计发起已经防住；
  * **上限 ≤ 0 = 不限额**（跳过整条检查），admin 恒不限额；
  * **未知维度名直接报错**——静默放行会造出一处「看起来在管、其实没管」的洞。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.auth import ROLE_ADMIN, ROLE_USER
from app.core.config import get_settings

#: 自然日的时区。仓里几处调度器各写一份同名常量（`etl/scheduler.py` / `memory/scheduler.py`），
#: 这里沿用同法——不为了一个常量把调度器模块拖进请求路径。
CN_TZ = ZoneInfo("Asia/Shanghai")

#: 维度名（落库进 `usage_counters.metric` / `quota_rejections.metric`，改它等于改数据）。
METRIC_AGENT_CALLS = "agent_calls"
METRIC_ACTIVE_TASKS = "active_tasks"


class QuotaExceeded(Exception):
    """用户配额用尽（E2）。由 `main.py` 翻成 **429 + `kind="quota_exceeded"`**。

    与 L0 的 `RateLimitedError` **同码不同 kind**：全仓「拒绝再来」的语义统一在 429，
    前端只认 `detail`（`messageFromBody`），多一套状态码只会多一套分支。
    """

    kind = "quota_exceeded"

    def __init__(
        self,
        metric: str,
        message: str,
        *,
        limit: int,
        used: int,
        reset_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.metric = metric
        self.limit = limit
        self.used = used
        #: 计数器的归零时刻（次日 00:00 +08）；gauge 没有它——不由时钟释放。
        self.reset_at = reset_at


class TaskAdmissionBusy(Exception):
    """槽位准入撞车用尽重试：**有空槽但没抢到**，不是配额满。

    429 `quota_exceeded` 的意思是“等一个任务结束再来”；这里是瞬时并发，客户端应
    带 `Retry-After` 短暂重试。由 `main.py` 映射成 **503 + Retry-After: 1**。
    """

    kind = "task_admission_busy"


def period_key(now: datetime | None = None) -> str:
    """计数器的周期键：Asia/Shanghai 的**自然日**。换天即换键，不需要定时清理。"""
    moment = now or datetime.now(CN_TZ)
    return moment.astimezone(CN_TZ).date().isoformat()


def day_start(now: datetime | None = None) -> datetime:
    """当期（自然日）的起点：今日 00:00 +08。

    `rejected_today` 这类**按事件时刻**过滤的读数用它——拒绝表记的是事件，
    没有 `period_key` 列（它不是计数，见 SPEC §3.5）。
    """
    moment = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    return datetime.combine(moment.date(), time.min, tzinfo=CN_TZ)


def window(days: int, now: datetime | None = None) -> tuple[str, str, datetime]:
    """配额视图的时间窗（E2c 追加）：返回 `(起始 period_key, 结束 period_key, 起始时刻)`。

    `days=1` 就是「今日」——与 E2a 的计数口径同一个键。跨月/跨年由日期运算兜住
    （`period_key` 是 ISO 文本，区间比较天然按字典序正确）。
    """
    anchor = day_start(now)
    since = anchor - timedelta(days=max(1, days) - 1)
    return period_key(since), period_key(anchor), since


def reset_at(now: datetime | None = None) -> str:
    """下一次归零的时刻（次日 00:00 +08）——进拒绝体，让用户知道要等多久。"""
    return (day_start(now) + timedelta(days=1)).isoformat()


def active_task_limit(user: dict[str, Any]) -> int | None:
    """在飞任务上限；`None` = 不限额（admin 或配置 ≤ 0）。

    这个值交给 `Database.create_task` 在**插入语句里**做条件判断——先 `count` 再 `insert`
    的两次调用之间有竞态，并发提交可以一起越过上限（E2 的 gauge 不是计数器，不能靠
    进程内状态兜底）。
    """
    if str(user.get("role") or ROLE_USER) == ROLE_ADMIN:
        return None
    limit = get_settings().quota_active_tasks
    return limit if limit > 0 else None


async def enforce(
    db: Any, user: dict[str, Any], metric: str, *, now: datetime | None = None
) -> None:
    """记账；超限抛 `QuotaExceeded`。

    **调用点规则**（SPEC §3.2）：客户端错误（401 / 404 / 422）已排除、服务已就绪之后，
    任何 LLM 调用或入队之前——即「**这个请求确定要被服务**」的那一刻。五个端点各自的位置
    写在各自的注释里；配额**不做成依赖**，因为依赖跑在 handler 之前，会挡住幂等重放
    （重复提交不建任务，也就不该被挡）与请求体校验（非法请求该拿 422 而不是 429）。
    """
    if str(user.get("role") or ROLE_USER) == ROLE_ADMIN:
        return  # admin 不限额

    user_id = int(user["id"])
    settings = get_settings()

    if metric == METRIC_ACTIVE_TASKS:
        limit = settings.quota_active_tasks
        if limit <= 0:
            return
        used = await db.count_active_tasks(user_id)
        if used >= limit:
            await db.record_quota_rejection(user_id, metric, used, limit)
            raise QuotaExceeded(
                metric,
                f"你已有 {used} 个任务在执行或排队（上限 {limit} 个），等一个结束再提交",
                limit=limit,
                used=used,
            )
        return

    if metric == METRIC_AGENT_CALLS:
        limit = settings.quota_agent_calls_per_day
        if limit <= 0:
            return
        used = await db.bump_usage_counter(user_id, metric, period_key(now), limit)
        if used is None:
            # `WHERE used < limit` 挡住的正是这一次递增 ⇒ 被拒的这次**没计数**，已用恒等于上限
            await db.record_quota_rejection(user_id, metric, limit, limit)
            raise QuotaExceeded(
                metric,
                f"今日 Agent 调用已用满 {limit} 次，明日 00:00 重置（Asia/Shanghai）",
                limit=limit,
                used=limit,
                reset_at=reset_at(now),
            )
        return

    raise ValueError(f"未知的配额维度：{metric}")
