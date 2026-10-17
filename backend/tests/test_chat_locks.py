"""chat 会话锁表的生命周期（D#28）。"""

from __future__ import annotations

import gc

from app.api.chat import _RUN_LOCKS, _run_lock


def test_run_lock_is_reclaimed_when_no_one_holds_it() -> None:
    """弱引用：运行结束且无等待者后，锁自动从表里消失，不会按 thread 只增不减。"""
    lock = _run_lock("thread-for-weakref-test")
    assert _RUN_LOCKS.get("thread-for-weakref-test") is lock

    del lock
    gc.collect()

    assert _RUN_LOCKS.get("thread-for-weakref-test") is None
