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
    WINDOW_DAYS,
    Event,
    build_expectations,
    category_histogram,
    dedupe_by_organizer,
    filter_by_categories,
    format_table,
    looks_like_test_account,
    looks_like_venue,
    next_anniversary,
    parse_attendees,
    parse_categories,
    parse_event,
    rank,
    split_into_chunks,
    target_ranges,
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
        # Идентификатор выводится из названия: иначе все тестовые события
        # оказываются одной организацией, и дедупликация склеивает разное.
        organizer_id=str(abs(hash(organizer)) % 10_000),
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


# ------------------------------------------------------- целевые периоды
#
# Два живых прогона подряд вернули обрезанную выборку: сперва сортировка
# по убыванию даты показала последние две недели, потом по возрастанию —
# первые пять дней периода. Оба раза корпоративный сезон, ради которого всё
# затевается, в выборку не попал. Отсюда эти тесты.


def test_range_matches_the_open_contact_window():
    """Период выводится из механики окна, а не назначается на глаз.

    Окно открыто, когда до события остаётся от 60 до 120 дней. Значит писать
    сегодня надо про то, что повторится через 60-120 дней, а его прошлогодний
    оригинал лежит ровно в том же промежутке год назад.
    """
    [(start, end)] = target_ranges(date(2026, 7, 30), years=1)

    assert start == date(2026, 7, 30) + timedelta(days=WINDOW_DAYS) - timedelta(days=365)
    assert (end - start).days == LEAD_DAYS - WINDOW_DAYS


def test_range_is_two_months_not_six():
    """Шестимесячный период московских событий не выбрать никаким потолком,
    и обрезка съедает именно нужный конец."""
    [(start, end)] = target_ranges(date(2026, 7, 30), years=1)

    assert 55 <= (end - start).days <= 65


def test_october_run_targets_last_years_corporate_season():
    """Стоя в начале октября, мы должны смотреть на прошлогодние декабрь
    и январь — то есть на корпоративы, которые вот-вот начнут планировать."""
    [(start, end)] = target_ranges(date(2026, 10, 1), years=1)

    assert start <= date(2025, 12, 10) <= end


def test_horizon_extends_only_the_far_edge():
    """Заглянуть вперёд можно, но ближний край двигать нельзя: он привязан
    к тому, когда окно открывается."""
    [(start, end)] = target_ranges(date(2026, 7, 30), years=1)
    [(start_far, end_far)] = target_ranges(date(2026, 7, 30), years=1, horizon_days=60)

    assert start_far == start
    assert (end_far - end).days == 60


def test_ranges_shift_by_calendar_year_not_365_days():
    ranges = target_ranges(date(2026, 3, 1), years=3)

    assert [r[0].year for r in ranges] == [2025, 2024, 2023]


# ------------------------------------------------------- нарезка на куски


def test_chunks_cover_the_period_without_gaps_or_overlaps():
    """Потолок применяется к запросу, поэтому единственный способ ничего
    не потерять — спрашивать короткими отрезками."""
    chunks = split_into_chunks(date(2025, 9, 28), date(2025, 11, 27), 7)

    assert chunks[0][0] == date(2025, 9, 28)
    assert chunks[-1][1] == date(2025, 11, 27)
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:], strict=False):
        assert next_start == prev_end + timedelta(days=1)


def test_short_period_is_a_single_chunk():
    chunks = split_into_chunks(date(2025, 9, 28), date(2025, 9, 30), 7)
    assert chunks == [(date(2025, 9, 28), date(2025, 9, 30))]


def test_single_day_period():
    day = date(2025, 9, 28)
    assert split_into_chunks(day, day, 7) == [(day, day)]


# ------------------------------------------------------- площадки не клиенты


@pytest.mark.parametrize(
    "organizer",
    [
        "Культурный центр ЗИЛ",
        "Госфильмофонд РФ / Кинотеатр «Иллюзион»",
        "Театр на Малой Бронной",
        "Библиотека имени Некрасова",
        "Московский Планетарий",
        "Концертный зал «Пример»",
        # Из живого прогона: эти прошли мимо первой версии фильтра.
        "ИСТОРИЧКА",
        "МОСКОВСКИЙ ДОМ КНИГИ",
        "Киношкола «Свободное кино»",
        "Кинопрокатная компани ВОЛЬГА",
        "Кино в The Rink",
        "Кудрявый фестиваль CURLFEST",
        "РОСКИНО",
    ],
)
def test_venues_are_recognised(organizer):
    """У них свой зал — наш они арендовать не будут. В первом прогоне список
    состоял ровно из таких."""
    assert looks_like_venue(organizer) is True


@pytest.mark.parametrize(
    "organizer",
    [
        "ООО Ромашка-Трейд",
        "АО Синий Кит",
        "Ассоциация производителей упаковки",
        "Группа компаний Пример",
    ],
)
def test_real_prospects_are_not_flagged(organizer):
    assert looks_like_venue(organizer) is False


# ------------------------------------------- заглушка вместо числа участников
#
# Из живого прогона: «Сила ветра — 270020 человек» на дне открытых дверей.
# Организаторы ставят гигантский лимит, когда ограничения нет.


def test_implausible_limit_is_not_a_number_of_people():
    """Такое число хуже отсутствующего: выглядит как факт и попадёт в письмо."""
    value, source = parse_attendees({"registration_data": {"tickets_limit": 270020}})

    assert value is None
    assert source == "лимит не задан"


def test_plausible_limit_survives():
    assert parse_attendees({"registration_data": {"tickets_limit": 650}})[0] == 650


def test_sold_count_is_trusted_even_if_large():
    """Проданные билеты — факт, а не заглушка."""
    assert parse_attendees({"registration_data": {"sold_count": 25000}})[0] == 25000


# ------------------------------------------------------------- категории


def test_categories_parsed_from_dicts():
    event = parse_event(raw_event(categories=[{"id": 1, "name": "Бизнес"}, {"name": "IT"}]))
    assert event.categories == ["Бизнес", "IT"]


def test_categories_tolerate_junk():
    assert parse_categories({"categories": None}) == []
    assert parse_categories({"categories": [{}, {"name": "  "}, "Кино"]}) == ["Кино"]


def test_category_histogram_counts_and_sorts():
    events = [
        _event(organizer="A", when=date(2025, 11, 1)),
        _event(organizer="B", when=date(2025, 11, 2)),
        _event(organizer="C", when=date(2025, 11, 3)),
    ]
    events[0].categories = ["Бизнес"]
    events[1].categories = ["Бизнес"]
    events[2].categories = ["Концерт"]

    histogram = category_histogram(build_expectations(events, today=TODAY))

    assert histogram[0] == ("Бизнес", 2)


def test_filter_by_categories_matches_substring_case_insensitively():
    business = _event(organizer="A")
    business.categories = ["Бизнес и предпринимательство"]
    concert = _event(organizer="B")
    concert.categories = ["Концерты"]
    expectations = build_expectations([business, concert], today=TODAY)

    kept = filter_by_categories(expectations, ["бизнес"])

    assert [e.organizer for e in kept] == ["A"]


def test_empty_filter_keeps_everything():
    expectations = build_expectations([_event()], today=TODAY)
    assert filter_by_categories(expectations, []) == expectations


# ------------------------------------------------- одна компания — одна строка


def test_dedupe_keeps_the_open_window_over_the_bigger_event():
    """Писать надо тому, кому пора, а не тому, у кого мероприятие крупнее."""
    soon = _event(organizer="Ромашка", when=date(2025, 11, 12), attendees=300)
    bigger_later = _event(organizer="Ромашка", when=date(2026, 3, 5), attendees=5000)

    [exp] = dedupe_by_organizer(build_expectations([soon, bigger_later], today=TODAY))

    assert exp.is_open is True
    assert exp.max_attendees == 300
    # Мартовское мероприятие 2026 года к концу июля уже прошло, поэтому
    # его повторение ждём в марте 2027 — оно и уходит в примечание.
    assert "март 2027" in exp.other_occasions


def test_dedupe_keeps_different_companies_apart():
    events = [_event(organizer="Ромашка"), _event(organizer="Синий Кит")]
    assert len(dedupe_by_organizer(build_expectations(events, today=TODAY))) == 2


def test_repeat_years_are_listed_once_each():
    """В выводе было «2024, 2024, 2024, 2024, 2025, 2025» — считалось верно,
    печаталось лишнее."""
    events = [
        _event(when=date(2024, 11, 5)),
        _event(when=date(2024, 11, 12)),
        _event(when=date(2025, 11, 9)),
    ]
    [exp] = build_expectations(events, today=TODAY)

    text = format_table([exp], today=TODAY)

    assert "2024, 2025" in text
    assert "2024, 2024" not in text


# ------------------------------------------------- тестовые аккаунты платформы


def test_platform_test_accounts_are_dropped():
    """В прогоне пролез «test-org» с «Премией ИТ»: выглядит правдоподобно,
    а компании за ним нет."""
    assert looks_like_test_account("test-org") is True
    assert looks_like_test_account("Тестовая организация") is True


def test_real_names_containing_test_survive():
    """«Protest Group» и «Тестов и партнёры» — настоящие названия."""
    assert looks_like_test_account("Protest Group") is False
    assert looks_like_test_account("X5 Tech") is False
