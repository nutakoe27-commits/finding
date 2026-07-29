"""Тесты движка ожиданий.

Главный сценарий системы здесь один: ноябрьская конференция прошлого года
в конце июля должна давать ожидание на ноябрь этого года с уже открытым
окном контакта. Если он сломается, система перестанет находить клиентов,
продолжая при этом бодро отчитываться о стадиях.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.expectations import (
    build_from_facts,
    contact_window,
    rescore_all,
    score_expectation,
)
from gtm.storage import repo
from gtm.storage.models import DatePrecision, Expectation, ExpectationKind, FactType

INN = "7701234567"


def _fact(
    session: Session,
    *,
    fact_type: str,
    uid: str,
    inn: str | None = INN,
    occurred_at: date | None = None,
    **payload,
):
    return repo.upsert_fact(
        session,
        source="test",
        source_uid=uid,
        fact_type=fact_type,
        inn=inn,
        occurred_at=occurred_at,
        payload=payload,
    )[0]


def _past_event(
    session: Session,
    when: date,
    *,
    uid: str | None = None,
    inn: str | None = INN,
    title: str = "Клиентская конференция",
    attendees: int | None = 900,
    venue: str | None = "Крокус Сити Холл",
):
    return _fact(
        session,
        fact_type=FactType.PAST_EVENT.value,
        uid=uid or f"event:{when.isoformat()}",
        inn=inn,
        occurred_at=when,
        title=title,
        attendees=attendees,
        venue=venue,
        city="Москва",
    )


def _of_kind(session: Session, kind: ExpectationKind) -> list[Expectation]:
    stmt = select(Expectation).where(Expectation.kind == kind.value).order_by(Expectation.id)
    return list(session.scalars(stmt))


def _only(session: Session, kind: ExpectationKind) -> Expectation:
    rows = _of_kind(session, kind)
    assert len(rows) == 1, f"ожидали одно ожидание {kind.value}, получили {len(rows)}"
    return rows[0]


# ------------------------------------------------------- ключевой сценарий


def test_november_conference_becomes_next_november_with_open_window(
    session: Session, config: Config, today: date, company_factory
):
    """Ноябрь 2025 → ноябрь 2026, окно контакта открыто в конце июля."""
    company_factory(INN)
    _past_event(session, date(2025, 11, 13))

    result = build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    assert expectation.expected_at == date(2026, 11, 13)
    assert expectation.expected_precision == DatePrecision.MONTH.value
    # lead_days=120, window_days=60 из config/signals.yaml
    assert expectation.window_opens_at == date(2026, 7, 16)
    assert expectation.window_closes_at == date(2026, 9, 14)
    assert expectation.window_opens_at <= today <= expectation.window_closes_at
    assert expectation.confidence == pytest.approx(0.7)
    assert expectation.expected_attendees == 900
    assert expectation.evidence["previous_event"]["date"] == "2025-11-13"
    assert "ноябре 2026" in expectation.evidence["reason"]
    assert result.count_out >= 1

    # То самое, ради чего всё построено: ежедневный запрос его видит.
    assert expectation.id in {e.id for e in repo.open_window_expectations(session, today)}


def test_rebuild_does_not_create_duplicates(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN)
    _past_event(session, date(2025, 11, 13))

    first = build_from_facts(session, config, today=today, run_id="run-1")
    before = session.scalars(select(Expectation)).all()
    second = build_from_facts(session, config, today=today, run_id="run-2")
    after = session.scalars(select(Expectation)).all()

    assert first.count_out > 0
    assert second.count_out == 0
    assert second.details["duplicates"] == len(before)
    assert len(after) == len(before)
    # Стадия должна быть видна в сводке запуска, иначе отладка вслепую.
    assert repo.run_summary(session, "run-1")[0]["stage"] == "expectations"


def test_repeats_raise_confidence_and_keep_biggest_attendees(
    session: Session, config: Config, today: date, company_factory
):
    """Повтор — то же мероприятие в том же месяце разных лет."""
    company_factory(INN)
    _past_event(session, date(2023, 11, 10), uid="e1", attendees=600)
    _past_event(session, date(2024, 11, 12), uid="e2", attendees=1100)
    _past_event(session, date(2025, 11, 13), uid="e3", attendees=None)

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    # base 0.7 + 0.1 * (3 - 1)
    assert expectation.confidence == pytest.approx(0.9)
    assert expectation.evidence["repeats"] == 3
    # Дата считается от последнего проведения, а не от первого.
    assert expectation.expected_at == date(2026, 11, 13)
    # Максимум из известных: у последнего мероприятия числа нет.
    assert expectation.expected_attendees == 1100


def test_two_events_in_different_months_give_two_expectations(
    session: Session, config: Config, today: date, company_factory
):
    """Мартовская конференция и декабрьский корпоратив — разные поводы."""
    company_factory(INN)
    _past_event(session, date(2025, 12, 20), uid="ny", title="Новогодний вечер")
    _past_event(session, date(2026, 3, 5), uid="conf", title="Весенний форум")

    build_from_facts(session, config, today=today)

    months = {e.expected_at.month for e in _of_kind(session, ExpectationKind.EVENT_ANNIVERSARY)}
    assert months == {12, 3}


def test_expectation_with_closed_window_is_kept_but_not_returned_daily(
    session: Session, config: Config, today: date, company_factory
):
    """Августовское мероприятие в конце июля уже упущено: окно закрылось
    в июне. Строку сохраняем — это история, — но в работу не отдаём."""
    company_factory(INN)
    _past_event(session, date(2025, 8, 20), uid="late", title="Летний форум")

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    assert expectation.expected_at == date(2026, 8, 20)
    assert expectation.window_closes_at == date(2026, 6, 21) < today
    assert expectation.id not in {e.id for e in repo.open_window_expectations(session, today)}


def test_fact_without_company_builds_nothing(session: Session, config: Config, today: date):
    """Факт, не сведённый к ИНН, ждёт разбора в карантине, а не идёт в работу."""
    _fact(
        session,
        fact_type=FactType.PAST_EVENT.value,
        uid="orphan",
        inn=None,
        occurred_at=date(2025, 11, 13),
        title="Конференция без организатора",
    )

    result = build_from_facts(session, config, today=today)

    assert session.scalars(select(Expectation)).all() == []
    assert result.count_in == 1
    assert result.details["unresolved"] == 1


# ------------------------------------------------------------ юбилей фирмы


def test_jubilee_15_years(session: Session, config: Config, today: date, company_factory):
    company_factory(INN, registered_at=date(2011, 11, 20))

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.COMPANY_JUBILEE)
    assert expectation.expected_at == date(2026, 11, 20)
    assert expectation.evidence["years"] == 15
    assert expectation.confidence == pytest.approx(0.45)
    # lead_days=150, window_days=75
    assert expectation.window_opens_at == date(2026, 6, 23)
    assert expectation.window_opens_at <= today <= expectation.window_closes_at


def test_major_jubilee_gets_confidence_bonus(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN, registered_at=date(2001, 11, 20))

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.COMPANY_JUBILEE)
    assert expectation.evidence["years"] == 25
    assert expectation.confidence == pytest.approx(0.6)


def test_passed_round_year_is_skipped_for_the_next_one(
    session: Session, config: Config, today: date, company_factory
):
    """15 лет исполнилось в марте — поезд ушёл, ждём двадцатилетия."""
    company_factory(INN, registered_at=date(2011, 3, 15))

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.COMPANY_JUBILEE)
    assert expectation.evidence["years"] == 20
    assert expectation.expected_at == date(2031, 3, 15)


def test_company_older_than_round_years_gives_nothing(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN, registered_at=date(1900, 5, 1))

    result = build_from_facts(session, config, today=today)

    assert _of_kind(session, ExpectationKind.COMPANY_JUBILEE) == []
    assert result.details["jubilee_out_of_range"] == 1


# ---------------------------------------------------------------- вакансии


@pytest.fixture()
def vacancy_signal_on(config: Config) -> Config:
    """В config/signals.yaml тип выключен вместе с закрытым API hh.

    Механику построения это не отменяет — она понадобится, как только
    появится замена источнику вакансий, поэтому тесты включают тип сами.
    """
    config.signals.kind(ExpectationKind.VACANCY_SIGNAL.value).enabled = True
    return config


def test_vacancy_gives_quarter_expectation(
    session: Session, config: Config, today: date, company_factory, vacancy_signal_on
):
    company_factory(INN)
    _fact(
        session,
        fact_type=FactType.VACANCY.value,
        uid="hh:1",
        occurred_at=date(2026, 7, 1),
        title="Event-менеджер",
        url="https://hh.ru/vacancy/1",
        keywords=["event-менеджер"],
    )

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.VACANCY_SIGNAL)
    # lead_days=90 от даты публикации
    assert expectation.expected_at == date(2026, 9, 29)
    assert expectation.expected_precision == DatePrecision.QUARTER.value
    assert expectation.confidence == pytest.approx(0.35)
    # Окно открывается сразу: наняли сейчас — писать можно сейчас.
    assert expectation.window_opens_at == date(2026, 7, 1)
    assert expectation.window_opens_at <= today <= expectation.window_closes_at


# ---------------------------------------------------------------- закупки


def test_disabled_kind_is_not_built_at_all(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN)
    _fact(
        session,
        fact_type=FactType.TENDER.value,
        uid="zak:1",
        occurred_at=date(2026, 7, 20),
        title="Аренда конференц-зала",
        event_date="2026-10-15",
    )

    assert config.signals.kind("tender").enabled is False
    build_from_facts(session, config, today=today)
    assert _of_kind(session, ExpectationKind.TENDER) == []

    config.signals.kind("tender").enabled = True
    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.TENDER)
    assert expectation.expected_at == date(2026, 10, 15)
    assert expectation.expected_precision == DatePrecision.DAY.value


def test_tender_without_event_date_falls_back_to_publication(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN)
    config.signals.kind("tender").enabled = True
    _fact(
        session,
        fact_type=FactType.TENDER.value,
        uid="zak:2",
        occurred_at=date(2026, 7, 20),
        title="Организация форума",
        notice_id="0173200001426000123",
        attendees=1200,
    )

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.TENDER)
    assert expectation.expected_at == date(2026, 7, 20) + timedelta(days=60)
    assert expectation.evidence["tender"]["notice_id"] == "0173200001426000123"
    assert expectation.expected_precision == DatePrecision.MONTH.value
    assert expectation.expected_attendees == 1200


# ------------------------------------------------------------------- окно


def test_contact_window_never_opens_after_the_event(config: Config):
    """window_days больше lead_days — окно всё равно закрывается датой
    мероприятия: писать про уже прошедшее незачем."""
    kind_cfg = config.signals.kind("venue_loss")  # lead 60, window 90
    opens, closes = contact_window(date(2026, 11, 13), kind_cfg)

    assert opens == date(2026, 11, 13) - timedelta(days=60)
    assert closes == date(2026, 11, 13)


# ---------------------------------------------------------------- скоринг


def test_score_with_all_bonuses(session: Session, config: Config, today: date, company_factory):
    """1.0 (вес) × 0.7 (уверенность) × 1.2 (ноябрь) × (1 + 0.30 + 0.50 + 0.20 + 0.15)."""
    company_factory(INN, region_code="77", city="Москва")
    _past_event(session, date(2025, 11, 13), attendees=900, venue="Крокус Сити Холл")

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    assert expectation.score == pytest.approx(1.806)


def test_score_without_data_gets_penalty(
    session: Session, config: Config, today: date, company_factory
):
    """Неизвестный масштаб — штраф, а не «наверное, крупное»."""
    company_factory(INN, region_code="66", city="Екатеринбург")
    _past_event(session, date(2025, 11, 13), attendees=None, venue=None)

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    # 1.0 × 0.7 × 1.2 × (1 − 0.20)
    assert expectation.score == pytest.approx(0.672)


def test_repeat_event_bonus(session: Session, config: Config, today: date, company_factory):
    company_factory(INN)
    _past_event(session, date(2024, 11, 12), uid="e1", attendees=900)
    _past_event(session, date(2025, 11, 13), uid="e2", attendees=900)

    build_from_facts(session, config, today=today)

    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    # 1.0 × 0.8 × 1.2 × (1 + 0.30 + 0.50 + 0.20 + 0.15 + 0.20)
    assert expectation.score == pytest.approx(2.256)


def test_event_manager_contact_raises_score(
    session: Session, config: Config, today: date, company_factory
):
    company = company_factory(INN)
    _past_event(session, date(2025, 11, 13))
    build_from_facts(session, config, today=today)
    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    base = expectation.score

    repo.upsert_contact(
        session,
        inn=INN,
        email="a.ivanova@example.ru",
        full_name="Анна Иванова",
        position="Event-менеджер",
        is_primary=True,
    )

    # Бонус 0.25 умножается на weight × confidence × сезонность.
    assert score_expectation(expectation, company, config) == pytest.approx(
        base + 0.25 * 1.0 * 0.7 * 1.2, abs=1e-4
    )


def test_generic_email_lowers_score(
    session: Session, config: Config, today: date, company_factory
):
    """info@ — письмо в никуда, приоритет такой заявки ниже."""
    company = company_factory(INN)
    _past_event(session, date(2025, 11, 13))
    build_from_facts(session, config, today=today)
    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    base = expectation.score

    repo.upsert_contact(session, inn=INN, email="info@example.ru", source="site", is_primary=True)

    assert score_expectation(expectation, company, config) == pytest.approx(
        base - 0.15 * 1.0 * 0.7 * 1.2, abs=1e-4
    )


def test_rescore_all_picks_up_new_weights(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN)
    _past_event(session, date(2025, 11, 13))
    build_from_facts(session, config, today=today)
    expectation = _only(session, ExpectationKind.EVENT_ANNIVERSARY)
    before = expectation.score

    config.signals.kind(ExpectationKind.EVENT_ANNIVERSARY.value).weight = 0.5
    changed = rescore_all(session, config, today=today)

    assert changed >= 1
    assert expectation.score == pytest.approx(before / 2)


def test_rescore_leaves_the_past_alone(
    session: Session, config: Config, today: date, company_factory, vacancy_signal_on
):
    """Приоритет решает, кому писать сегодня. Переписывать оценки архива
    незачем: в ежедневную выборку он всё равно не попадает."""
    company_factory(INN)
    _fact(
        session,
        fact_type=FactType.VACANCY.value,
        uid="hh:old",
        occurred_at=date(2025, 1, 1),
        title="Event-менеджер",
    )
    build_from_facts(session, config, today=today)
    stale = _only(session, ExpectationKind.VACANCY_SIGNAL)
    assert stale.expected_at < today
    before = stale.score

    config.signals.kind(ExpectationKind.VACANCY_SIGNAL.value).weight = 0.1
    rescore_all(session, config, today=today)

    assert stale.score == pytest.approx(before)
