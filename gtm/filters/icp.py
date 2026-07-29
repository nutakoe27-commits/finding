"""Фильтр ICP: кому вообще имеет смысл писать.

Второе звено конвейера и главный экономящий механизм. Работает только на
том, что уже лежит в базе: ни одного внешнего запроса. Обратный порядок —
обогатить всё, потом фильтровать — сжёг бы бюджет за неделю, потому что
из трёхсот собранных фактов до письма доходит десяток.

Ключевой критерий — масштаб мероприятия, а не размер компании. Если в
прошлом году собирали 80 человек, это не наш клиент даже при штате в пять
тысяч: на такой вместимости площадка конкурирует с сотнями московских залов
и проигрывает по локации.

Чего фильтр не делает — не отсекает по незаполненным данным. Компания без
штата и выручки могла просто не дойти до обогащения; такие проходят дальше
с пометкой `details["needs_enrichment"]`, по которой стадия обогащения
понимает, что именно дозапросить.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from gtm.config import Config, GeoConfig
from gtm.observability import StageResult, get_logger, stage
from gtm.storage import repo
from gtm.storage.models import Company, Expectation, ExpectationStatus, Fact

STAGE_NAME = "filter"

# Технические константы. Пороги, регионы, ключевые слова — в config/icp.yaml.
#
# Первые две цифры ИНН — код региона регистрации. Запасной вариант, когда
# компанию ещё не обогащали реестром: география известна бесплатно и сразу.
_INN_REGION_LENGTH = 2
# Всё, кроме букв и цифр, — разделитель: «Москва, ул. Шарикоподшипниковская»
# и «г.Москва» должны одинаково попадать под сравнение с городом из конфига.
_NON_WORD = re.compile(r"[^0-9a-zа-я]+")
# Адрес площадки в reason — для человека, а не для парсера: длинный хвост
# только мешает читать причину отказа.
_LOCATION_IN_REASON = 60

_SOURCE_TITLES = {
    "event": "по прошлому мероприятию",
    "headcount": "по штату",
    "unknown": "неизвестен",
}


@dataclass
class Verdict:
    """Ответ фильтра. Целиком уезжает в `expectation.filter_verdict`.

    Через месяц вопрос «почему этой компании не написали» задаст клиент,
    и ответ должен лежать в строке ожидания, а не восстанавливаться из логов.
    """

    passed: bool
    code: str | None = None
    reason: str = ""
    attendees_estimate: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "code": self.code,
            "reason": self.reason,
            "attendees_estimate": self.attendees_estimate,
            "details": dict(self.details),
        }


# ------------------------------------------------------------- вспомогательное


def _positive_int(value: Any) -> int | None:
    """Ноль и мусор — это «не знаем», а не «ноль человек»."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize(text: Any) -> str:
    if not text:
        return ""
    return _NON_WORD.sub(" ", str(text).lower().replace("ё", "е")).strip()


def _reject(config: Config, code: str, detail: str, **fields: Any) -> Verdict:
    """Причина собирается из формулировки конфига и конкретных чисел.

    Код всегда берётся из `reject_codes`: отсев без кода — это тихая потеря
    компании, которую потом не найти ни в сводке, ни в базе.
    """
    title = config.icp.reject_codes.get(code, code)
    return Verdict(passed=False, code=code, reason=f"{title}: {detail}", **fields)


# ------------------------------------------------------------------ масштаб


def estimate_attendees(
    expectation: Expectation, company: Company | None, config: Config
) -> tuple[int | None, str]:
    """(оценка числа участников, источник оценки: event | headcount | unknown).

    Известное число участников прошлого мероприятия важнее оценки по штату:
    компания на пять тысяч сотрудников может каждый год собирать восемьдесят
    партнёров, и это её настоящий масштаб.
    """
    known = _positive_int(expectation.expected_attendees)
    if known is not None:
        return known, "event"

    headcount = _positive_int(company.headcount) if company is not None else None
    if headcount is not None:
        estimate = round(headcount * config.icp.scale.attendees_from_headcount_ratio)
        if estimate > 0:
            return int(estimate), "headcount"
    return None, "unknown"


# ---------------------------------------------------------------- география


def _company_region(expectation: Expectation, company: Company | None) -> str | None:
    """Регион компании. Реестр его проставляет, но до обогащения его нет —
    тогда берём код из ИНН, он с него начинается."""
    code = (company.region_code or "").strip() if company is not None else ""
    if code:
        return code
    inn = (company.inn if company is not None else expectation.inn) or ""
    prefix = inn[:_INN_REGION_LENGTH]
    return prefix if prefix.isdigit() else None


def _event_location(session: Session, expectation: Expectation) -> str:
    """Где мероприятие: город из доказательной базы или из факта-источника.

    Оба лежат в своей базе, внешних запросов нет. Адрес площадки годится
    наравне с городом: «Москва, ул. …» называет город явно.
    """
    evidence = expectation.evidence or {}
    previous = evidence.get("previous_event")
    candidates: list[Any] = [evidence.get("city"), evidence.get("venue")]
    if isinstance(previous, dict):
        candidates += [previous.get("city"), previous.get("venue")]

    if expectation.source_fact_id is not None:
        # В repo нет выборки факта по id — единственное место, где читаем
        # таблицу напрямую (см. отчёт по модулю).
        fact = session.get(Fact, expectation.source_fact_id)
        payload = (fact.payload or {}) if fact is not None else {}
        candidates += [payload.get("city"), payload.get("venue"), payload.get("address")]

    return " ".join(str(value) for value in candidates if value).strip()


def _event_in_target_city(location: str, geo: GeoConfig) -> bool:
    text = _normalize(location)
    if not text:
        return False
    return any(city and city in text for city in (_normalize(name) for name in geo.event_cities))


# ------------------------------------------------------------ размер и отрасль


def _latest_revenue(company: Company | None) -> float | None:
    """Выручка за самый свежий известный год. Прошлогодняя лучше, чем никакой."""
    if company is None:
        return None
    known: list[tuple[int, float]] = []
    for year, value in (company.revenue or {}).items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        try:
            known.append((int(year), float(value)))
        except (TypeError, ValueError):
            continue
    positive = [(year, value) for year, value in known if value > 0]
    return max(positive)[1] if positive else None


def _okved_matches(code: str, prefix: str) -> bool:
    """«68.» покрывает «68» и «68.20.2», но не «680.1».

    Коды ОКВЭД иерархические, и сравнение голым startswith даёт ложные
    срабатывания на соседних группах.
    """
    head = prefix.strip().rstrip(".")
    return bool(head) and (code == head or code.startswith(head + "."))


@lru_cache(maxsize=8)
def _compiled_name_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Регулярки из конфига компилируются один раз на набор, а не на компанию:
    за прогон фильтр проходит сотни строк.

    Битая регулярка не роняет прогон — иначе опечатка в YAML останавливает
    систему целиком, — но обязана быть видна в логе.
    """
    log = get_logger("gtm.filters")
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            log.warning("icp.bad_name_pattern", pattern=pattern, error=str(exc))
    return tuple(compiled)


# ------------------------------------------------------------------ проверка


def check_icp(
    session: Session,
    expectation: Expectation,
    company: Company | None,
    config: Config,
    *,
    today: date,
) -> Verdict:
    """Проверить одно ожидание. Ни одного внешнего запроса.

    Порядок проверок — от самой дешёвой и самой отсеивающей к остальным:
    смысла считать выручку компании, которая в стоп-листе или собирает
    восемьдесят человек, нет.
    """
    # Импорт внутри функции: suppression возвращает объявленный здесь Verdict,
    # и на уровне модулей это был бы цикл.
    from gtm.filters.suppression import check_suppression

    icp = config.icp
    details: dict[str, Any] = {}
    needs: list[str] = []

    # 1. Стоп-лист: один индексный запрос, отсекает намертво.
    suppressed = check_suppression(session, expectation.inn)
    if not suppressed.passed:
        return suppressed

    # 2. Масштаб — ключевой критерий и самый частый отсев.
    estimate, source = estimate_attendees(expectation, company, config)
    details["attendees_source"] = source
    scale = icp.scale
    if estimate is None:
        needs.append("attendees")
    elif estimate < scale.min_attendees:
        return _reject(
            config,
            "too_small",
            f"оценка {estimate} человек ({_SOURCE_TITLES[source]}) "
            f"против порога {scale.min_attendees}",
            attendees_estimate=estimate,
            details=details,
        )
    elif estimate > scale.hard_max_attendees:
        # Отсекаем по «не вмещаем» только когда размер известен по прошлому
        # мероприятию. Оценка по штату — догадка, и на крупных компаниях она
        # систематически завышает: на юбилей компании из 5600 человек приходит
        # не 60% штата. Отбрасывать по ней — терять как раз лучших заказчиков,
        # поэтому оценку подрезаем до вместимости и пропускаем дальше.
        if source == "event":
            return _reject(
                config,
                "too_big",
                f"оценка {estimate} человек, зал вмещает {scale.hard_max_attendees}",
                attendees_estimate=estimate,
                details=details,
            )
        details["attendees_capped_from"] = estimate
        estimate = scale.hard_max_attendees

    # 3. География. Компания не из наших регионов — не приговор: важно,
    # где пройдёт мероприятие, а не где стоит юрлицо.
    region = _company_region(expectation, company)
    details["company_region"] = region
    if region not in icp.geo.company_regions:
        location = _event_location(session, expectation)
        in_target = icp.geo.allow_any_region_if_event_in_moscow and _event_in_target_city(
            location, icp.geo
        )
        details["event_in_target_city"] = in_target
        if not in_target:
            return _reject(
                config,
                "wrong_geo",
                f"регион компании {region or 'неизвестен'}, "
                f"место мероприятия «{location[:_LOCATION_IN_REASON] or 'неизвестно'}»",
                attendees_estimate=estimate,
                details=details,
            )

    # 4. Размер компании. Неизвестные штат и выручка — не отказ: компания
    # могла просто не дойти до обогащения. Отказ только когда всё, что
    # известно, ниже порога.
    criteria = icp.company
    headcount = _positive_int(company.headcount) if company is not None else None
    revenue = _latest_revenue(company)
    if headcount is None:
        needs.append("headcount")
    if revenue is None:
        needs.append("revenue")
    passed_checks = [
        value >= threshold
        for value, threshold in (
            (headcount, criteria.min_headcount),
            (revenue, criteria.min_revenue_rub),
        )
        if value is not None
    ]
    if passed_checks and not any(passed_checks):
        known = ", ".join(
            (
                f"штат {headcount}" if headcount is not None else "штат неизвестен",
                f"выручка {revenue / 1e6:.0f} млн ₽"
                if revenue is not None
                else "выручка неизвестна",
            )
        )
        return _reject(
            config,
            "too_small_company",
            f"{known} при порогах {criteria.min_headcount} и "
            f"{criteria.min_revenue_rub / 1e6:.0f} млн ₽",
            attendees_estimate=estimate,
            details=details,
        )

    # 5. Возраст. У компании младше года не бывает годовщин мероприятий,
    # а у нас нет ни одного повода к ней прийти.
    registered = company.registered_at if company is not None else None
    if registered is None:
        needs.append("registered_at")
    elif relativedelta(today, registered).years < criteria.min_age_years:
        return _reject(
            config,
            "too_small_company",
            f"зарегистрирована {registered:%d.%m.%Y}, меньше {criteria.min_age_years} лет",
            attendees_estimate=estimate,
            details=details,
        )

    # 6. Исключённые отрасли: конкуренты-площадки и агентства, с которыми
    # работают отдельным каналом, а не холодным письмом.
    okved = (company.okved or "").strip() if company is not None else ""
    if not okved:
        needs.append("okved")
    for prefix in icp.exclude.okved_prefixes:
        if okved and _okved_matches(okved, prefix):
            details["matched_okved"] = prefix
            return _reject(
                config,
                "excluded_industry",
                f"ОКВЭД {okved} под исключением «{prefix}»",
                attendees_estimate=estimate,
                details=details,
            )
    names = " / ".join(
        part for part in ((company.name, company.name_full) if company is not None else ()) if part
    )
    for pattern in _compiled_name_patterns(tuple(icp.exclude.name_patterns)):
        if pattern.search(names):
            details["matched_name_pattern"] = pattern.pattern
            return _reject(
                config,
                "excluded_industry",
                f"название «{names}» под исключением «{pattern.pattern}»",
                attendees_estimate=estimate,
                details=details,
            )

    # 7. Окно контакта. Ожидание могло пролежать до следующего прогона —
    # писать про мероприятие, до которого осталась неделя, поздно.
    if expectation.window_closes_at < today:
        return _reject(
            config,
            "window_closed",
            f"окно закрылось {expectation.window_closes_at:%d.%m.%Y}, сегодня {today:%d.%m.%Y}",
            attendees_estimate=estimate,
            details=details,
        )

    if needs:
        details["needs_enrichment"] = needs
    summary = (
        f"оценка {estimate} человек ({_SOURCE_TITLES[source]})"
        if estimate is not None
        else "масштаб неизвестен, нужно обогащение"
    )
    return Verdict(
        passed=True,
        code=None,
        reason=f"Проходит ICP: {summary}",
        attendees_estimate=estimate,
        details=details,
    )


# ------------------------------------------------------------------- стадия


def run_filter(
    session: Session,
    config: Config,
    *,
    today: date,
    run_id: str,
    limit: int | None = None,
) -> StageResult:
    """Прогнать ICP по ожиданиям с открытым сегодня окном.

    Каждое ожидание получает статус (`ready` или `filtered_out`) и вердикт
    в строке. Отсевы считаются по кодам: система, которая тихо теряет
    компании, отлаживается неделями.
    """
    expectations = repo.open_window_expectations(
        session, today, statuses=[ExpectationStatus.NEW.value], limit=limit
    )
    log = get_logger("gtm.filters")

    with stage(session, run_id, STAGE_NAME, count_in=len(expectations)) as result:
        gaps: list[int] = []
        for expectation in expectations:
            company = repo.get_company(session, expectation.inn)
            verdict = check_icp(session, expectation, company, config, today=today)
            expectation.filter_verdict = {**verdict.as_dict(), "checked_at": today.isoformat()}
            if verdict.passed:
                repo.set_status(session, expectation, ExpectationStatus.READY.value)
                result.bump_out()
                if verdict.details.get("needs_enrichment"):
                    gaps.append(expectation.id)
            else:
                repo.set_status(session, expectation, ExpectationStatus.FILTERED_OUT.value)
                # Ключ детали — код отказа: так его видно и в сводке прогона,
                # и в `gtm filter`.
                result.note(str(verdict.code))
        # Списком, а не счётчиком: числовые детали стадии — это отсевы,
        # а прошедшие с дырами в данных отсевом не являются.
        result.details["needs_enrichment"] = gaps
        session.flush()

    log.info(
        "filter.done",
        today=today.isoformat(),
        count_in=result.count_in,
        count_out=result.count_out,
        rejects={k: v for k, v in result.details.items() if isinstance(v, int)},
    )
    return result
