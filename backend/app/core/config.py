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

    # ── 样例数据（T2 落盘目录，DuckDB 查询层读它）──
    data_dir: Path = REPO_ROOT / "data"

    # ── 数据接入（M2b）：事件语料日增量 ──
    # **默认关**：定时任务会对外发请求、写数据目录，本地开发与测试不该被它打扰
    etl_enabled: bool = False
    etl_hour: int = 21  # 北京时间；行情当日 available_at 是 20:25，事件归档在盘后继续出
    etl_minute: int = 10
    etl_trailing_days: int = 7  # 回落窗口（自然日）：吸收迟到事件与平台修订
    etl_catchup_on_start: bool = True  # 启动时对一次缺口（关机/长假后自动对齐）

    # ── RAG（M3）：本地嵌入与检索 ──
    # 权重目录放仓库内 .tools/（已 gitignore），不散落到 ~/.cache；模型各约 2.3GB
    rag_model_dir: Path = REPO_ROOT / ".tools" / "models"
    # cpu / mps：Mac 上 MPS 更快但有长跑内存泄漏（按日分区重启规避，见 SPEC §4 M3a）
    rag_device: str = "cpu"
    # 批量小反而快：实测 batch=4 22.8 条/s、8 → 18.5、32 → 13.6（CPU，padding 到批内最长样本）
    rag_embed_batch_size: int = 4
    rag_rerank_batch_size: int = 8
    rag_max_length: int = 512  # 语料最长 2,065 字，512 token 足够；截断短一点省显存/内存
    rag_rerank_candidates: int = 50  # 进精排的候选数，由延迟实测定档（M3c）
    rag_top_k: int = 5
    # collection 名可配：集成用例必须能指向测试库，**绝不能拿生产索引当试验场**
    rag_collection: str = "cn_events_v1"

    # ── 模型（T3 使用）──
    deepseek_api_key: SecretStr = SecretStr("")

    # ── Langfuse（默认 Cloud；自托管时把 base_url 指向 http://127.0.0.1:3000）──
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_tracing_enabled: bool = True

    # ── 鉴权（M1）──
    # 只从环境变量读；为空时 /api/v1/auth/* 返回 503，**不静默降级为无鉴权**
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_seconds: int = 7 * 24 * 3600

    # ── 启动姿态：False 时连不上 Postgres 也降级启动（本地开发用）──
    startup_require_db: bool = False

    # ── 前端（T6）──
    # 前端跑 3001 而非 3000：3000 被 Langfuse 自托管 UI 占用
    cors_origins: list[str] = [
        "http://127.0.0.1:3001",
        "http://localhost:3001",
    ]

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
