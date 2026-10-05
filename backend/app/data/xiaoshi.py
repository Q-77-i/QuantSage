"""小石数据源接入。

T1 形态：只做「路径校验 + 连接参数构造」，不 spawn 子进程、不联网。
密钥只经子进程环境变量传递，绝不进 argv；调用方打印连接参数前必须掩码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

MCP_ARGS: tuple[str, ...] = ("--transport", "stdio")
MCP_SERVER_NAME = "xiaoshi"


class XiaoshiNotInstalled(RuntimeError):
    """小石工具包未安装或 .env 未回填稳定入口路径。"""


@dataclass(frozen=True)
class XiaoshiEndpoints:
    mcp_command: Path
    cli_command: Path


def endpoints() -> XiaoshiEndpoints:
    settings = get_settings()
    return XiaoshiEndpoints(
        mcp_command=Path(settings.xiaoshi_mcp_command).expanduser(),
        cli_command=Path(settings.xiaoshi_cli_command).expanduser(),
    )


def assert_installed() -> XiaoshiEndpoints:
    """校验两个稳定入口存在且可执行；失败时给出可读诊断（不猜安装器布局）。"""
    eps = endpoints()
    problems: list[str] = []
    for label, path in (
        ("XIAOSHI_MCP_COMMAND", eps.mcp_command),
        ("XIAOSHI_CLI_COMMAND", eps.cli_command),
    ):
        if not str(path) or str(path) == ".":
            problems.append(f"{label} 未在 .env 配置")
        elif not path.is_file():
            problems.append(f"{label} 指向的文件不存在：{path}")
        elif not os.access(path, os.X_OK):
            problems.append(f"{label} 不可执行：{path}")

    if not get_settings().xiaoshi_api_key.get_secret_value():
        problems.append("XIAOSHI_API_KEY 未在 .env 配置")

    if problems:
        raise XiaoshiNotInstalled("小石工具包未就绪：\n  - " + "\n  - ".join(problems))
    return eps


def mcp_stdio_params() -> dict:
    """给 `mcp` SDK 的 stdio_client 用（协议层验收走这条）。"""
    eps = assert_installed()
    return {
        "command": str(eps.mcp_command),
        "args": list(MCP_ARGS),
        "env": {
            **os.environ,
            "XIAOSHI_API_KEY": get_settings().xiaoshi_api_key.get_secret_value(),
        },
    }


def adapter_connection() -> dict:
    """给 langchain-mcp-adapters 的 MultiServerMCPClient 用（T3 生产路径）。"""
    return {MCP_SERVER_NAME: {"transport": "stdio", **mcp_stdio_params()}}
