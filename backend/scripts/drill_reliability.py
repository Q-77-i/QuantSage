"""可靠性演练（P3-E4c）—— SPEC §5.7 的「轨二」：真拔数据源 / 杀 LLM / 触发限流 / 熔断恢复。

**与 pytest 那条轨分工不同**（PRD 定）：用例在测试进程里用夹具注入故障，证明「代码在故障下
的行为对不对」，跑在 CI；这个脚本在**真进程、真配置、真上游**下演一遍，证明「这套东西真的
会被触发」，并把日志留档到 `logs/e4-drill/<时间戳>/`（验收证据，也是 E9 事故复盘的素材）。

四幕（每幕自断言，报告末尾给总结）：
  ① 拔数据源：`XIAOSHI_CLI_COMMAND` 指向不存在的命令 → 连撞几次后熔断，**不再起子进程**
  ② 杀 LLM：`DEEPSEEK_API_BASE` 指向不可达端口 → 重试 → 降级链 → 两档都熔断 → 快速失败
  ③ 触发限流：连打任务提交端点（生产额度）→ 429 + `Retry-After`，**未开流即拒**
  ④ 熔断恢复：环境还原 → 冷却未过仍被拒 → 冷却过后真调用成功、熔断复位

**它只动环境变量**（`os.environ`，进程内），不碰 `.env`、不碰密钥：脚本退出环境自然还原。
幕④ 会真发一次 `deepseek-v4-pro` 调用（关思考，秒级、不到一分钱）。

用法（**没有「快跑」开关**：幕④要等满冷却才算数，跳过它就等于不给证据）：
    cd backend && .venv/bin/python scripts/drill_reliability.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 跑脚本时 `sys.path[0]` 是**脚本所在目录**（不是 cwd），不显式插一下 `app` 就导不进来
# ——本仓所有脚本同此约定（也是 CLAUDE.md「临时脚本落点」那条规则的由来）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[2]
DRILL_ROOT = REPO_ROOT / "logs" / "e4-drill"

#: 不可达端口：连上去立刻被拒（不是超时），四幕才跑得快
DEAD_BASE = "http://127.0.0.1:9"
DEAD_CLI = "/nonexistent/xiaoshi-data"


@dataclass
class Act:
    """一幕：标题 + 证据行 + 结论。`check` 记一条断言，失败即整幕失败。"""

    title: str
    steps: list[str] = field(default_factory=list)
    ok: bool = True

    def note(self, text: str) -> None:
        self.steps.append(f"  {text}")

    def check(self, condition: bool, text: str) -> None:
        self.steps.append(f"  {'✓' if condition else '✗'} {text}")
        self.ok = self.ok and bool(condition)


def _settings():
    from app.core.config import get_settings

    get_settings.cache_clear()  # 环境变量改完必须清缓存，否则拿到的还是旧配置
    return get_settings()


# ── 幕① 拔数据源 ────────────────────────────────────────────────


def act_source_down() -> Act:
    from app.core import reliability
    from app.core.reliability import CircuitOpen
    from app.data import xiaoshi_cli

    act = Act("① 拔数据源（`XIAOSHI_CLI_COMMAND` 指向不存在的命令）")
    os.environ["XIAOSHI_CLI_COMMAND"] = DEAD_CLI
    _settings()
    reliability.reset()

    started_processes: list[str] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        started_processes.append(str(args[0][0]))
        return real_run(*args, **kwargs)

    subprocess.run = counting_run  # type: ignore[assignment]  # 只观测计数，不改行为
    try:
        threshold = reliability.get_dep("xiaoshi-cli").circuit.threshold
        act.note(f"熔断阈值 = {threshold} 次连续失败")
        for index in range(threshold + 1):
            began = time.perf_counter()
            try:
                xiaoshi_cli.run(["--status"])
                act.check(False, f"第 {index + 1} 次竟然成功了（CLI 该是被拔掉的状态）")
                break
            except CircuitOpen as exc:
                elapsed = time.perf_counter() - began
                act.check(index == threshold, f"第 {index + 1} 次被熔断拒绝：{exc}（{elapsed:.3f}s）")
                act.check(
                    len(started_processes) == threshold,
                    f"子进程总共只起了 {len(started_processes)} 次（阈值 {threshold}）——熔断后不再起",
                )
            except Exception as exc:  # noqa: BLE001 —— 前几次就是要看它失败
                act.note(f"第 {index + 1} 次：{type(exc).__name__}（{time.perf_counter() - began:.3f}s）")
        act.note(f"熔断状态：{json.dumps(reliability.snapshot(), ensure_ascii=False)}")
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
        os.environ.pop("XIAOSHI_CLI_COMMAND", None)
        _settings()
    return act


# ── 幕② 杀 LLM ─────────────────────────────────────────────────


def _try_complete(model: str, seconds: float = 5.0):
    """跑一次真实调用路径（连不上的时候会走重试 → 降级链 → 熔断）。"""
    from app.core import llm

    return asyncio.run(llm.complete("回一句「收到的」。", model=model, timeout=seconds, thinking=False))


def act_llm_down() -> Act:
    from app.core import llm, reliability
    from app.core.reliability import CircuitOpen

    act = Act(f"② 杀 LLM（`DEEPSEEK_API_BASE={DEAD_BASE}`）")
    os.environ["DEEPSEEK_API_BASE"] = DEAD_BASE
    _settings()
    reliability.reset()

    pro = llm.dep_name(llm.DEEP_MODEL)
    flash = llm.dep_name(llm.DEFAULT_MODEL)
    try:
        for round_ in (1, 2):
            began = time.perf_counter()
            try:
                result = _try_complete(llm.DEEP_MODEL)
                act.check(False, f"第 {round_} 轮竟然成功了（模型 {result.model}）")
            except Exception as exc:  # noqa: BLE001
                act.note(f"第 {round_} 轮 {time.perf_counter() - began:.1f}s 后失败：{type(exc).__name__}")
            state = reliability.snapshot()
            act.note(f"  轮后：{pro}={state[pro]['state']}({state[pro]['failures']}) "
                     f"{flash}={state[flash]['state']}({state[flash]['failures']})")

        act.check(reliability.get_dep(pro).state == "open", f"{pro} 已熔断")
        act.check(reliability.get_dep(flash).state == "open", f"{flash} 已熔断（降级链也够不着）")

        began = time.perf_counter()
        try:
            _try_complete(llm.DEEP_MODEL)
            act.check(False, "第三轮竟然还发起了调用")
        except CircuitOpen as exc:
            elapsed = time.perf_counter() - began
            # 这一条是整套策略的**体感证据**：上游死了之后不再有人去等超时
            act.check(elapsed < 0.05, f"第三轮 {elapsed * 1000:.1f}ms 内被拒（{exc}）——不再耗超时")
        except Exception as exc:  # noqa: BLE001
            act.check(False, f"第三轮抛的是 {type(exc).__name__}，期望 CircuitOpen")
    finally:
        os.environ.pop("DEEPSEEK_API_BASE", None)
        _settings()
    return act


# ── 幕③ 触发限流 ────────────────────────────────────────────────


def act_rate_limited() -> Act:
    from fastapi.testclient import TestClient

    from app.core import reliability
    from app.main import app

    act = Act("③ 触发限流（连打 `POST /backtest`，用生产额度）")
    reliability.reset()
    address = f"e4-drill-{uuid.uuid4().hex[:8]}@example.com"
    body = {
        "strategy": "ma_cross",
        "symbol": "600519",
        "start": "2026-07-01",
        "end": "2026-09-30",
    }
    try:
        with TestClient(app) as client:
            registered = client.post(
                "/api/v1/auth/register", json={"email": address, "password": "drill-pass-1234"}
            )
            act.check(registered.status_code == 201, f"演练账号就绪（{registered.status_code}）")

            for index in range(60):
                response = client.post("/api/v1/backtest", json=body)
                if response.status_code == 429:
                    act.check(True, f"第 {index + 1} 次提交被拒：429")
                    act.check(
                        response.headers.get("retry-after", "").isdigit(),
                        f"`Retry-After: {response.headers.get('retry-after')}`",
                    )
                    act.note(f"体：{response.text[:120]}")
                    break
            else:
                act.check(False, "连打 60 次都没触发限流——额度或桶挂错了？")
    except Exception as exc:  # noqa: BLE001 —— 真库/容器没起时如实报，不假装通过
        act.check(False, f"这一幕需要真库与容器：{type(exc).__name__}: {exc}")
    finally:
        try:
            import psycopg

            from app.core.config import get_settings

            with psycopg.connect(get_settings().postgres_dsn) as conn:
                conn.execute("DELETE FROM users WHERE email = %s", (address,))
            act.note("演练账号与它名下的任务已清理（外键级联）")
        except Exception as exc:  # noqa: BLE001
            act.note(f"⚠ 清理失败，请手工删 {address}：{type(exc).__name__}")
            act.ok = False
        reliability.reset()
    return act


# ── 幕④ 熔断恢复 ────────────────────────────────────────────────


def act_recovery(*, cooldown: float) -> Act:
    from app.core import llm, reliability
    from app.core.reliability import CircuitOpen

    act = Act("④ 熔断恢复（重新杀一次 → 冷却未过仍拒 → 冷却过后真调用成功）")
    pro = llm.dep_name(llm.DEEP_MODEL)

    # **自己重新制造一次真实的熔断**，不靠上一幕留下的状态：幕③ 要跑几十次提交，
    # 耗时早就越过了冷却（第一版就是这么错的——那个「冷却未过仍拒」的前提不成立，
    # 断言必红，而代码其实没毛病）。
    os.environ["DEEPSEEK_API_BASE"] = DEAD_BASE
    _settings()
    reliability.reset()
    try:
        for _ in range(6):  # 阈值 ÷ 每次尝试数，多跑两轮兜住配置变化
            if reliability.get_dep(pro).state == "open":
                break
            try:
                _try_complete(llm.DEEP_MODEL)
            except Exception:  # noqa: BLE001 —— 就是要它失败
                continue
    finally:
        os.environ.pop("DEEPSEEK_API_BASE", None)
        _settings()

    act.check(reliability.get_dep(pro).state == "open", f"重新熔断：{pro} 已开路")
    try:
        _try_complete(llm.DEEP_MODEL)
        act.check(False, "冷却没过却成功了——刚熔断的电路没留住")
    except CircuitOpen as exc:
        act.check(True, f"冷却未过仍被拒：{exc}")
    except Exception as exc:  # noqa: BLE001
        act.check(False, f"期望 CircuitOpen，实际 {type(exc).__name__}: {exc}")

    act.note(f"等待冷却 {cooldown:.0f}s …")
    time.sleep(cooldown + 1.0)

    try:
        began = time.perf_counter()
        result = _try_complete(llm.DEEP_MODEL, seconds=30.0)
        elapsed = time.perf_counter() - began
        act.check(bool(result.text.strip()), f"半开探测成功：模型 {result.model}，{elapsed:.1f}s")
        act.check(
            result.model == llm.DEEP_MODEL,
            f"用的是请求的那一档（{result.model}）——降级链没有被无谓触发",
        )
        act.check(reliability.get_dep(pro).state == "closed", f"{pro} 已复位")
    except Exception as exc:  # noqa: BLE001
        act.check(False, f"恢复失败：{type(exc).__name__}: {exc}")
    finally:
        reliability.reset()
    return act


# ── 主流程 ─────────────────────────────────────────────────────


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 —— 不在 git 工作区也照跑
        return "(未知)"


def write_report(out_dir: Path, acts: list[Act], started: datetime) -> Path:
    lines = [
        "# 可靠性演练报告（P3-E4c / SPEC §5.7 轨二）",
        "",
        f"- 时间：{started.isoformat(timespec='seconds')}",
        f"- 代码：`{git_sha()}`",
        "- 说明：环境变量在**进程内**改，脚本退出即还原；不碰 `.env` 与密钥。",
        "",
    ]
    for act in acts:
        lines += [f"## {'✅' if act.ok else '❌'} {act.title}", "", *act.steps, ""]
    lines += [
        "## 结论",
        "",
        f"四幕 {'全部通过' if all(a.ok for a in acts) else '有失败（见上）'}；"
        "完整日志见同目录 `journal.log`（含每次重试、降级、熔断的原始记录）。",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="可靠性演练（拔数据源 / 杀 LLM / 限流 / 恢复）")
    parser.add_argument("--out", type=Path, default=None, help="留档目录（默认 logs/e4-drill/<时间戳>）")
    args = parser.parse_args(argv)

    started = datetime.now()
    out_dir = args.out or (DRILL_ROOT / started.strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(out_dir / "journal.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("app")
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    print(f"演练留档：{out_dir}")
    acts: list[Act] = []
    try:
        for runner in (
            act_source_down,
            act_llm_down,
            act_rate_limited,
            lambda: act_recovery(cooldown=_cooldown()),
        ):
            act = runner()
            acts.append(act)
            print(f"{'✅' if act.ok else '❌'} {act.title}")
            for line in act.steps:
                print(line)
    finally:
        root.removeHandler(handler)
        handler.close()

    report = write_report(out_dir, acts, started)
    print(f"\n报告：{report}")
    return 0 if all(act.ok for act in acts) else 1


def _cooldown() -> float:
    from app.core import reliability

    return reliability.get_dep("llm:deepseek-v4-pro").circuit.cooldown


if __name__ == "__main__":
    sys.exit(main())
