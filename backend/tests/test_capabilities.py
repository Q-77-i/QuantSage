"""静态能力卡（SPEC 附录 A.2）：公开、只读、不含运行状态/密钥。

它**不是 A2A 协议实现**——用例顺带把这句话钉在 `x-quantsage.a2a` 上，防止后来者
把一份 JSON 说成「支持 A2A」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_card_is_public_and_lists_skills() -> None:
    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "知策 QuantSage"
    assert body["schemaVersion"] == "quantsage.agent-card/v1"
    assert body["url"] == "http://testserver"
    assert body["capabilities"]["streaming"] is True
    assert {skill["id"] for skill in body["skills"]} == {
        "pit-event-search",
        "deep-research",
        "paper-proposal",
        "backtest",
    }
    assert all(skill["description"] for skill in body["skills"])


def test_card_states_pit_hitl_and_risks() -> None:
    body = client.get("/.well-known/agent-card.json").json()
    extra = body["x-quantsage"]

    assert extra["a2a"] == "static_metadata_only"
    assert extra["pit"]["gate"] == "available_at"
    assert "T+1" in extra["hitl"]["paper"]
    assert extra["mcp"]["tool"] == "search_events"
    assert extra["mcp"]["transport"] == "stdio"
    assert extra["mcp"]["limits"]["cursor"] is False
    assert "Agent" in body["skills"][2]["description"]
    assert "不批准" in body["skills"][2]["description"]


def test_card_has_no_secret_shaped_fields() -> None:
    text = client.get("/.well-known/agent-card.json").text.lower()
    for forbidden in ("api_key", "secret", "token", "/users/", "postgres"):
        assert forbidden not in text
