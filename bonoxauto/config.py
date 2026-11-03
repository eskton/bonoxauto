"""Настройки из переменных окружения / файла .env."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    admin_ids: Annotated[frozenset[int], NoDecode]
    timezone: str = "Europe/Moscow"
    shop: Literal["demo", "bonoxs"] = "demo"
    dry_run: bool = True

    bonoxs_email: str | None = None
    bonoxs_password: SecretStr | None = None

    misfire_grace_minutes: int = Field(default=15, ge=0)
    payment_timeout_minutes: int = Field(default=30, ge=1)
    payment_poll_seconds: int = Field(default=20, ge=5)

    db_path: Path = Path("data/bonoxauto.db")
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: Any) -> Any:
        if isinstance(value, int):
            return frozenset({value})
        if isinstance(value, str):
            parts = value.replace(";", ",").replace(" ", ",").split(",")
            return frozenset(int(part) for part in parts if part)
        return value

    @field_validator("admin_ids")
    @classmethod
    def _require_admins(cls, value: frozenset[int]) -> frozenset[int]:
        if not value:
            raise ValueError("укажите хотя бы один Telegram ID в ADMIN_IDS")
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
