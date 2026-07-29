"""Тесты коллектора госзакупок.

Три вещи, из-за которых источник ломается в бою.

1. Число участников. Оно живёт в тексте предмета закупки, а рядом лежат
   рубли, квадратные метры и даты — вытащить не то легко, и цена ошибки
   реальная: крупный форум уедет в «мелкие» и не дойдёт до менеджера.
2. Разбор ответа посредника: у него одно и то же поле называется
   по-разному от метода к методу, а одна битая запись не должна уносить
   выдачу, за которую уже заплачено.
3. Деньги. Источник платный и по умолчанию выключен — выключенный
   коллектор обязан не делать ни одного запроса.

В сеть не ходим: offline на фикстурах, live — через respx.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx
from freezegun import freeze_time
from pydantic import ValidationError
from sqlalchemy import func, select

from gtm.collectors import zakupki
from gtm.collectors.base import CollectorError
from gtm.collectors.zakupki import (
    _MAX_PAGES,
    DamiaZakupkiClient,
    FixtureZakupkiClient,
    NoticeRecord,
    ZakupkiCollector,
    get_zakupki_client,
    normalize_law,
    parse_attendees,
    parse_damia_notice,
    parse_notice_date,
    parse_search_response,
    to_raw_fact,
)
from gtm.settings import REPO_ROOT, Settings
from gtm.spend import BudgetExceeded
from gtm.storage import repo
from gtm.storage.models import ApiSpend, Company, Fact, FactType

SEARCH = "https://damia.ru/api-zakupki/zsearch"

# Окно, покрывающее всю поставляемую фикстуру.
SINCE = date(2026, 1, 1)
UNTIL = date(2026, 7, 29)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Ретраи не должны тормозить тесты по-настоящему."""
    delays: list[float] = []
    monkeypatch.setattr(zakupki, "_pause", delays.append)
    return delays


def zakupki_source(config, **fields) -> dict:
    """Правка настроек источника: source() каждый раз собирает SourceConfig
    заново, поэтому менять надо исходный словарь."""
    raw = config.sources.model_extra["zakupki"]
    raw.update(fields)
    return raw


@pytest.fixture()
def offline(config):
    """Источник по умолчанию выключен; сбор проверяем на фикстурах."""
    zakupki_source(config, mode="offline", enabled=True)
    return config


@pytest.fixture()
def live(config):
    zakupki_source(config, mode="live", enabled=True)
    return config


@pytest.fixture()
def collector(session, config):
    return ZakupkiCollector(session, config, run_id="test", settings=Settings())


@pytest.fixture()
def live_collector(session, live):
    return ZakupkiCollector(
        session, live, run_id="test", settings=Settings(zakupki_token="secret-key")
    )


@pytest.fixture()
def damia():
    return DamiaZakupkiClient("secret-key", backoff=1.0)


# ------------------------------------------------------- число участников


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Формулировки, которые реально встречаются в предмете закупки.
        ("Оказание услуг по проведению форума на 800 участников", 800),
        ("Конференция на 1 200 человек", 1200),
        ("Требуется зал на 1000 посадочных мест", 1000),
        ("Мероприятие на 650 делегатов, двухдневная программа", 650),
        ("Ожидается не менее 400 человек", 400),
        ("Приём до 300 гостей", 300),
        ("Съезд на 750 слушателей", 750),
        ("Аренда зала на 500 чел.", 500),
        # Диапазон: площадка должна выдержать верхнюю границу.
        ("Форум на 250-300 участников", 300),
        ("Конгресс на 1,5 тыс. участников", 1500),
        # Несколько чисел: зал должен вместить всех, кого перечислили.
        ("Зал на 1000 мест и три аудитории по 120 мест", 1000),
        # Ловушки: числа рядом, но они не про людей.
        ("Аренда помещения площадью 800 кв. м", None),
        ("Начальная (максимальная) цена контракта 800 000 рублей", None),
        ("Цена 1 850 000,00 руб. за весь срок", None),
        ("Закупка проводится в соответствии с 44-ФЗ", None),
        ("Срок аренды 3 дня", None),
        ("Слёт продлится 12 ноября, для гостей программы", None),
        ("Мероприятие к 30-летию предприятия", None),
        # Смешанный случай: метры отбрасываем, людей берём.
        ("Экспозиция 2 300 кв. м, ожидается до 1 200 человек", 1200),
        # Ничего похожего на аудиторию.
        ("Аренда конференц-зала на календарный год", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_attendees(text, expected):
    assert parse_attendees(text) == expected


def test_parse_attendees_ignores_implausible_numbers():
    """Меньше двадцати — это не аудитория конференции, а число дней или
    столов; больше ста тысяч — опечатка. И то и другое честнее отдать
    как «не знаем», чем выкинуть закупку по выдуманному масштабу."""
    assert parse_attendees("Переговорная на 8 человек") is None
    assert parse_attendees("Стенды на 999 999 посетителей") is None


def test_parse_attendees_survives_nbsp():
    """В выгрузках ЕИС разряды разделены неразрывным пробелом."""
    assert parse_attendees("Форум на 1 200 участников") == 1200


# ------------------------------------------------------------ разбор полей


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("44-ФЗ", "44"),
        ("Федеральный закон № 223-ФЗ", "223"),
        (223, "223"),
        ({"Наим": "44-ФЗ"}, "44"),
        # 615-ПП (капремонт) и отменённый 94-ФЗ — другая процедура.
        ("615-ПП", None),
        ("94-ФЗ", None),
        (None, None),
    ],
)
def test_normalize_law(raw, expected):
    assert normalize_law(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-10", date(2026, 7, 10)),
        ("2026-07-15T09:12:00+03:00", date(2026, 7, 15)),
        ("18.11.2026", date(2026, 11, 18)),
        ("когда-нибудь осенью", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_notice_date(raw, expected):
    assert parse_notice_date(raw) == expected


def test_law_outside_44_and_223_is_rejected_by_the_record():
    """Закон определяет процедуру. Чужую объяснять клиенту нечем."""
    with pytest.raises(ValidationError):
        NoticeRecord(
            notice_id="1",
            law="94",
            customer_name="ГБУ «Тест»",
            title="Аренда зала",
            published_at=date(2026, 7, 10),
        )


def test_broken_inn_reaches_resolve_instead_of_disappearing():
    """Битый ИНН не повод выбрасывать извещение: его дело — доехать до
    resolve и лечь в карантин с причиной."""
    record = NoticeRecord(
        notice_id="1",
        law="44",
        customer_name="ГБУ «Тест»",
        customer_inn="ИНН 7701123450",
        title="Аренда зала",
        published_at=date(2026, 7, 10),
    )
    assert record.customer_inn == "7701123450"


# ------------------------------------------------------------ разбор ответа


def damia_response() -> dict:
    """Ответ поиска у посредника: словарь {реестровый номер: извещение}.
    Поля называются по-разному от метода к методу — обе формы здесь."""
    return {
        "0173200014126000501": {
            "Закон": "44-ФЗ",
            "Наим": "Аренда конференц-зала для проведения окружного форума",
            "Описание": "Форум на 900 участников. Начальная цена 4 850 000 рублей.",
            "ДатаПубл": "2026-07-10",
            "НачЦена": "4 850 000,00",
            "Регион": "77",
            "Заказчик": {"НаимСокр": "ГБУ «ЦРП Заречного округа»", "ИНН": "7701123451"},
            "ДатаНачИсп": "18.11.2026",
        },
        "32615012301": {
            "РегНомер": "32615012301",
            "ФЗ": 223,
            "ЗакНаим": "Организация ежегодной отраслевой конференции",
            "ДатаРазм": "2026-07-15T09:12:00+03:00",
            "НМЦК": 12400000,
            "Заказчик": "АО «Приволье-Энергосбыт»",
            "ИННЗаказчика": "7712345671",
            "КодРегион": "77",
            "Ссылка": "https://zakupki.gov.ru/223/purchase/info.html?regNumber=32615012301",
        },
    }


def test_search_response_becomes_records():
    records = {r.notice_id: r for r in parse_search_response(damia_response())}
    assert set(records) == {"0173200014126000501", "32615012301"}

    first = records["0173200014126000501"]
    assert first.law == "44"
    assert first.customer_name == "ГБУ «ЦРП Заречного округа»"
    # Ради этого источник и нужен: ИНН заказчика есть, сведение тривиально.
    assert first.customer_inn == "7701123451"
    assert first.published_at == date(2026, 7, 10)
    assert first.event_date == date(2026, 11, 18)
    assert first.price == 4_850_000.0
    # Число участников — из текста; рубли рядом не должны его подменить.
    assert first.attendees == 900
    assert first.region == "77"
    # Ссылки в ответе не было — собрана поисковая, чтобы в карточке
    # у менеджера не оказалось битого адреса.
    assert first.notice_id in first.url

    second = records["32615012301"]
    assert second.law == "223"
    assert second.customer_name == "АО «Приволье-Энергосбыт»"
    assert second.customer_inn == "7712345671"
    assert second.published_at == date(2026, 7, 15)
    assert second.event_date is None
    assert second.price == 12_400_000.0
    assert second.attendees is None
    assert second.url.endswith("regNumber=32615012301")


def test_notice_id_is_taken_from_the_response_key():
    """Реестровый номер лежит в ключе, а внутри записи его может не быть."""
    data = {"0173200014126000501": damia_response()["0173200014126000501"]}
    assert parse_search_response(data)[0].notice_id == "0173200014126000501"


def test_broken_record_does_not_take_the_whole_payload_down():
    """За выдачу уже заплачено: одна запись без заказчика не повод терять
    остальные."""
    data = damia_response()
    data["0173200014126000777"] = {"Закон": "44-ФЗ", "Наим": "Аренда зала"}
    data["0173200014126000778"] = {
        "Закон": "615-ПП",
        "Наим": "Капитальный ремонт",
        "ДатаПубл": "2026-07-11",
        "Заказчик": {"Наим": "ФКР", "ИНН": "7725001188"},
    }
    assert len(parse_search_response(data)) == 2


@pytest.mark.parametrize("data", [None, "", 42, {"x": "не словарь"}])
def test_unexpected_payload_shapes_are_empty_not_a_crash(data):
    assert parse_search_response(data) == []


def test_list_shaped_response_is_parsed_too():
    """Часть методов заворачивает ту же выдачу в список. Реестрового номера
    в ключе тогда нет — извещение без номера внутри записи опознать нечем."""
    records = parse_search_response(list(damia_response().values()))
    assert {r.notice_id for r in records} == {"32615012301"}


def test_record_becomes_fact_with_full_payload():
    record = parse_damia_notice(damia_response()["0173200014126000501"], notice_id="0173")
    fact = to_raw_fact(record)

    assert fact.source_uid == "zakupki:0173"
    assert fact.fact_type == FactType.TENDER.value
    assert fact.inn == "7701123451"
    # occurred_at — дата публикации: она датирует сам факт.
    assert fact.occurred_at == date(2026, 7, 10)
    assert set(fact.payload) == {
        "notice_id",
        "law",
        "title",
        "customer_name",
        "published_at",
        "event_date",
        "price",
        "attendees",
        "region",
        "url",
    }
    assert fact.payload["event_date"] == "2026-11-18"
    assert fact.payload["law"] == "44"


# --------------------------------------------------------- выбор клиента


def test_disabled_source_gets_fixture_client(config):
    assert isinstance(get_zakupki_client(config), FixtureZakupkiClient)


def test_live_without_key_falls_back_to_fixtures(live):
    """Без ключа ходить некуда, но конвейер обязан подниматься целиком."""
    client = get_zakupki_client(live, Settings(zakupki_token=None, damia_key=None))
    assert isinstance(client, FixtureZakupkiClient)


def test_live_with_key_gets_the_paid_client(live):
    client = get_zakupki_client(live, Settings(zakupki_token="secret-key"))
    assert isinstance(client, DamiaZakupkiClient)


def test_egrul_key_is_the_fallback_for_the_zakupki_key(live):
    """Ключи api-zakupki и api-egrul у damia разные, но выдают их вместе."""
    client = get_zakupki_client(live, Settings(damia_key="egrul-key"))
    assert isinstance(client, DamiaZakupkiClient)
    assert client.api_key == "egrul-key"


def test_unknown_provider_is_an_error(live):
    """Опечатка в конфиге не должна молча подсунуть выдуманные закупки."""
    zakupki_source(live, provider="сбербанк")
    with pytest.raises(CollectorError, match="provider"):
        get_zakupki_client(live, Settings(zakupki_token="secret-key"))


# ------------------------------------------------------- фикстурный клиент


def fixture_client(tmp_path, notices: list[dict]) -> FixtureZakupkiClient:
    path = tmp_path / "notices.json"
    path.write_text(json.dumps({"notices": notices}, ensure_ascii=False), encoding="utf-8")
    return FixtureZakupkiClient(path)


def fixture_notice(
    uid: str, published_at: str = "2026-07-10", *, region: str = "77", law: str = "44"
) -> dict:
    return {
        "notice_id": uid,
        "law": law,
        "customer_name": f"ГБУ «Тест {uid}»",
        "customer_inn": "7701123451",
        "title": "Аренда конференц-зала для проведения форума",
        "description": "Форум на 600 участников.",
        "published_at": published_at,
        "region": region,
    }


def test_fixture_client_returns_only_the_asked_window(tmp_path):
    client = fixture_client(
        tmp_path,
        [
            fixture_notice("a", "2026-05-31"),
            fixture_notice("b", "2026-06-01"),
            fixture_notice("c", "2026-06-30"),
            fixture_notice("d", "2026-07-01"),
        ],
    )
    records = client.search(
        keywords=["аренда"], region="77", since=date(2026, 6, 1), until=date(2026, 6, 30)
    )
    # Границы окна включительно.
    assert {r.notice_id for r in records} == {"b", "c"}


def test_fixture_client_filters_by_region(tmp_path):
    client = fixture_client(
        tmp_path, [fixture_notice("a"), fixture_notice("b", region="50")]
    )
    records = client.search(keywords=[], region="77", since=SINCE, until=UNTIL)
    assert [r.notice_id for r in records] == ["a"]


def test_missing_fixture_is_not_a_crash(tmp_path):
    """Offline включают, чтобы прогнать конвейер без внешних источников."""
    client = FixtureZakupkiClient(tmp_path / "нет-такого.json")
    assert client.search(keywords=[], region="77", since=SINCE, until=UNTIL) == []


def test_broken_fixture_is_not_a_crash(tmp_path):
    path = tmp_path / "notices.json"
    path.write_text("{сломано", encoding="utf-8")
    client = FixtureZakupkiClient(path)
    assert client.search(keywords=[], region="77", since=SINCE, until=UNTIL) == []


# ----------------------------------------------- поставляемая фикстура


def shipped_notices() -> list[dict]:
    path = REPO_ROOT / "data" / "fixtures" / "zakupki" / "notices.json"
    return json.loads(path.read_text(encoding="utf-8"))["notices"]


def test_shipped_fixture_covers_both_laws_and_both_sides_of_attendees():
    notices = shipped_notices()
    assert len(notices) >= 8
    assert {n["law"] for n in notices} == {"44", "223"}

    parsed = [parse_attendees(f"{n['title']}\n{n['description']}") for n in notices]
    # Часть извещений с числом участников в тексте, часть — без.
    assert any(a is not None for a in parsed)
    assert any(a is None for a in parsed)
    # И ловушки: рубли и метры не должны становиться участниками.
    assert any("кв. м" in n["description"] for n in notices)
    assert any("рублей" in n["description"] for n in notices)


# --------------------------------------------------------------- offline


@respx.mock
def test_offline_collect_never_touches_network(collector, offline):
    facts = list(collector.collect(since=SINCE, until=UNTIL))

    assert respx.calls.call_count == 0
    assert facts
    assert all(f.fact_type == FactType.TENDER.value for f in facts)
    # Главное отличие источника: ИНН заказчика есть у каждого извещения.
    assert all(f.inn for f in facts)


def test_small_notices_are_cut_before_anything_costly(collector, offline):
    """«Совещание на 120 человек» — не наш зал, и отсекается до записи
    в базу и до любого обогащения."""
    facts = list(collector.collect(since=SINCE, until=UNTIL))
    titles = {f.payload["title"] for f in facts}

    assert collector.skipped_small == 1
    assert not any("стратегической сессии" in t for t in titles)
    assert all(
        f.payload["attendees"] is None
        or f.payload["attendees"] >= collector.config.icp.scale.min_attendees
        for f in facts
    )


def test_notices_from_other_regions_do_not_come_along(collector, offline):
    """Регион задан конфигом: областная закупка в московскую выдачу
    попадать не должна."""
    facts = list(collector.collect(since=SINCE, until=UNTIL))
    assert all(f.payload["region"] == "77" for f in facts)


def test_law_filter_comes_from_config(collector, offline):
    zakupki_source(offline, laws=["44"])
    facts = list(collector.collect(since=SINCE, until=UNTIL))

    assert {f.payload["law"] for f in facts} == {"44"}
    assert collector.skipped_other_law == 3


def test_default_window_is_a_month_back(collector, offline):
    """Окно контакта по закупке — 30 дней: извещение постарше обсуждать
    уже не с кем, и тянуть его из источника незачем."""
    with freeze_time("2026-07-29"):
        facts = list(collector.collect())

    published = {f.payload["published_at"] for f in facts}
    assert published and min(published) >= "2026-06-29"
    # Извещение от 28 июня — уже за границей окна.
    assert "2026-06-28" not in published


def test_inverted_window_is_an_error(collector, offline):
    with pytest.raises(CollectorError, match="вывернуто"):
        collector.collect(since=UNTIL, until=SINCE)


def test_collect_without_keywords_is_an_error(collector, offline):
    zakupki_source(offline, keywords=[])
    with pytest.raises(CollectorError, match="ключевого слова"):
        collector.collect(since=SINCE, until=UNTIL)


# ------------------------------------------------------------ run на базе


def test_run_writes_facts_resolves_inn_and_is_idempotent(collector, session, offline):
    first = collector.run(since=SINCE, until=UNTIL)
    assert first.count_in == first.count_out == 8
    # Единственный источник, где сведение к компании ничего не стоит:
    # карантина быть не должно.
    assert "unresolved" not in first.details
    assert first.details["too_small"] == 1

    facts = session.scalars(select(Fact)).all()
    assert all(f.inn for f in facts)
    assert session.scalar(select(func.count()).select_from(Company)) == 8

    second = collector.run(since=SINCE, until=UNTIL)
    assert second.count_out == 0
    assert second.details["duplicates"] == 8
    assert session.scalar(select(func.count()).select_from(Fact)) == 8


@respx.mock
def test_disabled_source_makes_no_requests(collector, session, config):
    """Источник выключен по умолчанию: ни запроса, ни рубля, ни строки."""
    result = collector.run(since=SINCE, until=UNTIL)

    assert result.details == {"skipped_disabled": 1}
    assert respx.calls.call_count == 0
    assert session.scalar(select(func.count()).select_from(ApiSpend)) == 0
    assert session.scalar(select(func.count()).select_from(Fact)) == 0


def test_offline_run_costs_nothing(collector, session, offline):
    collector.run(since=SINCE, until=UNTIL)
    assert session.scalar(select(func.count()).select_from(ApiSpend)) == 0


# ---------------------------------------------------------- платный клиент


@respx.mock
def test_search_asks_for_keyword_region_and_window(damia):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={}))
    damia.search(
        keywords=["аренда конференц-зала"],
        region="77",
        since=date(2026, 7, 1),
        until=date(2026, 7, 29),
    )

    params = route.calls[0].request.url.params
    assert params["q"] == "аренда конференц-зала"
    assert params["key"] == "secret-key"
    assert params["region"] == "77"
    assert params["dfrom"] == "2026-07-01"
    assert params["dto"] == "2026-07-29"


@respx.mock
def test_search_merges_keywords_without_duplicating_notices(damia):
    """Одно извещение находится сразу по нескольким словам — это одно
    извещение."""
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json=damia_response()))
    records = damia.search(
        keywords=["аренда конференц-зала", "организация форума"],
        region="77",
        since=SINCE,
        until=UNTIL,
    )
    assert len(records) == 2


@respx.mock
def test_pagination_stops_when_no_new_notices_appear(damia):
    """Если параметр страницы посредник не понимает, он молча отдаёт первую
    страницу снова. У платного источника такой обход крутится за наши
    деньги — останавливаемся на первом же повторе."""
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=damia_response()))
    damia.search(keywords=["аренда"], region="77", since=SINCE, until=UNTIL)

    assert route.call_count == 2
    assert route.call_count < _MAX_PAGES


@respx.mock
def test_transient_500_is_retried(damia, no_sleep):
    ok = httpx.Response(200, json=damia_response())
    route = respx.get(SEARCH).mock(side_effect=[httpx.Response(500), ok, ok])
    assert len(damia.search(keywords=["аренда"], region="77", since=SINCE, until=UNTIL)) == 2
    # Две попытки на первую страницу и одна на вторую, где новых номеров нет.
    assert route.call_count == 3
    assert no_sleep == [1.0]


@respx.mock
def test_403_is_not_retried(damia):
    """Неверный ключ повтором не лечится, а каждый повтор стоит денег."""
    route = respx.get(SEARCH).mock(return_value=httpx.Response(403, text="bad key"))
    with pytest.raises(CollectorError, match="403"):
        damia.search(keywords=["аренда"], region="77", since=SINCE, until=UNTIL)
    assert route.call_count == 1


@respx.mock
def test_non_json_answer_is_reported(damia):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text="<html>тех.работы</html>"))
    with pytest.raises(CollectorError, match="не JSON"):
        damia.search(keywords=["аренда"], region="77", since=SINCE, until=UNTIL)


def test_client_without_key_fails_at_construction():
    """Упасть надо здесь, а не первым платным вызовом посреди стадии."""
    with pytest.raises(CollectorError, match="ключ"):
        DamiaZakupkiClient("")


@respx.mock
def test_paid_calls_are_journalled(live_collector, session, live):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json=damia_response()))
    live_collector.run(since=SINCE, until=UNTIL)

    spend = session.scalars(select(ApiSpend)).all()
    cost = live.sources.source("zakupki").cost_rub_per_call
    assert spend
    assert {row.provider for row in spend} == {"damia"}
    assert all(row.cost_rub == cost for row in spend)
    assert sum(row.cost_rub for row in spend) == cost * len(spend)


@respx.mock
def test_budget_exceeded_stops_before_any_request(live_collector, session):
    """Дневной лимит исчерпан — стадия останавливается, а не дожигает
    остаток бюджета."""
    repo.log_api_spend(
        session, provider="dadata", endpoint="/findById/party", stage="enrich", cost_rub=499.5
    )
    session.commit()

    with pytest.raises(BudgetExceeded):
        live_collector.collect(since=SINCE, until=UNTIL)
    assert respx.calls.call_count == 0

    result = live_collector.run(since=SINCE, until=UNTIL)
    assert result.details == {"budget_exceeded": 1}
    assert session.scalar(select(func.count()).select_from(Fact)) == 0
