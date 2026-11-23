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

from .domain.errors import ValidationError
from .domain.progress import PROGRESS_RESOURCES
from .domain.schedule import resolve_tz

PACKAGE_ROOT = Path(__file__).resolve().parent

GitHubMode = Literal["fake", "replay", "live"]
LLMProviderName = Literal["mock", "openai_compat"]
LLMThinking = Literal["default", "enabled", "disabled"]
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
    version: str = "1.0.0"
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
    # 限流退避：超过 max_retry_wait 的等待（主限流常见 30-60 分钟）直接失败，不让进程干等
    github_retry_attempts: int = 3
    github_max_retry_wait: float = 60.0

    # LLM
    llm_provider: LLMProviderName = "mock"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-flash"
    llm_timeout: float = 60.0
    llm_max_input_chars: int = 12000
    llm_fallback_to_mock: bool = True
    llm_temperature: float = 0.2
    # default: 不发送 thinking 字段；disabled: 显式关闭思考（temperature 才生效）
    llm_thinking: LLMThinking = "disabled"
    llm_json_mode: bool = True
    llm_report_timeout: float = 120.0

    # 每日进展
    daily_dir: Path | None = None
    daily_timezone: str | None = None
    daily_resources: str = "issues,pulls"
    daily_max_items_per_resource: int = 200
    daily_body_excerpt_chars: int = 160
    daily_skip_bots: bool = True

    # 每日报告
    report_max_input_chars: int = 24000
    report_max_output_tokens: int = 1600

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

    @field_validator("daily_dir", mode="before")
    @classmethod
    def _blank_path_to_none(cls, value: object) -> object:
        # `.env` 里写 `SENTINEL_DAILY_DIR=` 时 pydantic 会把空串解析成 Path(".")，
        # 那样日报会被写到当前工作目录而不是 data/daily，必须在这里拦一下
        if isinstance(value, str):
            text = value.strip()
            return Path(text).expanduser() if text else None
        return value

    @field_validator("daily_timezone", mode="before")
    @classmethod
    def _blank_text_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("daily_timezone")
    @classmethod
    def _check_daily_timezone(cls, value: str | None) -> str | None:
        if value:
            resolve_tz(value)
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
    def daily_root(self) -> Path:
        base = self.daily_dir or (self.data_dir / "daily")
        return base.resolve()

    @property
    def daily_tz(self) -> tzinfo:
        return resolve_tz(self.daily_timezone or self.timezone)

    @property
    def daily_resource_list(self) -> tuple[str, ...]:
        values = tuple(part.strip() for part in self.daily_resources.split(",") if part.strip())
        if not values:
            return ("issues", "pulls")
        unknown = [name for name in values if name not in PROGRESS_RESOURCES]
        if unknown:
            raise ValidationError(f"未知的每日进展资源：{', '.join(unknown)}")
        return values

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
        self.daily_root.mkdir(parents=True, exist_ok=True)

    def resolved_path(self, relative: str | Path) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (self.data_dir / path).resolve()


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
