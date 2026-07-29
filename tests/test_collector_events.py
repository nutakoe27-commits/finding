"""Тесты архива прошедших мероприятий.

Проверяем то, из-за чего сбор ломается в бою: разбор ответа Timepad,
пагинацию (иначе видно только первую сотню), параллельную выборку страниц
(она не должна ни терять, ни удваивать записи), отсев мелочи на входе и
границы интервалов бэкфилла. В сеть не ходим — respx.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import func, select

from gtm.collectors import events_archive
from gtm.collectors.base import CollectorError
from gtm.collectors.events_archive import (
    _MAX_CONCURRENT_PAGES,
    _PAGE_LIMIT,
    EventsArchiveCollector,
    FixtureProvider,
    TimepadProvider,
    backfill_windows,
    parse_attendees,
    parse_starts_at,
    to_event_record,
    to_raw_fact,
)
from gtm.settings import REPO_ROOT, Settings
from gtm.storage.models import ApiSpend, Fact, FactType

BASE_URL = "https://api.timepad.ru/v1"
EVENTS = f"{BASE_URL}/events"

SINCE = date(2024, 1, 1)
UNTIL = date(2025, 12, 31)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Ретраи не должны тормозить тесты по-настоящему."""
    delays: list[float] = []

    async def fake(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(events_archive, "_apause", fake)
    return delays


@pytest.fixture()
def collector(session, config):
    return EventsArchiveCollector(session, config, run_id="test", settings=Settings())


@pytest.fixture()
def live(config):
    """По умолчанию источник в offline; тесты по сети включают live явно."""
    return events_source(config, mode="live")


@pytest.fixture()
def provider():
    return TimepadProvider(base_url=BASE_URL, cities=["Москва"], retry_backoff=1.0)


def events_source(config, **fields) -> dict:
    """Правка настроек источника: source() каждый раз собирает SourceConfig
    заново, поэтому менять надо исходный словарь."""
    raw = config.sources.model_extra["events_archive"]
    raw.update(fields)
    return raw


ORGANIZER = "ООО «Атлант-Технологии»"


def timepad_event(
    event_id: int | str = 9001,
    *,
    name: str = "Атлант Тех Саммит 2025",
    organizer: str | None = ORGANIZER,
    starts_at: str = "2025-11-13T10:00:00+0300",
    tickets_limit: int | None = 900,
    ticket_types: list[dict] | None = None,
    city: str = "Москва",
    address: str | None = "Раменский бул., 1",
) -> dict:
    item: dict = {
        "id": event_id,
        "name": name,
        "starts_at": starts_at,
        "url": f"https://example.timepad.ru/event/{event_id}/",
        "location": {"city": city, "address": address},
        "categories": [{"id": 452, "name": "Бизнес"}, {"id": 462, "name": "IT"}],
    }
    if organizer is not None:
        item["organization"] = {"id": 55, "name": organizer}
    if tickets_limit is not None:
        item["registration_data"] = {"tickets_limit": tickets_limit}
    if ticket_types is not None:
        item["ticket_types"] = ticket_types
    return item


def page(items: list[dict], *, total: int | None = None) -> dict:
    return {"values": items, "total": len(items) if total is None else total}


def many(count: int, *, start: int = 1) -> list[dict]:
    return [timepad_event(event_id=n) for n in range(start, start + count)]


def route_pages(pages: dict[int, dict]):
    """Отдавать страницу по параметру skip — заодно проверяется, что
    коллектор просит именно те смещения, которые мы ждём."""

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("skip", 0))
        return httpx.Response(200, json=pages[skip])

    return respx.get(EVENTS).mock(side_effect=handler)


# ------------------------------------------------------------------ разбор


def test_event_becomes_record():
    record = to_event_record(timepad_event())
    assert record is not None
    assert record.source_uid == "timepad:9001"
    assert record.title == "Атлант Тех Саммит 2025"
    assert record.starts_at == date(2025, 11, 13)
    assert record.organizer_name == ORGANIZER
    # ИНН Timepad не отдаёт: сводить к компании будет resolve.
    assert record.organizer_inn is None
    assert record.attendees == 900
    assert record.venue == "Раменский бул., 1"
    assert record.city == "Москва"
    assert record.categories == ["Бизнес", "IT"]


def test_record_becomes_fact_with_full_payload():
    fact = to_raw_fact(to_event_record(timepad_event()))
    assert fact.fact_type == FactType.PAST_EVENT.value
    assert fact.company_name == ORGANIZER
    assert fact.occurred_at == date(2025, 11, 13)
    assert set(fact.payload) == {
        "title",
        "attendees",
        "venue",
        "city",
        "url",
        "categories",
        "organizer_name",
        "provider",
    }
    assert fact.payload["provider"] == "timepad"


def test_event_without_organizer_is_dropped():
    """Без организатора факт бесполезен: сводить не к чему, письмо некому."""
    assert to_event_record(timepad_event(organizer=None)) is None
    assert to_event_record(timepad_event(organizer="  ")) is None


def test_event_without_readable_date_is_dropped():
    """Годовщина считается от даты. Нет даты — нет сигнала."""
    assert to_event_record(timepad_event(starts_at="как-нибудь весной")) is None
    assert to_event_record(timepad_event(starts_at="")) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-11-13T10:00:00+0300", date(2025, 11, 13)),
        ("2025-11-13", date(2025, 11, 13)),
        ("13.11.2025", None),
        (None, None),
    ],
)
def test_parse_starts_at(raw, expected):
    assert parse_starts_at(raw) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"registration_data": {"tickets_limit": 700}}, 700),
        # Ноль в лимите у Timepad значит «без ограничения», а не «никого».
        ({"registration_data": {"tickets_limit": 0}}, None),
        ({"ticket_types": [{"limit": 300}, {"limit": 250}]}, 550),
        ({"registration_data": {"tickets_limit": 900}, "ticket_types": [{"limit": 10}]}, 900),
        ({}, None),
        ({"registration_data": {"tickets_limit": "мусор"}}, None),
    ],
)
def test_parse_attendees(item, expected):
    assert parse_attendees(item) == expected


def test_source_uid_is_stable_across_runs():
    """source_uid держится на id события: повторный сбор не удваивает базу."""
    first = to_event_record(timepad_event(event_id=9001))
    second = to_event_record(timepad_event(event_id=9001, name="Переименовали афишу"))
    assert first.source_uid == second.source_uid == "timepad:9001"
    assert to_raw_fact(first).source_uid == to_raw_fact(second).source_uid


# ---------------------------------------------------------------- запросы


@respx.mock
def test_request_carries_city_window_and_stable_sort(provider):
    route = respx.get(EVENTS).mock(return_value=httpx.Response(200, json=page([])))
    provider.fetch(since=SINCE, until=UNTIL)

    params = route.calls[0].request.url.params
    assert params["cities"] == "Москва"
    assert params["starts_at_min"] == "2024-01-01"
    assert params["starts_at_max"] == "2025-12-31"
    assert params["limit"] == str(_PAGE_LIMIT)
    # Порядок обязан быть детерминированным: страницы читаются параллельно.
    assert params["sort"] == "+starts_at"
    # Организация и место в кратком ответе не приходят, а без них факта нет.
    assert "organization" in params["fields"] and "location" in params["fields"]


@respx.mock
def test_pagination_walks_all_pages(provider):
    route = route_pages(
        {
            0: page(many(100, start=1), total=250),
            100: page(many(100, start=101), total=250),
            200: page(many(50, start=201), total=250),
        }
    )
    records = provider.fetch(since=SINCE, until=UNTIL)

    assert len(records) == 250
    assert sorted(int(c.request.url.params["skip"]) for c in route.calls) == [0, 100, 200]


@respx.mock
def test_single_page_does_not_ask_for_more(provider):
    """total уже покрыт первой страницей — второй запрос был бы холостым."""
    route = respx.get(EVENTS).mock(return_value=httpx.Response(200, json=page(many(30))))
    assert len(provider.fetch(since=SINCE, until=UNTIL)) == 30
    assert route.call_count == 1


@respx.mock
def test_parallel_pages_neither_lose_nor_duplicate(provider):
    """Выборка на стороне Timepad живая: на границе страниц одно и то же
    событие приезжает дважды. Записей должно остаться ровно столько,
    сколько разных id, и в порядке страниц."""
    route_pages(
        {
            0: page([timepad_event(1), timepad_event(2), timepad_event(3)], total=200),
            100: page([timepad_event(3), timepad_event(4), timepad_event(5)], total=200),
        }
    )
    records = provider.fetch(since=SINCE, until=UNTIL)

    uids = [r.source_uid for r in records]
    assert uids == ["timepad:1", "timepad:2", "timepad:3", "timepad:4", "timepad:5"]
    assert len(uids) == len(set(uids))


@respx.mock
def test_pages_are_fetched_in_parallel_with_a_ceiling(provider):
    """Ради бэкфилла страницы и берутся параллельно — но не все разом,
    иначе Timepad начинает отвечать 429."""
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        skip = int(request.url.params.get("skip", 0))
        return httpx.Response(200, json=page(many(100, start=skip + 1), total=600))

    respx.get(EVENTS).mock(side_effect=handler)
    records = provider.fetch(since=SINCE, until=UNTIL)

    assert len(records) == 600
    assert 1 < peak <= _MAX_CONCURRENT_PAGES


@respx.mock
def test_fetch_async_is_the_real_entry_point(provider):
    """Синхронный fetch — обёртка: async не должен протекать в конвейер,
    но и прятаться от вызывающего, которому нужен свой цикл, не должен."""
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=page(many(3))))
    records = asyncio.run(provider.fetch_async(since=SINCE, until=UNTIL))
    assert [r.source_uid for r in records] == ["timepad:1", "timepad:2", "timepad:3"]


@respx.mock
def test_broken_event_in_a_page_does_not_kill_the_page(provider):
    respx.get(EVENTS).mock(
        return_value=httpx.Response(
            200, json=page([timepad_event(1), timepad_event(2, organizer=None)])
        )
    )
    assert [r.source_uid for r in provider.fetch(since=SINCE, until=UNTIL)] == ["timepad:1"]


# ------------------------------------------------------------------ ошибки


@respx.mock
def test_transient_500_is_retried(provider, no_sleep):
    route = respx.get(EVENTS).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=page(many(2)))]
    )
    assert len(provider.fetch(since=SINCE, until=UNTIL)) == 2
    assert route.call_count == 2
    assert no_sleep == [1.0]


@respx.mock
def test_permanent_429_fails_with_readable_error(provider):
    route = respx.get(EVENTS).mock(return_value=httpx.Response(429))
    with pytest.raises(CollectorError, match="не ответил"):
        provider.fetch(since=SINCE, until=UNTIL)
    assert route.call_count == provider.retries


@respx.mock
def test_400_is_not_retried(provider):
    route = respx.get(EVENTS).mock(return_value=httpx.Response(400, text="bad range"))
    with pytest.raises(CollectorError, match="400"):
        provider.fetch(since=SINCE, until=UNTIL)
    assert route.call_count == 1


@respx.mock
def test_non_json_answer_is_reported(provider):
    respx.get(EVENTS).mock(return_value=httpx.Response(200, text="<html>тех.работы</html>"))
    with pytest.raises(CollectorError, match="не JSON"):
        provider.fetch(since=SINCE, until=UNTIL)


@respx.mock
def test_network_error_is_retried_then_reported(provider):
    route = respx.get(EVENTS).mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(CollectorError, match="сеть"):
        provider.fetch(since=SINCE, until=UNTIL)
    assert route.call_count == provider.retries


# -------------------------------------------------------- отсев по масштабу


@respx.mock
def test_small_events_are_cut_before_anything_costly(collector, config, live):
    """Самый дешёвый фильтр в системе: мелочь отсекается до записи в базу.
    Неизвестный масштаб — не повод отсекать, крупные события бывают без числа."""
    threshold = collector.min_attendees
    respx.get(EVENTS).mock(
        return_value=httpx.Response(
            200,
            json=page(
                [
                    timepad_event(1, tickets_limit=threshold - 1),
                    timepad_event(2, tickets_limit=threshold),
                    timepad_event(3, tickets_limit=None),
                ]
            ),
        )
    )
    facts = list(collector.collect(since=SINCE, until=UNTIL))

    assert [f.source_uid for f in facts] == ["timepad:2", "timepad:3"]
    assert [f.payload["attendees"] for f in facts] == [threshold, None]
    assert collector.skipped_small == 1


@respx.mock
def test_threshold_comes_from_config(collector, config, live):
    config.sources.model_extra["events_archive"]["providers"]["timepad"]["min_attendees"] = 700
    respx.get(EVENTS).mock(
        return_value=httpx.Response(
            200, json=page([timepad_event(1, tickets_limit=650), timepad_event(2)])
        )
    )
    facts = list(collector.collect(since=SINCE, until=UNTIL))
    assert [f.source_uid for f in facts] == ["timepad:2"]


@respx.mock
def test_too_small_is_visible_in_the_run_summary(collector, live):
    """Отсев должен быть виден в сводке запуска, а не только в логе."""
    respx.get(EVENTS).mock(
        return_value=httpx.Response(
            200, json=page([timepad_event(1, tickets_limit=10), timepad_event(2)])
        )
    )
    result = collector.run(since=SINCE, until=UNTIL)
    assert result.count_in == 1
    assert result.details["too_small"] == 1


# ---------------------------------------------------------------- провайдеры


def test_unimplemented_provider_is_an_error(collector, live):
    """Молча пропустить нельзя: источник «работал» бы и давал ноль."""
    with pytest.raises(CollectorError, match="timepad"):
        collector.collect(since=SINCE, until=UNTIL, providers=["expomap"])


def test_providers_switched_off_in_yaml_are_not_used(collector, config, live):
    """YAML 1.1 читает `off` как False — ловушка на ровном месте: выключенный
    провайдер не должен молча превратиться во включённый."""
    providers = config.sources.model_extra["events_archive"]["providers"]
    providers["expomap"]["mode"] = False
    providers["timepad"]["mode"] = "off"
    with pytest.raises(CollectorError, match="ни одного рабочего провайдера"):
        collector.collect(since=SINCE, until=UNTIL)


def test_inverted_window_is_an_error(collector, live):
    with pytest.raises(CollectorError, match="вывернуто"):
        collector.collect(since=UNTIL, until=SINCE)


# ------------------------------------------------------------------ offline


def fixture_provider(tmp_path, entries: list[dict]) -> FixtureProvider:
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": entries}, ensure_ascii=False), encoding="utf-8")
    return FixtureProvider(path=path)


def fixture_entry(uid: str, starts_at: str, *, attendees: int | None = 500) -> dict:
    return {
        "id": uid,
        "title": f"Мероприятие {uid}",
        "starts_at": starts_at,
        "organizer_name": ORGANIZER,
        "attendees": attendees,
        "venue": "Кластер «Ломоносов»",
        "city": "Москва",
        "url": f"https://events.example.org/{uid}",
        "categories": ["Бизнес"],
    }


def test_fixture_provider_returns_only_the_asked_window(tmp_path):
    """Провайдер обязан отдавать ровно запрошенный интервал: иначе бэкфилл
    по годовым кускам собрал бы одно и то же несколько раз."""
    provider = fixture_provider(
        tmp_path,
        [
            fixture_entry("a", "2024-11-14"),
            fixture_entry("b", "2025-11-13"),
            fixture_entry("c", "2026-11-12"),
        ],
    )
    records = provider.fetch(since=date(2025, 1, 1), until=date(2025, 12, 31))
    assert [r.source_uid for r in records] == ["fixture:b"]


def test_fixture_provider_includes_window_edges(tmp_path):
    provider = fixture_provider(
        tmp_path, [fixture_entry("a", "2025-01-01"), fixture_entry("b", "2025-12-31")]
    )
    records = provider.fetch(since=date(2025, 1, 1), until=date(2025, 12, 31))
    assert {r.source_uid for r in records} == {"fixture:a", "fixture:b"}


def test_fixture_provider_skips_rows_without_organizer(tmp_path):
    entry = fixture_entry("a", "2025-11-13")
    entry["organizer_name"] = ""
    provider = fixture_provider(tmp_path, [entry, fixture_entry("b", "2025-11-14")])
    assert [r.source_uid for r in provider.fetch(since=SINCE, until=UNTIL)] == ["fixture:b"]


def test_missing_fixture_is_not_a_crash(tmp_path):
    """Offline включают, чтобы прогнать конвейер без внешних источников.
    Отсутствие фикстуры — не авария."""
    provider = FixtureProvider(path=tmp_path / "нет-такого.json")
    assert provider.fetch(since=SINCE, until=UNTIL) == []


def test_broken_fixture_is_not_a_crash(tmp_path):
    path = tmp_path / "events.json"
    path.write_text("{сломано", encoding="utf-8")
    assert FixtureProvider(path=path).fetch(since=SINCE, until=UNTIL) == []


@respx.mock
def test_offline_mode_never_touches_network(collector, session):
    facts = list(collector.collect(since=date(2024, 1, 1), until=date(2025, 12, 31)))
    assert respx.calls.call_count == 0
    assert facts and all(f.source_uid.startswith("fixture:") for f in facts)
    assert all(f.fact_type == FactType.PAST_EVENT.value for f in facts)


# --------------------------------------------------- поставляемая фикстура


def shipped_events() -> list[dict]:
    path = REPO_ROOT / "data" / "fixtures" / "events" / "events.json"
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def test_shipped_fixture_has_repeat_organizers():
    """Ради этого фикстура и нужна: на повторяющемся из года в год
    мероприятии проверяется рост уверенности сигнала годовщины."""
    years: dict[str, set[int]] = {}
    for entry in shipped_events():
        years.setdefault(entry["organizer_name"], set()).add(int(entry["starts_at"][:4]))
    repeated = [name for name, seen in years.items() if len(seen) > 1]
    assert len(repeated) >= 2


def test_shipped_fixture_covers_both_sides_of_the_threshold(collector):
    attendees = [e["attendees"] for e in shipped_events()]
    threshold = collector.min_attendees
    assert any(a is not None and a >= threshold for a in attendees)
    assert any(a is not None and a < threshold for a in attendees)
    # Мероприятие без известного числа участников тоже должно быть.
    assert any(a is None for a in attendees)


# ----------------------------------------------------------------- backfill


def test_backfill_windows_are_adjacent_without_overlap():
    windows = backfill_windows(date(2026, 7, 29), 2)
    assert windows == [
        (date(2024, 7, 29), date(2025, 7, 28)),
        (date(2025, 7, 29), date(2026, 7, 29)),
    ]


def test_backfill_windows_cover_every_year_once():
    windows = backfill_windows(date(2026, 7, 29), 3)
    assert len(windows) == 3
    assert windows[0][0] == date(2023, 7, 29)
    assert windows[-1][1] == date(2026, 7, 29)
    # Стык интервалов ровно в один день: ни дыр, ни нахлёста.
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        assert (start - end).days == 1


def test_backfill_of_zero_years_is_an_error():
    with pytest.raises(ValueError, match="нечего собирать"):
        backfill_windows(date(2026, 7, 29), 0)


def test_backfill_collects_only_its_depth(collector, session):
    """Два года назад от 29 июля 2026 — апрельское мероприятие 2024 года
    в окно уже не попадает."""
    with freeze_time("2026-07-29"):
        result = collector.backfill(years=2)

    assert result.details["windows"] == 2
    titles = {f.payload["title"] for f in session.scalars(select(Fact))}
    assert "Атлант Тех Саммит 2024" in titles
    assert "Медфарм Форум 2024" not in titles
    # Мелочь отсеяна ещё до записи в базу.
    assert "Финтех Митап" not in titles
    assert result.details["too_small"] == 4
    assert result.count_in == result.count_out == 14


def test_backfill_depth_defaults_to_config(collector, config, session):
    with freeze_time("2026-07-29"):
        result = collector.backfill()

    assert result.details["windows"] == config.sources.source("events_archive").backfill_years
    titles = {f.payload["title"] for f in session.scalars(select(Fact))}
    assert "Медфарм Форум 2024" in titles


def test_backfill_is_idempotent(collector, session):
    with freeze_time("2026-07-29"):
        first = collector.backfill(years=2)
        second = collector.backfill(years=2)

    assert first.count_out == 14
    assert second.count_out == 0
    assert second.details["duplicates"] == 14
    assert session.scalar(select(func.count()).select_from(Fact)) == 14


# -------------------------------------------------------------- run на базе


@respx.mock
def test_run_writes_facts_and_is_idempotent(collector, session, live):
    respx.get(EVENTS).mock(
        return_value=httpx.Response(200, json=page([timepad_event(1), timepad_event(2)]))
    )

    first = collector.run(since=SINCE, until=UNTIL)
    assert (first.count_in, first.count_out) == (2, 2)

    second = collector.run(since=SINCE, until=UNTIL)
    assert (second.count_in, second.count_out) == (2, 0)
    assert second.details["duplicates"] == 2
    assert session.scalar(select(func.count()).select_from(Fact)) == 2


@respx.mock
def test_run_leaves_organizers_for_resolve(collector, session, live):
    """ИНН архив не отдаёт — все факты уходят на сведение по названию.
    Это ожидаемо и должно быть видно в сводке, а не выглядеть потерей."""
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=page([timepad_event(1)])))
    result = collector.run(since=SINCE, until=UNTIL)

    assert result.details["unresolved"] == 1
    fact = session.scalars(select(Fact)).one()
    assert fact.inn is None
    assert fact.company_name_raw == ORGANIZER
    assert fact.occurred_at == date(2025, 11, 13)


@respx.mock
def test_requests_are_journalled(collector, session, live):
    """Запросы к Timepad бесплатны, но в журнал попадают: по нему видно,
    что источник ходил, и он же ловит зациклившийся сбор."""
    route_pages({0: page(many(100), total=150), 100: page(many(50, start=101), total=150)})
    collector.run(since=SINCE, until=UNTIL)

    spend = session.scalars(select(ApiSpend)).all()
    assert len(spend) == 2
    assert {row.provider for row in spend} == {"timepad"}
    assert all(row.cost_rub == 0.0 for row in spend)


@respx.mock
def test_run_is_skipped_when_source_disabled(collector, config):
    events_source(config, enabled=False)
    result = collector.run()
    assert result.details == {"skipped_disabled": 1}
    assert respx.calls.call_count == 0
