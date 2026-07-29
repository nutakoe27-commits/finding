"""Досье: что попадает в письмо и чего в нём не должно оказаться."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import pytest
from dateutil.relativedelta import relativedelta

from gtm.enrich import build_dossier, run_enrich
from gtm.spend import BudgetExceeded, SpendGuard
from gtm.storage import repo
from gtm.storage.models import ExpectationKind, ExpectationStatus

REGISTRY_ENRICH = "gtm.collectors.registry.enrich_company"


@pytest.fixture()
def guard(session):
    return SpendGuard(session, stage="test")


def _ready(
    session,
    *,
    inn: str,
    kind: str = ExpectationKind.EVENT_ANNIVERSARY.value,
    expected_at: date = date(2026, 11, 20),
    score: float = 0.5,
    attendees: int | None = None,
    evidence: dict | None = None,
):
    """Ожидание, прошедшее фильтр: ровно то, с чем работает стадия."""
    expectation, _ = repo.upsert_expectation(
        session,
        inn=inn,
        kind=kind,
        expected_at=expected_at,
        window_opens_at=expected_at - timedelta(days=120),
        window_closes_at=expected_at,
        confidence=0.7,
        expected_attendees=attendees,
        evidence=evidence or {},
    )
    expectation.status = ExpectationStatus.READY.value
    expectation.score = score
    session.flush()
    return expectation


def _past_event(**fields) -> dict:
    previous = {
        "title": "Итоги года",
        "date": "2025-11-20",
        "attendees": 600,
        "venue": "Крокус Экспо",
    }
    previous.update(fields)
    return {"previous_event": previous, "repeats": 2, "reason": "заглушка из билдера"}


# ------------------------------------------------------------ полное досье


def test_full_dossier_collects_every_block(session, config, company_factory, guard):
    company_factory("7701234567", name="ООО Ромашка", city="Москва", headcount=900)
    repo.upsert_contact(
        session,
        inn="7701234567",
        email="a.petrova@romashka.ru",
        full_name="Анна Петрова",
        position="event-менеджер",
        is_primary=True,
    )
    expectation = _ready(session, inn="7701234567", attendees=600, evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["company"]["name"] == "ООО Ромашка"
    assert dossier["company"]["headcount"] == 900
    assert dossier["company"]["revenue_last"] == 1_500_000_000.0
    assert dossier["company"]["registered_at"] == "2011-03-15"
    assert dossier["event"] == {
        "title": "Итоги года",
        "date": "2025-11-20",
        "attendees": 600,
        "venue": "Крокус Экспо",
        "is_repeat": True,
        "years_repeated": 2,
    }
    assert dossier["contact"] == {
        "full_name": "Анна Петрова",
        "position": "event-менеджер",
        "email": "a.petrova@romashka.ru",
        "is_generic": False,
    }
    assert dossier["gaps"] == []
    assert dossier["reason"] == (
        "в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо"
    )
    # Досье уезжает в JSON-колонку: любой date внутри уронил бы стадию записи.
    json.dumps(dossier, ensure_ascii=False)


def test_age_years_counted_from_registration(session, config, company_factory, guard):
    registered = date.today() - relativedelta(years=15, months=2)
    company_factory("7701234567", registered_at=registered)
    expectation = _ready(session, inn="7701234567", evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["company"]["age_years"] == 15


def test_revenue_last_takes_the_freshest_year(session, config, company_factory, guard):
    company_factory("7701234567", revenue={"2023": 100.0, "2025": 300.0, "2024": 200.0})
    expectation = _ready(session, inn="7701234567", evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["company"]["revenue_last"] == 300.0


# -------------------------------------------------------------- досье с дырами


def test_dossier_with_holes_lists_them_and_invents_nothing(
    session, config, company_factory, guard
):
    company_factory("7701234567")
    expectation = _ready(
        session,
        inn="7701234567",
        evidence=_past_event(attendees=None, venue=None),
    )

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["contact"] == {
        "full_name": None,
        "position": None,
        "email": None,
        "is_generic": None,
    }
    assert dossier["event"]["attendees"] is None
    assert dossier["event"]["venue"] is None
    assert dossier["gaps"] == [
        "нет контакта",
        "неизвестно число участников",
        "неизвестна площадка",
    ]
    # Повод укоротился, но не досочинил ни числа участников, ни площадки.
    assert dossier["reason"] == "в ноябре 2025 проводили «Итоги года»"
    assert "человек" not in dossier["reason"]


def test_unknown_company_size_is_a_gap(session, config, company_factory, guard, monkeypatch):
    monkeypatch.setattr(REGISTRY_ENRICH, lambda *a, **kw: None)
    company_factory("7701234567", headcount=None, revenue=None, registered_at=None)
    expectation = _ready(session, inn="7701234567", attendees=600, evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["company"]["headcount"] is None
    assert dossier["company"]["age_years"] is None
    assert "неизвестен размер компании" in dossier["gaps"]


def test_generic_mailbox_is_marked(session, config, company_factory, guard):
    company_factory("7701234567")
    repo.upsert_contact(session, inn="7701234567", email="INFO@romashka.ru", is_primary=True)
    expectation = _ready(session, inn="7701234567", attendees=600, evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["contact"]["is_generic"] is True
    # Ящик есть, значит письму есть куда уйти — это не дыра.
    assert "нет контакта" not in dossier["gaps"]


# ------------------------------------------------------------ русский повод


@pytest.mark.parametrize(
    ("event_date", "expected"),
    [
        ("2025-11-20", "в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо"),
        ("2026-03-04", "в марте 2026 проводили «Итоги года» на ~600 человек в Крокус Экспо"),
        ("2025-05-12", "в мае 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо"),
    ],
)
def test_anniversary_reason_declines_the_month(
    session, config, company_factory, guard, event_date, expected
):
    company_factory("7701234567")
    expectation = _ready(session, inn="7701234567", evidence=_past_event(date=event_date))

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["reason"] == expected


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (15, "в марте 2026 компании исполняется 15 лет"),
        (10, "в марте 2026 компании исполняется 10 лет"),
        (21, "в марте 2026 компании исполняется 21 год"),
        (22, "в марте 2026 компании исполняется 22 года"),
    ],
)
def test_jubilee_reason_agrees_with_the_number(
    session, config, company_factory, guard, years, expected
):
    company_factory("7701234567")
    expectation = _ready(
        session,
        inn="7701234567",
        kind=ExpectationKind.COMPANY_JUBILEE.value,
        expected_at=date(2026, 3, 15),
        evidence={"years": years, "registered_at": "2011-03-15"},
    )

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["reason"] == expected
    # Юбилей ничего не говорит о масштабе — числа участников в письме быть не должно.
    assert dossier["event"]["attendees"] is None
    assert "неизвестно число участников" in dossier["gaps"]


def test_reason_falls_back_to_evidence_for_other_kinds(session, config, company_factory, guard):
    company_factory("7701234567")
    expectation = _ready(
        session,
        inn="7701234567",
        kind=ExpectationKind.VACANCY_SIGNAL.value,
        evidence={"reason": "Ищут «event-менеджера» (вакансия от 01.06.2026)"},
    )

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["reason"] == "Ищут «event-менеджера» (вакансия от 01.06.2026)"


def test_reason_survives_empty_evidence(session, config, company_factory, guard):
    company_factory("7701234567")
    expectation = _ready(session, inn="7701234567", expected_at=date(2026, 12, 10))

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["reason"] == "ожидаем мероприятие в декабре 2026"


# ------------------------------------------------------------------ реестр


def test_registry_is_asked_only_when_data_is_missing(
    session, config, company_factory, guard, monkeypatch
):
    asked: list[str] = []

    def fake_enrich(sess, inn, *, config, guard, **kwargs):
        asked.append(inn)
        return repo.upsert_company(
            sess,
            inn,
            headcount=800,
            revenue={"2025": 900_000_000.0},
            registered_at=date(2010, 5, 1),
        )

    monkeypatch.setattr(REGISTRY_ENRICH, fake_enrich)
    company_factory("7701234567")  # полная карточка — спрашивать нечего
    company_factory("7702345678", headcount=None, revenue=None, registered_at=None)
    full = _ready(session, inn="7701234567", evidence=_past_event())
    empty = _ready(session, inn="7702345678", evidence=_past_event())

    build_dossier(session, config, full, guard=guard)
    assert asked == []

    dossier = build_dossier(session, config, empty, guard=guard)
    assert asked == ["7702345678"]
    assert dossier["company"]["headcount"] == 800
    assert dossier["company"]["registered_at"] == "2010-05-01"


def test_missing_registry_module_does_not_break_the_dossier(
    session, config, company_factory, guard, monkeypatch
):
    # Реестра может не быть в момент прогона; досье собирается без него.
    monkeypatch.setitem(sys.modules, "gtm.collectors.registry", None)
    company_factory("7701234567", headcount=None, revenue=None, registered_at=None)
    expectation = _ready(session, inn="7701234567", attendees=600, evidence=_past_event())

    dossier = build_dossier(session, config, expectation, guard=guard)

    assert dossier["company"]["headcount"] is None
    assert dossier["reason"].startswith("в ноябре 2025 проводили")


# ------------------------------------------------------------------ стадия


def test_run_enrich_respects_limit_and_score_order(
    session, config, company_factory, monkeypatch
):
    monkeypatch.setattr(REGISTRY_ENRICH, lambda *a, **kw: None)
    for inn, score in (("7701234567", 0.1), ("7702345678", 0.9), ("7703456789", 0.5)):
        company_factory(inn)
        _ready(session, inn=inn, score=score, attendees=600, evidence=_past_event())

    result = run_enrich(session, config, run_id="run-1", limit=2)

    assert (result.count_in, result.count_out) == (2, 2)
    enriched = {
        e.inn: e.status
        for e in repo.expectations_by_status(
            session, [ExpectationStatus.READY.value, ExpectationStatus.ENRICHED.value]
        )
    }
    # Обогащены две верхние по score, слабейшая осталась ждать.
    assert enriched["7702345678"] == ExpectationStatus.ENRICHED.value
    assert enriched["7703456789"] == ExpectationStatus.ENRICHED.value
    assert enriched["7701234567"] == ExpectationStatus.READY.value


def test_run_enrich_touches_only_ready(session, config, company_factory, monkeypatch):
    monkeypatch.setattr(REGISTRY_ENRICH, lambda *a, **kw: None)
    company_factory("7701234567")
    other = _ready(session, inn="7701234567", evidence=_past_event())
    other.status = ExpectationStatus.FILTERED_OUT.value
    session.flush()

    result = run_enrich(session, config, run_id="run-2")

    assert (result.count_in, result.count_out) == (0, 0)
    assert other.status == ExpectationStatus.FILTERED_OUT.value
    assert other.dossier == {}


def test_run_enrich_stores_dossier_and_notes_gaps(
    session, config, company_factory, monkeypatch
):
    monkeypatch.setattr(REGISTRY_ENRICH, lambda *a, **kw: None)
    company_factory("7701234567")
    expectation = _ready(
        session, inn="7701234567", evidence=_past_event(attendees=None, venue=None)
    )

    result = run_enrich(session, config, run_id="run-3")

    assert expectation.status == ExpectationStatus.ENRICHED.value
    assert expectation.dossier["reason"] == "в ноябре 2025 проводили «Итоги года»"
    assert result.details["with_gaps"] == 1
    # Стадия попадает в сводку прогона — иначе непонятно, где обнулилось.
    summary = {row["stage"]: row for row in repo.run_summary(session, "run-3")}
    assert summary["enrich"]["out"] == 1


def test_zero_limit_enriches_nothing(session, config, company_factory, monkeypatch):
    monkeypatch.setattr(REGISTRY_ENRICH, lambda *a, **kw: None)
    company_factory("7701234567")
    expectation = _ready(session, inn="7701234567", evidence=_past_event())

    result = run_enrich(session, config, run_id="run-4", limit=0)

    assert (result.count_in, result.count_out) == (0, 0)
    assert expectation.status == ExpectationStatus.READY.value


def test_budget_stop_keeps_what_was_already_enriched(
    session, config, company_factory, monkeypatch
):
    calls = {"n": 0}

    def fake_enrich(sess, inn, *, config, guard, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise BudgetExceeded("Дневной лимит вызовов исчерпан")
        return repo.upsert_company(
            sess, inn, headcount=700, revenue={"2025": 5e8}, registered_at=date(2012, 1, 1)
        )

    monkeypatch.setattr(REGISTRY_ENRICH, fake_enrich)
    for inn, score in (("7701234567", 0.9), ("7702345678", 0.4)):
        company_factory(inn, headcount=None, revenue=None, registered_at=None)
        _ready(session, inn=inn, score=score, evidence=_past_event())

    result = run_enrich(session, config, run_id="run-5")

    assert (result.count_in, result.count_out) == (2, 1)
    assert result.details["stopped"] == "budget_exceeded"
    assert result.details["not_enriched"] == 1
    assert "лимит" in result.details["stop_reason"]

    by_inn = {
        e.inn: e
        for e in repo.expectations_by_status(
            session, [ExpectationStatus.READY.value, ExpectationStatus.ENRICHED.value]
        )
    }
    # За первое досье уже заплачено — оно обязано остаться в базе.
    assert by_inn["7701234567"].status == ExpectationStatus.ENRICHED.value
    assert by_inn["7701234567"].dossier["company"]["headcount"] == 700
    assert by_inn["7702345678"].status == ExpectationStatus.READY.value
    assert by_inn["7702345678"].dossier == {}
