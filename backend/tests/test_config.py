"""离线单测：配置解析与「密钥不回显」。"""

from __future__ import annotations

import logging

from app.core.config import REPO_ROOT, Settings
from app.core.logging import REDACTED, RedactingFilter


def _settings(**kwargs) -> Settings:
    """不读真实 .env，纯构造。"""
    return Settings(_env_file=None, **kwargs)


def test_dsn_derived_from_parts() -> None:
    settings = _settings(
        postgres_user="u", postgres_password="p", postgres_db="d", postgres_host_port=5433
    )
    assert settings.postgres_dsn == "postgresql://u:p@127.0.0.1:5433/d"


def test_explicit_database_url_wins() -> None:
    settings = _settings(
        database_url="postgresql://x:y@example:15432/z", postgres_password="p"
    )
    assert settings.postgres_dsn == "postgresql://x:y@example:15432/z"


def test_summary_contains_no_password() -> None:
    settings = _settings(postgres_password="pg-secret-value")
    assert "pg-secret-value" not in settings.postgres_summary


def test_secrets_masked_in_repr_and_str() -> None:
    settings = _settings(
        xiaoshi_api_key="xiaoshi-secret-value", postgres_password="pg-secret-value"
    )
    assert "xiaoshi-secret-value" not in repr(settings)
    assert "pg-secret-value" not in repr(settings)
    assert "xiaoshi-secret-value" not in str(settings)


def test_redacting_filter_masks_secret_in_log_record() -> None:
    redactor = RedactingFilter(["xiaoshi-secret-value"])
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "key=%s", ("xiaoshi-secret-value",), None
    )
    assert redactor.filter(record) is True
    assert "xiaoshi-secret-value" not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_repo_root_resolution() -> None:
    """`REPO_ROOT` 指向仓库根 —— 用**入库的**文件当路标，保证配置回溯路径没写错。

    ⚠ **别拿 `.env` 当路标**：它被 gitignore，**任何一次干净 clone 上都不存在**
    （不只是 CI——CI 首跑只是把这件事暴露出来了，2026-10-15）。入库文件才证明得了
    路径回溯正确。
    """
    assert (REPO_ROOT / "docker-compose.yml").is_file()
    assert (REPO_ROOT / "backend" / "pyproject.toml").is_file()


def test_strict_msgpack_is_enforced() -> None:
    """加固清单之一（E8a，SPEC §8.1）：checkpoint 反序列化白名单**必须**是开着的。

    `app/__init__.py` 在 import 期 `setdefault("LANGGRAPH_STRICT_MSGPACK", "true")`，而
    langgraph 把结果读成模块级常量并缓存——所以「导入了 app」与「langgraph 认为是 strict」
    必须同时成立。这条防的是**回退**：那两行看着像无用代码，被「顺手清理」掉之后
    checkpoint 会退回「任何 Python 可调用对象都能在加载时执行」，而**没有任何测试会因此变红**
    （CLAUDE.md §五 曾把这条误记为「Platform 专属、不适用」，实为库级开关，SPEC §8.1 已订正）。
    """
    from langgraph.checkpoint.serde import _msgpack  # noqa: PLC0415 —— 该开关只在私有模块暴露

    import app  # noqa: PLC0415 —— 就是要「先 import app」

    assert app.STRICT_MSGPACK_ENFORCED is True
    assert _msgpack.STRICT_MSGPACK_ENABLED is True
