"""Общий интерфейс коллектора.

Два требования ко всем источникам:

1. Валидация на границе. Коллектор отдаёт `RawFact` — pydantic-модель.
   Источники отдают мусор, пропущенные поля и внезапно меняют формат;
   ломаться это должно здесь, с внятным сообщением, а не через три стадии
   в генераторе письма.
2. Идемпотентность. Каждый сбор безопасно перезапускаем: упало посередине,
   перезапустили — дублей нет. Держится на `source_uid` — естественном
   идентификаторе записи внутри источника.

Базовый класс здесь нужен. Фабрики стратегий обогащения — нет.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from gtm.config import Config, SourceConfig
from gtm.observability import StageResult, get_logger
from gtm.settings import Settings, get_settings
from gtm.spend import BudgetExceeded, SpendGuard
from gtm.storage import repo


class RawFact(BaseModel):
    """Факт на выходе коллектора, до записи в БД и до сведения к ИНН."""

    model_config = ConfigDict(extra="forbid")

    source_uid: str = Field(min_length=1, max_length=256)
    fact_type: str
    company_name: str | None = None
    inn: str | None = None
    occurred_at: date | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("inn")
    @classmethod
    def _clean_inn(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return digits or None

    @field_validator("company_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


class CollectorError(RuntimeError):
    pass


class Collector(ABC):
    """Источник фактов.

    Наследник реализует только `collect`. Запись в БД, сведение к ИНН,
    счётчики и учёт трат — общие и живут здесь.
    """

    name: str = "base"
    fact_type: str = "news"

    def __init__(
        self,
        session: Session,
        config: Config,
        *,
        run_id: str = "manual",
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.run_id = run_id
        self.settings = settings or get_settings()
        self.log = get_logger(f"gtm.collector.{self.name}")
        self.guard = SpendGuard(session, stage=f"collect:{self.name}")

    @property
    def source_config(self) -> SourceConfig:
        return self.config.sources.source(self.name)

    @property
    def mode(self) -> str:
        return self.source_config.mode

    @property
    def cost_per_call(self) -> float:
        return float(self.source_config.cost_rub_per_call)

    @abstractmethod
    def collect(self, **kwargs: Any) -> Iterable[RawFact]:
        """Отдать факты из источника. Ходить в БД отсюда не нужно."""

    def run(self, **kwargs: Any) -> StageResult:
        """Собрать, записать, свести к ИНН. Безопасно перезапускаемо."""
        from gtm.resolve import resolve_company

        result = StageResult(stage=f"collect:{self.name}")
        if not self.source_config.is_active:
            self.log.info("collector.skipped", source=self.name, mode=self.mode)
            result.note("skipped_disabled")
            return result

        try:
            facts = list(self.collect(**kwargs))
        except BudgetExceeded as exc:
            self.log.warning("collector.budget_exceeded", source=self.name, error=str(exc))
            result.note("budget_exceeded")
            return result

        result.count_in = len(facts)
        for raw in facts:
            inn = resolve_company(
                self.session,
                name=raw.company_name,
                inn=raw.inn,
                source=self.name,
                source_uid=raw.source_uid,
                payload=raw.payload,
            )
            _, created = repo.upsert_fact(
                self.session,
                source=self.name,
                source_uid=raw.source_uid,
                fact_type=raw.fact_type,
                inn=inn,
                company_name_raw=raw.company_name,
                occurred_at=raw.occurred_at,
                payload=raw.payload,
            )
            if created:
                result.bump_out()
            else:
                result.note("duplicates")
            if inn is None:
                result.note("unresolved")
        self.session.commit()
        return result


COLLECTORS: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    COLLECTORS[cls.name] = cls
    return cls


def get_collector(name: str) -> type[Collector]:
    # Импорт здесь, а не наверху: иначе циклическая зависимость модулей.
    import gtm.collectors  # noqa: F401

    if name not in COLLECTORS:
        raise KeyError(f"Коллектор '{name}' не зарегистрирован. Есть: {sorted(COLLECTORS)}")
    return COLLECTORS[name]
