"""用户策略子进程的 OS 级隔离。

**为什么需要这一层**：`build_namespace()` 只是「手滑与无意绕行」的闸门，不是安全边界——
属性访问本身不受限，`object.__subclasses__()` 就能拿到 `os`。把用户代码放在同 UID、同文件系统、
同网络的子进程里，恶意策略可以直接读 `.env`、连业务库、起任意进程。CPU/内存/墙钟配额只管
单个进程的资源，管不住这些。

三层姿态（`STRATEGY_ISOLATION`）：

* `require`（默认）：必须有可用的 OS 沙箱，否则**拒绝执行**用户策略（fail closed）。
* `auto`：没有沙箱时降级为「清洗环境变量后直接跑」，并明确告警——只给本地调试/无沙箱 CI。
* `off`：显式关闭 OS 隔离，仍然清洗环境变量；不推荐，且每次运行都会告警。

平台实现：

* macOS：`sandbox-exec` + 读白名单 profile。只有 Python 解释器可 exec；写只允许本次运行的
  临时目录；网络全部拒绝；`backend/`、`data/`、venv 与 Python 安装目录只读。
  **仓库根的 `.env` 不在白名单里**——这正是要挡的东西。
* Linux：`bubblewrap`（bwrap）。`--unshare-all` + `--unshare-net`，只读绑定系统目录、后端代码、
  venv 与数据目录，`/tmp` 用 tmpfs，`--die-with-parent`。
* 其它平台：`require` 直接拒绝；`auto`/`off` 降级。

环境变量在**所有模式**下都会清洗：不继承 `XIAOSHI_API_KEY` / `DEEPSEEK_API_KEY` /
`POSTGRES_PASSWORD` 等宿主变量；`QS_ENV_FILE=/dev/null` 让子进程不读仓库根的 `.env`
（`Settings` 的配置文件路径支持这个覆盖，见 `core/config.py`）。
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from app.core.config import get_settings
from app.strategy import SandboxError

log = logging.getLogger(__name__)

#: 允许的配置值（与 `Settings.strategy_isolation` 同源）
MODES = ("require", "auto", "off")

#: 子进程的 `PATH`：只要系统目录，不把 venv/bin 或宿主 PATH 带进去
_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: 系统只读目录（macOS 读白名单用）。`/` 单独用 literal，避免 `subpath "/"` 的歧义。
_SYSTEM_READ_PATHS = (
    "/usr",
    "/System",
    "/Library",
    "/private/etc",
    "/private/var/db",
    "/dev",
)


class IsolationUnavailable(SandboxError):
    """当前机器没有可用的 OS 沙箱，而配置要求必须隔离。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="crash")


def sandbox_kind() -> str | None:
    """当前平台可用的隔离实现：`sandbox-exec` / `bwrap` / None（只做探测，不执行）。"""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def _quote(value: str | Path) -> str:
    """sandbox profile 的字符串字面量：路径来自配置，仍按规则转义引号与反斜杠。"""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _python_read_paths() -> list[Path]:
    """Python 解释器自身要读的路径（解释器、标准库、venv、uv 的版本根）。

    `.venv/bin/python` 常是符号链接（uv 会把 `cpython-3.12` 指向 `cpython-3.12.13`），
    所以 symlink 路径与 realpath 都要给；版本根的**父目录**也一起给，避免小版本改名后失效。
    """
    executable = Path(sys.executable)
    base = Path(sys.base_prefix)
    prefix = Path(sys.prefix)
    candidates = {
        executable,
        executable.resolve(),
        executable.parent,
        executable.resolve().parent,
        prefix,
        prefix.resolve(),
        base,
        base.resolve(),
        base.parent,
    }
    return sorted(candidates)


def _macos_profile(*, backend_dir: Path, data_dir: Path, work_dir: Path) -> str:
    read_paths = [
        *(_SYSTEM_READ_PATHS),
        str(backend_dir),
        str(data_dir),
        str(work_dir),
        *(str(path) for path in _python_read_paths()),
    ]
    reads = " ".join(f"(subpath {_quote(path)})" for path in read_paths)
    executable = Path(sys.executable)
    execs = " ".join(
        f"(literal {_quote(path)})" for path in {executable, executable.resolve()}
    )
    return f"""(version 1)
(deny default)
(deny network*)
(allow sysctl-read)
(allow mach-lookup)
(allow process-exec {execs})
(allow file-read* (literal "/") {reads})
(allow file-write* (subpath {_quote(work_dir)}))
"""


def _bwrap_command(
    command: list[str], *, backend_dir: Path, data_dir: Path
) -> list[str]:
    """Linux：只读绑定必要路径，`/tmp` 用 tmpfs，网络与 PID/用户命名空间全隔离。"""
    bwrap = shutil.which("bwrap")
    assert bwrap is not None  # 调用方已探测
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-net",
    ]
    # 系统目录：存在才绑（不同发行版布局不同）
    for directory in ("/usr", "/lib", "/lib64", "/etc"):
        if Path(directory).exists():
            args.extend(["--ro-bind", directory, directory])
    # 后端代码（含 venv）、数据目录、Python 安装目录：只读
    for directory in (
        backend_dir,
        data_dir,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.base_prefix).resolve(),
    ):
        if Path(directory).exists():
            args.extend(["--ro-bind", str(directory), str(directory)])
    args.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
    args.extend(["--chdir", str(backend_dir), "--"])
    args.extend(command)
    return args


def _base_env(work_dir: Path, data_dir: Path) -> dict[str, str]:
    """子进程的最小环境：不继承宿主密钥；也不让它去读仓库根的 `.env`。"""
    return {
        "PATH": _SAFE_PATH,
        "HOME": str(work_dir),
        "TMPDIR": str(work_dir),
        "TMP": str(work_dir),
        "TEMP": str(work_dir),
        "TZ": "Asia/Shanghai",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        # 让子进程的 Settings 读一个空文件，而不是仓库根的 .env（见 core/config.py）
        "QS_ENV_FILE": "/dev/null",
        # 显式给数据目录：沙箱里拿不到 .env 时，取数仍走这里
        "DATA_DIR": str(data_dir),
    }


def wrap_command(
    command: list[str],
    *,
    backend_dir: Path,
    data_dir: Path,
    work_dir: Path,
    mode: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """返回 `(要执行的 argv, 子进程环境)`。

    `mode` 缺省取 `settings.strategy_isolation`；显式传入只为用例可测。
    """
    configured = (mode or get_settings().strategy_isolation).strip().lower()
    if configured not in MODES:
        raise IsolationUnavailable(f"未知的 STRATEGY_ISOLATION 取值：{configured!r}")
    backend_dir = backend_dir.resolve()
    data_dir = data_dir.resolve()
    work_dir = work_dir.resolve()
    env = _base_env(work_dir, data_dir)
    original = list(command)

    if configured == "off":
        log.warning("用户策略 OS 隔离已关闭（STRATEGY_ISOLATION=off）——仅限可信环境")
        return original, env

    kind = sandbox_kind()
    if kind == "sandbox-exec":
        profile = work_dir / "strategy.sandbox"
        profile.write_text(
            _macos_profile(backend_dir=backend_dir, data_dir=data_dir, work_dir=work_dir),
            encoding="utf-8",
        )
        return ["sandbox-exec", "-f", str(profile), *original], env
    if kind == "bwrap":
        # bwrap 里 /tmp 是 tmpfs；`work_dir` 是宿主路径，不能直接当 HOME/TMPDIR
        env = {**env, "HOME": "/tmp", "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"}
        return _bwrap_command(original, backend_dir=backend_dir, data_dir=data_dir), env

    message = (
        "用户策略隔离不可用：当前平台没有 sandbox-exec（macOS）或 bwrap（Linux）。"
        "安装 bubblewrap 后重试；确认环境可信时才能显式设置 STRATEGY_ISOLATION=off"
    )
    if configured == "auto":
        log.warning("%s——本次降级为无 OS 隔离执行", message)
        return original, env
    raise IsolationUnavailable(message)


__all__ = [
    "IsolationUnavailable",
    "MODES",
    "sandbox_kind",
    "wrap_command",
]
