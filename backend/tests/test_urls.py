"""外部链接协议白名单（D#24）。"""

from __future__ import annotations

import pytest

from app.api.events import _feed_row
from app.core.urls import safe_source_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("http://example.com/a", "http://example.com/a"),
        ("javascript:alert(1)", None),
        ("data:text/html,<script>alert(1)</script>", None),
        ("file:///etc/passwd", None),
        ("//evil.example/a", None),
        ("", None),
        (None, None),
        ("https://example.com/\r\nX-Injected: 1", None),
    ],
)
def test_safe_source_url(value: object, expected: str | None) -> None:
    assert safe_source_url(value) == expected


def test_feed_row_drops_unsafe_source_url() -> None:
    row = {
        "event_id": "e1",
        "event_type": "news",
        "title": "标题",
        "event_time": None,
        "available_at": None,
        "direction_norm": None,
        "industries": [],
        "importance_score": None,
        "source": "s",
        "original_source": "o",
        "source_url": "javascript:alert(1)",
        "content_hash": "h",
        "symbols": [],
    }

    assert _feed_row(row, {})["source_url"] is None
