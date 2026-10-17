"""研报 PDF 导出（M7d）：**服务端渲染，一键出文件**。

为什么不是「前端打印样式」（SPEC v1.32 的原方案）：打印对话框不是导出——用户得在对话框里
再选一次存储位置；而且打印版式为了分页安全要退化成文档流，卡片内部会摊平（v1.37/v1.38 两轮
的来回就出在这里）。服务端渲染渲染的是**页面本身**，与屏幕逐像素同款，且一次点击直接下载。

三条设计：

1. **渲染器在子进程里**（`pdf_worker.py`，同策略沙箱的思路）：浏览器崩了不拖垮 API，
   超时可控，父进程只读一个临时文件；
2. **用本机已装的 Chrome**（`channel="chrome"`）而不是 `playwright install chromium`——
   实测零下载（前端那份 Playwright 1.63 与本机 Chrome 已在）；没有 Chrome 的环境回落到
   Playwright 自带浏览器（`channel=None`），文档里写明；
3. **打印页靠一次性令牌进**（`issue_export_token`）：渲染器没有会话 cookie，而报告是用户资产。
   令牌 = `HMAC(JWT_SECRET, report_id|exp)`，默认 10 分钟——**不新增密钥**，也不改变分享状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger(__name__)

#: 导出令牌有效期（秒）：够渲染器打开页面即可，越短越好
EXPORT_TTL_SECONDS = 600

#: 渲染超时（秒）：冷启动 Chrome 约 1–3s，RAG 预热那类大页面也远用不到 120s
RENDER_TIMEOUT_SECONDS = 120.0

#: 并发闸门按事件循环缓存（原因同 `api/paper.py::replay_gate`）。每次渲染是一个 Chrome，
#: 匿名分享链接也能触发——没有上限时几十个并发请求就能把单机内存打穿。
_RENDER_GATE: asyncio.Semaphore | None = None
_RENDER_GATE_LOOP: asyncio.AbstractEventLoop | None = None


def render_gate() -> asyncio.Semaphore:
    global _RENDER_GATE, _RENDER_GATE_LOOP
    loop = asyncio.get_running_loop()
    if _RENDER_GATE is None or _RENDER_GATE_LOOP is not loop:
        _RENDER_GATE = asyncio.Semaphore(max(1, get_settings().pdf_concurrency))
        _RENDER_GATE_LOOP = loop
    return _RENDER_GATE


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """杀整个进程组：Chrome 会 fork 出 renderer/zygote，只 kill worker 会留孤儿进程。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


class PdfRenderError(RuntimeError):
    """渲染失败（浏览器起不来 / 页面打不开 / 超时）。端点翻 500，detail 带原因。"""


def _secret() -> bytes:
    return get_settings().jwt_secret.get_secret_value().encode("utf-8")


def _digest(report_id: str, expires_at: int) -> str:
    return hmac.new(_secret(), f"{report_id}|{expires_at}".encode(), hashlib.sha256).hexdigest()


def issue_export_token(report_id: str, *, ttl: int = EXPORT_TTL_SECONDS, now: float | None = None) -> str:
    """签发导出令牌：`<过期时间戳>.<签名>`。**不落库**——它是一次性能力的自证。"""
    expires_at = int((now if now is not None else time.time()) + ttl)
    return f"{expires_at}.{_digest(report_id, expires_at)}"


def verify_export_token(
    report_id: str, token: str, *, now: float | None = None
) -> bool:
    """校验导出令牌：格式、有效期、签名三者都要过。**任何异常都返回 False**（不泄露细节）。"""
    try:
        raw_expires, signature = token.split(".", 1)
        expires_at = int(raw_expires)
    except (ValueError, AttributeError):
        return False
    if expires_at < int(now if now is not None else time.time()):
        return False
    return hmac.compare_digest(signature, _digest(report_id, expires_at))


def print_page_url(report_id: str, token: str, *, base_url: str | None = None) -> str:
    """渲染器要打开的地址：打印页 + 一次性令牌（令牌由后端签发，前端不参与）。"""
    base = (base_url or get_settings().frontend_base_url).rstrip("/")
    return f"{base}/print/{report_id}?t={token}"


async def render_report_pdf(
    report_id: str, *, base_url: str | None = None, timeout: float = RENDER_TIMEOUT_SECONDS
) -> bytes:
    """渲染打印页为 PDF 字节。**在子进程里跑**（`app.report.pdf_worker`），失败抛 `PdfRenderError`。

    **并发上限**：全局 `render_gate()`（默认 2）挡住「匿名分享链接/登录用户循环点导出」把
    单机内存打穿；**超时/异常都杀进程组**，Chrome 的 renderer/zygote 不留在后台。
    """
    url = print_page_url(report_id, issue_export_token(report_id), base_url=base_url)
    async with render_gate():
        with tempfile.TemporaryDirectory(prefix="quantsage-pdf-") as tmp:
            out = Path(tmp) / "report.pdf"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.report.pdf_worker",
                "--url",
                url,
                "--out",
                str(out),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # 自成进程组：超时/异常时能连 Chrome 子孙一起收
            )
            try:
                _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                _kill_tree(process)
                await process.wait()
                raise PdfRenderError(f"渲染超时（>{timeout:.0f}s）") from exc
            except BaseException:
                # 取消（客户端断连/服务停机）也要收掉浏览器，不能留孤儿
                _kill_tree(process)
                await process.wait()
                raise
            if process.returncode != 0:
                reason = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-1:] or [""]
                raise PdfRenderError(f"渲染失败（退出码 {process.returncode}）：{reason[0][:200]}")
            if not out.exists() or out.stat().st_size == 0:
                raise PdfRenderError("渲染进程没有产出文件")
            return out.read_bytes()
