"""小石 CLI 调用的**唯一入口**：起子进程、注入密钥、解析 JSON、识别限流。

两条通道（`etl/archive.py` 的事件归档、`data/bars.py` 的行情整片）共用同一份 CLI 与
同一个数据目录，**机制必须一致**——此前各写一份，代价已经出现过：行情那条路连
`timeout` 都没有，实测 10-12 手敲那轮卡在网络 20 分钟零写入。

这里只放**机制**，不放语义：「非零退出算不算错」由各通道自己决定——归档把 404 当
「这天没分片」，行情把它当失败。

三条硬规则（P1 沿用至今）：
* **密钥只走环境变量**：CLI 的 `--api-key` 会把密钥放进 argv（进程列表可见），一律不传；
* **子进程一律有超时**：超时即杀掉并抛 `CliTimeout`，绝不无限等；
* **限流是停止信号，不是单次失败**（`rate_limited_no_retry` / `HTTP 429` /
  `bulk_download_required`）：平台要求停下，重试只会把窗口越推越远。
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from app.core.config import get_settings

#: 单次调用的默认上限（秒）。实测行情整片单片 12–17s、归档单日更快；300s 是
#: 「慢」与「卡住」的分界，超了就是网络或平台的问题，杀掉重来强过干等。
DEFAULT_TIMEOUT_SECONDS = 300

#: 平台要求停下的信号（与 P1 的 MCP 拉取同族语义）
RATE_LIMIT_MARKERS = ("rate_limited_no_retry", "HTTP 429", "bulk_download_required")

_RETRY_AFTER = re.compile(r"Retry-After=(\d+)")


class CliError(RuntimeError):
    """CLI 不可用或返回了无法解释的结果。"""


class CliNotConfigured(CliError):
    """`XIAOSHI_CLI_COMMAND` 没配（见 `.env.example`）。"""


class CliTimeout(CliError):
    """单次调用超过上限——**子进程已被杀掉**，不是「还在跑」。"""


class RateLimited(CliError):
    """平台限流（429 / `rate_limited_no_retry` / `bulk_download_required`）。

    **这是停止信号，不是单次失败**：`Retry-After` 是平台明确给出的等待秒数，继续发请求
    只会把窗口越推越远（本项目的红线里也写着「429 按 Retry-After 停」）。调用方必须立刻
    中止整轮，而不是换下一个目标接着试。
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def cli_command() -> str:
    """CLI 的稳定入口路径（由安装器回填到 `.env`）。"""
    command = get_settings().xiaoshi_cli_command
    if not command:
        raise CliNotConfigured("XIAOSHI_CLI_COMMAND 未配置（见 .env.example）")
    return command


def run(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """跑一次 CLI，**密钥只经环境变量**。不判退出码——语义归调用方。"""
    env = {**os.environ, "XIAOSHI_API_KEY": get_settings().xiaoshi_api_key.get_secret_value()}
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else int(timeout)
    try:
        return subprocess.run(  # noqa: S603 —— argv 由调用方构造，密钥不进 argv
            [cli_command(), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=limit,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliTimeout(f"CLI 调用超过 {limit}s 未返回（已终止）：{' '.join(args)}") from exc


def load_json(text: str) -> dict:
    """CLI 的 stdout 里挑出 JSON 对象。

    CLI 会在正式输出前打印版本提示一类的行（实测「小石工具有新版本…」），所以从
    **第一个 `{`** 开始解析，而不是要求整段就是 JSON。
    """
    start = text.find("{")
    if start < 0:
        raise CliError(f"CLI 输出不是 JSON：{text[:200]}")
    try:
        return json.loads(text[start:])
    except ValueError as exc:
        raise CliError(f"CLI 输出无法解析为 JSON：{exc}") from exc


def rate_limit_hit(output: str) -> bool:
    """输出里是否出现平台的停止信号。"""
    return any(marker in output for marker in RATE_LIMIT_MARKERS)


def retry_after(output: str) -> int | None:
    """从 `Retry-After=<秒>` 里取出平台给的等待秒数。"""
    match = _RETRY_AFTER.search(output)
    return int(match.group(1)) if match else None


def tail(result: subprocess.CompletedProcess[str]) -> str:
    """失败时的可读摘要（优先 stderr，都没有就取 stdout 尾部）。"""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text[-300:]
