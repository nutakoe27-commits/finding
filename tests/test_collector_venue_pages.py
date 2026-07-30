"""Краулер страниц площадок-конкурентов — главный бесплатный источник.

Эвристики по определению неточны, поэтому тесты фиксируют не «находит всё»,
а два свойства, от которых зависит доверие к источнику: не выдумывает числа
(площадь и рубли за участников не берутся) и не плодит дубли при повторном
обходе.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import select

from gtm.collectors.venue_pages import (
    VenuePagesCollector,
    _TextAndLinks,
    company_names,
    extract_events,
    parse_attendees,
    parse_event_date,
)
from gtm.config import VenueSite
from gtm.storage.models import Fact, FactType, Quarantine

INN = "7718260181"

PAGE = """<html><head><style>.x{color:red}</style></head><body>
<h2>Прошедшие мероприятия</h2>
<div class="card"><p>ООО «Ромашка-Трейд» — конференция дистрибьюторов,
   ноябрь 2025, 650 человек</p></div>
<div class="card"><p>Годовое собрание АО «Синий Кит», 12 декабря 2024,
   более 1 200 участников</p></div>
<div class="card"><p>ПАО «Северный Ветер» провело форум на 90 гостей в 2025 году</p></div>
<div class="card"><p>Площадь зала 800 кв. м, аренда от 800 000 руб.</p></div>
<div class="card"><p>ООО «Тихая Гавань» — просто упоминание без даты и масштаба</p></div>
<a href="/proshedshie-meropriyatiya">Ещё мероприятия</a>
<a href="/contacts">Контакты</a>
<a href="https://other.example/events">Чужой домен</a>
</body></html>"""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Пол-запроса в секунду — правильно для чужого сайта и невыносимо для тестов."""
    monkeypatch.setattr(VenuePagesCollector, "_pause", lambda self: None)


@pytest.fixture()
def one_venue(config):
    """Одна площадка, одна страница, без перехода по ссылкам.

    Обход discover_paths и переходы вглубь здесь только шумят — им посвящён
    отдельный тест, который включает глубину явно.
    """
    config.venues.venues = [
        VenueSite(
            name="Тестовый Холл",
            site="https://venue.example",
            capacity=1500,
            enabled=True,
            pages=["https://venue.example/events"],
        )
    ]
    config.venues.catalogs = []
    config.venues.crawl.max_depth = 0
    return config


# ------------------------------------------------------------- разбор HTML


def test_text_extraction_drops_styles_and_keeps_line_breaks():
    parser = _TextAndLinks()
    parser.feed(PAGE)

    assert "color:red" not in parser.text
    # Карточки не должны склеиваться: иначе дата одной пристанет к компании другой.
    assert "ООО «Ромашка-Трейд» — конференция дистрибьюторов, ноябрь 2025, 650 человек" in (
        parser.text
    )


def test_links_are_collected_with_anchor_text():
    parser = _TextAndLinks()
    parser.feed(PAGE)

    hrefs = dict(parser.links)
    assert hrefs["/proshedshie-meropriyatiya"] == "Ещё мероприятия"


# ------------------------------------------------------------------ числа


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("на 650 человек", 650),
        ("более 1 200 участников", 1200),
        ("около 800 гостей", 800),
        ("500 делегатов", 500),
    ],
)
def test_attendees_parsed(text, expected):
    assert parse_attendees(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Площадь зала 800 кв. м",
        "аренда от 800 000 руб.",
        "площадь 1200 м²",
        "бюджет 500 тыс",
    ],
)
def test_area_and_money_are_not_attendees(text):
    """Выдуманное число участников в письме — не стилистика, а сгоревший лид."""
    assert parse_attendees(text) is None


def test_absurd_counts_are_rejected():
    assert parse_attendees("зал 3 человека") is None
    assert parse_attendees("город 12 000 000 человек") is None


# ------------------------------------------------------------------- даты


def test_date_with_day_month_year():
    assert parse_event_date("12 декабря 2024 года") == (date(2024, 12, 12), "day")


def test_date_with_month_and_year_only():
    assert parse_event_date("ноябрь 2025") == (date(2025, 11, 1), "month")


def test_date_with_year_only():
    assert parse_event_date("итоги 2025 года") == (date(2025, 1, 1), "year")


def test_future_year_is_not_a_past_event():
    assert parse_event_date("конференция 2030", today=date(2026, 7, 29)) is None


def test_text_without_year_gives_nothing():
    assert parse_event_date("в прошлом декабре") is None


# ------------------------------------------------------------------ имена


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ООО «Ромашка-Трейд» провело", "ООО Ромашка-Трейд"),
        ("собрание АО «Синий Кит» прошло", "АО Синий Кит"),
        ("ООО Ромашка Трейд провело", "ООО Ромашка Трейд"),
        ("Группа компаний «Первый Гвоздь»", "Группа компаний Первый Гвоздь"),
        ("Холдинг Пример-Логистика организовал", "Холдинг Пример-Логистика"),
    ],
)
def test_company_names_keep_multiword_names(text, expected):
    assert company_names(text)[0] == expected


def test_event_titles_in_quotes_are_not_companies():
    assert company_names('фестиваль «Большая конференция» прошёл') == []


# --------------------------------------------------------------- извлечение


def test_extract_events_needs_date_or_scale():
    parser = _TextAndLinks()
    parser.feed(PAGE)

    events = extract_events(parser.text, min_attendees=0)
    names = {event["company_name"] for event in events}

    assert "ООО Ромашка-Трейд" in names
    assert "АО Синий Кит" in names
    # Упоминание без даты и без масштаба — не событие, а шум.
    assert "ООО Тихая Гавань" not in names


def test_extract_events_applies_min_attendees():
    parser = _TextAndLinks()
    parser.feed(PAGE)

    events = extract_events(parser.text, min_attendees=250)
    names = {event["company_name"] for event in events}

    # Форум на 90 человек — не наш сегмент, отсекаем на входе.
    assert "ПАО Северный Ветер" not in names
    assert "ООО Ромашка-Трейд" in names


# ---------------------------------------------------------------- обход


@respx.mock
def test_crawl_collects_facts_and_follows_event_links(session, one_venue):
    one_venue.venues.crawl.max_depth = 1
    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(return_value=httpx.Response(200, text=PAGE))
    deeper = respx.get("https://venue.example/proshedshie-meropriyatiya").mock(
        return_value=httpx.Response(200, text=PAGE)
    )
    other = respx.get("https://other.example/events").mock(return_value=httpx.Response(200))

    collector = VenuePagesCollector(session, one_venue, run_id="test")
    facts = list(collector.collect())

    assert deeper.called, "ссылку про мероприятия обходим"
    assert not other.called, "чужой домен не наш"
    assert facts
    assert all(fact.payload["venue"] == "Тестовый Холл" for fact in facts)
    assert all(fact.fact_type == FactType.PAST_EVENT.value for fact in facts)


@respx.mock
def test_robots_disallow_is_respected(session, one_venue):
    respx.get("https://venue.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /events\n")
    )
    page = respx.get("https://venue.example/events").mock(return_value=httpx.Response(200))

    facts = list(VenuePagesCollector(session, one_venue, run_id="test").collect())

    assert not page.called
    assert facts == []


@respx.mock
def test_source_uid_is_stable_between_runs(session, one_venue):
    """Идемпотентность держится на source_uid. Встроенный hash() рандомизирован
    между процессами, поэтому здесь обязателен устойчивый дайджест."""
    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(return_value=httpx.Response(200, text=PAGE))

    def uids() -> set[str]:
        collector = VenuePagesCollector(session, one_venue, run_id="test")
        return {fact.source_uid for fact in collector.collect()}

    first, second = uids(), uids()

    assert first == second
    assert all(uid.startswith("venue:") for uid in first)


@respx.mock
def test_run_is_idempotent_and_quarantines_unknown_companies(session, one_venue):
    """Названия с сайта к ИНН не сводятся — компаний ещё нет в базе.
    Это штатный путь: в карантин, а не в письма."""
    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(return_value=httpx.Response(200, text=PAGE))

    first = VenuePagesCollector(session, one_venue, run_id="test").run()
    assert first.count_out > 0
    assert first.details["unresolved"] == first.count_out

    second = VenuePagesCollector(session, one_venue, run_id="test").run()
    assert second.count_out == 0
    assert second.details["duplicates"] == first.count_out

    assert session.scalars(select(Quarantine)).all()
    facts = session.scalars(select(Fact)).all()
    assert len(facts) == first.count_out


@respx.mock
def test_check_mode_writes_nothing_and_reports(session, one_venue):
    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(return_value=httpx.Response(200, text=PAGE))

    collector = VenuePagesCollector(session, one_venue, run_id="test")
    facts = list(collector.collect(check=True))

    assert facts == [], "сверка адресов ничего не сохраняет"
    report = collector.check_report()
    assert "Тестовый Холл" in report
    assert "https://venue.example/events" in report


@respx.mock
def test_dead_address_is_reported_not_swallowed(session, one_venue):
    """Список площадок собран без доступа к сети, поэтому битый адрес обязан
    быть видимым — иначе он выглядит как «на сайте нет мероприятий»."""
    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(return_value=httpx.Response(404))

    collector = VenuePagesCollector(session, one_venue, run_id="test")
    list(collector.collect(check=True))

    report = collector.check_report()
    assert "404" in report
    assert "проверьте адреса" in report


@respx.mock
def test_network_error_does_not_stop_the_crawl(session, config):
    config.venues.crawl.max_depth = 0
    config.venues.catalogs = []
    config.venues.venues = [
        VenueSite(name="Мёртвый", site="https://dead.example", pages=["https://dead.example/e"]),
        VenueSite(name="Живой", site="https://live.example", pages=["https://live.example/e"]),
    ]
    respx.get("https://dead.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://dead.example/e").mock(side_effect=httpx.ConnectError("нет связи"))
    respx.get("https://live.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://live.example/e").mock(return_value=httpx.Response(200, text=PAGE))

    collector = VenuePagesCollector(session, config, run_id="test")
    facts = list(collector.collect())

    assert facts, "падение одной площадки не должно ронять обход остальных"
    assert "ConnectError" in collector.check_report()


def test_empty_config_is_not_a_crash(session, config):
    config.venues.venues = []
    config.venues.catalogs = []

    assert list(VenuePagesCollector(session, config, run_id="test").collect()) == []


@respx.mock
def test_skip_domains_are_not_crawled(session, config):
    """Собственную страницу обходить незачем — это свои же клиенты."""
    config.venues.venues = [
        VenueSite(name="Свои", site="https://eventlocation.ru", pages=[
            "https://eventlocation.ru/location/soyuz_hall_moscow"
        ])
    ]
    config.venues.catalogs = []
    page = respx.get("https://eventlocation.ru/location/soyuz_hall_moscow").mock(
        return_value=httpx.Response(200, text=PAGE)
    )

    list(VenuePagesCollector(session, config, run_id="test").collect())

    assert not page.called


@respx.mock
def test_user_agent_carries_contact(session, one_venue):
    """Так принято, и это снимает половину вопросов, если администратор
    сайта заметит обход."""
    captured: list[str] = []

    def record(request):
        captured.append(request.headers["user-agent"])
        return httpx.Response(200, text=PAGE)

    respx.get("https://venue.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://venue.example/events").mock(side_effect=record)

    list(VenuePagesCollector(session, one_venue, run_id="test").collect())

    assert captured
    assert "gtm-research" in captured[0]
    assert "mailto:" in captured[0]


def test_total_page_budget_stops_the_run_and_says_so(session, config):
    """Потолок на весь прогон, а не только на площадку. Без него обход двух
    десятков сайтов с паузами растягивается на часы, и непонятно, работает
    он или завис. Недобранные площадки должны быть видны в отчёте, иначе
    выглядят как «на их сайтах ничего нет»."""
    config.venues.catalogs = []
    config.venues.crawl.max_depth = 0
    config.venues.crawl.max_pages_total = 2
    config.venues.venues = [
        VenueSite(name=f"Холл {i}", site=f"https://v{i}.example", pages=[f"https://v{i}.example/e"])
        for i in range(5)
    ]

    with respx.mock:
        respx.get(url__regex=r"https://v\d\.example/robots\.txt").mock(
            return_value=httpx.Response(404)
        )
        respx.get(url__regex=r"https://v\d\.example/e").mock(
            return_value=httpx.Response(200, text=PAGE)
        )
        collector = VenuePagesCollector(session, config, run_id="test")
        list(collector.collect(check=True))

    assert collector._pages_fetched == 2
    assert "лимит страниц на прогон исчерпан" in collector.check_report()


def test_crawler_is_not_part_of_the_daily_run():
    """Страницы «прошедшие мероприятия» обновляются раз в сезон, не раз в сутки,
    а обход занимает десятки минут. Один медленный сайт не должен задерживать
    выдачу утреннего списка."""
    from gtm.pipeline import COLLECT_ORDER, PERIODIC_SOURCES

    assert "venue_pages" not in COLLECT_ORDER
    assert "venue_pages" in PERIODIC_SOURCES


def test_max_pages_limit_is_honoured(session, one_venue):
    """Лимит страниц на площадку конечен: без него обход уходит гулять
    по всему сайту."""
    one_venue.venues.venues[0].pages = [f"https://venue.example/{i}" for i in range(50)]
    one_venue.venues.crawl.max_pages_per_site = 3

    with respx.mock:
        respx.get(url__regex=r"https://venue\.example/.*").mock(
            return_value=httpx.Response(200, text=PAGE)
        )
        collector = VenuePagesCollector(session, one_venue, run_id="test")
        list(collector.collect(check=True))

    assert len(collector.reports[0].pages) <= 3
