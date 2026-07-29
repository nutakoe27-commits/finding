"""Технические настройки: подключения, ключи, лимиты.

Здесь только то, что зависит от окружения. Бизнес-логика (критерии ICP,
веса сигналов, длина окна контакта) живёт в config/*.yaml, потому что
менять её будут каждую неделю первые два месяца.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GTM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # postgresql+psycopg://gtm:gtm@localhost:5432/gtm — в проде
    database_url: str = "sqlite:///var/gtm.db"
    config_dir: Path = CONFIG_DIR
    output_dir: Path = REPO_ROOT / "out"
    data_dir: Path = REPO_ROOT / "data"

    log_level: str = "INFO"
    log_json: bool = False

    # Ключи внешних источников. Пусто = коллектор работает в offline-режиме
    # (из фикстур) либо отключается с внятным сообщением.
    dadata_token: str | None = None
    dadata_secret: str | None = None
    damia_key: str | None = None
    ofdata_key: str | None = None
    zakupki_token: str | None = None
    hh_user_agent: str = "gtm-research/0.1 (contact: ops@example.com)"

    # LLM для генерации писем.
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 1200

    # Жёсткий дневной лимит на платные запросы — на случай бесконечного
    # цикла. При достижении стадия останавливается, а не продолжает жечь.
    daily_api_budget_rub: float = 500.0
    daily_api_call_limit: int = 2000

    # Сколько карточек в утреннем списке менеджеру.
    daily_delivery_limit: int = 15

    dry_run: bool = True

    @property
    def prompts_dir(self) -> Path:
        return self.config_dir / "prompts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
