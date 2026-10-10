"""M6 模拟盘（回放式纸上交易）。

一句话形态：**模拟盘 = 回测引擎 + 审批闸门 + 账户结算**。撮合（`Broker`）、费用（`CostModel`）、
A 股规则（`a_share_rules`）、PIT 闸门（`EventFeed`）全部复用同一批对象；本包新增的只有两件
回测里没有的东西：**带审批状态的决策**与**多标的共享现金的账户**。

三条不可动摇的性质（SPEC §7）：

* 推进是**纯函数**：`replay(config, 决策日志, 数据)` 同一份输入必得同一个账户状态——幂等、可对账、可测试；
* 闸门不改价格口径：决策在 T 日收盘生成、成交在 T+1 开盘，**审批时成交价还不知道**；
* **未审批不成交**（PRD 硬要求）——推进越过成交窗口即为 `expired`。

轮次说明（规划底稿 F1）：行情是年度整片重下载、不日更，故「跟着真实日历滚」的实时形态
在当前数据通道上不成立，本包一律是**回放历史 bar**；调度器形态留 P3-E1。
"""

from __future__ import annotations

from app.backtest.engine import MAX_DEFER_BARS

#: 池子上限，沿用 M5b 批量回测的 1–20（同一批消费方，不另立数字）
MAX_SYMBOLS = 20

#: 区间上限（交易日数）。超出一律 422，不隐式截断——截断过的会话在界面上看不出来。
MAX_SESSIONS = 250

#: 停牌顺延上限：**直接复用引擎的常量**。抄一个 5 在这里，两处总会在某天悄悄漂移，
#: 而「同一场景两边结论不同」正是这类漂移最难查的形态。
MAX_DEFER_DAYS = MAX_DEFER_BARS


class PaperError(RuntimeError):
    """模拟盘的配置或数据不满足前提（池子为空、区间越界、数据未落盘等）→ 400。"""


class PaperConflict(PaperError):
    """状态冲突（决策不是待审批、会话已到末端、并发推进）→ 409。"""


__all__ = ["MAX_DEFER_DAYS", "MAX_SESSIONS", "MAX_SYMBOLS", "PaperConflict", "PaperError"]
