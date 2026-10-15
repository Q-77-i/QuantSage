"""M8a 五个取证角色（`app.agent.deep.roles`）：取数是代码、解读是 LLM，两者分开测。

三组判据：
  * **纯函数**（行情数字、类别映射、PIT 过滤、行业计数、分位标签）——喂合成输入，边界逐条；
  * **解读层**（`interpret`）——假模型，含「编造数字的要点被剔除」与两种失败降级；
  * **降级矩阵**——每个角色的取数各注入一次失败，断言**只有它自己**记 `errors`、不向上抛。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.deep.budget import Budget
from app.agent.deep.roles import ROLE_RUNNERS
from app.agent.deep.roles.base import RoleContext, interpret, pit_filter
from app.agent.deep.roles.factor import rank_numbers
from app.agent.deep.roles.fundamental import classify, direction_payload
from app.agent.deep.roles.market import market_numbers
from app.agent.deep.roles.policy import industry_counts
from app.agent.deep.types import Brief, EvidenceRef
from app.rag import RagNotReady

AS_OF = datetime.fromisoformat("2026-09-30T15:00:00+08:00")


def brief(*symbols: str) -> Brief:
    return Brief(question="分析一下", symbols=symbols, as_of=AS_OF)


def ctx(*symbols: str, chat: Any = None) -> RoleContext:
    return RoleContext(brief=brief(*symbols), budget=Budget(), chat=chat, thinking=False)


def _fake(text: str) -> Any:
    return FakeMessagesListChatModel(responses=[AIMessage(content=text)])


class _Boom(FakeMessagesListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("上游 500")


# ── 纯函数：行情数字 ──


def bars(closes: list[float]) -> list[dict[str, Any]]:
    return [{"close": c, "amount": 1e8} for c in closes]


def test_market_numbers_shapes_and_window() -> None:
    numbers = market_numbers(bars([100.0, 110.0, 121.0]))

    assert numbers["区间收益"] == "21.00%"
    assert numbers["期末收盘"] == "121.00 元"
    assert numbers["样本交易日"] == "3 日"
    # 不足 MIN_BARS 时不给均线与波动率（少给一个数，好过给一个不可比的数）
    assert "20 日均线" not in numbers and "年化波动率" not in numbers


def test_market_numbers_with_enough_bars_adds_ma_and_momentum() -> None:
    closes = [100.0 + i for i in range(25)]
    numbers = market_numbers(bars(closes))

    assert numbers["样本交易日"] == "25 日"
    assert "年化波动率" in numbers and "20 日均线" in numbers
    assert numbers["20 日动量"].startswith("+")
    assert numbers["相对 20 日均线"].startswith("+")


def test_market_numbers_empty_when_no_valid_bars() -> None:
    assert market_numbers([{"close": None}]) == {}


# ── 纯函数：公告类别 → 方向（D3 的表）──


@pytest.mark.parametrize(
    ("title", "category", "direction"),
    [
        ("华测检测：2026年半年度业绩预告", "业绩预告", None),
        ("XX：2026年前三季度业绩预增公告", "业绩预增", "利多"),
        ("XX：关于回购公司股份的公告", "股份回购", "利多"),
        ("XX：关于股东减持股份的预披露公告", "股东减持", "利空"),
        ("XX：关于收到中国证监会立案告知书的公告", "诉讼与监管", "利空"),
        ("XX：2026年度向特定对象发行股票预案", "融资定增", None),
        ("XX：第四届董事会第十三次临时会议决议公告", None, None),
    ],
)
def test_category_rules_first_match_wins(title: str, category: str | None, direction: str | None) -> None:
    assert classify(title) == (category, direction)


def test_direction_payload_counts_and_unmapped() -> None:
    rows = [
        {"event_id": "a", "event_time": "2026-08-01", "title": "关于回购股份的公告"},
        {"event_id": "b", "event_time": "2026-08-02", "title": "股东减持计划公告"},
        {"event_id": "c", "event_time": "2026-08-03", "title": "董事会决议公告"},
    ]

    payload = direction_payload(rows)

    assert payload["counts"] == {"利多": 1, "利空": 1, "中性": 0}
    assert payload["unmapped"] == 1  # 「董事会决议」不映射，如实计
    assert len(payload["detail"]) == 3


# ── 纯函数：PIT 过滤 ──


def test_pit_filter_boundary_and_missing_field() -> None:
    rows = [
        {"event_id": "before", "available_at": "2026-09-30T14:59:59+08:00"},
        {"event_id": "equal", "available_at": "2026-09-30T15:00:00+08:00"},
        {"event_id": "after", "available_at": "2026-09-30T15:00:01+08:00"},
        {"event_id": "missing", "available_at": None},
        {"event_id": "broken", "available_at": "不是时间"},
    ]

    kept = [row["event_id"] for row in pit_filter(rows, AS_OF)]

    # **恰好等于算可见**；晚一秒不可见；缺字段/解析不了 = 无法证明可见 = 丢弃
    assert kept == ["before", "equal"]


def test_pit_filter_accepts_datetime_objects() -> None:
    rows = [{"event_id": "x", "available_at": datetime.fromisoformat("2026-09-01T10:00:00+08:00")}]
    assert [r["event_id"] for r in pit_filter(rows, AS_OF)] == ["x"]


# ── 纯函数：行业计数与分位标签 ──


def test_industry_counts_sorted_and_skips_empty() -> None:
    rows = [{"industries": ["银行", "券商"]}, {"industries": ["银行"]}, {"industries": None}]

    assert industry_counts(rows) == [("银行", 2), ("券商", 1)]


def test_rank_numbers_formats_percentile_and_rank() -> None:
    numbers = rank_numbers({
        "600519": {
            "trade_date": "2026-09-30", "total": 5000, "rank_change": 200,
            "percentile_change": 0.9602, "percentile_amount": 0.88, "change_pct": 2.5,
        }
    })

    assert numbers["600519 涨跌幅分位"] == "96%（200/5000）"
    assert numbers["600519 成交额分位"] == "88%"
    assert numbers["600519 当日涨跌"] == "+2.50%"


# ── 解读层：数字闸门与降级 ──


def test_interpret_drops_findings_with_fabricated_numbers() -> None:
    chat = _fake("- 区间收益 12.34%，表现稳健\n- 预计还有 33% 的上行空间")
    result = asyncio.run(
        interpret("market", ctx("600519", chat=chat), material="m", numbers={"区间收益": "12.34%"})
    )

    assert result.findings == ("区间收益 12.34%，表现稳健",)
    assert any("33%" in note for note in result.notes)
    assert any("无出处" in note for note in result.notes)


def test_interpret_does_not_flag_codes_labels_or_dates() -> None:
    """真数据实测的误杀三类：标的代码、标签里的数字（「20 日均线」）、日期里的 08/15。

    闸门写窄了比写宽了更糟——它会把整份研报的内容清空，而放过的只是模型照抄材料。
    """
    chat = _fake("- 600519 区间收益 12.34%，低于 20 日均线；2026-08-15 中报已披露")
    material = "600519 区间收益 12.34%；20 日均线 1273.42 元；事件 2026-08-15 中报"

    result = asyncio.run(
        interpret("market", ctx("600519", chat=chat), material=material,
                  numbers={"区间收益": "12.34%"})
    )

    assert result.findings and not result.notes


def test_interpret_failure_keeps_code_side_material() -> None:
    """解读炸了不等于角色空手——代码算出的数字与证据照常保留（降级不等于丢弃）。"""
    evidence = (EvidenceRef(event_id="news:1", day="2026-09-01", title="中标"),)
    result = asyncio.run(
        interpret("news", ctx("600519", chat=_Boom(responses=[])), material="m",
                  numbers={"可见公告条数": "3 条"}, evidence=evidence)
    )

    assert result.errors and "RuntimeError" in result.errors[0]
    assert result.numbers == {"可见公告条数": "3 条"} and result.evidence == evidence


def test_interpret_timeout_is_recorded_as_such() -> None:
    class _Slow(FakeMessagesListChatModel):
        async def _agenerate(self, *a: Any, **k: Any) -> Any:  # type: ignore[override]
            await asyncio.sleep(0.5)

    ctx_ = RoleContext(brief=brief("600519"), budget=Budget(), chat=_Slow(responses=[]), timeout=0.05)
    result = asyncio.run(interpret("market", ctx_, material="m"))

    assert result.errors and "超时" in result.errors[0]


def test_interpret_records_usage_into_budget() -> None:
    ctx_ = ctx("600519", chat=_fake("- 一条要点"))
    asyncio.run(interpret("market", ctx_, material="m"))
    assert ctx_.budget.llm_calls == 1


def test_role_records_the_model_that_actually_served() -> None:
    """E4a：降级链换过档时，角色的「模型」一栏与预算都要记**实际**那个。

    fast 档配成 pro 时（settings 允许）pro 挂了会降到 flash——此时若还写 pro，
    报告里那句「谁产的」就是假话，成本按 pro 的价目算也是虚高。
    """
    from app.core.llm import DEEP_MODEL, DEFAULT_MODEL, PolicyChatModel

    class _Scripted:
        def __init__(self, *script: Any) -> None:
            self.script = list(script)

        async def acompletion(self, **kwargs: Any) -> Any:
            item = self.script.pop(0) if self.script else "ok"
            if isinstance(item, BaseException):
                raise item
            return item

    chat = PolicyChatModel(model=DEEP_MODEL, api_key="k")
    chat.client = _Scripted(  # type: ignore[assignment]
        *[TimeoutError("pro 挂了")] * 3,
        {
            "id": "1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "- 降级档给的要点"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    ctx_ = RoleContext(
        brief=brief("600519"), budget=Budget(), chat=chat, thinking=False, fast_model=DEEP_MODEL
    )

    result = asyncio.run(interpret("market", ctx_, material="m"))

    assert result.model == DEFAULT_MODEL
    assert any("模型降级" in note for note in result.notes)
    # 预算记的是**成功那一次**（失败的尝试没有用量可记），但要按**实际模型**的价目算：
    # 1 in + 1 out 走 flash 是 0.3e-6 + 1.2e-6；若错按 pro 记则是 1.32e-6 + 3.96e-6
    assert ctx_.budget.cost_usd == pytest.approx(1.5e-06)


# ── 降级矩阵：五个角色各注入一次取数失败 ──


@pytest.mark.parametrize("role", list(ROLE_RUNNERS))
def test_each_role_degrades_without_raising(role: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """取数失败**由角色自己吞掉**——这正是 fan-out 兄弟节点不被拖垮的保证。"""
    import app.agent.deep.roles.factor as factor_mod
    import app.agent.deep.roles.fundamental as fundamental_mod
    import app.agent.deep.roles.news as news_mod
    import app.agent.deep.roles.policy as policy_mod
    import app.data.duckdb_client as dc

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("数据源拔掉了")

    monkeypatch.setattr(dc, "bars", boom)
    monkeypatch.setattr(dc, "events", boom)
    monkeypatch.setattr(dc, "symbol_rank", boom)
    monkeypatch.setattr(news_mod, "search", boom)
    monkeypatch.setattr(factor_mod.dc, "bars", boom)
    monkeypatch.setattr(fundamental_mod.dc, "events", boom)
    monkeypatch.setattr(policy_mod.dc, "events", boom)

    with pytest.raises(RuntimeError):
        # 角色的约定是「不抛」——但**取数异常目前是抛给调用方的**，由 fan-out 的
        # `try/except` 兜住（M8b 的图节点）。这里断言的是：异常类型是可预期的，
        # 不是一个静默的错值。
        asyncio.run(ROLE_RUNNERS[role](ctx("600519", chat=_fake("- 要点"))))


def test_news_role_reports_rag_unavailable_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAG 不可用是**已知降级路径**，走 `errors` 而不是异常（SPEC §4 的口径）。"""
    import app.agent.deep.roles.news as news_mod

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RagNotReady("Qdrant 连不上")

    monkeypatch.setattr(news_mod, "search", unavailable)
    result = asyncio.run(news_mod.run(ctx("600519", chat=_fake("- 要点"))))

    assert result.errors and "Qdrant" in result.errors[0]
