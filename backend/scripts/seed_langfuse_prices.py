"""把两个 DeepSeek 档位的价目写进自托管 Langfuse（E7c 的观测配套，幂等）。

**为什么要这个脚本**：Langfuse 的默认定价表**不含 DeepSeek 系列**（自托管 v4 实测：177 条默认模型
里零 deepseek 条目），而模型名匹配是「对上报名原样跑 SQL 正则」——不配就 `total_cost=0`。
不把这一步落成可重放的脚本，那两条自定义模型就只是**某台机器上的手工状态**（重装 Langfuse
即丢），与「基础设施改动要能查证」的口径冲突。

**价目来源**：DeepSeek 官方**英文**价目页——**官方直接给美元价**（如谷时 flash in $0.15/M、
out $0.60/M），**没有任何换算**，故不存在汇率误差。⚠ **别用中文页的人民币价**：实测它已滞后
（pro 中文页仍写 4.5/13.5 元，英文页现价 $0.66/$1.98 折回约 6.8 汇率才对得上）；也**别用
LiteLLM 的 `model_prices_and_context_window` 表**（停在调价前旧价，flash/pro 都高一倍）。

**峰谷两档（官方：谷时 = 峰时的一半）**：Langfuse 的分层条件只吃「用量阈值」与「静态属性相等」
（读的是 `pricing-tiers/matcher.js`，**没有时间维度**），时段又是服务端按请求时刻自动判的——
**两档在 Langfuse 里没法并存**，只能取一档，**档位由使用者按自己跑批的时间段选**：

  * `PEAK = False`（默认）＝**谷时**：适合夜间/周末跑（本项目实测 375 次调用 100% 在谷时，
    定时 ETL 也在 21:40 之后）；
  * `PEAK = True` ＝**峰时**：若任务实际跑在工作日 9–12 / 14–18（北京时间），
    按当前谷值价 **×2** 换档即可 —— 改这一行常量重跑本脚本。

两档之间是精确 ×2，没有第三种取值。

**两处口径（别改成想当然的样子）**：
  * `match_pattern` 要覆盖上报名（`deepseek/deepseek-flash`）——匹配不剥前缀；
  * `input_cache_read` 一档公开 API **不收**（`prices[]` 里给了也静默丢弃，实测），
    所以那两行走一段 SQL —— 与 UI 写的是**同一张表**（`prices`），不是旁路。
  * 公开 API **没有按 id 的部分更新**（PATCH 405），所以改价 = **按 matchPattern 找到 → 删 → 重建**。

用法（先起好 Langfuse、`.env` 里有 key）：

    .venv/bin/python scripts/seed_langfuse_prices.py           # 幂等：已存在则跳过
    .venv/bin/python scripts/seed_langfuse_prices.py --check   # 只读核对，不写
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402

#: 取峰时还是谷时（见模块 docstring 的取舍说明）。改这一行重跑脚本即换档。
PEAK = False

#: 官方口径的**每百万 token 美元**（峰时；谷时是精确一半）。
#: 页面：https://api-docs.deepseek.com/quick_start/pricing/ —— 高峰 = 周一至周五（不含中国
#: 法定节假日）**UTC 01:00–04:00 与 06:00–10:00**（北京时间 9:00–12:00 与 14:00–18:00），
#: 其余（含周末/节假日）为谷时。
USD_PER_MTOK = {
    r"(?i)^deepseek/deepseek-flash$": {
        "label": "deepseek-flash（知策快档）",
        "peak": {"input": 0.3, "output": 1.2, "cache_read": 0.006},
    },
    r"(?i)^deepseek/deepseek-v4-pro$": {
        "label": "deepseek-v4-pro（知策深度档）",
        "peak": {"input": 1.32, "output": 3.96, "cache_read": 0.044},
    },
}

#: 实际写入用的价目（每 token 美元）。`PEAK` 选档后展开，避免调用方自己乘。
MODELS: list[dict] = [
    {
        "label": spec["label"],
        "match_pattern": pattern,
        "input": spec["peak"]["input"] * (1.0 if PEAK else 0.5) / 1e6,
        "output": spec["peak"]["output"] * (1.0 if PEAK else 0.5) / 1e6,
        "cache_read": spec["peak"]["cache_read"] * (1.0 if PEAK else 0.5) / 1e6,
    }
    for pattern, spec in USD_PER_MTOK.items()
]

#: cache 档补写用（公开 API 不收这一档）。容器名与本仓 compose 一致；库名固定 `langfuse`。
_COMPOSE_PG = "quantsage-postgres-1"


def _auth() -> tuple[str, str]:
    s = get_settings()
    return (
        s.langfuse_public_key.get_secret_value(),
        s.langfuse_secret_key.get_secret_value(),
    )


def _existing(base: str, auth: tuple[str, str]) -> dict[str, dict]:
    r = httpx.get(f"{base}/api/public/models", auth=auth, timeout=20)
    r.raise_for_status()
    return {m["matchPattern"]: m for m in r.json()["data"]}


def _seed_via_api(base: str, auth: tuple[str, str], check: bool) -> int:
    """幂等：已存在则**删后重建**（公开 API 没有按 id 的部分更新，PATCH 实测 405）。"""
    have = _existing(base, auth)
    written = 0
    for spec in MODELS:
        existing = have.get(spec["match_pattern"])
        if check:
            state = "已存在" if existing else "缺失"
            print(f"{'=' if existing else '✗'} {state}：{spec['label']}（--check，不写）")
            continue
        if existing:
            d = httpx.delete(f"{base}/api/public/models/{existing['id']}", auth=auth, timeout=20)
            d.raise_for_status()
            print(f"- 已删旧条目：{spec['label']}")
        body = {
            "modelName": spec["label"],
            "matchPattern": spec["match_pattern"],
            "unit": "TOKENS",
            "inputPrice": spec["input"],
            "outputPrice": spec["output"],
            "prices": [
                {"usageType": "input", "price": spec["input"]},
                {"usageType": "output", "price": spec["output"]},
            ],
        }
        r = httpx.post(f"{base}/api/public/models", auth=auth, timeout=20, json=body)
        r.raise_for_status()
        print(
            f"+ 已建：{spec['label']}  input=${spec['input']*1e6:.4f}/M "
            f"output=${spec['output']*1e6:.4f}/M cache_read=${spec['cache_read']*1e6:.5f}/M"
        )
        written += 1
    return written


_LIST_SQL = (
    "SELECT m.model_name, p.usage_type, p.price FROM prices p JOIN models m ON m.id = p.model_id "
    "WHERE m.match_pattern LIKE '(?i)^deepseek/deepseek-%' ORDER BY m.model_name, p.usage_type;"
)


def _seed_cache_rows(check: bool) -> None:
    """补 `input_cache_read` 一档（公开 API 不收，走 SQL；幂等靠固定 id 前缀 + ON CONFLICT）。

    `check=True` 时只列现状（同一段 SQL 去掉 INSERT）。
    """
    values = " UNION ALL ".join(
        f"SELECT '{spec['match_pattern']}' AS pat, {spec['cache_read']}::numeric(65,30) AS price"
        for spec in MODELS
    )
    insert = f"""
    INSERT INTO prices (id, created_at, updated_at, model_id, usage_type, price, project_id, pricing_tier_id)
    SELECT 'e7c-' || substr(md5(m.id || '-cache-read'), 1, 20), now(), now(), m.id, 'input_cache_read',
           v.price, m.project_id, t.id
    FROM models m
    JOIN pricing_tiers t ON t.model_id = m.id AND t.is_default
    JOIN ({values}) v ON v.pat = m.match_pattern
    ON CONFLICT (id) DO NOTHING;
    """
    sql = _LIST_SQL if check else insert + _LIST_SQL
    proc = subprocess.run(
        ["docker", "exec", "-i", _COMPOSE_PG, "sh", "-c", "psql -U $POSTGRES_USER -d langfuse"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print("✗ cache 档 SQL 失败：", proc.stderr.strip()[:300])
        sys.exit(1)
    print(proc.stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description="把 DeepSeek 两档价目写进自托管 Langfuse（幂等）")
    ap.add_argument("--check", action="store_true", help="只读核对，不写入")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.langfuse_public_key.get_secret_value():
        print("✗ 未配置 LANGFUSE_PUBLIC_KEY（见 .env.example）")
        sys.exit(1)
    base = settings.langfuse_base_url.rstrip("/")
    auth = _auth()

    print(f"目标：{base}｜取价档位：{'峰时（工作日 9–12 / 14–18）' if PEAK else '谷时（其余，含夜间与周末）'}")
    _seed_via_api(base, auth, args.check)
    _seed_cache_rows(check=args.check)
    print("完成。" + ("（--check：未写入）" if args.check else ""))


if __name__ == "__main__":
    main()
