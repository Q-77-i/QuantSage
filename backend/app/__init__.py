"""app 包初始化。

`LANGGRAPH_STRICT_MSGPACK` 是 langgraph.checkpoint 在 import 期读取的模块级常量，
一旦 checkpoint 模块被导入就再也改不动；而 `uv run` 不会自动加载 .env。
因此在这里硬编码兜底，保证任何入口（脚本 / pytest / uvicorn）都先锁死反序列化白名单。
"""

from __future__ import annotations

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

STRICT_MSGPACK_ENFORCED: bool = os.environ["LANGGRAPH_STRICT_MSGPACK"].lower() in (
    "1",
    "true",
    "yes",
)

__all__ = ["STRICT_MSGPACK_ENFORCED"]
