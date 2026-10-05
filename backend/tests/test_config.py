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
    """.env 与 docker-compose.yml 都在仓库根 —— 保证配置回溯路径没写错。"""
    assert (REPO_ROOT / ".env").is_file()
    assert (REPO_ROOT / "docker-compose.yml").is_file()
