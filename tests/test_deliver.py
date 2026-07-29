"""Утренний список: что попадает менеджеру на стол и в каком порядке."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from gtm.deliver import build_daily, render_csv, render_markdown, run_deliver
from gtm.settings import get_settings
from gtm.storage import repo
from gtm.storage.models import ExpectationKind, ExpectationStatus, OutreachStatus

BODY = "Здравствуйте, Анна.\n\nВ ноябре вы проводили конференцию.\n\nПосмотрим зал?"


def _dossier(**fields):
    dossier = {
        "company": {"name": "ООО Ромашка", "headcount": 900},
        "event": {
            "title": "Итоги года",
            "date": "2025-11-20",
            "attendees": 600,
            "venue": "Крокус Экспо",
        },
        "contact": {"full_name": "Анна Петрова", "email": "a.petrova@romashka.ru"},
        "reason": "в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо",
        "gaps": [],
    }
    dossier.update(fields)
    return dossier


def _drafted(
    session,
    *,
    inn: str,
    score: float | None = 0.5,
    expected_at: date = date(2026, 11, 20),
    attendees: int | None = 600,
    dossier: dict | None = None,
    evidence: dict | None = None,
    subject: str = "Ноябрьская конференция: зал на 1000 у Дубровки",
    body: str = BODY,
    with_letter: bool = True,
    letter_status: str = OutreachStatus.DRAFT.value,
    contact_id: int | None = None,
):
    """Ожидание с готовым письмом — ровно то состояние, с которым работает стадия."""
    expectation, _ = repo.upsert_expectation(
        session,
        inn=inn,
        kind=ExpectationKind.EVENT_ANNIVERSARY.value,
        expected_at=expected_at,
        window_opens_at=expected_at - timedelta(days=120),
        window_closes_at=expected_at,
        confidence=0.7,
        expected_attendees=attendees,
        evidence=evidence or {},
    )
    expectation.status = ExpectationStatus.DRAFTED.value
    expectation.score = score
    expectation.dossier = _dossier() if dossier is None else dossier
    session.flush()
    if with_letter:
        repo.create_outreach(
            session,
            expectation_id=expectation.id,
            contact_id=contact_id,
            subject=subject,
            body=body,
            generated_by="template",
            status=letter_status,
        )
    return expectation


# ------------------------------------------------------- порядок и лимит


def test_cards_go_by_score_and_respect_limit(session, config, company_factory, today):
    for inn, score in (("7701234567", 0.40), ("7702345678", 0.95), ("7703456789", 0.62)):
        company_factory(inn, name=f"ООО {inn[-4:]}")
        _drafted(session, inn=inn, score=score)

    items = build_daily(session, config, today=today, limit=2)

    assert [i.inn for i in items] == ["7702345678", "7703456789"]
    assert [i.score for i in items] == [0.95, 0.62]
    # Отбор ничего не помечает: статусы меняет только стадия.
    assert all(
        e.status == ExpectationStatus.DRAFTED.value
        for e in repo.expectations_by_status(session, [ExpectationStatus.DRAFTED.value])
    )


def test_equal_scores_are_ordered_by_urgency(session, config, company_factory, today):
    company_factory("7701234567")
    company_factory("7702345678")
    _drafted(session, inn="7701234567", score=0.5, expected_at=date(2026, 12, 20))
    _drafted(session, inn="7702345678", score=0.5, expected_at=date(2026, 9, 10))

    items = build_daily(session, config, today=today)

    # При равном приоритете вперёд идёт то, что случится раньше.
    assert [i.inn for i in items] == ["7702345678", "7701234567"]


def test_default_limit_comes_from_settings(session, config, company_factory, today):
    limit = get_settings().daily_delivery_limit
    for n in range(limit + 3):
        inn = f"77{n:08d}"
        company_factory(inn)
        _drafted(session, inn=inn, score=0.9)

    assert len(build_daily(session, config, today=today)) == limit


# ------------------------------------------------------------- отсечения


def test_min_score_cuts_the_tail(session, config, company_factory, today):
    threshold = config.signals.scoring.min_score_to_deliver
    assert threshold > 0, "тест бессмысленен при нулевом пороге"
    company_factory("7701234567")
    company_factory("7702345678")
    company_factory("7703456789")
    _drafted(session, inn="7701234567", score=threshold)
    _drafted(session, inn="7702345678", score=threshold - 0.01)
    _drafted(session, inn="7703456789", score=None)

    items = build_daily(session, config, today=today)

    # Порог включающий, а ожидание без оценки — не «наверное, хорошее».
    assert [i.inn for i in items] == ["7701234567"]


def test_expectation_without_letter_is_not_delivered(session, config, company_factory, today):
    company_factory("7701234567")
    company_factory("7702345678")
    _drafted(session, inn="7701234567", with_letter=True)
    _drafted(session, inn="7702345678", with_letter=False)

    items = build_daily(session, config, today=today)

    # Карточка без письма — работа, а не список: менеджеру она бесполезна.
    assert [i.inn for i in items] == ["7701234567"]


def test_already_sent_letter_is_not_delivered_again(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(session, inn="7701234567", letter_status=OutreachStatus.SENT.value)

    assert build_daily(session, config, today=today) == []


def test_passed_event_drops_out_of_the_list(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(session, inn="7701234567", expected_at=today - timedelta(days=1))
    company_factory("7702345678")
    _drafted(session, inn="7702345678", expected_at=today)

    items = build_daily(session, config, today=today)

    # Про вчерашнее мероприятие «планируете ли вы» уже не спросишь.
    assert [i.inn for i in items] == ["7702345678"]


def test_suppressed_company_never_reaches_the_manager(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(session, inn="7701234567")
    repo.add_suppression(session, reason="opted_out", inn="7701234567")

    assert build_daily(session, config, today=today) == []


# --------------------------------------------------------------- карточка


def test_card_carries_everything_for_the_call(session, config, company_factory, today):
    company_factory("7701234567", name="ООО Ромашка")
    contact = repo.upsert_contact(
        session,
        inn="7701234567",
        email="a.petrova@romashka.ru",
        full_name="Анна Петрова",
        position="event-менеджер",
        is_primary=True,
    )
    expectation = _drafted(session, inn="7701234567", score=0.87, contact_id=contact.id)

    (item,) = build_daily(session, config, today=today)

    assert item.expectation_id == expectation.id
    assert item.inn == "7701234567"
    assert item.company == "ООО Ромашка"
    assert item.reason == "в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо"
    assert item.expected_at == date(2026, 11, 20)
    assert item.expected_attendees == 600
    assert item.previous_venue == "Крокус Экспо"
    assert item.score == 0.87
    assert item.contact == "Анна Петрова, event-менеджер <a.petrova@romashka.ru>"
    assert item.subject == "Ноябрьская конференция: зал на 1000 у Дубровки"
    assert item.body == BODY
    assert item.gaps == []
    assert item.outreach_id is not None


def test_card_invents_nothing_when_dossier_is_thin(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(
        session,
        inn="7701234567",
        attendees=None,
        dossier=_dossier(
            event={"title": None, "date": None, "attendees": None, "venue": None},
            gaps=["нет контакта", "неизвестно число участников", "неизвестна площадка"],
        ),
    )

    (item,) = build_daily(session, config, today=today)

    assert item.expected_attendees is None
    assert item.previous_venue is None
    assert item.contact is None
    assert item.gaps == ["нет контакта", "неизвестно число участников", "неизвестна площадка"]


def test_venue_falls_back_to_evidence_without_dossier(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(
        session,
        inn="7701234567",
        dossier={},
        evidence={"previous_event": {"venue": "Известия Hall"}, "reason": "повод из билдера"},
    )

    (item,) = build_daily(session, config, today=today)

    assert item.previous_venue == "Известия Hall"
    assert item.reason == "повод из билдера"


def test_reason_never_stays_empty(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(session, inn="7701234567", dossier={}, evidence={})

    (item,) = build_daily(session, config, today=today)

    assert item.reason == "мероприятие ожидается 20 ноября 2026"


# --------------------------------------------------------------- markdown


def test_markdown_has_every_block_of_the_card(session, config, company_factory, today):
    company_factory("7701234567", name="ООО Ромашка")
    repo.upsert_contact(
        session,
        inn="7701234567",
        email="a.petrova@romashka.ru",
        full_name="Анна Петрова",
        position="event-менеджер",
        is_primary=True,
    )
    _drafted(session, inn="7701234567", score=0.87)

    items = build_daily(session, config, today=today)
    text = render_markdown(items, today=today, summary="собрано 340 → фильтр 45 → писем 12")

    assert "# Утренний список — 29 июля 2026" in text
    assert "собрано 340 → фильтр 45 → писем 12" in text
    assert "1. ООО Ромашка" in text
    assert "в ноябре 2025 проводили «Итоги года»" in text
    assert "20 ноября 2026, ~600 человек" in text
    assert "**Прошлый зал:** Крокус Экспо" in text
    assert "Анна Петрова, event-менеджер <a.petrova@romashka.ru>" in text
    assert "Ноябрьская конференция: зал на 1000 у Дубровки" in text
    # Текст письма — цитатой, целиком, включая пустые строки.
    assert "> Здравствуйте, Анна." in text
    assert "> Посмотрим зал?" in text
    # Что делать со списком и чем платить за него системе.
    assert "gtm feedback record" in text


def test_markdown_shows_holes_only_when_there_are_holes(session, config, company_factory, today):
    company_factory("7701234567")
    company_factory("7702345678")
    _drafted(session, inn="7701234567", score=0.9)  # досье полное
    _drafted(
        session,
        inn="7702345678",
        score=0.8,
        attendees=None,
        dossier=_dossier(gaps=["нет контакта", "неизвестно число участников"]),
    )

    text = render_markdown(build_daily(session, config, today=today), today=today)

    assert text.count("Чего не знаем:") == 1
    assert "**Чего не знаем:** нет контакта; неизвестно число участников" in text


def test_markdown_of_empty_day_explains_itself(today):
    text = render_markdown([], today=today)

    assert "# Утренний список — 29 июля 2026" in text
    assert "**Карточек:** 0" in text
    assert "gtm stats" in text


def test_unknown_scale_is_named_not_guessed(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(
        session,
        inn="7701234567",
        attendees=None,
        dossier=_dossier(
            event={"venue": "Крокус Экспо", "attendees": None},
            reason="в ноябре 2025 проводили «Итоги года» в Крокус Экспо",
        ),
    )

    text = render_markdown(build_daily(session, config, today=today), today=today)

    # Число участников не досочиняется ни в строке масштаба, ни где-либо ещё.
    assert "- **Когда:** 20 ноября 2026, масштаб неизвестен" in text
    assert "человек" not in text


# -------------------------------------------------------------------- csv


def test_csv_is_flat_and_keeps_the_letter(session, config, company_factory, today):
    company_factory("7701234567", name="ООО Ромашка")
    _drafted(
        session,
        inn="7701234567",
        score=0.87,
        dossier=_dossier(gaps=["нет контакта", "неизвестна площадка"]),
    )

    rows = list(csv.reader(io.StringIO(render_csv(build_daily(session, config, today=today)))))

    assert rows[0][:5] == ["expectation_id", "outreach_id", "inn", "company", "reason"]
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["inn"] == "7701234567"
    assert row["company"] == "ООО Ромашка"
    assert row["expected_at"] == "2026-11-20"
    assert row["expected_attendees"] == "600"
    assert row["previous_venue"] == "Крокус Экспо"
    assert row["score"] == "0.87"
    assert row["subject"] == "Ноябрьская конференция: зал на 1000 у Дубровки"
    # Многострочное письмо переживает круговой рейс через CSV.
    assert row["body"] == BODY
    assert row["gaps"] == "нет контакта; неизвестна площадка"


def test_csv_leaves_unknown_fields_empty(session, config, company_factory, today):
    company_factory("7701234567")
    _drafted(
        session,
        inn="7701234567",
        attendees=None,
        dossier=_dossier(event={"attendees": None, "venue": None}),
    )

    rows = list(csv.reader(io.StringIO(render_csv(build_daily(session, config, today=today)))))
    row = dict(zip(rows[0], rows[1], strict=True))

    # Пустая ячейка, а не «None»: файл уезжает в импорт CRM.
    assert row["expected_attendees"] == ""
    assert row["previous_venue"] == ""
    assert row["contact"] == ""


# ----------------------------------------------------------------- стадия


def test_run_deliver_writes_both_files_and_marks_delivered(
    session, config, company_factory, today, tmp_path
):
    company_factory("7701234567", name="ООО Ромашка")
    expectation = _drafted(session, inn="7701234567", score=0.9)

    result, paths = run_deliver(
        session, config, today=today, run_id="run-1", out_dir=tmp_path, summary="собрано 40 → 1"
    )

    assert (result.count_in, result.count_out) == (1, 1)
    assert [p.name for p in paths] == ["2026-07-29.md", "2026-07-29.csv"]
    assert all(p.exists() for p in paths)
    assert "ООО Ромашка" in paths[0].read_text(encoding="utf-8")
    assert "собрано 40 → 1" in paths[0].read_text(encoding="utf-8")
    assert expectation.status == ExpectationStatus.DELIVERED.value
    # Стадия попадает в сводку прогона — иначе непонятно, где обнулилось.
    summary = {row["stage"]: row for row in repo.run_summary(session, "run-1")}
    assert summary["deliver"]["out"] == 1


def test_second_run_the_same_day_does_not_duplicate_cards(
    session, config, company_factory, today, tmp_path
):
    company_factory("7701234567", name="ООО Ромашка")
    _drafted(session, inn="7701234567", score=0.9)

    _, paths = run_deliver(session, config, today=today, run_id="run-1", out_dir=tmp_path)
    first = paths[0].read_text(encoding="utf-8")

    result, paths_again = run_deliver(
        session, config, today=today, run_id="run-2", out_dir=tmp_path
    )

    assert (result.count_in, result.count_out) == (0, 0)
    assert result.details["skipped"] == "already_delivered"
    assert paths_again == paths
    # Файл тот же и карточка в нём одна: утреннюю работу не стёрли и не удвоили.
    text = paths[0].read_text(encoding="utf-8")
    assert text == first
    assert text.count("## 1. ООО Ромашка") == 1
    assert "## 2." not in text


def test_stage_counts_where_the_list_shrank(session, config, company_factory, today, tmp_path):
    company_factory("7701234567")
    company_factory("7702345678")
    company_factory("7703456789")
    _drafted(session, inn="7701234567", score=0.9)
    _drafted(session, inn="7702345678", score=0.01)
    _drafted(session, inn="7703456789", score=0.9, with_letter=False)

    result, _ = run_deliver(session, config, today=today, run_id="run-1", out_dir=tmp_path)

    assert (result.count_in, result.count_out) == (3, 1)
    assert result.details["below_min_score"] == 1
    assert result.details["no_letter"] == 1


def test_limit_is_reported_not_silently_lost(session, config, company_factory, today, tmp_path):
    for n in range(4):
        inn = f"77{n:08d}"
        company_factory(inn)
        _drafted(session, inn=inn, score=0.9)

    result, _ = run_deliver(
        session, config, today=today, run_id="run-1", limit=2, out_dir=tmp_path
    )

    assert result.count_out == 2
    assert result.details["over_limit"] == 2
    # Не попавшие в лимит остаются черновиками и уедут в завтрашний список.
    left = repo.expectations_by_status(session, [ExpectationStatus.DRAFTED.value])
    assert len(left) == 2


def test_empty_day_still_produces_a_file(session, config, today, tmp_path):
    result, paths = run_deliver(session, config, today=today, run_id="run-1", out_dir=tmp_path)

    assert (result.count_in, result.count_out) == (0, 0)
    assert all(p.exists() for p in paths)
    assert "**Карточек:** 0" in paths[0].read_text(encoding="utf-8")
