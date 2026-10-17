"""外部链接的协议白名单。

`source_url` 来自外部语料，不是我们生成的。只放行 `http` / `https`：`javascript:`、
`data:`、`file:`、协议相对 `//host` 一律当没有。前端有 React 的 `javascript:` 兜底，
但那是框架实现细节，不是契约——服务端先把不合格的值清掉。
"""

from __future__ import annotations

from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})


def safe_source_url(value: object) -> str | None:
    """合法外链原样返回；空值 / 非 http(s) / 带控制字符 → None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or any(char in text for char in "\r\n\t"):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
        return None
    return text


__all__ = ["ALLOWED_SCHEMES", "safe_source_url"]
