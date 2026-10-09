"""过拟合检验（M5b）：Deflated Sharpe Ratio 及其输入矩，**纯函数**。

参考：Bailey & López de Prado (2014), *The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting and Non-Normality*, JPM。

    SR₀ = √V[{SRₙ}] · [ (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]     γ = Mascheroni
    DSR = Φ[ (SR − SR₀)·√(T−1) / √(1 − γ₃·SR + ((γ₄−1)/4)·SR²) ]

它回答的是「从 N 组参数里挑出夏普最高的那一组」这个动作**本身**会不会制造出假信号：
SR₀ 是「纯运气下 N 次试验里最大夏普的期望」，DSR 把观测到的 SR 与它比。**不引 numpy、
不引第三方回测检验库**（SPEC §6 M5b）——样本量在 10^1~10^2 的格数、10^2~10^3 的观测数，
标准库足够，且这个公式值得被逐行读懂而不是调包。

四条口径（SPEC §6 M5b 写死，每条都有对应用例）：

1. **SR 一律「每期」口径**（日频、未年化）。`metrics.sharpe` 是年化值（×√252），
   进公式前必须 `/√252`——**混用会静默差 ≈15.9 倍**，两边都是「合理数字」，不会报错。
   全网格的 `V[{SRₙ}]` 同口径。
2. **N 取全网格格数，不做试验间相关性校正**。相邻参数的试验高度相关，真实独立试验数
   比格数少 → 用格数会把 SR₀ 抬**高** → DSR 更**低**，即本值是**保守**估计。
   如实标注，不假装是精确值。
3. **γ₃ / γ₄ 取最优格自身的日收益矩**，且 γ₄ 是**原始峰度**（正态 = 3），
   **不是**超额峰度（正态 = 0）。这两个数差 3，混淆会让分母整体偏移。
4. **退化一律返 `None` + 原因码，绝不硬凑**（见 `OVERFIT_REASONS`）。

本模块不 import engine、不做 I/O、不看报告结构——只吃「一串夏普」与「一组矩」。
网格怎么展开、最优格怎么选、结果怎么落库，都是 `batch.py` 的事。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import e, sqrt
from statistics import NormalDist
from typing import Any

from app.backtest.metrics import TRADING_DAYS_PER_YEAR

#: Euler–Mascheroni 常数：期望最大值校正里「两个分位数各占多少」的权重。
EULER_MASCHERONI = 0.5772156649015329

#: 原因码 → 展示文案。与 `a_share_rules.REJECT_REASONS` 同姿态：码进数据、文案进 UI，
#: 两边不各写一份字面量。
OVERFIT_REASONS: dict[str, str] = {
    "insufficient_trials": "有效试验不足 2 组，期望最大夏普无定义（DSR 不适用）",
    "best_sharpe_undefined": "最优格没有夏普值，选择偏差无从校正",
    "moments_undefined": "最优格的收益矩算不出来（净值退化），DSR 不适用",
    "insufficient_observations": "最优格收益样本不足 2 个，DSR 不适用",
    "degenerate_denominator": "非正态修正项退化（分母非正），DSR 无实数解",
}

#: 标注里写死的一句：读者不该以为这是个精确值。
CONSERVATIVE_NOTE = (
    "N 取全网格格数、未做试验间相关性校正——相邻参数的试验高度相关，真实独立试验数更少，"
    "故 SR₀ 偏高、本值是保守估计"
)

_NORMAL = NormalDist()


def daily_returns(equity: Sequence[float]) -> list[float]:
    """日频收益序列，口径与 `metrics.sharpe_ratio` **逐字一致**（净值归零那一段跳过）。

    两处各写一份（本模块不 import metrics 的内部实现），`test_overfit.py` 里有一条用例
    把两条路径钉在一起——那是防漂移的锁。
    """
    return [
        equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity)) if equity[i - 1] > 0
    ]


def per_period_sharpe(returns: Sequence[float]) -> float | None:
    """每期（未年化）夏普：均值 / 样本标准差（ddof=1）。

    样本 <2 或方差为 0 时无定义，返 `None`——与 `metrics.sharpe_ratio` 的退化分支同口径，
    只是差一个 `×√252`。
    """
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    return mean / sqrt(variance)


def moments(returns: Sequence[float]) -> tuple[float | None, float | None]:
    """总体偏度 γ₃ 与**原始**峰度 γ₄（正态时 γ₄ = 3，不是 0）。

    用**总体矩**（除以 n）而非样本无偏估计：DSR 的推导建立在总体矩上，且观测数在
    10²~10³ 量级时两者之差远小于「用错哪一个」的代价。口径写在这里，不留给读者猜。
    """
    n = len(returns)
    if n < 2:
        return None, None
    mean = sum(returns) / n
    deviations = [r - mean for r in returns]
    m2 = sum(d * d for d in deviations) / n
    if m2 <= 0:
        return None, None
    m3 = sum(d**3 for d in deviations) / n
    m4 = sum(d**4 for d in deviations) / n
    return m3 / m2**1.5, m4 / (m2 * m2)


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float | None:
    """SR₀：N 组独立试验下「最大夏普」的期望（每期口径）。前收 `sr_variance` 同口径。

    - `N < 2` → `None`：`Φ⁻¹(1 − 1/1) = Φ⁻¹(0) = −∞`，公式本身无定义，**不硬凑 0**
    - `V = 0` → `0.0`：所有试验夏普一样 = 没有「选择」可言，校正项就是 0（仍是有效结论）
    """
    if n_trials < 2:
        return None
    if sr_variance <= 0:
        return 0.0
    weight = (1.0 - EULER_MASCHERONI) * _NORMAL.inv_cdf(1.0 - 1.0 / n_trials) + (
        EULER_MASCHERONI * _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * e))
    )
    return sqrt(sr_variance) * weight


def deflated_sharpe(
    sr: float | None,
    sr0: float | None,
    skew: float | None,
    kurt: float | None,
    observations: int | None,
) -> float | None:
    """DSR：把 `sr` 相对「纯运气下的期望最大夏普」`sr0` 标准化成概率。两者同为每期口径。

    分母是**非正态修正**：偏度与厚尾都会改变 SR 估计量的方差。γ₃=0 且 γ₄=1 时分母恰为 1
    （对称两点分布就是这种形态），此时 `DSR = Φ((SR−SR₀)·√(T−1))`——可闭式手算，
    是对本公式结构的一条正交验算（见 `test_overfit.py`）。
    """
    if sr is None or sr0 is None or skew is None or kurt is None:
        return None
    if observations is None or observations < 2:
        return None
    denominator = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denominator <= 0:
        return None
    return _NORMAL.cdf((sr - sr0) * sqrt(observations - 1) / sqrt(denominator))


def _population_variance(values: Sequence[float]) -> float:
    """ddof=0 —— 与 Bailey & López de Prado 参考实现的 `np.var` 默认同口径。"""
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def grid_overfit(
    *,
    sharpes: Sequence[float | None],
    best_index: int | None,
    best_skew: float | None,
    best_kurt: float | None,
    observations: int | None,
) -> dict[str, Any]:
    """把一份网格的夏普分布 + 最优格的收益矩，折算成 DSR **与它的全部输入**。

    `sharpes` 是**年化**值（与 `metrics.sharpe` 同口径，直接取报告里的那份），内部统一
    折成每期——转换只在这一处发生，别在调用方先除一次。

    `best_index` 由调用方给：**「最优」的判据是 `batch.py` 的规则（夏普最大、并列看总收益），
    不是本模块的事**。但 DSR 的「被选中者」必须与那个判据同源，所以这里只认传进来的下标，
    指到没有夏普的格子上就如实报 `best_sharpe_undefined`，**不静默换一格**。

    返回块**自带全部输入**（`sr` / `sr0` / `sr_variance` / 四个矩与样本数）：落库后重开时
    不必回算就能复核这个数是怎么来的。
    """
    valid = [s for s in sharpes if s is not None]
    out: dict[str, Any] = {
        "dsr": None,
        "reason": None,
        "note": CONSERVATIVE_NOTE,
        "n_trials": len(sharpes),
        "n_valid": len(valid),
        "best_index": best_index,
        "sr": None,
        "sr0": None,
        "sr_variance": None,
        "skew": best_skew,
        "kurt": best_kurt,
        "observations": observations,
    }

    best = sharpes[best_index] if best_index is not None and 0 <= best_index < len(sharpes) else None
    if best is None:
        out["reason"] = "best_sharpe_undefined"
        return out

    per_period = [s / sqrt(TRADING_DAYS_PER_YEAR) for s in valid]
    sr = best / sqrt(TRADING_DAYS_PER_YEAR)
    variance = _population_variance(per_period)
    out["sr"] = sr
    out["sr_variance"] = variance

    if len(valid) < 2:
        out["reason"] = "insufficient_trials"
        return out
    if best_skew is None or best_kurt is None:
        out["reason"] = "moments_undefined"
        return out
    if observations is None or observations < 2:
        out["reason"] = "insufficient_observations"
        return out

    sr0 = expected_max_sharpe(variance, len(valid))
    out["sr0"] = sr0
    if sr0 is not None and 1.0 - best_skew * sr + (best_kurt - 1.0) / 4.0 * sr * sr <= 0:
        out["reason"] = "degenerate_denominator"
        return out

    out["dsr"] = deflated_sharpe(sr, sr0, best_skew, best_kurt, observations)
    if out["dsr"] is None:  # pragma: no cover - 上面的前置检查已覆盖全部分支
        out["reason"] = "degenerate_denominator"
    return out


__all__ = [
    "CONSERVATIVE_NOTE",
    "EULER_MASCHERONI",
    "OVERFIT_REASONS",
    "daily_returns",
    "deflated_sharpe",
    "expected_max_sharpe",
    "grid_overfit",
    "moments",
    "per_period_sharpe",
]
