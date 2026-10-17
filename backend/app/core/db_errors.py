"""业务库的领域异常（端点按类型映射 HTTP 状态）。"""

from __future__ import annotations


class EmailTaken(RuntimeError):
    """邮箱已被注册（`users.email` 唯一约束）。"""


class SymbolTracked(RuntimeError):
    """该标的已在本人的自选股里（`watchlist` 的 `UNIQUE(user_id, symbol)`）。"""


class StrategyNameTaken(RuntimeError):
    """策略名已被本人占用（`strategies` 的 `UNIQUE(user_id, name)`）。"""


class StrategyVersionConflict(RuntimeError):
    """策略的 `expected_version` 与库里不一致（并发保存）→ 409。

    传的是「用户打开编辑器时看到的版本」；别人先保存了，就拒绝这次写入，
    不做事后覆盖（覆盖会静默丢掉别人的改动）。
    """
