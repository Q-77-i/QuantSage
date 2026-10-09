"""M5b 过拟合检验单测：全部用**手工可算**的合成输入，期望值在注释里给出推导过程。

判据来自 SPEC §12 M5b 增量：**与手算对照**（三条正交验算 + 一个独立实现钉死的常数）
与**四个退化分支**。三条正交路径都绕开本模块的组装逻辑：

1. `SR = SR₀ ⇒ DSR = Φ(0) = 0.5` —— 不含任何常数，最干净的一条；
2. `γ₃=0 且 γ₄=1 ⇒ 分母恰为 1 ⇒ DSR = Φ((SR−SR₀)·√(T−1))` —— 闭式可心算；
3. 一组固定输入，期望值由 **scipy 的独立实现**算得（推导见下面 `FIXED_*` 常量处），
   用来抓「组装错位」这类错误——尤其是**年化 / 每期混用**（差 √252 ≈ 15.9 倍，静默且不报错）。

第 3 条只钉常数、**不引入 scipy 依赖**：模块本身与运行期一律纯标准库（SPEC §6 M5b）。
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from app.backtest.metrics import TRADING_DAYS_PER_YEAR, sharpe_ratio
from app.backtest.overfit import (
    OVERFIT_REASONS,
    daily_returns,
    deflated_sharpe,
    expected_max_sharpe,
    grid_overfit,
    moments,
    per_period_sharpe,
)

SQRT_YEAR = math.sqrt(TRADING_DAYS_PER_YEAR)  # √252 ≈ 15.8745


def annualized(per_period: float) -> float:
    """每期夏普 → 年化（`grid_overfit` 的输入口径是年化的，与 `metrics.sharpe` 同）。"""
    return per_period * SQRT_YEAR


# ── 固定输入（期望值由 scipy 独立实现算得，见模块 docstring 第 3 条）──────────
#
#   每期夏普  = [0.05, 0.06, 0.04, 0.07, 0.08]（N=5）
#   总体方差 V = 0.0002        （ddof=0，与 Bailey & López de Prado 参考实现的 np.var 同口径）
#   Φ⁻¹(1−1/5)=0.8416212  Φ⁻¹(1−1/(5e))=1.7506861  γ=0.5772156649
#   SR₀ = √0.0002 · [(1−γ)·0.8416212 + γ·1.7506861] = 0.0141421 · 1.1925712 = 0.0168658
#   最优格 SR = 0.08，γ₃ = −0.5，γ₄ = 4.0（**原始**峰度），T = 101
#   分母 = √(1 − (−0.5)(0.08) + (3/4)(0.08²)) = √1.0448 = 1.0221546
#   DSR = Φ( (0.08 − 0.0168658)·√100 / 1.0221546 ) = Φ(0.6178911) = 0.7315995
FIXED_SR0 = 0.016865826106399077
FIXED_DSR = 0.7315995277598144
FIXED_VARIANCE = 0.0002


def test_daily_returns_matches_the_metrics_convention() -> None:
    """与 `metrics.sharpe_ratio` 逐字同口径：同一曲线两条路径必须给出同一个夏普。

    两处各写一份收益序列（本模块不 import metrics 的内部实现），**这条用例就是那道锁**。
    """
    curve = [100.0, 103.0, 101.0, 108.0, 107.0, 112.0]
    returns = daily_returns(curve)

    assert returns == pytest.approx(
        [0.03, 101 / 103 - 1, 108 / 101 - 1, 107 / 108 - 1, 112 / 107 - 1]
    )
    assert per_period_sharpe(returns) * SQRT_YEAR == pytest.approx(sharpe_ratio(curve))


def test_daily_returns_skips_the_ruined_segment() -> None:
    """净值归零（`curve[i-1] <= 0`）那一段跳过——照抄 `metrics.sharpe_ratio` 的守卫。"""
    assert daily_returns([100.0, 0.0, 50.0]) == pytest.approx([-1.0])


def test_per_period_sharpe_needs_two_returns() -> None:
    assert per_period_sharpe([]) is None
    assert per_period_sharpe([0.01]) is None


def test_per_period_sharpe_is_undefined_on_a_flat_series() -> None:
    assert per_period_sharpe([0.0, 0.0, 0.0]) is None


def test_per_period_sharpe_is_exact_on_a_hand_computable_series() -> None:
    """[1%, 2%, 3%]：均值 2%，样本标准差 1%（ddof=1）⇒ 每期夏普 = 2。"""
    assert per_period_sharpe([0.01, 0.02, 0.03]) == pytest.approx(2.0)


def test_moments_are_population_moments_with_raw_kurtosis() -> None:
    """对称序列偏度为 0；**原始**峰度（正态 = 3）而非超额峰度——差 3 是最常见的口径错。

    手算：[−1, 1] 各占一半（n=2）：m2 = 1、m3 = 0、m4 = 1 ⇒ γ₃ = 0、γ₄ = 1/1² = 1。
    """
    skew, kurt = moments([-1.0, 1.0])
    assert skew == pytest.approx(0.0)
    assert kurt == pytest.approx(1.0)


def test_moments_of_a_normal_sample_sit_near_three() -> None:
    """正态样本的原始峰度应当在 3 附近（用确定性的分位数序列，不用随机数）。

    容差取 0.1 而不是更紧：400 个**分位数中点**构成的样本尾部被截短，实测 2.942。
    本条要钉的不是小数点后第二位，而是「**≈3 而不是 ≈0**」——返超额峰度的话这里会是
    −0.058，两者差着 3，是最常见的口径错。
    """
    normal = NormalDist()
    sample = [normal.inv_cdf((i + 0.5) / 400) for i in range(400)]
    skew, kurt = moments(sample)
    assert skew == pytest.approx(0.0, abs=1e-9)
    assert kurt == pytest.approx(3.0, abs=0.1)


def test_moments_degrade_when_variance_is_zero() -> None:
    assert moments([0.01]) == (None, None)
    assert moments([0.01, 0.01, 0.01]) == (None, None)


def test_expected_max_sharpe_needs_at_least_two_trials() -> None:
    """N=1：Φ⁻¹(1−1/1) = Φ⁻¹(0) = −∞，公式无定义——**不硬凑 0**。"""
    assert expected_max_sharpe(0.0002, 1) is None


def test_expected_max_sharpe_is_zero_when_all_trials_tie() -> None:
    """V=0：所有试验的夏普一样，没有「选择」可言，校正项就是 0（仍出数、标注 V=0）。"""
    assert expected_max_sharpe(0.0, 5) == 0.0


def test_expected_max_sharpe_matches_the_independent_value() -> None:
    assert expected_max_sharpe(FIXED_VARIANCE, 5) == pytest.approx(FIXED_SR0, abs=1e-15)


def test_deflated_sharpe_is_one_half_when_the_best_equals_the_benchmark() -> None:
    """正交验算 1：`SR = SR₀` ⇒ 括号内为 0 ⇒ `Φ(0) = 0.5`。不牵任何常数。"""
    assert deflated_sharpe(0.08, 0.08, 0.0, 1.0, 101) == pytest.approx(0.5)
    assert deflated_sharpe(-0.03, -0.03, 1.7, 9.0, 7) == pytest.approx(0.5)


def test_deflated_sharpe_collapses_to_the_closed_form_when_denominator_is_one() -> None:
    """正交验算 2：γ₃=0 且 γ₄=1 ⇒ 分母 √(1−0+0) = 1 ⇒ `DSR = Φ((SR−SR₀)·√(T−1))`。

    γ₄=1 不是随手凑的：**对称两点分布**（±a 各半）的原始峰度恰为 1（m4/m2² = a⁴/a⁴），
    是真实（退化）的收益分布。刻意**不用「正态」**——正态的 γ₄=3、`(γ₄−1)/4 = 0.5`，
    分母是 √(1 + SR²/2) 而**不是** 1（SPEC v1.20 初稿在这里写错过一次，见变更记录 v1.21）。
    """
    assert deflated_sharpe(0.1, 0.0, 0.0, 1.0, 101) == pytest.approx(NormalDist().cdf(1.0))
    assert deflated_sharpe(0.1, 0.0, 0.0, 1.0, 101) == pytest.approx(0.8413447460685429)
    # 观测数越多，同一个 SR 越显著（√(T−1) 的作用方向）
    assert deflated_sharpe(0.1, 0.0, 0.0, 1.0, 401) > deflated_sharpe(0.1, 0.0, 0.0, 1.0, 101)


def test_deflated_sharpe_needs_two_observations_and_all_moments() -> None:
    assert deflated_sharpe(0.1, 0.0, 0.0, 3.0, 1) is None
    assert deflated_sharpe(0.1, 0.0, None, 3.0, 101) is None
    assert deflated_sharpe(0.1, 0.0, 0.0, None, 101) is None
    assert deflated_sharpe(None, 0.0, 0.0, 3.0, 101) is None
    assert deflated_sharpe(0.1, None, 0.0, 3.0, 101) is None


def test_deflated_sharpe_degrades_when_the_denominator_goes_non_positive() -> None:
    """强正偏 + 正 SR 会把根号内压到 ≤0（`1 − 10·0.5 = −4`）：返 None，不抛 math domain error。

    注意方向：`−γ₃·SR` 要**为负**才压分母，故偏度与 SR 必须**同号**（γ₃=10、SR=+0.5）。
    写成 γ₃=10、SR=−0.5 是 `1+5`，分母反而更大——第一版在这儿写反过。
    """
    assert deflated_sharpe(0.5, 0.0, 10.0, 1.0, 101) is None
    assert deflated_sharpe(-0.5, 0.0, -10.0, 1.0, 101) is None


# ── 组装层：`grid_overfit` ──────────────────────────────────

FIXED_CELLS = dict(best_index=4, best_skew=-0.5, best_kurt=4.0, observations=101)
FIXED_SHARPES = [annualized(s) for s in (0.05, 0.06, 0.04, 0.07, 0.08)]


def test_grid_overfit_matches_the_independent_value() -> None:
    """全链路（年化输入 → 每期 → SR₀ → DSR）与 scipy 独立实现逐位一致。"""
    out = grid_overfit(sharpes=FIXED_SHARPES, **FIXED_CELLS)

    assert out["dsr"] == pytest.approx(FIXED_DSR, abs=1e-15)
    assert out["sr0"] == pytest.approx(FIXED_SR0, abs=1e-15)
    assert out["sr_variance"] == pytest.approx(FIXED_VARIANCE, abs=1e-18)
    assert out["n_trials"] == 5
    assert out["n_valid"] == 5
    assert out["reason"] is None


def test_grid_overfit_does_not_mix_annualized_with_per_period() -> None:
    """防「16 倍」：少除一次 √252（把每期值当年化值喂进去）必须给出明显不同的结果。"""
    right = grid_overfit(sharpes=FIXED_SHARPES, **FIXED_CELLS)
    as_if_annualized = grid_overfit(
        sharpes=[s / SQRT_YEAR for s in FIXED_SHARPES], **FIXED_CELLS
    )
    assert right["dsr"] != pytest.approx(as_if_annualized["dsr"])


def test_grid_overfit_is_undefined_with_a_single_valid_trial() -> None:
    """退化分支 ①：有效格 <2（N=1 ⇒ SR₀ 无定义）。"""
    out = grid_overfit(
        sharpes=[annualized(0.08), None],  # 一格里夏普就无定义
        best_index=0,
        best_skew=-0.5,
        best_kurt=4.0,
        observations=101,
    )
    assert out["dsr"] is None
    assert out["reason"] == "insufficient_trials"
    assert out["n_valid"] == 1 and out["n_trials"] == 2


def test_grid_overfit_is_undefined_without_a_best_index() -> None:
    """退化分支 ②：全网格没有一格有夏普 ⇒ 谈不上「被选中者」。"""
    out = grid_overfit(
        sharpes=[None, None], best_index=None, best_skew=None, best_kurt=None, observations=None
    )
    assert out["dsr"] is None
    assert out["reason"] == "best_sharpe_undefined"


def test_best_index_pointing_at_an_undefined_sharpe_is_reported() -> None:
    """调用方把 best_index 指到没有夏普的格子：如实报错，**不静默换成别的格**。"""
    out = grid_overfit(
        sharpes=[annualized(0.05), None],
        best_index=1,
        best_skew=-0.5,
        best_kurt=4.0,
        observations=101,
    )
    assert out["dsr"] is None
    assert out["reason"] == "best_sharpe_undefined"


def test_grid_overfit_is_undefined_without_moments() -> None:
    """退化分支 ③：最优格的收益矩算不出来（净值恒定一类）。"""
    out = grid_overfit(
        sharpes=[annualized(0.05), annualized(0.08)],
        best_index=1,
        best_skew=None,
        best_kurt=None,
        observations=101,
    )
    assert out["dsr"] is None
    assert out["reason"] == "moments_undefined"


def test_grid_overfit_is_undefined_with_too_few_observations() -> None:
    """退化分支 ④：观测数 <2（√(T−1) 无定义）。"""
    out = grid_overfit(
        sharpes=[annualized(0.05), annualized(0.08)],
        best_index=1,
        best_skew=-0.5,
        best_kurt=4.0,
        observations=1,
    )
    assert out["dsr"] is None
    assert out["reason"] == "insufficient_observations"


def test_grid_overfit_still_reports_when_all_trials_tie() -> None:
    """V=0 是**出数**的：校正项为 0，但标注里必须看得见 V=0（不是静默当正常值）。"""
    out = grid_overfit(
        sharpes=[annualized(0.08)] * 5, best_index=0, best_skew=-0.5, best_kurt=4.0, observations=101
    )
    assert out["reason"] is None
    assert out["sr_variance"] == 0.0
    assert out["sr0"] == 0.0
    assert out["dsr"] == pytest.approx(deflated_sharpe(0.08, 0.0, -0.5, 4.0, 101))


def test_grid_overfit_carries_its_own_inputs_for_replay() -> None:
    """落库的 `overfit` 块要**自证**：重开时不用回算就能看懂这个数是哪来的。"""
    out = grid_overfit(sharpes=FIXED_SHARPES, **FIXED_CELLS)
    assert out["best_index"] == 4
    assert out["skew"] == -0.5 and out["kurt"] == 4.0 and out["observations"] == 101
    assert out["sr"] == pytest.approx(0.08)
    assert "相关" in out["note"]  # 明文写着「未做试验间相关性校正（保守）」


def test_every_reason_code_has_a_display_text() -> None:
    """原因码与展示文案同源，UI 不各写一份（同 `REJECT_REASONS` 的姿态）。"""
    used = {
        "insufficient_trials",
        "best_sharpe_undefined",
        "moments_undefined",
        "insufficient_observations",
        "degenerate_denominator",
    }
    assert used <= set(OVERFIT_REASONS)


def test_degenerate_denominator_is_reported_through_the_composer() -> None:
    """退化分支：分母非正时走 `degenerate_denominator` 而不是抛 `math domain error`。"""
    out = grid_overfit(
        sharpes=[annualized(0.5), annualized(0.6)],  # 正 SR 配正偏（见上一条的方向说明）
        best_index=0,
        best_skew=10.0,
        best_kurt=1.0,
        observations=101,
    )
    assert out["dsr"] is None
    assert out["reason"] == "degenerate_denominator"


def test_grid_overfit_on_a_real_curve_is_reproducible() -> None:
    """端到端：从净值曲线到 DSR，两次调用逐位一致（无隐藏随机 / 无顺序依赖）。"""
    curve = [100.0 * (1.0 + 0.001 * ((i % 7) - 3)) ** i for i in range(1, 61)]
    returns = daily_returns(curve)
    skew, kurt = moments(returns)
    sr = per_period_sharpe(returns)
    assert sr is not None and skew is not None and kurt is not None

    args = dict(
        sharpes=[annualized(sr * 0.9), annualized(sr)],
        best_index=1,
        best_skew=skew,
        best_kurt=kurt,
        observations=len(returns),
    )
    first = grid_overfit(**args)
    assert first["dsr"] is not None  # 真数据形态下必须出得来数，不是一路退化
    assert first == grid_overfit(**args)
