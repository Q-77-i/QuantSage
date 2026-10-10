"""PDF 渲染**子进程**（M7d）：打开打印页 → 等报告渲染完 → 存 PDF。

单独一个进程的三条理由（同策略沙箱）：① 浏览器崩了不拖垮 API；② 超时由父进程控；
③ 同步 API 不必和 asyncio 事件循环纠缠。

用**本机已装的 Chrome**（`channel="chrome"`）：实测零下载。没有 Chrome 的环境（如干净的
CI 机器）自动回落到 Playwright 自带浏览器——那需要先跑一次 `playwright install chromium`，
缺了会在 stderr 里给出可读的原因。

用法（父进程调）：`python -m app.report.pdf_worker --url <打印页> --out <pdf>`
退出码：0 成功；1 失败（原因进 stderr 最后一行）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def render(url: str, out: Path, *, timeout_ms: int = 60_000) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome")
        except Exception:  # noqa: BLE001 —— 没装 Chrome 就用 Playwright 自带的
            browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # 报告是客户端取数渲染的：等块出来再印，否则会印出一张空壳
            page.wait_for_function(
                "() => document.querySelectorAll('[data-block-id]').length >= 5",
                timeout=timeout_ms,
            )
            page.emulate_media(media="print")
            page.wait_for_timeout(300)  # 让字体与画布落定
            page.pdf(
                path=str(out),
                format="A4",
                print_background=False,
                margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
            )
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染研报 PDF")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        render(args.url, Path(args.out))
    except Exception as exc:  # noqa: BLE001 —— 原因交给父进程转成人话
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
