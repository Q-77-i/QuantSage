"""集中配置：全项目唯一读取 .env 的地方。

密钥一律用 SecretStr —— `repr()` / `str()` 输出 `**********`，从结构上保证不回显。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py → 仓库根；按文件位置回溯，不依赖运行时 cwd
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "quantsage-backend"
    env: str = "local"
    debug: bool = False

    # ── 基础设施（compose 与本文件共用仓库根 .env）──
    postgres_user: str = "quantsage"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "quantsage"
    postgres_host_port: int = 5433
    database_url: str = ""  # 留空则由上面几项推导；填了则以它为准
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://127.0.0.1:6333"

    # ── 小石数据源（稳定入口绝对路径由安装器回填）──
    xiaoshi_api_key: SecretStr = SecretStr("")
    xiaoshi_mcp_command: str = ""
    xiaoshi_cli_command: str = ""

    # ── 模型（T3 使用）──
    deepseek_api_key: SecretStr = SecretStr("")

    # ── Langfuse（默认 Cloud；自托管时把 base_url 指向 http://127.0.0.1:3000）──
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_tracing_enabled: bool = True

    # ── 启动姿态：False 时连不上 Postgres 也降级启动（本地开发用）──
    startup_require_db: bool = False

    @property
    def postgres_dsn(self) -> str:
        """后端连库用的 DSN。端口只在 .env 里写一次（POSTGRES_HOST_PORT）。"""
        if self.database_url:
            return self.database_url
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{password}"
            f"@127.0.0.1:{self.postgres_host_port}/{self.postgres_db}"
        )

    @property
    def postgres_summary(self) -> str:
        """可安全写进日志的连库摘要（不含口令）。"""
        return f"{self.postgres_user}@{self.postgres_db}:{self.postgres_host_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
