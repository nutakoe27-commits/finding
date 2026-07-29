"""Пересчёт весов сигналов по накопленным исходам.

Модуль НИКОГДА не переписывает config/signals.yaml. Он возвращает отчёт
и готовый YAML-фрагмент, который человек вставляет руками. Причины две.

Первая: программная правка YAML затирает комментарии, а комментарии
в signals.yaml — половина его ценности, там объяснено, почему у сигнала
именно такой вес и такое окно.

Вторая: автоматический сдвиг весов без человека — чёрный ящик, который
потом не отладить. Через месяц никто не вспомнит, почему вес сигнала
стал 0.42, и доверять выдаче системы будет не за что.

Отсюда же порог в минимум исходов на тип: без него система переобучится
на трёх письмах и выключит сигнал, который просто не успел выстрелить.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.observability import get_logger
from gtm.storage import repo
from gtm.storage.models import Outcome

# Технические границы веса. Ноль выключил бы сигнал молча, а вес больше
# полутора ломает сопоставимость score между типами: приоритет перестаёт
# что-либо значить. Пороги и максимальный сдвиг — в config/signals.yaml.
_WEIGHT_FLOOR = 0.05
_WEIGHT_CEILING = 1.5
# Веса в конфиге пишутся с двумя знаками — больше точности здесь ложь.
_WEIGHT_DIGITS = 2


@dataclass
class CalibrationReport:
    """Что предлагается изменить и почему.

    `per_kind` — по каждому участвующему типу: n, positives, conversion,
    current_weight, proposed_weight, applied. `skipped` — тип → причина,
    по которой он в пересчёт не попал. `yaml_snippet` — кусок для вставки
    в config/signals.yaml руками.
    """

    per_kind: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    yaml_snippet: str = ""


def _settled(outcomes: dict[str, int]) -> int:
    """Исходом считается всё, кроме «ещё ждём». Касание без результата —
    не ноль в статистике, а отсутствие статистики."""
    return sum(count for outcome, count in outcomes.items() if outcome != Outcome.PENDING.value)


def _shift(current: float, ratio: float, max_shift: float) -> float:
    """Новый вес: текущий × отношение конверсий, но не более чем на
    max_shift за пересчёт. Ограничение важнее точности: без него первая же
    удачная неделя по редкому сигналу утроит ему вес, и следующий месяц
    система будет писать только по нему.
    """
    low = current * (1.0 - max_shift)
    high = current * (1.0 + max_shift)
    proposed = min(max(current * ratio, low), high)
    proposed = min(max(proposed, _WEIGHT_FLOOR), _WEIGHT_CEILING)
    return round(proposed, _WEIGHT_DIGITS)


def _snippet(per_kind: dict[str, dict[str, Any]], average: float, total: int) -> str:
    """Фрагмент для вставки в config/signals.yaml.

    Рядом с каждым весом — на чём он посчитан, иначе через месяц число
    объяснить будет нечем. Комментарии живут в YAML законно, safe_load
    их просто не видит.
    """
    if not per_kind:
        return ""
    lines = [
        f"# Пересчёт весов от {date.today():%Y-%m-%d} по {total} исходам.",
        f"# Средняя конверсия по участвующим типам — {average:.1%}.",
        "# Вставлять руками, сверяясь с комментариями к каждому типу.",
        "kinds:",
    ]
    for kind, row in per_kind.items():
        lines.append(f"  {kind}:")
        lines.append(
            f"    weight: {row['proposed_weight']}"
            f"  # исходов {row['n']}, положительных {row['positives']}"
            f" ({row['conversion']:.1%}), было {row['current_weight']}"
        )
    return "\n".join(lines) + "\n"


def recalibrate(session: Session, config: Config) -> CalibrationReport:
    """Предложить новые веса сигналов по накопленным исходам.

    Конфиг не трогает: возвращает отчёт и YAML-фрагмент (см. докстринг
    модуля). Типы с недостаточной статистикой уходят в `skipped` — это
    защита от переобучения, и она принципиальна.
    """
    log = get_logger("gtm.feedback")
    calibration = config.signals.calibration
    positive_outcomes = set(calibration.positive_outcomes)
    stats = repo.outcomes_by_kind(session)
    report = CalibrationReport()

    measured: dict[str, dict[str, Any]] = {}
    for kind in sorted(set(stats) | set(config.signals.kinds)):
        outcomes = stats.get(kind, {})
        if kind not in config.signals.kinds:
            # Тип выкинули из конфига, а исходы по нему остались. Текущего
            # веса нет — сдвигать нечего, но молчать об этом нельзя.
            report.skipped[kind] = "тип не описан в config/signals.yaml"
            continue
        n = _settled(outcomes)
        if n < calibration.min_outcomes_per_kind:
            report.skipped[kind] = (
                f"исходов {n}, нужно {calibration.min_outcomes_per_kind} — "
                "статистики мало, вес не трогаем"
            )
            continue
        positives = sum(count for o, count in outcomes.items() if o in positive_outcomes)
        measured[kind] = {"n": n, "positives": positives, "conversion": positives / n}

    if not measured:
        log.info("calibration.skipped_all", kinds=len(report.skipped))
        return report

    average = sum(row["conversion"] for row in measured.values()) / len(measured)
    if average <= 0.0:
        # Ни одного положительного исхода ни по одному типу: относительная
        # конверсия не определена, и любое число здесь было бы выдумкой.
        for kind in measured:
            report.skipped[kind] = "ни одного положительного исхода — сравнивать не с чем"
        log.info("calibration.no_positives", kinds=len(measured))
        return report

    # Сильные сигналы первыми: так в таблице сразу видно, кто кормит воронку.
    order = sorted(measured.items(), key=lambda kv: (-kv[1]["conversion"], kv[0]))
    for kind, row in order:
        current = config.signals.kind(kind).weight
        proposed = _shift(current, row["conversion"] / average, calibration.max_weight_shift)
        report.per_kind[kind] = {
            "n": row["n"],
            "positives": row["positives"],
            "conversion": round(row["conversion"], 4),
            "current_weight": current,
            "proposed_weight": proposed,
            "applied": proposed != current,
        }

    report.yaml_snippet = _snippet(
        report.per_kind, average, sum(row["n"] for row in measured.values())
    )
    log.info(
        "calibration.done",
        kinds=len(report.per_kind),
        skipped=len(report.skipped),
        average_conversion=round(average, 4),
    )
    return report
