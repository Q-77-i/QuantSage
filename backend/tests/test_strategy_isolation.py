"""用户策略 OS 隔离（安全修复）：命令构造 + 真沙箱的行为验证。

`require` 是生产默认：没有 sandbox-exec / bwrap 就拒绝执行。离线套件在 conftest 里把
默认改成 `auto`，所以 CI 上缺系统包不会整片红；本机（macOS）会真跑一次 sandbox-exec，
验证「读不到仓库根文件 / 连不上网 / 起不了子进程」这三条。Linux 有 bwrap 时同样可跑，
测试按 `sandbox_kind()` 自动选择。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.strategy import SandboxError, isolation
from app.strategy.sandbox import BACKEND_DIR

REPO_ROOT = BACKEND_DIR.parent


def _python_command(code: str) -> list[str]:
    return [sys.executable, "-B", "-s", "-c", code]


def test_require_refuses_when_no_sandbox(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(isolation, "sandbox_kind", lambda: None)
    with pytest.raises(SandboxError) as info:
        isolation.wrap_command(
            _python_command("print(1)"),
            backend_dir=BACKEND_DIR,
            data_dir=tmp_path,
            work_dir=tmp_path,
            mode="require",
        )
    assert "隔离不可用" in str(info.value)


def test_auto_falls_back_with_scrubbed_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(isolation, "sandbox_kind", lambda: None)
    command, env = isolation.wrap_command(
        _python_command("print(1)"),
        backend_dir=BACKEND_DIR,
        data_dir=tmp_path,
        work_dir=tmp_path,
        mode="auto",
    )
    assert command == _python_command("print(1)")
    assert env["QS_ENV_FILE"] == "/dev/null"
    assert env["DATA_DIR"] == str(tmp_path.resolve())
    assert "XIAOSHI_API_KEY" not in env and "DEEPSEEK_API_KEY" not in env


def test_off_still_scrubs_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XIAOSHI_API_KEY", "must-not-leak")
    _, env = isolation.wrap_command(
        _python_command("print(1)"),
        backend_dir=BACKEND_DIR,
        data_dir=tmp_path,
        work_dir=tmp_path,
        mode="off",
    )
    assert "XIAOSHI_API_KEY" not in env


def test_bwrap_env_uses_the_inner_tmpfs(monkeypatch, tmp_path: Path) -> None:
    """bwrap 的 `/tmp` 是 tmpfs；HOME/TMPDIR 必须是容器内路径，不能指向宿主 work_dir。"""
    monkeypatch.setattr(isolation, "sandbox_kind", lambda: "bwrap")
    monkeypatch.setattr(
        isolation.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
    )
    command, env = isolation.wrap_command(
        _python_command("print(1)"),
        backend_dir=BACKEND_DIR,
        data_dir=tmp_path,
        work_dir=tmp_path,
        mode="require",
    )
    assert command[0] == "/usr/bin/bwrap"
    assert env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp"


def test_bwrap_command_isolates_network_and_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(isolation.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    command = isolation._bwrap_command(
        _python_command("print(1)"), backend_dir=BACKEND_DIR, data_dir=tmp_path
    )
    joined = " ".join(command)
    assert "--unshare-all" in command and "--unshare-net" in command
    assert "--die-with-parent" in command
    assert f"--ro-bind {BACKEND_DIR} {BACKEND_DIR}" in joined
    assert f"--ro-bind {tmp_path.resolve()} {tmp_path.resolve()}" in joined


@pytest.mark.skipif(
    isolation.sandbox_kind() != "sandbox-exec",
    reason="需要 macOS sandbox-exec（其它平台由 bwrap 路径覆盖）",
)
def test_macos_sandbox_denies_repo_files_network_and_subprocess(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    code = f"""
import socket, subprocess
for path in [{str(REPO_ROOT / '.env')!r}, {str(REPO_ROOT / 'README.md')!r}]:
    try:
        open(path, "rb").read(1)
        print("READ_OK", path)
    except Exception as exc:
        print("READ_DENIED", path, type(exc).__name__)
try:
    subprocess.run(["/bin/echo", "x"], check=True)
    print("EXEC_OK")
except Exception as exc:
    print("EXEC_DENIED", type(exc).__name__)
try:
    socket.create_connection(("127.0.0.1", 5433), timeout=0.5)
    print("NET_OK")
except Exception as exc:
    print("NET_DENIED", type(exc).__name__)
"""
    command, env = isolation.wrap_command(
        _python_command(code),
        backend_dir=BACKEND_DIR,
        data_dir=data_dir,
        work_dir=work_dir,
        mode="require",
    )
    result = subprocess.run(
        command, cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert f"READ_DENIED {REPO_ROOT / '.env'}" in result.stdout
    assert f"READ_DENIED {REPO_ROOT / 'README.md'}" in result.stdout
    assert "EXEC_DENIED" in result.stdout
    assert "NET_DENIED" in result.stdout
