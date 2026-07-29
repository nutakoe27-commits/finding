"""Загрузка бизнес-конфигурации из config/*.yaml.

Критерии ICP, веса сигналов и длина окна контакта живут в YAML, а не в коде,
потому что менять их будут каждую неделю первые два месяца. Здесь — только
чтение и валидация: ошибка в YAML должна ломаться на старте с внятным
сообщением, а не через три стадии в генераторе письма.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gtm.settings import get_settings


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- icp.yaml


class HallConfig(_Model):
    capacity_max: int
    capacity_theatre: int | None = None
    is_target: bool = False


class VenueConfig(_Model):
    name: str
    city: str = "Москва"
    district: str | None = None
    area_sqm: int | None = None
    halls: dict[str, HallConfig] = Field(default_factory=dict)
    advantages: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        target = [h for h in self.halls.values() if h.is_target]
        cap = max((h.capacity_max for h in target), default=0)
        parts = [f"{self.name}, {self.city}"]
        if self.area_sqm:
            parts.append(f"площадка-трансформер {self.area_sqm} м²")
        if cap:
            parts.append(f"большой зал до {cap} человек")
        parts.extend(self.advantages[:2])
        return "; ".join(parts)


class ScaleConfig(_Model):
    min_attendees: int = 250
    target_attendees_min: int = 400
    target_attendees_max: int = 1500
    hard_max_attendees: int = 1500
    attendees_from_headcount_ratio: float = 0.6


class GeoConfig(_Model):
    company_regions: list[str] = Field(default_factory=lambda: ["77"])
    allow_any_region_if_event_in_moscow: bool = True
    event_cities: list[str] = Field(default_factory=lambda: ["Москва"])


class CompanyCriteria(_Model):
    min_headcount: int = 0
    min_revenue_rub: float = 0.0
    allowed_statuses: list[str] = Field(default_factory=lambda: ["active"])
    min_age_years: int = 0


class ExcludeConfig(_Model):
    okved_prefixes: list[str] = Field(default_factory=list)
    name_patterns: list[str] = Field(default_factory=list)


class IcpConfig(_Model):
    venue: VenueConfig
    scale: ScaleConfig = Field(default_factory=ScaleConfig)
    geo: GeoConfig = Field(default_factory=GeoConfig)
    company: CompanyCriteria = Field(default_factory=CompanyCriteria)
    exclude: ExcludeConfig = Field(default_factory=ExcludeConfig)
    reject_codes: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------ signals.yaml


class SignalKind(_Model):
    enabled: bool = True
    weight: float = 0.5
    lead_days: int = 120
    window_days: int = 60
    base_confidence: float = 0.5
    max_confidence: float = 0.95
    confidence_per_repeat: float = 0.0
    round_years: list[int] = Field(default_factory=list)
    major_years: list[int] = Field(default_factory=list)
    major_confidence_bonus: float = 0.0
    keywords: list[str] = Field(default_factory=list)


class SeasonalityConfig(_Model):
    month_multiplier: dict[str, float] = Field(default_factory=dict)

    def factor(self, month: int) -> float:
        return self.month_multiplier.get(str(month), 1.0)


class ScoringConfig(_Model):
    bonuses: dict[str, float] = Field(default_factory=dict)
    penalties: dict[str, float] = Field(default_factory=dict)
    min_score_to_deliver: float = 0.0


class CalibrationConfig(_Model):
    min_outcomes_per_kind: int = 25
    max_weight_shift: float = 0.25
    positive_outcomes: list[str] = Field(default_factory=list)


class SignalsConfig(_Model):
    kinds: dict[str, SignalKind]
    seasonality: SeasonalityConfig = Field(default_factory=SeasonalityConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)

    def kind(self, name: str) -> SignalKind:
        if name not in self.kinds:
            raise KeyError(
                f"Тип сигнала '{name}' не описан в config/signals.yaml. "
                f"Известные: {sorted(self.kinds)}"
            )
        return self.kinds[name]


# ------------------------------------------------------------ sources.yaml


class SourceConfig(_Model):
    mode: str = "off"  # live | offline | off
    enabled: bool = False
    cost_rub_per_call: float = 0.0

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_yaml_bool(cls, value: Any) -> Any:
        # YAML 1.1 читает `off`/`on` как булево. Ловушка на ровном месте:
        # `mode: off` приезжает сюда как False.
        if isinstance(value, bool):
            return "off" if value is False else "on"
        return value

    @property
    def is_active(self) -> bool:
        return self.enabled and self.mode in {"live", "offline"}


class SourcesConfig(_Model):
    defaults: dict[str, Any] = Field(default_factory=dict)

    def source(self, name: str) -> SourceConfig:
        raw = getattr(self, name, None) or self.model_extra.get(name)  # type: ignore[union-attr]
        if raw is None:
            raise KeyError(f"Источник '{name}' не описан в config/sources.yaml")
        if isinstance(raw, SourceConfig):
            return raw
        return SourceConfig.model_validate(raw)


# --------------------------------------------------------------- фасад


class Config(_Model):
    icp: IcpConfig
    signals: SignalsConfig
    sources: SourcesConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Не найден конфиг {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался словарь на верхнем уровне")
    return data


def load_config(config_dir: Path | None = None) -> Config:
    directory = config_dir or get_settings().config_dir
    return Config(
        icp=IcpConfig.model_validate(_read_yaml(directory / "icp.yaml")),
        signals=SignalsConfig.model_validate(_read_yaml(directory / "signals.yaml")),
        sources=SourcesConfig.model_validate(_read_yaml(directory / "sources.yaml")),
    )


@lru_cache(maxsize=4)
def get_config(config_dir: Path | None = None) -> Config:
    return load_config(config_dir)


def reset_config_cache() -> None:
    get_config.cache_clear()


def load_prompt(name: str, prompts_dir: Path | None = None) -> str:
    directory = prompts_dir or get_settings().prompts_dir
    path = directory / name
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8")
