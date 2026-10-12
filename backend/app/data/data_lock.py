"""数据目录的写入互斥：事件语料与行情整片**共用** `data/raw`。

两条通道（`etl/archive.py` 的按日归档、`data/bars.py` 的年度整片）落在同一个目录：
**同一份回执**（`.xiaoshi-history-state.json`）与**同一个 CLI 文件锁**
（`.xiaoshi-execution.lock`）。实测代价是具体的——10-12 夜里语料那轮把行情的 21 条
回执条目冲掉了（`same_publication` 为假时 `files_state` 清空重填）。

所以两个任务**必须串行**，光错峰不够（任一轮跑久了就会重叠）。本模块是**进程内**那一层；
跨进程由 CLI 自己的文件锁兜底（并发会报 `data_directory_busy`）。
**它是单进程 uvicorn 的互斥，不是分布式锁**——如实记，见 SPEC §3 M2d「不做」。
"""

from __future__ import annotations

import threading

#: 两个通道共用的一把锁。`app/etl/runner.py` 与 `app/data/bars.py` 都读它
LOCK = threading.Lock()


class Busy(RuntimeError):
    """已有一次数据同步在跑——并发跑会同时写同一批文件与同一份回执。"""


def is_busy() -> bool:
    """是否有同步在跑（端点据此回 409，状态页据此显示在跑）。"""
    return LOCK.locked()
