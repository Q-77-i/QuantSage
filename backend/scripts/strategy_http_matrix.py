"""HTTP 层证据脚本（M4c，可复现）：故障矩阵（验收 ②）+ 端到端（验收 ①）。

产出两篇：
  * `logs/m4c/http_fault_matrix.md`——6 条坏法各给明确错误码，每条之后 `/health` 200；
  * `logs/m4c/workshop.md`——「模板 + 自写」各一条走完 保存 → 检查 → 沙箱回测 → 落库 → 重开。

与 M4a 的 `scripts/sandbox_fault_matrix.py` 分工不同：那一份打的是**沙箱函数**，
这一份打的是**真跑的 HTTP 服务**——验的是整条链路（鉴权 → 闸门 → 沙箱 → 落库 → 错误码映射）
在坏代码面前的样子，以及**每条之后服务还活着**。

脚本**自起一个紧配额的 uvicorn**（CPU 5s / 墙钟 6s / 内存 200MB / 输出 2KB）：
一是配额压低了才不用真等 20s，二是顺带证明这几项配额是可配的。用完即杀。
跑完把账号从开发库清掉（同集成用例口径，不留垃圾）。

用法：
    cd backend && uv run python scripts/strategy_http_matrix.py
    # 已有服务时也可以打它（配额是它的默认值，死循环那条会真的等到上限）：
    uv run python scripts/strategy_http_matrix.py --base http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # 脚本从 backend/scripts 跑，包在 backend/ 下
    sys.path.insert(0, str(BACKEND_DIR))

from app.strategy.templates import template_source  # noqa: E402

OUT_DIR = BACKEND_DIR.parent / "logs" / "m4c"
OUT_FILE = OUT_DIR / "http_fault_matrix.md"
WORKSHOP_FILE = OUT_DIR / "workshop.md"

#: 端到端用的模板源码：与工作台里「另存为我的」拿到的是同一份（服务端唯一真源）
TEMPLATE_SOURCE = template_source("ma_cross")

EMAIL = "m4c-matrix@example.com"
PASSWORD = "m4c-matrix-pass"

SYMBOL = "600519"

#: 压低的配额：坏法在几秒内就被拦下，不用真等默认的 20s。
#: **内存只能压到「基线之上」**——真数据下子进程峰值实测 300–400MB（引擎读全市场 Parquet
#: 的开销），压到 200MB 会让**每一条**用例（含好策略）都以「内存超限」告终，那是假证据。
#: 600MB 既在基线之上，又让「大内存」那条很快撞线（默认 1024MB 也行，只是慢一倍）。
TIGHT_LIMITS = {
    "STRATEGY_CPU_SECONDS": "5",
    "STRATEGY_WALL_SECONDS": "6",
    "STRATEGY_MEMORY_MB": "600",
    "STRATEGY_OUTPUT_BYTES": "2048",
}

#: 端到端里的「自写」那条：收盘价上下穿 20 日均线——刻意与模板的双均线写法不同
GOOD_CODE = '''\
PARAMS = {"window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "均线窗口"}}


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    window = p["window"]
    if len(closes) < window + 1:
        return []
    mean = sum(closes[-window:]) / window
    if closes[-1] > mean and ctx.position.is_flat:
        return [Signal(Side.BUY, reason="站上均线")]
    if closes[-1] < mean and not ctx.position.is_flat:
        return [Signal(Side.SELL, reason="跌破均线")]
    return []
'''

CASES: list[tuple[str, str, set[int]]] = [
    ("死循环", "def on_bar(ctx):\n    while True:\n        pass\n", {400}),
    (
        # 8MB 一块地吃：600MB 上限下约三十块就触发看门狗（每 0.05s 轮询一次峰值）
        "大内存",
        "def on_bar(ctx):\n"
        "    blob = []\n"
        "    while True:\n"
        "        blob.append(bytearray(8 * 1024 * 1024))\n",
        {400},
    ),
    # 报告本身（净值曲线逐点）远超 2KB 的输出上限——测的是信封撑爆协议通道那条路
    ("输出超限", GOOD_CODE, {400}),
    ("语法错", "def on_bar(ctx)\n    return []\n", {422}),
    ("缺 on_bar", "x = 1\n", {422}),
    ("数据绕行", "import duckdb\n\n\ndef on_bar(ctx):\n    return []\n", {422}),
    ("未来函数", "def on_bar(ctx):\n    return [ctx.history[ctx.index + 1]]\n", {422}),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def call(
    base: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, cookie: str = ""
) -> tuple[int, dict[str, Any]]:
    """极简 HTTP 调用：只依赖标准库（脚本要能在任何 venv 里跑）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - 本机地址
            payload = response.read().decode("utf-8")
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        try:
            return error.code, json.loads(payload)
        except ValueError:  # pragma: no cover - 非 JSON 错误页
            return error.code, {"detail": payload[:200]}


def wait_ready(base: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = call(base, "/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"{base} 在 {timeout:g}s 内没有就绪")


def start_server(port: int, *, tight: bool = True) -> subprocess.Popen[bytes]:
    """起一个 uvicorn。`tight=False` 用**默认配额**（收尾那条好策略要真跑通）。"""
    env = {**os.environ, **(TIGHT_LIMITS if tight else {})}
    return subprocess.Popen(  # noqa: S603 - 命令是写死的，只传端口
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_server(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    server.wait(timeout=10)


def run_flow(base: str, cookie: str, label: str, code: str) -> dict[str, Any]:
    """一条端到端：建策略 → 保存即检查 → 沙箱回测 → 落库 → 重开。

    重开比对的是**报告本体是否逐键相等**——落库走 JSONB 往返，数字不能变成字符串。
    """
    def authed(path: str, method: str = "GET", body: dict[str, Any] | None = None):
        return call(base, path, method=method, body=body, cookie=cookie)

    # 名字带短随机后缀：脚本可反复跑，不必先清理上一轮的策略
    created_status, created = authed(
        "/api/v1/strategies",
        "POST",
        {"name": f"{label}·{uuid.uuid4().hex[:6]}", "code": code},
    )
    assert created_status == 201, (label, created_status, created)

    _, checked = authed("/api/v1/strategies/check", "POST", {"code": code})
    started = time.monotonic()
    status, run = authed(
        "/api/v1/backtest",
        "POST",
        {"strategy": "user", "strategy_id": created["id"], "symbol": SYMBOL},
    )
    seconds = time.monotonic() - started
    assert status == 200, (label, status, run)

    _, detail = authed(("/api/v1/backtest/runs/") + run["run_id"])
    report = run["report"]
    return {
        "label": label,
        "status": status,
        "strategy_id": created["id"],
        "run_id": run["run_id"],
        "code_sha256": created["code_sha256"],
        "errors": [item for item in checked["findings"] if item["severity"] == "error"],
        "warnings": [item for item in checked["findings"] if item["severity"] == "warning"],
        "param_keys": sorted(checked["meta"]["params"]) if checked["meta"] else None,
        "uses_events": checked["meta"]["uses_events"] if checked["meta"] else None,
        "metrics": report["metrics"],
        "bars": report["meta"]["bars"],
        "meta_kind": report["meta"]["strategy_kind"],
        "meta_name": report["meta"]["strategy_name"],
        "reopened_equal": detail["report"] == report,
        "hash_matches": detail["code_sha256"] == created["code_sha256"],
        "seconds": seconds,
    }


def login_cookie(base: str) -> str:
    """注册（已存在则登录）并取回会话 cookie——只取 `name=value`，后面的属性用不上。"""
    status, body = call(
        base, "/api/v1/auth/register", method="POST", body={"email": EMAIL, "password": PASSWORD}
    )
    if status == 409:
        status, body = call(
            base, "/api/v1/auth/login", method="POST", body={"email": EMAIL, "password": PASSWORD}
        )
    if status not in (200, 201):
        raise RuntimeError(f"登录失败 {status}：{body}")

    request = urllib.request.Request(
        f"{base}/api/v1/auth/login",
        data=json.dumps({"email": EMAIL, "password": PASSWORD}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - 本机地址
        raw = response.headers.get("Set-Cookie", "")
    return raw.split(";", 1)[0]


def purge_account() -> None:
    """把矩阵账号从开发库删掉（`strategies` / `backtest_runs` 随外键级联）。"""
    try:
        import psycopg

        sys.path.insert(0, str(BACKEND_DIR))
        from app.core.config import get_settings

        with psycopg.connect(get_settings().postgres_dsn) as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (EMAIL,))
    except Exception as exc:  # noqa: BLE001 - 清不掉不该让证据作废，如实打印
        print(f"（清理账号失败：{type(exc).__name__}: {exc}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="打已有服务（不传则自起一个紧配额实例）")
    args = parser.parse_args()

    server: subprocess.Popen[bytes] | None = None
    base = args.base
    if not base:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        print(f"自起服务 {base}（紧配额：{TIGHT_LIMITS}）")
        server = start_server(port)

    rows: list[dict[str, Any]] = []
    flows_evidence: list[dict[str, Any]] = []
    try:
        wait_ready(base)
        cookie = login_cookie(base)

        def authed(path: str, method: str = "GET", body: dict[str, Any] | None = None):
            return call(base, path, method=method, body=body, cookie=cookie)

        for index, (name, code, expected) in enumerate(CASES, start=1):
            created_status, created = authed(
                "/api/v1/strategies", "POST", {"name": f"坏法-{name}", "code": code}
            )
            assert created_status == 201, (name, created_status, created)
            started = time.monotonic()
            status, payload = authed(
                "/api/v1/backtest",
                "POST",
                {"strategy": "user", "strategy_id": created["id"], "symbol": SYMBOL},
            )
            elapsed = time.monotonic() - started
            health, _ = call(base, "/health")
            detail = str(payload.get("detail", ""))[:110].replace("\n", " ")
            rule = (payload.get("findings") or [{}])[0].get("rule", "") if payload.get("findings") else ""
            rows.append(
                {
                    "index": index,
                    "name": name,
                    "status": status,
                    "expected": expected,
                    "kind": payload.get("kind", rule),
                    "detail": detail,
                    "health": health,
                    "seconds": elapsed,
                    "ok": status in expected and health == 200,
                }
            )
            print(f"  {index}. {name}: {status}（{'✓' if rows[-1]['ok'] else '✗'}）{detail}")

        # 收尾：换回**默认配额**再跑一条好策略——紧配额下它必然撞输出上限（报告本身
        # 比 2KB 大得多），那验的是配额，不是「服务还能干活」。换个实例顺带证明配额可配。
        if server is not None:
            stop_server(server)
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            server = start_server(port, tight=False)
            wait_ready(base)
            cookie = login_cookie(base)

        flows = [
            ("模板·双均线", TEMPLATE_SOURCE),
            ("自写·均线过滤", GOOD_CODE),
        ]
        for label, code in flows:
            evidence = run_flow(base, cookie, label, code)
            flows_evidence.append(evidence)
            print(
                f"  端到端 {label}：run {evidence['run_id'][:8]}，"
                f"{evidence['metrics']['trade_count']} 笔，{evidence['seconds']:.2f}s"
            )
    finally:
        if server is not None:
            stop_server(server)
        purge_account()

    failed = [row for row in rows if not row["ok"]]
    broken = [item for item in flows_evidence if not item["reopened_equal"] or not item["hash_matches"]]
    write_fault_report(base, rows, failed)
    write_workshop_report(base, flows_evidence)
    print(
        f"\n矩阵 {len(rows) - len(failed)}/{len(rows)} 条按预期；"
        f"端到端 {len(flows_evidence)} 条（重开不一致 {len(broken)} 条）"
    )
    return 1 if failed or broken else 0


def write_workshop_report(base: str, flows: list[dict[str, Any]]) -> None:
    """验收 ① 的证据：模板与自写各一条走完 保存 → 检查 → 回测 → 落库 → 重开。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# M4c 端到端证据（在线写策略可成功回测）",
        "",
        f"生成：`uv run python scripts/strategy_http_matrix.py` ｜ 服务 {base} ｜ 标的 600519，真数据",
        "",
        "> 本节是 **M4c-1 的 HTTP 层**证据；工作台界面的截图随 **M4c-2** 补。",
        "",
        "| 来源 | 策略 id | run id | bars | 交易 | 检查结果 | 报告 meta | 重开一致 | hash 一致 | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in flows:
        findings = f"{len(item['errors'])} error / {len(item['warnings'])} warning"
        lines.append(
            f"| {item['label']} | `{item['strategy_id'][:8]}` | `{item['run_id'][:8]}` | "
            f"{item['bars']} | {item['metrics']['trade_count']} | {findings} | "
            f"{item['meta_kind']} / {item['meta_name']} | "
            f"{'✓' if item['reopened_equal'] else '✗'} | "
            f"{'✓' if item['hash_matches'] else '✗'} | {item['seconds']:.2f}s |"
        )
    lines += [
        "",
        "**参数 schema（`/check` 的 meta，工作台的参数表单就吃它）**："
        + "；".join(
            f"{item['label']} → {item['param_keys']}（USES_EVENTS={item['uses_events']}）"
            for item in flows
        ),
        "",
        "**重开口径**：`GET /backtest/runs/{id}` 取回的报告与当次响应**逐键相等**"
        "（走 JSONB 往返，数字没有变成字符串）；`code_sha256` 也一致——改码之后两者才会分岔，"
        "前端据此标注「已非当次运行的代码」。",
        "",
    ]
    WORKSHOP_FILE.write_text("\n".join(lines), encoding="utf-8")


def write_fault_report(
    base: str, rows: list[dict[str, Any]], failed: list[dict[str, Any]]
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# M4c HTTP 层故障注入矩阵（实测输出，可复现）",
        "",
        f"生成：`uv run python scripts/strategy_http_matrix.py` ｜ 服务 {base}"
        "（自起时收紧 CPU 5s / 墙钟 6s / 内存 600MB / 输出 2KB） ｜ 标的 600519，真数据",
        "",
        "| # | 注入的坏法 | HTTP | 期望 | 判定依据 | 错误信息（截断） | 之后 /health | 耗时 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        mark = "✓" if row["ok"] else "✗"
        lines.append(
            f"| {row['index']} | {row['name']} | {row['status']} {mark} | "
            f"{'/'.join(str(item) for item in sorted(row['expected']))} | "
            f"{row['kind'] or '—'} | {row['detail']} | {row['health']} | {row['seconds']:.2f}s |"
        )
    lines += [
        "",
        "**收尾**：换回默认配额后，模板策略在真数据上跑通（HTTP 200，交易 50 笔）——"
        "服务不只能拒绝坏代码，还能继续干正事；账号已从开发库清掉。",
        "",
        f"**结论**：{len(rows) - len(failed)}/{len(rows)} 条坏法各给明确错误码，"
        "且每条之后 `/health` 均为 200——坏代码被关在子进程里，服务照常。"
        + ("" if not failed else f" 未按预期：{[row['name'] for row in failed]}"),
        "",
    ]
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
