"""Runtime configuration, read from environment variables (and an optional .env file).

Every setting is documented in ``.env.example``. Production mode
(``APP_ENV=production``) refuses to start with insecure defaults.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

INSECURE_SECRET = "change-me-in-production"  # noqa: S105  (sentinel, rejected in production)
INSECURE_ADMIN_PASSWORD = "ChangeMe123!"  # noqa: S105  (sentinel, rejected in production)
KNOWN_CONNECTORS = ("ted", "contracts_finder", "sam_gov", "csv")

CsvList = Annotated[list[str], NoDecode]


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or inconsistent."""


def split_csv(value: object) -> list[str]:
    if value is None:
        return []
    items = [str(v) for v in value] if isinstance(value, (list, tuple)) else str(value).split(",")
    return [i.strip() for i in items if i.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- app ---
    APP_NAME: str = "Tenderbuddy"
    APP_ENV: Literal["development", "test", "production"] = "development"
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8004, ge=1, le=65535)
    DATABASE_URL: str = "sqlite:///./data/tenderbuddy.db"
    DATA_DIR: Path = Path("./data")
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"

    # --- auth ---
    SECRET_KEY: str = INSECURE_SECRET
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 12, ge=5, le=60 * 24 * 30)
    COOKIE_SECURE: bool | None = None  # default: True in production
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = INSECURE_ADMIN_PASSWORD
    LOGIN_RATE_LIMIT: int = Field(default=10, ge=1)  # failed attempts per window
    LOGIN_RATE_WINDOW_SECONDS: int = Field(default=300, ge=1)

    # --- uploads ---
    MAX_UPLOAD_MB: int = Field(default=20, ge=1, le=200)

    # --- demo data ---
    SEED_DEMO_DATA: bool = False

    # --- connectors / sync ---
    CONNECTORS: CsvList = Field(default_factory=lambda: ["ted", "contracts_finder", "csv"])
    SYNC_INTERVAL_MINUTES: int = Field(default=0, ge=0)  # 0 disables the in-process scheduler
    SYNC_LOOKBACK_DAYS: int = Field(default=7, ge=1, le=365)
    SYNC_MAX_RECORDS: int = Field(default=200, ge=1, le=5000)
    HTTP_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    HTTP_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    TED_API_URL: str = "https://api.ted.europa.eu/v3/notices/search"
    TED_COUNTRIES: CsvList = Field(default_factory=list)  # ISO-3166 alpha-3, e.g. IRL,MLT
    TED_CPV_PREFIXES: CsvList = Field(default_factory=list)
    CONTRACTS_FINDER_URL: str = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    SAM_API_URL: str = "https://api.sam.gov/opportunities/v2/search"
    SAM_API_KEY: str = ""
    SAM_PTYPES: CsvList = Field(default_factory=lambda: ["o", "k"])
    CSV_STAGING_DIR: Path | None = None  # default: DATA_DIR/staging

    # --- LLM ---
    LLM_PROVIDER: Literal["auto", "mock", "openai"] = "auto"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.1-8b-instant"
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_INPUT_CHARS: int = Field(default=24000, ge=1000)

    @field_validator("CONNECTORS", "TED_COUNTRIES", "TED_CPV_PREFIXES", "SAM_PTYPES", mode="before")
    @classmethod
    def _csv_list(cls, value: object) -> list[str]:
        return split_csv(value)

    @field_validator("CONNECTORS")
    @classmethod
    def _known_connectors(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in KNOWN_CONNECTORS]
        if unknown:
            raise ValueError(f"unknown connector(s): {', '.join(unknown)}")
        return value

    @field_validator("TED_COUNTRIES")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [v.upper() for v in value]

    @field_validator("ADMIN_EMAIL")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("ADMIN_EMAIL must be an email address")
        return value

    @model_validator(mode="after")
    def _production_guard(self) -> Settings:
        if self.APP_ENV == "production":
            problems = []
            if self.SECRET_KEY == INSECURE_SECRET or len(self.SECRET_KEY) < 32:
                problems.append("SECRET_KEY must be set to a random value of 32+ characters")
            if self.ADMIN_PASSWORD == INSECURE_ADMIN_PASSWORD:
                problems.append("ADMIN_PASSWORD must not be the default")
            if self.SEED_DEMO_DATA:
                problems.append("SEED_DEMO_DATA must be off in production")
            if problems:
                raise ConfigError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.APP_ENV == "production"

    @property
    def upload_dir(self) -> Path:
        return self.DATA_DIR / "uploads"

    @property
    def staging_dir(self) -> Path:
        return self.CSV_STAGING_DIR or (self.DATA_DIR / "staging")

    @property
    def llm_mode(self) -> Literal["mock", "openai"]:
        if self.LLM_PROVIDER == "auto":
            return "openai" if self.LLM_BASE_URL else "mock"
        return self.LLM_PROVIDER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
