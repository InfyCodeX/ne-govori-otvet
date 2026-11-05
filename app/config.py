import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    bot_mode: Literal["demo", "max"] = "demo"
    max_bot_token: SecretStr | None = Field(default=None, repr=False)
    expected_bot_username: str = "ideya_fiks_106_bot"
    max_poll_timeout: int = Field(default=30, ge=1, le=90)
    max_poll_limit: int = Field(default=100, ge=1, le=1000)
    max_welcome_logo: bool = False
    max_ca_bundle: Path | None = None
    database_path: Path = Path("data/bot.sqlite3")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    enable_ai_setup: bool = False
    ai_owner_managed: bool = False
    ai_enabled: bool = False
    ai_daily_request_limit: int = Field(default=0, ge=0, le=100)
    ai_trial_budget_rub: float = Field(default=0, ge=0, le=10000)
    yandex_api_key: SecretStr | None = Field(default=None, repr=False)
    yandex_model_uri: str = ""

    @field_validator("max_bot_token", "max_ca_bundle", "yandex_api_key", mode="before")
    @classmethod
    def blank_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def validate_live_configuration(self) -> "Settings":
        if self.bot_mode == "max" and self.max_bot_token is None:
            raise ValueError("В режиме max заполните MAX_BOT_TOKEN в локальном .env")
        if self.max_ca_bundle is not None and not self.max_ca_bundle.is_file():
            raise ValueError("MAX_CA_BUNDLE должен указывать на существующий PEM-файл")
        return self


def load_settings() -> Settings:

    config_file = os.environ.get("BOT_ENV_FILE")
    if not config_file:
        config_file = dotenv_values(".env", interpolate=False).get("BOT_ENV_FILE")
    if config_file and not Path(config_file).is_file():
        raise ValueError("Не найден локальный файл настроек BOT_ENV_FILE")
    return Settings(_env_file=config_file or ".env")
