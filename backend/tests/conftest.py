"""pytest 全局配置。

放在任何 langgraph.checkpoint 导入之前锁死反序列化白名单（与 app/__init__.py 同源）。
"""

from __future__ import annotations

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
