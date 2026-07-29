"""Тесты фильтра ICP.

Главное, что здесь проверяется: фильтр решает по масштабу мероприятия, а не
по размеру компании, и не выбрасывает никого молча. Второе по важности —
неизвестные данные не считаются отказом: компания, которую ещё не обогащали,
должна дойти до обогащения, а не исчезнуть на этой стадии.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.filters import icp as icp_module
from gtm.filters import suppression as suppression_module
from gtm.filters.icp import (
    _compiled_name_patterns,
    check_icp,
    estimate_attendees,
    run_filter,
)
from gtm.filters.suppression import (
    check_suppression,
    import_suppression_csv,
    suppress_on_bounce,
)
from gtm.storage import repo
from gtm.storage.models import (
    Expectation,
    ExpectationKind,
    ExpectationStatus,
    FactType,
    Suppression,
    SuppressionReason,
)

INN = "7701234567"


def _expectation(
    session: Session,
    today: date,
    *,
    inn: str = INN,
    attendees: int | None = 900,
    kind: str = ExpectationKind.EVENT_ANNIVERSARY.value,
    expected_at: date | None = None,
    opens: date | None = None,
    closes: date | None = None,
    evidence: dict | None = None,
    source_fact_id: int | None = None,
) -> Expectation:
    """Ожидание с окном, открытым на `today`, — то, что видит ежедневный запрос."""
    expectation, _ = repo.upsert_expectation(
        session,
        inn=inn,
        kind=kind,
        expected_at=expected_at or today + timedelta(days=110),
        window_opens_at=opens or today - timedelta(days=10),
        window_closes_at=closes or today + timedelta(days=50),
        confidence=0.7,
        expected_attendees=attendees,
        evidence=evidence,
        source_fact_id=source_fact_id,
    )
    return expectation


def _moscow_event_fact(session: Session, *, inn: str = INN, city: str = "Москва") -> int:
    fact, _ = repo.upsert_fact(
        session,
        source="test",
        source_uid=f"event:{inn}:{city}",
        fact_type=FactType.PAST_EVENT.value,
        inn=inn,
        occurred_at=date(2025, 11, 13),
        payload={"title": "Клиентская конференция", "city": city, "attendees": 900},
    )
    return fact.id


# ----------------------------------------------------------------- проходит


def test_typical_moscow_conference_passes(
    session: Session, config: Config, today: date, company_factory
):
    """Опорный сценарий: 900 человек, московская компания со штатом и выручкой."""
    company = company_factory(INN)
    expectation = _expectation(session, today)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert verdict.code is None
    assert verdict.attendees_estimate == 900
    assert verdict.details["attendees_source"] == "event"
    # Штат и выручка известны — дозапрашивать нечего, кроме ОКВЭД.
    assert verdict.details.get("needs_enrichment") == ["okved"]


# -------------------------------------------------------------- масштаб


def test_small_event_rejected_even_for_huge_company(
    session: Session, config: Config, today: date, company_factory
):
    """Восемьдесят человек — не наш клиент, даже если в компании пять тысяч.

    На такой вместимости площадка в ЮВАО конкурирует с сотнями залов
    и проигрывает по локации.
    """
    company = company_factory(INN, headcount=5000, revenue={"2025": 40_000_000_000.0})
    expectation = _expectation(session, today, attendees=80)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is False
    assert verdict.code == "too_small"
    assert verdict.code in config.icp.reject_codes
    assert verdict.attendees_estimate == 80
    assert "80" in verdict.reason and "250" in verdict.reason


def test_event_bigger_than_the_hall_is_rejected(
    session: Session, config: Config, today: date, company_factory
):
    company = company_factory(INN)
    expectation = _expectation(session, today, attendees=3000)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "too_big"
    assert str(config.icp.scale.hard_max_attendees) in verdict.reason


def test_huge_headcount_is_not_rejected_as_too_big(
    session: Session, config: Config, today: date, company_factory
):
    """Оценка по штату — догадка, и на крупных компаниях она завышает:
    на юбилей компании из 5600 человек не приходит 60% штата. Отсев по такой
    оценке выбрасывал бы как раз лучших заказчиков."""
    company = company_factory(INN, headcount=5600)
    expectation = _expectation(session, today, attendees=None)

    estimate, source = estimate_attendees(expectation, company, config)
    assert (estimate, source) == (3360, "headcount")

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert verdict.attendees_estimate == config.icp.scale.hard_max_attendees
    assert verdict.details["attendees_capped_from"] == 3360


def test_attendees_estimated_from_headcount(
    session: Session, config: Config, today: date, company_factory
):
    """Числа участников нет — считаем по штату: 1000 × 0.6 = 600."""
    company = company_factory(INN, headcount=1000)
    expectation = _expectation(session, today, attendees=None)

    estimate, source = estimate_attendees(expectation, company, config)

    assert (estimate, source) == (600, "headcount")
    assert check_icp(session, expectation, company, config, today=today).passed is True


def test_known_event_size_beats_headcount(
    session: Session, config: Config, today: date, company_factory
):
    """Прошлое мероприятие важнее штата: пять тысяч сотрудников, но зал на 300."""
    company = company_factory(INN, headcount=5000)
    expectation = _expectation(session, today, attendees=300)

    assert estimate_attendees(expectation, company, config) == (300, "event")


def test_headcount_estimate_below_threshold_rejects(
    session: Session, config: Config, today: date, company_factory
):
    """Штат 200 → оценка 120 человек, ниже порога 250."""
    company = company_factory(INN, headcount=200)
    expectation = _expectation(session, today, attendees=None)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "too_small"
    assert verdict.attendees_estimate == 120
    assert "по штату" in verdict.reason


def test_unknown_scale_does_not_reject_but_asks_for_enrichment(
    session: Session, config: Config, today: date, company_factory
):
    """Ни числа участников, ни штата — компанию ещё не обогащали. Пропускаем."""
    company = company_factory(INN, headcount=None, revenue={})
    expectation = _expectation(session, today, attendees=None)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert verdict.attendees_estimate is None
    assert verdict.details["attendees_source"] == "unknown"
    assert set(verdict.details["needs_enrichment"]) >= {"attendees", "headcount", "revenue"}


# -------------------------------------------------------------- география


def test_regional_company_with_regional_event_is_rejected(
    session: Session, config: Config, today: date, company_factory
):
    company = company_factory(
        "6601234567", region_code="66", city="Екатеринбург", name="ООО Уралхим"
    )
    fact_id = _moscow_event_fact(session, inn="6601234567", city="Екатеринбург")
    expectation = _expectation(session, today, inn="6601234567", source_fact_id=fact_id)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "wrong_geo"
    assert verdict.details["event_in_target_city"] is False


def test_petersburg_company_with_moscow_event_passes(
    session: Session, config: Config, today: date, company_factory
):
    """Юрлицо не в наших регионах — не приговор: важно, где мероприятие."""
    company = company_factory("7801234567", region_code="78", city="Санкт-Петербург")
    fact_id = _moscow_event_fact(session, inn="7801234567", city="Москва")
    expectation = _expectation(session, today, inn="7801234567", source_fact_id=fact_id)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert verdict.details["event_in_target_city"] is True


def test_moscow_address_in_evidence_counts_as_moscow_event(
    session: Session, config: Config, today: date, company_factory
):
    """Архивы отдают адрес одной строкой — город в нём назван явно."""
    company = company_factory("7801234567", region_code="78", city="Санкт-Петербург")
    expectation = _expectation(
        session,
        today,
        inn="7801234567",
        evidence={"previous_event": {"venue": "Москва, ул. Шарикоподшипниковская, 13"}},
    )

    assert check_icp(session, expectation, company, config, today=today).passed is True


def test_region_falls_back_to_inn_prefix(
    session: Session, config: Config, today: date, company_factory
):
    """Компанию ещё не обогащали реестром — регион берём из ИНН."""
    company = company_factory(INN, region_code=None)
    expectation = _expectation(session, today)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert verdict.details["company_region"] == "77"


# ----------------------------------------------------------- размер компании


def test_small_company_with_big_event_is_rejected(
    session: Session, config: Config, today: date, company_factory
):
    """Мероприятие на 400 человек прошло масштаб, но платить за зал нечем."""
    company = company_factory(INN, headcount=30, revenue={"2025": 10_000_000.0})
    expectation = _expectation(session, today, attendees=400)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "too_small_company"
    assert "30" in verdict.reason


def test_small_headcount_but_big_revenue_passes(
    session: Session, config: Config, today: date, company_factory
):
    """Достаточно одного показателя выше порога: у торговых домов штат мал,
    а бюджет на мероприятие есть."""
    company = company_factory(INN, headcount=30, revenue={"2025": 5_000_000_000.0})
    expectation = _expectation(session, today, attendees=400)

    assert check_icp(session, expectation, company, config, today=today).passed is True


def test_unknown_headcount_and_revenue_do_not_reject(
    session: Session, config: Config, today: date, company_factory
):
    """Неизвестный штат — это «не знаем», а не «мало»."""
    company = company_factory(INN, headcount=None, revenue={})
    expectation = _expectation(session, today, attendees=600)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.passed is True
    assert set(verdict.details["needs_enrichment"]) >= {"headcount", "revenue"}


def test_latest_known_revenue_year_is_used(
    session: Session, config: Config, today: date, company_factory
):
    """Свежий год ниже порога решает: рост прошлых лет уже не в счёт."""
    company = company_factory(
        INN, headcount=30, revenue={"2023": 5_000_000_000.0, "2024": 12_000_000.0}
    )
    expectation = _expectation(session, today, attendees=400)

    assert check_icp(session, expectation, company, config, today=today).code == (
        "too_small_company"
    )


def test_young_company_is_rejected(
    session: Session, config: Config, today: date, company_factory
):
    """Компании полгода: годовщин мероприятий у неё быть не может."""
    company = company_factory(INN, registered_at=today - timedelta(days=180))
    expectation = _expectation(session, today)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "too_small_company"
    assert "зарегистрирована" in verdict.reason


# ---------------------------------------------------------- исключения


def test_excluded_okved_prefix(session: Session, config: Config, today: date, company_factory):
    company = company_factory(INN, okved="68.20.2")
    expectation = _expectation(session, today)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "excluded_industry"
    assert verdict.details["matched_okved"] == "68."


def test_neighbouring_okved_is_not_excluded(
    session: Session, config: Config, today: date, company_factory
):
    """«68.» — это группа 68, а не всё, что начинается на «68»."""
    company = company_factory(INN, okved="680.1")
    expectation = _expectation(session, today)

    assert check_icp(session, expectation, company, config, today=today).passed is True


def test_excluded_name_pattern(session: Session, config: Config, today: date, company_factory):
    """Конкурентам-площадкам холодные письма не пишем."""
    company = company_factory(INN, name="ООО «Крокус Экспо»")
    expectation = _expectation(session, today)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "excluded_industry"
    assert "крокус" in verdict.details["matched_name_pattern"]


def test_name_patterns_are_compiled_once_per_config(
    session: Session, config: Config, today: date, company_factory
):
    """Регулярки компилируются на набор из конфига, а не на каждую компанию."""
    _compiled_name_patterns.cache_clear()
    for index in range(5):
        inn = f"770123456{index}"
        company = company_factory(inn, name=f"ООО Ромашка {index}")
        expectation = _expectation(session, today, inn=inn)
        assert check_icp(session, expectation, company, config, today=today).passed is True

    info = _compiled_name_patterns.cache_info()
    assert (info.misses, info.hits) == (1, 4)


def test_broken_pattern_does_not_break_the_run(
    session: Session, config: Config, today: date, company_factory
):
    """Опечатка в YAML не должна останавливать систему целиком."""
    _compiled_name_patterns.cache_clear()
    config.icp.exclude.name_patterns = ["(?i)крокус", "(незакрытая скобка"]
    company = company_factory(INN, name="ООО «Крокус Экспо»")
    expectation = _expectation(session, today)

    assert check_icp(session, expectation, company, config, today=today).code == (
        "excluded_industry"
    )


# ------------------------------------------------------------------- окно


def test_closed_window_is_rejected(session: Session, config: Config, today: date, company_factory):
    company = company_factory(INN)
    expectation = _expectation(
        session, today, opens=today - timedelta(days=90), closes=today - timedelta(days=1)
    )

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "window_closed"


# -------------------------------------------------------------- стоп-лист


def test_suppressed_company_is_rejected_before_anything_else(
    session: Session, config: Config, today: date, company_factory
):
    """Стоп-лист — первая проверка: мелкое мероприятие тут уже не важно."""
    company = company_factory(INN)
    repo.add_suppression(session, reason=SuppressionReason.CLIENT.value, inn=INN)
    expectation = _expectation(session, today, attendees=80)

    verdict = check_icp(session, expectation, company, config, today=today)

    assert verdict.code == "suppressed"
    assert verdict.details["matched_by"] == ["ИНН"]


def test_check_suppression_matches_domain(session: Session):
    repo.add_suppression(session, reason=SuppressionReason.COMPETITOR.value, domain="crocus.ru")

    assert check_suppression(session, INN).passed is True
    assert check_suppression(session, INN, "a.ivanova@crocus.ru").passed is False


def test_suppress_on_bounce_is_idempotent(session: Session):
    suppress_on_bounce(session, "Dead.Box@Example.RU")
    suppress_on_bounce(session, "dead.box@example.ru")

    rows = session.scalars(select(Suppression)).all()
    assert len(rows) == 1
    assert rows[0].email == "dead.box@example.ru"
    assert rows[0].reason == SuppressionReason.BOUNCED.value
    assert check_suppression(session, INN, "dead.box@example.ru").passed is False


def test_suppress_on_bounce_ignores_garbage(session: Session):
    suppress_on_bounce(session, "не адрес")
    suppress_on_bounce(session, "")

    assert session.scalars(select(Suppression)).all() == []


def test_import_suppression_csv(session: Session, tmp_path: Path):
    """Файл ведут руками: разделитель «;», кривой ИНН, неизвестная причина."""
    path = tmp_path / "stop.csv"
    path.write_text(
        "inn;email;domain;reason;note\n"
        "7701234567;;;client;действующий клиент\n"
        ";a.ivanova@example.ru;;opted_out;просила не писать\n"
        ";;@crocus.ru;competitor;площадка\n"
        "123;;;manual;битый ИНН — строка без идентификатора\n"
        ";;;manual;совсем пустая\n"
        "7801234567;;;неизвестная причина;\n",
        encoding="utf-8",
    )

    added = import_suppression_csv(session, path)

    assert added == 4
    assert check_suppression(session, "7701234567").passed is False
    assert check_suppression(session, "0000000000", "a.ivanova@example.ru").passed is False
    assert check_suppression(session, "0000000000", "sales@crocus.ru").passed is False
    # Причина не из перечисления не роняет импорт, но и не сохраняется как есть.
    row = session.scalars(select(Suppression).where(Suppression.inn == "7801234567")).one()
    assert row.reason == SuppressionReason.MANUAL.value


def test_repeated_import_adds_nothing(session: Session, tmp_path: Path):
    path = tmp_path / "stop.csv"
    path.write_text("inn,email\n7701234567,\n,a@example.ru\n", encoding="utf-8")

    assert import_suppression_csv(session, path) == 2
    assert import_suppression_csv(session, path) == 0
    assert len(session.scalars(select(Suppression)).all()) == 2


# ---------------------------------------------------------------- стадия


def test_run_filter_sorts_statuses_and_counts_rejects(
    session: Session, config: Config, today: date, company_factory
):
    """Раскладка стадии: кто прошёл, кто отсеян и по каким кодам."""
    good = company_factory(INN)
    company_factory("7702345678", name="ООО Мелкое")
    company_factory("6601234567", region_code="66", city="Екатеринбург")
    company_factory("7703456789", name="ООО Второе")

    passing = _expectation(session, today, inn=good.inn)
    second = _expectation(session, today, inn="7703456789", attendees=500)
    small = _expectation(session, today, inn="7702345678", attendees=80)
    far = _expectation(session, today, inn="6601234567")
    # Окно ещё не открылось — ежедневный запрос такое не отдаёт.
    _expectation(
        session,
        today,
        inn="7703456789",
        kind=ExpectationKind.COMPANY_JUBILEE.value,
        opens=today + timedelta(days=5),
        closes=today + timedelta(days=60),
    )

    result = run_filter(session, config, today=today, run_id="run-1")

    assert (result.count_in, result.count_out) == (4, 2)
    assert result.details["too_small"] == 1
    assert result.details["wrong_geo"] == 1
    assert {passing.status, second.status} == {ExpectationStatus.READY.value}
    assert small.status == ExpectationStatus.FILTERED_OUT.value
    assert far.status == ExpectationStatus.FILTERED_OUT.value
    assert small.filter_verdict["code"] == "too_small"
    assert small.filter_verdict["checked_at"] == today.isoformat()
    assert passing.filter_verdict["passed"] is True

    # Стадия обязана быть видна в сводке прогона.
    summary = repo.run_summary(session, "run-1")[0]
    assert (summary["stage"], summary["in"], summary["out"]) == ("filter", 4, 2)


def test_run_filter_marks_expectations_needing_enrichment(
    session: Session, config: Config, today: date, company_factory
):
    company_factory(INN, headcount=None, revenue={})
    expectation = _expectation(session, today, attendees=600)

    result = run_filter(session, config, today=today, run_id="run-2")

    assert result.details["needs_enrichment"] == [expectation.id]
    assert expectation.status == ExpectationStatus.READY.value


def test_run_filter_touches_only_new_expectations(
    session: Session, config: Config, today: date, company_factory
):
    """Повторный прогон не переводит уже отобранные ожидания обратно."""
    company_factory(INN)
    expectation = _expectation(session, today)
    run_filter(session, config, today=today, run_id="run-1")
    expectation.filter_verdict = {}

    result = run_filter(session, config, today=today, run_id="run-2")

    assert (result.count_in, result.count_out) == (0, 0)
    assert expectation.status == ExpectationStatus.READY.value
    assert expectation.filter_verdict == {}


def test_run_filter_respects_limit(
    session: Session, config: Config, today: date, company_factory
):
    for index in range(3):
        inn = f"770123456{index}"
        company_factory(inn)
        _expectation(session, today, inn=inn)

    result = run_filter(session, config, today=today, run_id="run-1", limit=2)

    assert result.count_in == 2
    still_new = repo.open_window_expectations(session, today)
    assert len(still_new) == 1


def test_every_reject_code_comes_from_config(
    session: Session, config: Config, today: date, company_factory
):
    """Код отказа — ключ из config/icp.yaml: иначе отсев не найти в сводке."""
    company_factory(INN, headcount=10, revenue={"2025": 1_000_000.0}, okved="93.29.1")
    repo.add_suppression(session, reason=SuppressionReason.MANUAL.value, inn="7702345678")
    company_factory("7702345678")

    codes = set()
    for inn, attendees in ((INN, 80), (INN, 5000), ("7702345678", 900)):
        expectation = _expectation(
            session,
            today,
            inn=inn,
            attendees=attendees,
            kind=f"probe-{attendees}",
        )
        verdict = check_icp(
            session, expectation, repo.get_company(session, inn), config, today=today
        )
        codes.add(verdict.code)

    assert codes == {"too_small", "too_big", "suppressed"}
    assert codes <= set(config.icp.reject_codes)


def test_filter_never_reaches_outside():
    """Инвариант стадии: ни сети, ни коллекторов. Иначе бюджет сгорит."""
    forbidden = ("httpx", "requests", "urllib", "gtm.collectors", "gtm.enrich")
    for module in (icp_module, suppression_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [name for name in imported if name.startswith(forbidden)]


@pytest.mark.parametrize(
    ("code", "prefix", "expected"),
    [
        ("68.20.2", "68.", True),
        ("68", "68.", True),
        ("680.1", "68.", False),
        ("93.29.1", "93.29", True),
        ("93.2", "93.29", False),
    ],
)
def test_okved_prefix_matching(code: str, prefix: str, expected: bool):
    assert icp_module._okved_matches(code, prefix) is expected
