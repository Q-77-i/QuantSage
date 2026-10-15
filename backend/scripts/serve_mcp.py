"""对外 MCP Server 的启动壳（E8b，SPEC §8.6）。

MCP 客户端用**绝对路径**拉起它（`.mcp.json` / `claude_desktop_config.json`）：

    command: /…/QuantSage/backend/.venv/bin/python
    args:    ["/…/QuantSage/backend/scripts/serve_mcp.py"]

**为什么要有这层壳**：直接 `-m app.mcp.server` 要求客户端能设好工作目录或 `PYTHONPATH`，
而 MCP 配置里这两样不通用；脚本把 `backend/` 自己放上 `sys.path` 就与客户端的 cwd 无关
（与 `verify_xiaoshi_mcp.py` 同一手法）。

**stdio 纪律**：stdout 归 JSON-RPC，日志走 stderr——这层壳里一个字都不能往 stdout 写。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp.server import main  # noqa: E402 —— 必须在 sys.path 就位之后

if __name__ == "__main__":
    main()
