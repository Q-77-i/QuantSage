"""日志：统一格式 + 明文密钥脱敏（安全红线：密钥不入日志）。"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

REDACTED = "***REDACTED***"

# 这些环境变量若已被宿主注入，其值也纳入脱敏集合
SENSITIVE_ENV_KEYS = (
    "XIAOSHI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_NEXTAUTH_SECRET",
    "LANGFUSE_SALT",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_REDIS_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "POSTGRES_PASSWORD",
)

# 太短的值不做替换，避免把普通单词打成掩码
_MIN_SECRET_LENGTH = 8


class RedactingFilter(logging.Filter):
    """把日志文本中出现的已知密钥明文替换为掩码。"""

    def __init__(self, secrets: Iterable[str] = (), name: str = "") -> None:
        super().__init__(name)
        self._secrets = tuple(
            s for s in {*secrets} if s and len(s) >= _MIN_SECRET_LENGTH
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def collect_known_secrets() -> set[str]:
    """从 .env 配置 + 宿主环境变量汇总所有已知密钥值。"""
    from app.core.config import get_settings

    settings = get_settings()
    values = {
        settings.xiaoshi_api_key.get_secret_value(),
        settings.deepseek_api_key.get_secret_value(),
        settings.postgres_password.get_secret_value(),
        settings.langfuse_public_key.get_secret_value(),
        settings.langfuse_secret_key.get_secret_value(),
    }
    values |= {os.environ.get(key, "") for key in SENSITIVE_ENV_KEYS}
    return values


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter(collect_known_secrets()))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
