"""日志：统一格式 + 明文密钥脱敏（安全红线：密钥不入日志）。"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime

REDACTED = "***REDACTED***"

#: 当前请求的 id（E7e）。中间件设置、JSON 格式化器读取；后台任务没有请求上下文时为 None。
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

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


class JsonFormatter(logging.Formatter):
    """一行一个 JSON（E7e，`LOG_FORMAT=json`）。字段固定、不把任意对象塞进来。

    `request_id` 优先取记录上的显式字段（访问日志会带），否则取当前请求的 context var；
    后台任务没有请求上下文时该字段缺省，不编一个空串。
    """

    #: 结构化日志允许带出的附加字段（都是标量；需要更多字段时在这里显式加）
    EXTRA_KEYS = (
        "method",
        "path",
        "status",
        "duration_ms",
        "user_id",
        "task_id",
        "run_id",
        "account_id",
        "model",
        "symbol",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key in self.EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO, fmt: str | None = None) -> None:
    """装配根日志。`fmt` 缺省读 `settings.log_format`：`text`（默认）/ `json`（E7e）。"""
    if fmt is None:
        from app.core.config import get_settings

        fmt = get_settings().log_format
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if fmt.strip().lower() == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter(collect_known_secrets()))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
