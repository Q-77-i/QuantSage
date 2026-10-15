"""把两个 DeepSeek 档位的价目写进自托管 Langfuse（E7c 的观测配套，幂等）。

**为什么要这个脚本**：Langfuse 的默认定价表**不含 DeepSeek 系列**（自托管 v4 实测：177 条默认模型
里零 deepseek 条目），而模型名匹配是「对上报名原样跑 SQL 正则」——不配就 `total_cost=0`。
不把这一步落成可重放的脚本，那两条自定义模型就只是**某台机器上的手工状态**（重装 Langfuse
即丢），与「基础设施改动要能查证」的口径冲突。

**价目来源**：DeepSeek 官方价目页（2026-09-10 调价后），单位是**每百万 token 的人民币**，
本脚本按 `USD_PER_CNY = 1/6.67` 折成 Langfuse 要的**每 token 美元**——**这个换算会引入误差**
（页面的 `$` 价就是这个汇率折出来的，实测与官方美元页一致：谷时输入 $0.15/M ↔ 1 元/M）。

**峰谷两档（官方：谷时 = 峰时的一半）**：Langfuse 的分层条件只吃「用量阈值」与「静态属性相等」
（读的是 `pricing-tiers/matcher.js`，**没有时间维度**），时段又是服务端按请求时刻自动判的——
**两档在 Langfuse 里没法并存**，只能取一档。`PEAK = False` 取**谷时**：本项目实测 375 次调用
**100% 落在谷时**（开发/使用都在北京时间夜间与周末；定时 ETL 也在 21:40 之后），
且谷时与我们的真实使用形态一致。**若哪天改到工作日 9–12 / 14–18 跑，把 `PEAK = True`
重跑本脚本即可**；两档之间是精确的 ×2，没有别的差别。

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

#: 官方口径的**每百万 token 人民币**（2026-09-10 调价后）：峰时；谷时是它的精确一半。
#: 页面：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ —— 高峰 = 北京时间
#: 周一至周五（不含法定节假日）9:00–12:00 与 14:00–18:00，其余（含周末/节假日）为谷时。
CNY_PER_MTOK = {
    r"(?i)^deepseek/deepseek-flash$": {
        "label": "deepseek-flash（知策快档）",
        "peak": {"input": 2.0, "output": 8.0, "cache_read": 0.04},
    },
    r"(?i)^deepseek/deepseek-v4-pro$": {
        "label": "deepseek-v4-pro（知策深度档）",
        "peak": {"input": 9.0, "output": 27.0, "cache_read": 0.30},
    },
}

#: 人民币 → 美元的换算率。**这是误差来源，认下来**：官方美元页（英文站）给的 `$` 价
#: 与此汇率折出来的数一致（$0.15/M ↔ 1 元/M），所以是官方同源的近似，不是我们拍的。
USD_PER_CNY = 1 / 6.67


def _to_usd_per_token(cny_per_mtok: float) -> float:
    """元/百万 → 每 token 美元（Langfuse 的定价单位）。"""
    return cny_per_mtok / 1e6 * USD_PER_CNY


#: 实际写入用的价目（每 token 美元）。`PEAK` 选档后展开，避免调用方自己乘。
MODELS: list[dict] = [
    {
        "label": spec["label"],
        "match_pattern": pattern,
        "input": _to_usd_per_token(spec["peak"]["input"] * (1.0 if PEAK else 0.5)),
        "output": _to_usd_per_token(spec["peak"]["output"] * (1.0 if PEAK else 0.5)),
        "cache_read": _to_usd_per_token(spec["peak"]["cache_read"] * (1.0 if PEAK else 0.5)),
    }
    for pattern, spec in CNY_PER_MTOK.items()
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
