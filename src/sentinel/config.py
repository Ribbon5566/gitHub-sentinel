"""集中配置。

原则：所有可调项都从环境变量（SENTINEL_ 前缀）或 .env 进入，代码里不散落魔法数字。
Windows 上时区必须显式校验，因为系统没有 IANA 库时 ZoneInfo 会直接抛错，
报错信息只会说「找不到 Asia/Shanghai」，不会提示你少装了 tzdata。
"""

from __future__ import annotations

from datetime import tzinfo
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.schedule import resolve_tz

PACKAGE_ROOT = Path(__file__).resolve().parent

GitHubMode = Literal["fake", "replay", "live"]
LLMProviderName = Literal["mock", "openai_compat"]
NotifyChannel = Literal["null", "smtp"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SENTINEL_",
        extra="ignore",
        case_sensitive=False,
    )

    # 基础
    env: str = "dev"
    app_name: str = "GitHub Sentinel"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"

    # 鉴权：留空则只允许本机访问
    api_key: str | None = None

    # 存储
    data_dir: Path = Path("data")
    database_url: str | None = None

    # 订阅默认值
    default_cron: str = "0 9 * * *"

    # GitHub
    github_mode: GitHubMode = "fake"
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_per_page: int = 50
    github_timeout: float = 30.0
    github_max_pages: int = 5

    # LLM
    llm_provider: LLMProviderName = "mock"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 60.0
    llm_max_input_chars: int = 12000
    llm_fallback_to_mock: bool = True

    # 通知
    notify_channel: NotifyChannel = "null"
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_ssl: bool = True
    smtp_starttls: bool = False

    # 调度
    enable_scheduler: bool = True
    tick_interval_seconds: int = 60
    bootstrap_lookback_days: int = 7
    max_events_per_report: int = 200
    emit_empty_reports: bool = False

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        resolve_tz(value)
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return Path(value.strip()).expanduser()
        return value

    @property
    def tz(self) -> tzinfo:
        return resolve_tz(self.timezone)

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = (self.data_dir / "sentinel.db").resolve()
        return f"sqlite+aiosqlite:///{db_path.as_posix()}"

    @property
    def reports_root(self) -> Path:
        return (self.data_dir / "reports").resolve()

    @property
    def templates_dir(self) -> Path:
        return PACKAGE_ROOT / "templates"

    @property
    def static_dir(self) -> Path:
        return PACKAGE_ROOT / "static"

    @property
    def smtp_recipients(self) -> tuple[str, ...]:
        raw = self.smtp_to or ""
        return tuple(addr.strip() for addr in raw.replace(";", ",").split(",") if addr.strip())

    @property
    def auth_required(self) -> bool:
        return bool(self.api_key)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_root.mkdir(parents=True, exist_ok=True)

    def resolved_path(self, relative: str | Path) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (self.data_dir / path).resolve()


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
