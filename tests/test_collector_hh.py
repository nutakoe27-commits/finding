"""Тесты коллектора вакансий hh.ru.

Проверяем то, из-за чего сбор ломается в бою: пагинацию (иначе видно только
первую сотню), заголовок User-Agent (без него hh отвечает 403), отсев
кадровых агентств и поведение при пятисотке. В сеть не ходим — respx.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import func, select

from gtm.collectors import hh
from gtm.collectors.base import CollectorError
from gtm.collectors.hh import HHCollector, matched_keywords, parse_published_at, to_raw_fact
from gtm.settings import Settings
from gtm.storage.models import ApiSpend, Fact, FactType

BASE_URL = "https://api.hh.ru"
VACANCIES = f"{BASE_URL}/vacancies"
USER_AGENT = "gtm-test/9.9 (contact: qa@example.com)"

EVENT_MANAGER = "event-менеджер"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Ретраи и пауза между страницами не должны тормозить тесты."""
    delays: list[float] = []
    monkeypatch.setattr(hh, "_pause", delays.append)
    return delays


@pytest.fixture()
def collector(session, config):
    return HHCollector(session, config, run_id="test", settings=Settings(hh_user_agent=USER_AGENT))


def vacancy(
    vid: str = "123",
    *,
    name: str = "Event-менеджер",
    employer_name: str | None = "ООО Ромашка-Трейд",
    employer_id: str | None = "1001",
    employer_type: str | None = "company",
    published_at: str = "2026-07-20T12:00:00+0300",
) -> dict:
    employer: dict = {}
    if employer_name is not None:
        employer["name"] = employer_name
    if employer_id is not None:
        employer["id"] = employer_id
    if employer_type is not None:
        employer["type"] = employer_type
    return {
        "id": vid,
        "name": name,
        "employer": employer,
        "published_at": published_at,
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "url": f"{BASE_URL}/vacancies/{vid}",
        "area": {"id": "1", "name": "Москва"},
    }


def page(items: list[dict], *, pages: int = 1, number: int = 0) -> dict:
    return {"items": items, "found": len(items), "pages": pages, "page": number, "per_page": 100}


def hh_source(config, **fields) -> dict:
    """Правка настроек источника: source() каждый раз собирает SourceConfig
    заново, поэтому менять надо исходный словарь."""
    raw = config.sources.model_extra["hh"]
    raw.update(fields)
    return raw


@pytest.fixture(autouse=True)
def source_enabled(config):
    """В config/sources.yaml источник выключен — публичный API hh закрыт.

    Тесты проверяют механику коллектора, а не решение о выключении, поэтому
    включают источник сами. Само выключение проверяется отдельным тестом.
    """
    hh_source(config, mode="live", enabled=True)


# ------------------------------------------------------------------ разбор


def test_vacancy_becomes_raw_fact():
    fact = to_raw_fact(vacancy(), [EVENT_MANAGER])
    assert fact is not None
    assert fact.source_uid == "hh:123"
    assert fact.fact_type == FactType.VACANCY.value
    # ИНН hh не отдаёт: сводить к компании будет resolve.
    assert fact.inn is None
    assert fact.company_name == "ООО Ромашка-Трейд"
    assert fact.occurred_at == date(2026, 7, 20)
    assert fact.payload == {
        "vacancy_id": "123",
        "title": "Event-менеджер",
        "employer_name": "ООО Ромашка-Трейд",
        "employer_id": "1001",
        "published_at": "2026-07-20T12:00:00+0300",
        "url": "https://hh.ru/vacancy/123",
        "area": "Москва",
        "keywords": [EVENT_MANAGER],
    }


def test_vacancy_without_employer_name_is_dropped():
    """Без работодателя факт бесполезен: сводить не к чему, письмо некому."""
    assert to_raw_fact(vacancy(employer_name=None), [EVENT_MANAGER]) is None


def test_vacancy_without_id_is_dropped():
    item = vacancy()
    item["id"] = ""
    assert to_raw_fact(item, [EVENT_MANAGER]) is None


def test_query_keyword_is_kept_when_literal_match_fails():
    """hh ищет с учётом морфологии: «Менеджеру по мероприятиям» он найдёт,
    а наше буквальное сравнение — нет. Сигнал при этом есть."""
    fact = to_raw_fact(vacancy(name="Менеджеру по мероприятиям"), [EVENT_MANAGER], EVENT_MANAGER)
    assert fact is not None and fact.payload["keywords"] == [EVENT_MANAGER]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Event-менеджер", [EVENT_MANAGER]),
        ("EVENT МЕНЕДЖЕР в крупную компанию", [EVENT_MANAGER]),
        ("Ивент-менеджер / event manager", ["ивент-менеджер", "event manager"]),
        ("Курьер", []),
    ],
)
def test_matched_keywords(title, expected):
    keywords = [EVENT_MANAGER, "ивент-менеджер", "event manager"]
    assert matched_keywords(title, keywords) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-20T12:00:00+0300", date(2026, 7, 20)),
        ("2026-07-20", date(2026, 7, 20)),
        ("вчера", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_published_at(raw, expected):
    assert parse_published_at(raw) == expected


# --------------------------------------------------------------- запросы


@respx.mock
def test_request_carries_user_agent_and_config_params(collector, config):
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(200, json=page([vacancy()])))
    collector.collect(keywords=[EVENT_MANAGER])

    request = route.calls[0].request
    # Без внятного User-Agent hh отвечает 403 — это его требование.
    assert request.headers["user-agent"] == USER_AGENT
    params = request.url.params
    assert params["text"] == EVENT_MANAGER
    # Слово из описания вакансии сигналом не является — ищем по названию.
    assert params["search_field"] == "name"
    assert params["area"] == str(config.sources.source("hh").area)
    assert params["period"] == str(config.sources.source("hh").search_period_days)


@respx.mock
def test_keywords_default_to_signal_config(collector, config):
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(200, json=page([])))
    collector.collect()

    asked = [call.request.url.params["text"] for call in route.calls]
    assert asked == config.signals.kind("vacancy_signal").keywords


@respx.mock
def test_period_days_is_clamped_to_api_limit(collector):
    """У hh параметр period не может быть больше 30 — иначе 400 на ровном месте."""
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(200, json=page([])))
    collector.collect(keywords=[EVENT_MANAGER], period_days=90)
    assert route.calls[0].request.url.params["period"] == "30"


@respx.mock
def test_pagination_walks_all_pages(collector, no_sleep):
    route = respx.get(VACANCIES).mock(
        side_effect=[
            httpx.Response(200, json=page([vacancy("1")], pages=3, number=0)),
            httpx.Response(200, json=page([vacancy("2")], pages=3, number=1)),
            httpx.Response(200, json=page([vacancy("3")], pages=3, number=2)),
        ]
    )
    facts = list(collector.collect(keywords=[EVENT_MANAGER]))

    assert [call.request.url.params["page"] for call in route.calls] == ["0", "1", "2"]
    assert {f.source_uid for f in facts} == {"hh:1", "hh:2", "hh:3"}
    # Между страницами выдерживаем rate_limit_rps.
    assert no_sleep == [pytest.approx(1 / 3), pytest.approx(1 / 3)]


@respx.mock
def test_pagination_stops_at_max_pages(collector, config):
    """hh обещает 50 страниц — берём столько, сколько разрешено конфигом."""
    hh_source(config, max_pages=2)
    route = respx.get(VACANCIES).mock(
        side_effect=[
            httpx.Response(200, json=page([vacancy("1")], pages=50, number=0)),
            httpx.Response(200, json=page([vacancy("2")], pages=50, number=1)),
        ]
    )
    facts = list(collector.collect(keywords=[EVENT_MANAGER]))
    assert route.call_count == 2
    assert len(facts) == 2


@respx.mock
def test_pagination_stops_on_empty_page(collector):
    """Пустая страница раньше обещанного числа — дальше ходить незачем."""
    route = respx.get(VACANCIES).mock(
        side_effect=[
            httpx.Response(200, json=page([vacancy("1")], pages=5, number=0)),
            httpx.Response(200, json=page([], pages=5, number=1)),
        ]
    )
    list(collector.collect(keywords=[EVENT_MANAGER]))
    assert route.call_count == 2


# ------------------------------------------------------------------ отсев


@respx.mock
def test_agencies_are_skipped(collector):
    """Кадровое агентство ищет не себе — мероприятие будет у его клиента."""
    respx.get(VACANCIES).mock(
        return_value=httpx.Response(
            200,
            json=page(
                [
                    vacancy("1", employer_name="ООО Ромашка-Трейд"),
                    vacancy("2", employer_name="Кадры Плюс", employer_type="agency"),
                ]
            ),
        )
    )
    facts = list(collector.collect(keywords=[EVENT_MANAGER]))
    assert [f.company_name for f in facts] == ["ООО Ромашка-Трейд"]
    assert collector.skipped_agencies == 1


@respx.mock
def test_employer_without_type_field_survives(collector):
    """Поля type у работодателя может не быть — это не повод отсекать."""
    respx.get(VACANCIES).mock(
        return_value=httpx.Response(200, json=page([vacancy("1", employer_type=None)]))
    )
    assert len(list(collector.collect(keywords=[EVENT_MANAGER]))) == 1


@respx.mock
def test_same_vacancy_from_two_keywords_is_one_fact(collector):
    """Одна вакансия находится по нескольким словам. Факт один, слова — все."""
    respx.get(VACANCIES).mock(
        return_value=httpx.Response(
            200, json=page([vacancy("1", name="Event-менеджер / ивент-менеджер")])
        )
    )
    facts = list(collector.collect(keywords=[EVENT_MANAGER, "ивент-менеджер"]))
    assert len(facts) == 1
    assert facts[0].payload["keywords"] == [EVENT_MANAGER, "ивент-менеджер"]


def test_collect_without_keywords_is_an_error(collector, config):
    hh_source(config)
    with pytest.raises(CollectorError, match="ключевого слова"):
        collector.collect(keywords=[" ", ""])


# ------------------------------------------------------------------ ошибки


@respx.mock
def test_transient_500_is_retried(collector, no_sleep, config):
    route = respx.get(VACANCIES).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json=page([vacancy("1")])),
        ]
    )
    facts = list(collector.collect(keywords=[EVENT_MANAGER]))
    assert route.call_count == 2
    assert len(facts) == 1
    assert no_sleep == [config.sources.defaults["retry_backoff_seconds"]]


@respx.mock
def test_permanent_500_fails_with_readable_error(collector, config):
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(500))
    with pytest.raises(CollectorError, match="не ответил"):
        collector.collect(keywords=[EVENT_MANAGER])
    assert route.call_count == config.sources.defaults["retries"]


@respx.mock
def test_network_error_is_retried_then_reported(collector, config):
    route = respx.get(VACANCIES).mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(CollectorError, match="сеть"):
        collector.collect(keywords=[EVENT_MANAGER])
    assert route.call_count == config.sources.defaults["retries"]


@respx.mock
def test_403_points_at_user_agent_and_is_not_retried(collector):
    """403 — это заголовок, а не всплеск нагрузки: ретраить бессмысленно."""
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(403))
    with pytest.raises(CollectorError, match="User-Agent"):
        collector.collect(keywords=[EVENT_MANAGER])
    assert route.call_count == 1


@respx.mock
def test_400_is_not_retried(collector):
    route = respx.get(VACANCIES).mock(return_value=httpx.Response(400, text="bad period"))
    with pytest.raises(CollectorError, match="400"):
        collector.collect(keywords=[EVENT_MANAGER])
    assert route.call_count == 1


@respx.mock
def test_non_json_answer_is_reported(collector):
    respx.get(VACANCIES).mock(return_value=httpx.Response(200, text="<html>тех.работы</html>"))
    with pytest.raises(CollectorError, match="не JSON"):
        collector.collect(keywords=[EVENT_MANAGER])


# ----------------------------------------------------------------- offline


@respx.mock
def test_offline_reads_fixture_without_network(collector, config, tmp_path):
    fixture = tmp_path / "hh.json"
    fixture.write_text(
        json.dumps(page([vacancy("1"), vacancy("2", employer_type="agency")]), ensure_ascii=False),
        encoding="utf-8",
    )
    hh_source(config, mode="offline", fixtures_path=str(fixture))

    facts = list(collector.collect())
    assert [f.source_uid for f in facts] == ["hh:1"]
    assert collector.skipped_agencies == 1
    assert respx.calls.call_count == 0


@respx.mock
def test_offline_without_fixtures_path_is_empty(collector, config):
    """Offline включают, чтобы прогнать конвейер без внешних источников.
    Отсутствие фикстуры — не авария."""
    hh_source(config, mode="offline")
    assert list(collector.collect()) == []
    assert respx.calls.call_count == 0


@respx.mock
def test_offline_with_missing_file_is_empty(collector, config, tmp_path):
    hh_source(config, mode="offline", fixtures_path=str(tmp_path / "нет-такого.json"))
    assert list(collector.collect()) == []


@respx.mock
def test_offline_with_broken_fixture_does_not_crash(collector, config, tmp_path):
    fixture = tmp_path / "hh.json"
    fixture.write_text("{сломано", encoding="utf-8")
    hh_source(config, mode="offline", fixtures_path=str(fixture))
    assert list(collector.collect()) == []


# -------------------------------------------------------------- run на базе


def facts_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Fact))


@respx.mock
def test_run_writes_facts_and_is_idempotent(collector, session):
    """source_uid держится на id вакансии: повторный сбор не удваивает базу."""
    respx.get(VACANCIES).mock(
        return_value=httpx.Response(200, json=page([vacancy("1"), vacancy("2")]))
    )

    first = collector.run(keywords=[EVENT_MANAGER])
    assert (first.count_in, first.count_out) == (2, 2)

    second = collector.run(keywords=[EVENT_MANAGER])
    assert (second.count_in, second.count_out) == (2, 0)
    assert second.details["duplicates"] == 2
    assert facts_count(session) == 2


@respx.mock
def test_run_reports_agencies_and_unresolved(collector, session):
    """ИНН у hh нет — все факты уходят на сведение по имени. Это ожидаемо
    и должно быть видно в сводке, а не выглядеть потерей."""
    respx.get(VACANCIES).mock(
        return_value=httpx.Response(
            200,
            json=page([vacancy("1"), vacancy("2", employer_name="Кадры", employer_type="agency")]),
        )
    )
    result = collector.run(keywords=[EVENT_MANAGER])
    assert result.count_out == 1
    assert result.details["agencies"] == 1
    assert result.details["unresolved"] == 1

    fact = session.scalars(select(Fact)).one()
    assert fact.inn is None
    assert fact.company_name_raw == "ООО Ромашка-Трейд"
    assert fact.fact_type == FactType.VACANCY.value


@respx.mock
def test_run_is_skipped_when_source_disabled(collector, config, session):
    hh_source(config, enabled=False)
    result = collector.run()
    assert result.details == {"skipped_disabled": 1}
    assert respx.calls.call_count == 0


@respx.mock
def test_requests_are_journalled(collector, session):
    """Запросы к hh бесплатны, но в журнал попадают: по нему видно, что
    источник вообще ходил, и он же ловит зациклившийся сбор."""
    respx.get(VACANCIES).mock(return_value=httpx.Response(200, json=page([vacancy("1")])))
    collector.run(keywords=[EVENT_MANAGER])

    spend = session.scalars(select(ApiSpend)).all()
    assert len(spend) == 1
    assert spend[0].provider == "hh"
    assert spend[0].cost_rub == 0.0
