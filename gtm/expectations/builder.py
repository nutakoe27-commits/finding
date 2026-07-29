"""Построение ожиданий из фактов.

Система — не поток, а календарь. Прошлогодняя конференция, дата регистрации
юрлица, вакансия event-менеджера и закупка выглядят по-разному, но отвечают
на один вопрос: «когда у этой компании ожидается мероприятие». Ответ живёт
в одной таблице, и ежедневная работа сводится к одному запросу по ней.

Два правила, из которых следует всё остальное:

1. Ожидание смотрит вперёд. Годовщина ноябрьского мероприятия — это ноябрь
   следующего года, а не прошедший. Иначе окно контакта открывается задним
   числом и сигнал бесполезен: зал к этому моменту уже забронирован.
2. Строим только то, что можно проверить по своей же базе. Ни одного
   внешнего запроса на этой стадии — она отрабатывает на 300 фактах
   за секунды и стоит ноль.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from gtm.config import Config, SignalKind
from gtm.observability import StageResult, get_logger, stage
from gtm.resolve.ogrn import is_year_reliable, registration_year
from gtm.storage import repo
from gtm.storage.models import DatePrecision, Expectation, ExpectationKind, Fact, FactType

STAGE_NAME = "expectations"

# Технические константы. Бизнес-параметры (окна, уверенность, круглые даты)
# берутся из config/signals.yaml.
#
# Закупка без явной даты мероприятия: от публикации до проведения у
# госзаказчика обычно около двух месяцев — процедура плюс подготовка.
_TENDER_FALLBACK_LEAD_DAYS = 60
# Месяцы в предложном падеже: evidence.reason читает человек, а не парсер.
_MONTHS_PREP = (
    "январе",
    "феврале",
    "марте",
    "апреле",
    "мае",
    "июне",
    "июле",
    "августе",
    "сентябре",
    "октябре",
    "ноябре",
    "декабре",
)


def _month_year(value: date) -> str:
    return f"{_MONTHS_PREP[value.month - 1]} {value.year}"


def _positive_int(value: Any) -> int | None:
    """Ноль и мусор — это «не знаем», а не «ноль человек»."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_date(value: Any) -> date | None:
    """Даты в payload лежат строками — JSON другого типа не знает."""
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _note(result: StageResult | None, key: str) -> None:
    if result is not None:
        result.note(key)


def _seen(result: StageResult | None) -> None:
    if result is not None:
        result.bump_in()


# --------------------------------------------------------------------- окно


def contact_window(expected_at: date, kind_cfg: SignalKind) -> tuple[date, date]:
    """Когда писать: (окно открылось, окно закрылось).

    Закрытие упирается в саму дату мероприятия — писать про мероприятие
    после того, как оно прошло, смысла нет, даже если окно по конфигу
    ещё «не истекло».
    """
    opens = expected_at - timedelta(days=kind_cfg.lead_days)
    closes = min(expected_at, opens + timedelta(days=kind_cfg.window_days))
    return opens, closes


def _upsert(
    session: Session,
    result: StageResult | None,
    *,
    inn: str,
    kind: str,
    kind_cfg: SignalKind,
    expected_at: date,
    confidence: float,
    precision: str,
    expected_attendees: int | None = None,
    source_fact_id: int | None = None,
    evidence: dict[str, Any] | None = None,
    window: tuple[date, date] | None = None,
) -> Expectation:
    """Единственная точка создания ожиданий: дедупликация живёт в repo.

    `window` задаётся явно только там, где ожидаемая дата — не настоящая дата
    события, а условный якорь (юбилей по году из ОГРН). В остальных случаях
    окно считается от даты.
    """
    opens, closes = window if window is not None else contact_window(expected_at, kind_cfg)
    expectation, created = repo.upsert_expectation(
        session,
        inn=inn,
        kind=kind,
        expected_at=expected_at,
        window_opens_at=opens,
        window_closes_at=closes,
        confidence=round(min(confidence, kind_cfg.max_confidence), 4),
        expected_precision=precision,
        expected_attendees=expected_attendees,
        source_fact_id=source_fact_id,
        evidence=evidence,
    )
    if result is not None:
        if created:
            result.bump_out()
        else:
            result.note("duplicates")
    return expectation


def _has_company(session: Session, fact: Fact, result: StageResult | None) -> bool:
    """Ожидание без компании нежизнеспособно: писать некому и фильтровать
    нечем. Такие факты ждут разбора в карантине, а не идут в работу."""
    if fact.inn is None:
        _note(result, "unresolved")
        return False
    if repo.get_company(session, fact.inn) is None:
        _note(result, "no_company")
        return False
    return True


# ------------------------------------------------------- годовщина события


def _next_anniversary(occurred_at: date, today: date) -> date:
    """Та же дата в следующем году относительно проведения.

    Если она уже позади — двигаем дальше вперёд: ожидание, смотрящее в
    прошлое, не даст ни одного письма, а календарь замусорит.
    """
    expected = occurred_at + relativedelta(years=1)
    while expected < today:
        expected += relativedelta(years=1)
    return expected


def build_event_anniversaries(
    session: Session,
    config: Config,
    *,
    today: date,
    result: StageResult | None = None,
) -> list[Expectation]:
    """Самый сильный сигнал: проводили в ноябре — проведут и в этом ноябре.

    Группируем по (ИНН, месяц): «декабрьский корпоратив» и «мартовская
    конференция» у одной компании — два разных ожидания, а одно и то же
    мероприятие за три года — один повтор с растущей уверенностью.
    """
    kind = ExpectationKind.EVENT_ANNIVERSARY.value
    kind_cfg = config.signals.kind(kind)
    if not kind_cfg.enabled:
        return []

    groups: dict[tuple[str, int], list[Fact]] = {}
    for fact in repo.facts_by_type(session, FactType.PAST_EVENT.value, only_resolved=False):
        _seen(result)
        if fact.occurred_at is None:
            _note(result, "event_without_date")
            continue
        if not _has_company(session, fact, result):
            continue
        groups.setdefault((str(fact.inn), fact.occurred_at.month), []).append(fact)

    built: list[Expectation] = []
    for (inn, _month), facts in groups.items():
        facts.sort(key=lambda f: (f.occurred_at, f.id))
        last = facts[-1]
        # Повтор — то же мероприятие в разные годы. Два факта об одном
        # мероприятии из двух источников повтором не считаются.
        repeats = len({f.occurred_at.year for f in facts})
        expected_at = _next_anniversary(last.occurred_at, today)
        confidence = kind_cfg.base_confidence + kind_cfg.confidence_per_repeat * (repeats - 1)

        payload = last.payload or {}
        attendees = max(
            (
                size
                for size in (_positive_int((f.payload or {}).get("attendees")) for f in facts)
                if size is not None
            ),
            default=None,
        )
        title = payload.get("title") or "мероприятие"
        reason = (
            f"Проводили «{title}» в {_month_year(last.occurred_at)}"
            f"{f', повторов: {repeats}' if repeats > 1 else ''} — "
            f"ждём повторения в {_month_year(expected_at)}"
        )
        built.append(
            _upsert(
                session,
                result,
                inn=inn,
                kind=kind,
                kind_cfg=kind_cfg,
                expected_at=expected_at,
                confidence=confidence,
                precision=DatePrecision.MONTH.value,
                expected_attendees=attendees,
                source_fact_id=last.id,
                evidence={
                    "reason": reason,
                    "previous_event": {
                        "title": payload.get("title"),
                        "date": last.occurred_at.isoformat(),
                        "attendees": _positive_int(payload.get("attendees")),
                        "venue": payload.get("venue"),
                    },
                    # Нужен скорингу (бонус repeat_event) и тексту письма.
                    "repeats": repeats,
                },
            )
        )
    return built


# --------------------------------------------------------- юбилей компании


def _jubilee_from_ogrn(
    session: Session,
    config: Config,
    company,
    *,
    today: date,
    result: StageResult | None = None,
) -> Expectation | None:
    """Круглая годовщина по году из ОГРН, когда точной даты нет.

    ОГРН несёт год внесения записи во второй и третьей цифрах — это бесплатно
    заменяет платный запрос в реестр. Ценой того, что месяц неизвестен:
    ожидаемая дата здесь условный якорь, а не дата события, поэтому окно
    контакта широкое и задаётся явно.

    Известное ограничение: если позже появится точная дата регистрации,
    построенное здесь ожидание не пересчитывается — на компанию берётся один
    юбилей в год, и первым выигрывает тот, что построился раньше. На практике
    реестр либо есть, либо нет, и менять источник посреди сезона незачем.
    """
    kind = ExpectationKind.COMPANY_JUBILEE.value
    kind_cfg = config.signals.kind(kind)
    ogrn_cfg = kind_cfg.ogrn_year
    if not ogrn_cfg.enabled:
        _note(result, "no_registered_at")
        return None

    year = registration_year(company.ogrn)
    if year is None or not is_year_reliable(year):
        # 2002-2004 — годы массовой перерегистрации: год в ОГРН там
        # не год основания, и юбилей по нему был бы выдумкой.
        _note(result, "ogrn_year_unreliable" if year else "no_registered_at")
        return None

    candidates = [y for y in sorted(kind_cfg.round_years) if y <= ogrn_cfg.max_round_years]
    found = next(((y, year + y) for y in candidates if year + y >= today.year), None)
    if found is None:
        _note(result, "jubilee_out_of_range")
        return None
    years, jubilee_year = found

    anchor = date(jubilee_year, ogrn_cfg.anchor_month, 1)
    # Один юбилей на компанию в год: месяц условный, и второе ожидание
    # на тот же год означало бы два письма об одном и том же.
    if any(
        e.kind == kind and e.expected_at.year == jubilee_year
        for e in repo.expectations_for_company(session, company.inn, kind=kind)
    ):
        _note(result, "jubilee_already_known")
        return None

    confidence = kind_cfg.base_confidence - ogrn_cfg.confidence_penalty
    if years in set(kind_cfg.major_years):
        confidence += kind_cfg.major_confidence_bonus

    # Окно задаём явно: якорь — не дата события, обрезать окно по нему нельзя.
    opens = anchor - timedelta(days=kind_cfg.lead_days)
    closes = opens + timedelta(days=ogrn_cfg.window_days)

    return _upsert(
        session,
        result,
        inn=company.inn,
        kind=kind,
        kind_cfg=kind_cfg,
        expected_at=anchor,
        confidence=max(confidence, 0.0),
        precision=DatePrecision.YEAR.value,
        window=(opens, closes),
        evidence={
            "reason": f"В {jubilee_year} году компании исполняется {years} лет",
            "years": years,
            "registration_year": year,
            "year_source": "ogrn",
            "month_unknown": True,
        },
    )


def build_company_jubilees(
    session: Session,
    config: Config,
    *,
    today: date,
    result: StageResult | None = None,
) -> list[Expectation]:
    """Круглая дата компании считается арифметикой от ЕГРЮЛ.

    Конкурентов на этом сигнале практически нет: дата регистрации лежит
    в открытом реестре, но ей никто не пользуется.
    """
    kind = ExpectationKind.COMPANY_JUBILEE.value
    kind_cfg = config.signals.kind(kind)
    if not kind_cfg.enabled:
        return []
    round_years = sorted(kind_cfg.round_years)
    if not round_years:
        # Без списка круглых дат сигнала нет — молча строить «любую» годовщину
        # нельзя, это шум на каждую компанию в базе.
        return []
    major_years = set(kind_cfg.major_years)

    built: list[Expectation] = []
    for company in repo.iter_companies(session):
        _seen(result)
        if company.registered_at is None:
            # Точной даты нет — пробуем год из ОГРН. Это бесплатная замена
            # платному реестру, ценой неизвестного месяца.
            from_ogrn = _jubilee_from_ogrn(session, config, company, today=today, result=result)
            if from_ogrn is not None:
                built.append(from_ogrn)
            continue

        # Ближайшая круглая дата, которая ещё не прошла. Прошедшие юбилеи
        # не история, а мусор: отмечают их один раз.
        upcoming = (
            (years, company.registered_at + relativedelta(years=years)) for years in round_years
        )
        found = next(((y, d) for y, d in upcoming if d >= today), None)
        if found is None:
            _note(result, "jubilee_out_of_range")
            continue
        years, expected_at = found

        confidence = kind_cfg.base_confidence
        if years in major_years:
            confidence += kind_cfg.major_confidence_bonus
        reason = (
            f"В {_month_year(expected_at)} компании исполняется {years} лет "
            f"(зарегистрирована {company.registered_at:%d.%m.%Y})"
        )
        built.append(
            _upsert(
                session,
                result,
                inn=company.inn,
                kind=kind,
                kind_cfg=kind_cfg,
                expected_at=expected_at,
                confidence=confidence,
                # День регистрации известен точно, но празднуют «примерно тогда»,
                # а не в этот день. Точность выше месяца была бы враньём.
                precision=DatePrecision.MONTH.value,
                evidence={
                    "reason": reason,
                    "years": years,
                    "registered_at": company.registered_at.isoformat(),
                },
            )
        )
    return built


# ------------------------------------------------------------- по вакансиям


def build_from_vacancies(
    session: Session,
    config: Config,
    *,
    today: date,
    result: StageResult | None = None,
) -> list[Expectation]:
    """Наняли event-менеджера — мероприятия будут.

    Сигнал слабый и размазанный по времени: точной даты нет, есть квартал
    «через несколько месяцев после найма». Зато срабатывает раньше всех
    остальных — людей нанимают до того, как выбирают зал.
    """
    kind = ExpectationKind.VACANCY_SIGNAL.value
    kind_cfg = config.signals.kind(kind)
    if not kind_cfg.enabled:
        return []

    built: list[Expectation] = []
    for fact in repo.facts_by_type(session, FactType.VACANCY.value, only_resolved=False):
        _seen(result)
        payload = fact.payload or {}
        published = fact.occurred_at or _as_date(payload.get("published_at"))
        if published is None:
            _note(result, "vacancy_without_date")
            continue
        if not _has_company(session, fact, result):
            continue

        # Окно открывается сразу: lead_days от публикации — это и есть
        # ожидаемая дата, писать можно с первого дня.
        expected_at = published + timedelta(days=kind_cfg.lead_days)
        title = payload.get("title") or "event-менеджер"
        reason = (
            f"Ищут «{title}» (вакансия от {published:%d.%m.%Y}) — "
            f"строят событийную функцию, мероприятие ожидаем к {_month_year(expected_at)}"
        )
        built.append(
            _upsert(
                session,
                result,
                inn=str(fact.inn),
                kind=kind,
                kind_cfg=kind_cfg,
                expected_at=expected_at,
                confidence=kind_cfg.base_confidence,
                precision=DatePrecision.QUARTER.value,
                source_fact_id=fact.id,
                evidence={
                    "reason": reason,
                    "vacancy": {
                        "title": payload.get("title"),
                        "published_at": published.isoformat(),
                        "url": payload.get("url"),
                        "keywords": payload.get("keywords") or [],
                    },
                },
            )
        )
    return built


# --------------------------------------------------------------- по закупкам


def build_from_tenders(
    session: Session,
    config: Config,
    *,
    today: date,
    result: StageResult | None = None,
) -> list[Expectation]:
    """Закупка на проведение мероприятия — намерение с датой и бюджетом.

    Оговорка, которую придётся объяснить клиенту: госзаказчик обязан
    проводить конкурс, это участие в процедуре, а не письмо и договор.
    """
    kind = ExpectationKind.TENDER.value
    kind_cfg = config.signals.kind(kind)
    if not kind_cfg.enabled:
        return []

    built: list[Expectation] = []
    for fact in repo.facts_by_type(session, FactType.TENDER.value, only_resolved=False):
        _seen(result)
        payload = fact.payload or {}
        event_date = _as_date(payload.get("event_date"))
        if event_date is not None:
            expected_at = event_date
            # Дата мероприятия названа в самой закупке — это точный день.
            precision = DatePrecision.DAY.value
        else:
            published = fact.occurred_at or _as_date(payload.get("published_at"))
            if published is None:
                _note(result, "tender_without_date")
                continue
            expected_at = published + timedelta(days=_TENDER_FALLBACK_LEAD_DAYS)
            precision = DatePrecision.MONTH.value
        if not _has_company(session, fact, result):
            continue

        title = payload.get("title") or "закупка на проведение мероприятия"
        reason = f"Закупка «{title}» — мероприятие ожидается в {_month_year(expected_at)}"
        built.append(
            _upsert(
                session,
                result,
                inn=str(fact.inn),
                kind=kind,
                kind_cfg=kind_cfg,
                expected_at=expected_at,
                confidence=kind_cfg.base_confidence,
                precision=precision,
                expected_attendees=_positive_int(payload.get("attendees")),
                source_fact_id=fact.id,
                evidence={
                    "reason": reason,
                    "tender": {
                        "title": payload.get("title"),
                        "notice_id": payload.get("notice_id"),
                        "law": payload.get("law"),
                        "price": payload.get("price"),
                        "url": payload.get("url"),
                    },
                },
            )
        )
    return built


# ------------------------------------------------------------------ стадия


def build_from_facts(
    session: Session,
    config: Config,
    *,
    today: date,
    run_id: str | None = None,
) -> StageResult:
    """Прогнать все включённые типы сигналов. Безопасно перезапускаемо.

    Повторный запуск не плодит дублей: ключ inn|тип|год-месяц один и тот же,
    а repo.upsert_expectation при совпадении только усиливает ожидание.
    """
    if run_id is None:
        result = StageResult(stage=STAGE_NAME)
        _build_all(session, config, today=today, result=result)
        return result

    with stage(session, run_id, STAGE_NAME) as result:
        _build_all(session, config, today=today, result=result)
    return result


def _build_all(session: Session, config: Config, *, today: date, result: StageResult) -> None:
    from gtm.expectations.scoring import score_expectation

    built: list[Expectation] = []
    for build in (
        build_event_anniversaries,
        build_company_jubilees,
        build_from_vacancies,
        build_from_tenders,
    ):
        built.extend(build(session, config, today=today, result=result))

    # Приоритет считаем сразу: ожидание без score бесполезно — по нему
    # нельзя ни отсортировать список менеджеру, ни отсечь мелочь.
    for expectation in built:
        company = repo.get_company(session, expectation.inn)
        expectation.score = score_expectation(expectation, company, config)
    session.flush()

    open_now = sum(1 for e in built if e.window_opens_at <= today <= e.window_closes_at)
    result.details["open_window"] = open_now
    get_logger("gtm.expectations").info(
        "expectations.built",
        today=today.isoformat(),
        facts=result.count_in,
        created=result.count_out,
        open_window=open_now,
    )
