"""Проверка логики шага 1 без сети.

Сетевую часть проверить отсюда нельзя — из окружения, где писался код, нет
доступа наружу. Зато можно проверить всё остальное, и именно там живут
ошибки, которые дороже всего: выдуманное число участников и неверная дата,
из-за которой письмо уходит после того, как площадку уже выбрали.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from anniversary import (
    LEAD_DAYS,
    Event,
    build_expectations,
    next_anniversary,
    parse_attendees,
    parse_event,
    rank,
)

TODAY = date(2026, 7, 30)


def raw_event(**overrides):
    base = {
        "id": 1001,
        "name": "Конференция дистрибьюторов",
        "starts_at": "2025-11-12T10:00:00+0300",
        "url": "https://example.timepad.ru/event/1001/",
        "organization": {"id": 55, "name": "ООО Ромашка-Трейд"},
        "location": {"city": "Москва"},
        "registration_data": {"sold_count": 650},
    }
    base.update(overrides)
    return base


# ------------------------------------------------------- число участников


def test_sold_count_wins():
    value, source = parse_attendees(
        {"registration_data": {"sold_count": 650, "tickets_limit": 800}}
    )
    assert (value, source) == (650, "продано билетов")


def test_falls_back_to_declared_limit():
    value, source = parse_attendees({"registration_data": {"tickets_limit": 800}})
    assert (value, source) == (800, "заявлено мест")


def test_sums_ticket_types():
    value, source = parse_attendees(
        {"ticket_types": [{"limit": 300}, {"limit": 200}, {"name": "без лимита"}]}
    )
    assert (value, source) == (500, "сумма по типам билетов")


def test_unknown_is_none_not_a_guess():
    """Выдуманное число попадёт в письмо и сожжёт лид. Лучше пусто."""
    assert parse_attendees({}) == (None, "неизвестно")
    assert parse_attendees({"registration_data": {}}) == (None, "неизвестно")
    assert parse_attendees({"registration_data": {"sold_count": 0}}) == (None, "неизвестно")


# ------------------------------------------------------------ разбор события


def test_event_parsed():
    event = parse_event(raw_event())
    assert event is not None
    assert event.organizer == "ООО Ромашка-Трейд"
    assert event.starts_at == date(2025, 11, 12)
    assert event.attendees == 650


def test_event_without_organizer_is_dropped():
    """Без организатора событие бесполезно: писать некому."""
    assert parse_event(raw_event(organization={})) is None
    assert parse_event(raw_event(organization={"id": 55})) is None


def test_event_with_broken_date_is_dropped():
    assert parse_event(raw_event(starts_at="позавчера")) is None
    assert parse_event(raw_event(starts_at=None)) is None


def test_subdomain_used_when_name_missing():
    event = parse_event(raw_event(organization={"id": 7, "subdomain": "romashka"}))
    assert event.organizer == "romashka"


# ------------------------------------------------------------ дата повторения


def test_next_year_same_month():
    assert next_anniversary(date(2025, 11, 12), TODAY) == date(2026, 11, 12)


def test_already_passed_this_year_rolls_forward():
    """Февральская конференция 2026 в июле 2026 уже прошла — ждём февраля 2027,
    иначе письмо уйдёт вдогонку состоявшемуся мероприятию."""
    assert next_anniversary(date(2025, 2, 10), TODAY) == date(2027, 2, 10)


def test_29_february_does_not_crash():
    assert next_anniversary(date(2024, 2, 29), TODAY).month == 2


# ---------------------------------------------------------------- ожидания


def _event(organizer="ООО Ромашка-Трейд", when=date(2025, 11, 12), attendees=650, title="Форум"):
    return Event(
        event_id="1",
        title=title,
        starts_at=when,
        organizer=organizer,
        organizer_id="55",
        attendees=attendees,
        attendees_source="продано билетов",
        city="Москва",
        url=None,
    )


def test_window_opens_120_days_before():
    """Ключевой сценарий всей затеи: ноябрьская конференция, письмо в июле."""
    [exp] = build_expectations([_event()], today=TODAY)

    assert exp.expected_at == date(2026, 11, 12)
    assert exp.window_opens == exp.expected_at - timedelta(days=LEAD_DAYS)
    assert exp.window_opens <= TODAY <= exp.window_closes
    assert exp.is_open is True


def test_window_closes_before_the_event():
    """Писать за неделю до мероприятия поздно: зал давно забронирован."""
    [exp] = build_expectations([_event()], today=TODAY)
    assert exp.window_closes < exp.expected_at


def test_repeats_raise_confidence():
    """Одно мероприятие может быть разовым. Три в один месяц разных лет —
    традиция, и она повторится."""
    events = [
        _event(when=date(2023, 11, 10)),
        _event(when=date(2024, 11, 14)),
        _event(when=date(2025, 11, 12)),
    ]
    [exp] = build_expectations(events, today=TODAY)

    assert exp.repeats == 3
    assert exp.confidence > 0.8


def test_same_year_twice_is_not_a_repeat():
    events = [_event(when=date(2025, 11, 5)), _event(when=date(2025, 11, 20))]
    [exp] = build_expectations(events, today=TODAY)
    assert exp.repeats == 1


def test_different_months_are_different_occasions():
    """Весенняя конференция и декабрьский корпоратив — два повода
    и два разных письма, а не одно усреднённое."""
    events = [_event(when=date(2025, 11, 12)), _event(when=date(2026, 3, 5))]

    expectations = build_expectations(events, today=TODAY)

    assert len(expectations) == 2
    assert {e.expected_at.month for e in expectations} == {11, 3}


def test_max_attendees_across_history():
    events = [_event(when=date(2024, 11, 10), attendees=400),
              _event(when=date(2025, 11, 12), attendees=900)]
    [exp] = build_expectations(events, today=TODAY)
    assert exp.max_attendees == 900


def test_unknown_scale_does_not_crash():
    [exp] = build_expectations([_event(attendees=None)], today=TODAY)
    assert exp.max_attendees is None


# ---------------------------------------------------------------- сортировка


def test_open_window_comes_first():
    """Список читают сверху вниз, поэтому первыми — те, кому писать сегодня."""
    now = _event(when=date(2025, 11, 12), attendees=300)
    later = _event(organizer="АО Синий Кит", when=date(2026, 3, 5), attendees=5000)

    ranked = rank(build_expectations([now, later], today=TODAY))

    assert ranked[0].organizer == "ООО Ромашка-Трейд"
    assert ranked[0].is_open and not ranked[1].is_open


def test_bigger_first_within_the_open_window():
    small = _event(organizer="Малый", attendees=300)
    big = _event(organizer="Крупный", attendees=1200)

    ranked = rank(build_expectations([small, big], today=TODAY))

    assert [e.organizer for e in ranked] == ["Крупный", "Малый"]


@pytest.mark.parametrize("payload", [{}, {"organization": None}, {"starts_at": ""}])
def test_garbage_never_raises(payload):
    assert parse_event(payload) is None
