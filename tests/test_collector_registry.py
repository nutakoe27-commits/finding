"""Тесты реестра юрлиц.

Проверяем то, из-за чего реестр ломается в бою: разбор ответа каждого
провайдера (форматы разные до неузнаваемости), подмену на фикстуры без
ключа и — главное — деньги. Кэш и остановка по бюджету здесь не
украшение: без них стадия обогащения тихо сжигает месячный лимит.

Сети в тестах нет ни одного байта: разбор отделён от HTTP именно ради этого.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from gtm.collectors.base import CollectorError
from gtm.collectors.registry import (
    SOURCE_NAME,
    CompanyRecord,
    DadataClient,
    DamiaClient,
    FixtureClient,
    OfdataClient,
    RegistryCollector,
    enrich_company,
    get_registry_client,
    normalize_status,
    parse_dadata_party,
    parse_damia_company,
    parse_headcount,
    parse_ofdata_company,
    parse_registry_date,
    parse_revenue,
)
from gtm.resolve.inn import is_valid_inn
from gtm.settings import REPO_ROOT, Settings
from gtm.spend import BudgetExceeded, SpendGuard
from gtm.storage import repo
from gtm.storage.models import ApiSpend, Company, Fact, FactType

FIXTURES = REPO_ROOT / "data" / "fixtures" / "registry" / "companies.json"

# Компании из штатной фикстуры: круглая годовщина в 2026-м и обычная.
JUBILEE_INN = "7718260181"  # регистрация 2016-03-12 → 10 лет
PLAIN_INN = "7727219937"  # регистрация 2013-06-20 → 13 лет
LIQUIDATED_INN = "7724975438"


# --------------------------------------------------------------- вспомогательное


def registry_config(config, **overrides):
    """Копия конфига с изменённой секцией registry.

    Секция лежит в extra-полях SourcesConfig, поэтому правится словарём.
    """
    copy = config.model_copy(deep=True)
    raw = copy.sources.source(SOURCE_NAME).model_dump()
    raw.update(overrides)
    copy.sources.__pydantic_extra__[SOURCE_NAME] = raw
    return copy


@pytest.fixture(autouse=True)
def source_enabled(config):
    """В config/sources.yaml платный реестр выключен: год регистрации бесплатно
    берётся из ОГРН, а размер — из наборов ФНС.

    Тесты проверяют механику коллектора, а не решение о выключении, поэтому
    включают источник сами. Само выключение проверяется отдельным тестом.
    """
    config.sources.__pydantic_extra__[SOURCE_NAME].update({"mode": "offline", "enabled": True})


class StubClient:
    """Платный клиент без сети: нужен там, где проверяются деньги,
    а не разбор ответа."""

    provider = "stub"
    lookup_endpoint = "/stub"

    def __init__(self, record: CompanyRecord | None) -> None:
        self.record = record
        self.calls: list[str] = []

    def lookup(self, inn: str) -> CompanyRecord | None:
        self.calls.append(inn)
        return self.record

    def suggest(self, name: str) -> list[CompanyRecord]:
        return [self.record] if self.record else []


def fixture_record(inn: str = JUBILEE_INN) -> CompanyRecord:
    return FixtureClient(FIXTURES).lookup(inn)


def spend_rows(session) -> list[ApiSpend]:
    return list(session.scalars(select(ApiSpend)))


# ------------------------------------------------------------- разбор полей


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ACTIVE", "active"),
        ("Действующее", "active"),
        ({"Код": 1, "Наим": "Действующая"}, "active"),
        ("LIQUIDATED", "liquidated"),
        ("Ликвидировано", "liquidated"),
        ("Прекращение деятельности", "liquidated"),
        ("В стадии ликвидации", "liquidating"),
        ("REORGANIZING", "reorganizing"),
        ("Банкротство", "bankrupt"),
        (None, "unknown"),
        ("", "unknown"),
        ("нечто невиданное", "unknown"),
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


def test_unknown_status_is_not_active():
    """Пустой статус нельзя считать «действующая»: письмо ликвидированному
    юрлицу необратимо, а отсев виден в вердикте фильтра."""
    assert normalize_status(None) != "active"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2016-03-12", date(2016, 3, 12)),
        ("12.03.2016", date(2016, 3, 12)),
        ("2016-03-12T00:00:00+03:00", date(2016, 3, 12)),
        (1457730000000, date(2016, 3, 12)),  # dadata: миллисекунды эпохи
        (date(2016, 3, 12), date(2016, 3, 12)),
        (None, None),
        ("", None),
        ("когда-то давно", None),
        ("31.02.2016", None),
    ],
)
def test_parse_registry_date(raw, expected):
    assert parse_registry_date(raw) == expected


def test_dadata_timestamp_does_not_slip_a_day():
    """Полночь по Москве в UTC — предыдущий день. Юбилей считается по году,
    но дата уезжает в письмо, и «зарегистрирована 11 марта» вместо 12-го
    заметит первый же читатель."""
    assert parse_registry_date(1457730000000) == date(2016, 3, 12)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"2024": {"2110": 2_140_000_000}, "2023": {"2110": 1_780_000_000}},
         {"2024": 2_140_000_000.0, "2023": 1_780_000_000.0}),
        ({"2024": 1_000_000.0}, {"2024": 1_000_000.0}),
        # Рядом с годами в тех же словарях лежат служебные ключи.
        ({"ОтчетДата": "2025-03-31", "2024": {"2110": 5.0}}, {"2024": 5.0}),
        ({"2024": {"1600": 42}}, {}),  # другая строка отчётности — не выручка
        ({"2024": None}, {}),
        ({}, {}),
        (None, {}),
    ],
)
def test_parse_revenue(raw, expected):
    assert parse_revenue(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (640, (640, None)),
        ("640", (640, None)),
        ({"2023": 500, "2024": 640}, (640, 2024)),  # берём самый свежий год
        (0, (None, None)),
        (None, (None, None)),
        ("нет данных", (None, None)),
    ],
)
def test_parse_headcount(raw, expected):
    assert parse_headcount(raw) == expected


# ------------------------------------------------------------- разбор dadata

DADATA_RESPONSE = {
    "suggestions": [
        {
            "value": "ООО «РОМАШКА-ТРЕЙД»",
            "unrestricted_value": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА-ТРЕЙД"',
            "data": {
                "inn": "7718260181",
                "ogrn": "1167718100000",
                "kpp": "771801001",
                "branch_type": "MAIN",
                "type": "LEGAL",
                "name": {
                    "full_with_opf": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА-ТРЕЙД"',
                    "short_with_opf": 'ООО "РОМАШКА-ТРЕЙД"',
                    "full": "РОМАШКА-ТРЕЙД",
                    "short": "РОМАШКА-ТРЕЙД",
                },
                "okved": "46.90",
                "okved_type": "2014",
                "state": {
                    "status": "ACTIVE",
                    "actuality_date": 1751328000000,
                    "registration_date": 1457730000000,
                    "liquidation_date": None,
                },
                "address": {
                    "value": "г Москва, ул Пример, д 1",
                    "data": {
                        "postal_code": "107140",
                        "region_kladr_id": "7700000000000",
                        "region_iso_code": "RU-MOW",
                        "region": "Москва",
                        "city": "Москва",
                        "settlement": None,
                    },
                },
                "employee_count": 640,
                "finance": {
                    "tax_system": None,
                    "income": 2_400_000_000,
                    "revenue": 2_140_000_000,
                    "expense": 1_900_000_000,
                    "year": 2024,
                },
            },
        }
    ]
}


def test_parse_dadata_party():
    record = parse_dadata_party(DADATA_RESPONSE["suggestions"][0])
    assert record is not None
    assert record.inn == "7718260181"
    assert record.ogrn == "1167718100000"
    assert record.name == 'ООО "РОМАШКА-ТРЕЙД"'
    assert record.registered_at == date(2016, 3, 12)
    assert record.region_code == "77"  # из КЛАДР-идентификатора, не целиком
    assert record.city == "Москва"
    assert record.okved == "46.90"
    assert record.status == "active"
    assert record.headcount == 640
    assert record.headcount_year == 2024
    # revenue приоритетнее income: доходы включают всё подряд.
    assert record.revenue == {"2024": 2_140_000_000.0}


def test_parse_dadata_falls_back_to_income():
    """Выручку dadata отдаёт не всем; доходы за тот же год лучше, чем ничего."""
    payload = json.loads(json.dumps(DADATA_RESPONSE))
    payload["suggestions"][0]["data"]["finance"]["revenue"] = None
    record = parse_dadata_party(payload["suggestions"][0])
    assert record.revenue == {"2024": 2_400_000_000.0}


def test_parse_dadata_without_finance_and_state():
    """Половина полей у dadata необязательна — разбор обязан это переживать."""
    record = parse_dadata_party(
        {"value": "ООО «ТИХАЯ ГАВАНЬ»", "data": {"inn": "7722281949", "name": {}}}
    )
    assert record is not None
    assert record.name == "ООО «ТИХАЯ ГАВАНЬ»"  # из value, раз имени нет
    assert record.registered_at is None
    assert record.revenue == {}
    assert record.status == "unknown"


@pytest.mark.parametrize(
    "suggestion",
    [
        {"data": {"inn": "7718260182"}},  # битая контрольная сумма
        {"data": {"inn": ""}},
        {"data": {}},
        {},
    ],
)
def test_parse_dadata_rejects_bad_inn(suggestion):
    """Компания-призрак под битым ИНН потом не разлепляется с настоящей."""
    assert parse_dadata_party(suggestion) is None


def test_dadata_lookup_prefers_head_office(monkeypatch):
    """Филиал приезжает с тем же ИНН, но со своим адресом и без финансов —
    отфильтровать компанию по данным филиала значит отфильтровать не ту."""
    branch = json.loads(json.dumps(DADATA_RESPONSE["suggestions"][0]))
    branch["data"]["branch_type"] = "BRANCH"
    branch["data"]["address"]["data"]["city"] = "Тверь"
    branch["data"]["finance"] = {}

    response = {"suggestions": [branch, DADATA_RESPONSE["suggestions"][0]]}
    client = DadataClient("token")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response)
    record = client.lookup("7718260181")
    assert record.city == "Москва"
    assert record.revenue == {"2024": 2_140_000_000.0}


def test_dadata_requires_token():
    with pytest.raises(CollectorError):
        DadataClient("")


def test_dadata_sets_auth_header():
    client = DadataClient("secret-token")
    assert client._client.headers["Authorization"] == "Token secret-token"


# -------------------------------------------------------------- разбор damia

DAMIA_RESPONSE = {
    "7705590834": {
        "ИНН": "7705590834",
        "ОГРН": "1117705101370",
        "НаимСокрЮЛ": 'АО "СИНИЙ КИТ"',
        "НаимПолнЮЛ": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "СИНИЙ КИТ"',
        "ДатаРег": "2011-09-05",
        "Статус": "Действующее",
        "ОКВЭД": {"Код": "62.01", "Наим": "Разработка компьютерного ПО"},
        "Адрес": {"Индекс": "115035", "Регион": "77", "НасПункт": "Москва"},
        "СЧР": {"2023": 1100, "2024": 1240},
        "Финансы": {"2024": {"2110": 8_400_000_000}, "2023": {"2110": 7_050_000_000}},
        "Контакты": {"Сайт": ["sinykit.example"]},
    }
}


def test_parse_damia_company():
    record = parse_damia_company(DAMIA_RESPONSE["7705590834"], inn="7705590834")
    assert record.inn == "7705590834"
    assert record.name == 'АО "СИНИЙ КИТ"'
    assert record.registered_at == date(2011, 9, 5)
    assert record.status == "active"
    assert record.region_code == "77"
    assert record.city == "Москва"
    assert record.okved == "62.01"
    assert (record.headcount, record.headcount_year) == (1240, 2024)
    assert record.revenue == {"2024": 8_400_000_000.0, "2023": 7_050_000_000.0}
    assert record.site == "sinykit.example"


def test_parse_damia_takes_inn_from_key():
    """ИНН у damia лежит в ключе ответа, внутри записи его может не быть."""
    record = parse_damia_company({"НаимСокрЮЛ": "АО «СИНИЙ КИТ»"}, inn="7705590834")
    assert record.inn == "7705590834"


def test_parse_damia_unwraps_legal_entity():
    record = parse_damia_company({"ЮЛ": DAMIA_RESPONSE["7705590834"]}, inn="7705590834")
    assert record.registered_at == date(2011, 9, 5)


def test_damia_lookup_reads_keyed_response(monkeypatch):
    client = DamiaClient("key")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: DAMIA_RESPONSE)
    assert client.lookup("7705590834").name == 'АО "СИНИЙ КИТ"'


def test_damia_lookup_on_empty_response(monkeypatch):
    client = DamiaClient("key")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: {})
    assert client.lookup("7705590834") is None


# ------------------------------------------------------------- разбор ofdata

OFDATA_RESPONSE = {
    "meta": {"status": "ok", "message": None},
    "data": {
        "ИНН": "7702166137",
        "ОГРН": "1017702102747",
        "НаимСокр": 'ПАО "СЕВЕРНЫЙ ВЕТЕР"',
        "НаимПолн": 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "СЕВЕРНЫЙ ВЕТЕР"',
        "ДатаРег": "2001-11-27",
        "Статус": {"Код": 1, "Наим": "Действующая"},
        "ОКВЭД": {"Код": "64.19", "Наим": "Денежное посредничество прочее"},
        "ЮрАдрес": {
            "Индекс": "117312",
            "Регион": {"Код": "77", "Наим": "Москва"},
            "НасПункт": "г Москва",
        },
        "СЧР": 3400,
        "СЧРГод": 2025,
        "Контакты": {"Сайт": ["sevveter.example"], "Тел": ["+7 495 000-00-00"]},
        "Финансы": {
            "ОтчетДата": "2025-03-31",
            "2024": {"2110": 41_200_000_000, "2400": 3_100_000_000},
            "2023": {"2110": 38_900_000_000},
        },
    },
}


def test_parse_ofdata_company():
    record = parse_ofdata_company(OFDATA_RESPONSE["data"])
    assert record.inn == "7702166137"
    assert record.name == 'ПАО "СЕВЕРНЫЙ ВЕТЕР"'
    assert record.registered_at == date(2001, 11, 27)
    assert record.status == "active"
    assert record.region_code == "77"
    assert record.okved == "64.19"
    assert (record.headcount, record.headcount_year) == (3400, 2025)
    # Из всей формы 2 берётся только строка выручки.
    assert record.revenue == {"2024": 41_200_000_000.0, "2023": 38_900_000_000.0}
    assert record.site == "sevveter.example"


def test_ofdata_lookup(monkeypatch):
    client = OfdataClient("key")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: OFDATA_RESPONSE)
    assert client.lookup("7702166137").inn == "7702166137"


def test_ofdata_error_in_meta_is_not_silent(monkeypatch):
    """Ошибку ofdata отдаёт с HTTP 200 и статусом в meta: принять её
    за пустой результат значит молча потерять компанию."""
    client = OfdataClient("key")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **kw: {"meta": {"status": "error", "message": "Неверный ключ"}, "data": None},
    )
    with pytest.raises(CollectorError, match="Неверный ключ"):
        client.lookup("7702166137")


# ------------------------------------------------------------ FixtureClient


def test_fixture_file_is_usable():
    """Фикстура — живая документация формата и основа всех offline-прогонов."""
    records = FixtureClient(FIXTURES).records
    assert 10 <= len(records) <= 15
    assert all(is_valid_inn(inn) for inn in records), "ИНН обязаны проходить контрольную сумму"
    assert all(r.registered_at for r in records.values())


def test_fixture_covers_round_anniversaries(config):
    """Ради круглых годовщин источник и нужен: в фикстуре обязаны быть
    компании, у которых юбилей приходится ровно на текущий год."""
    round_years = set(config.signals.kind("company_jubilee").round_years)
    ages = {2026 - r.registered_at.year for r in FixtureClient(FIXTURES).records.values()}
    assert len(ages & round_years) >= 3
    assert ages - round_years, "нужны и компании без юбилея, иначе фильтр не проверить"


def test_fixture_covers_filter_edge_cases():
    """Фикстура должна ломать фильтр, а не только его радовать."""
    records = list(FixtureClient(FIXTURES).records.values())
    assert any(r.status != "active" for r in records)
    assert any(r.region_code not in {"77", "50"} for r in records)
    assert any(r.headcount is None for r in records)
    assert any(r.headcount and r.headcount < 100 for r in records)


def test_fixture_client_lookup():
    client = FixtureClient(FIXTURES)
    record = client.lookup(JUBILEE_INN)
    assert record.registered_at == date(2016, 3, 12)
    assert record.revenue["2024"] > 0
    assert client.lookup("7707083893") is None  # настоящий ИНН, но не наш


def test_fixture_client_ignores_inn_formatting():
    assert FixtureClient(FIXTURES).lookup(" 7718 260 181 ") is not None


def test_fixture_client_suggest():
    found = FixtureClient(FIXTURES).suggest("Ромашка-Трейд")
    assert [r.inn for r in found] == [JUBILEE_INN]
    assert FixtureClient(FIXTURES).suggest("такой компании нет") == []
    assert FixtureClient(FIXTURES).suggest("") == []


def test_fixture_client_reports_broken_file(tmp_path):
    """Ошибка в фикстуре — ошибка разработчика: половина записей молча
    хуже, чем падение с именем записи."""
    path = tmp_path / "companies.json"
    path.write_text(json.dumps({"7718260181": {"name": "ООО Тест", "registered_at": "вчера"}}))
    client = FixtureClient(path)
    with pytest.raises(CollectorError, match="7718260181"):
        client.lookup("7718260181")


def test_fixture_client_reports_missing_file(tmp_path):
    client = FixtureClient(tmp_path / "нет-такого.json")
    with pytest.raises(CollectorError):
        client.lookup("7718260181")


# ------------------------------------------------------- выбор реализации


def test_offline_mode_gives_fixture_client(config):
    client = get_registry_client(config, Settings(dadata_token="есть-ключ"))
    assert isinstance(client, FixtureClient)


def test_live_without_key_falls_back_to_fixtures(config):
    """Без ключа система обязана работать, а не падать: иначе её нельзя
    ни развернуть у клиента, ни прогнать на CI."""
    client = get_registry_client(registry_config(config, mode="live"), Settings(dadata_token=None))
    assert isinstance(client, FixtureClient)


@pytest.mark.parametrize(
    ("provider", "settings", "expected"),
    [
        ("dadata", Settings(dadata_token="t"), DadataClient),
        ("damia", Settings(damia_key="t"), DamiaClient),
        ("ofdata", Settings(ofdata_key="t"), OfdataClient),
    ],
)
def test_live_with_key_gives_provider(config, provider, settings, expected):
    client = get_registry_client(registry_config(config, mode="live", provider=provider), settings)
    assert isinstance(client, expected)


def test_unknown_provider_is_an_error(config, settings=None):
    """Опечатка в конфиге не должна тихо подсовывать выдуманные компании."""
    cfg = registry_config(config, mode="live", provider="dadatta")
    with pytest.raises(CollectorError, match="dadatta"):
        get_registry_client(cfg, Settings(dadata_token="t"))


def test_fixtures_path_from_config(config, tmp_path):
    other = tmp_path / "registry"
    other.mkdir()
    (other / "companies.json").write_text("{}", encoding="utf-8")
    client = get_registry_client(registry_config(config, fixtures_path=str(other)))
    assert client.records == {}


# ------------------------------------------------------------- обогащение


def test_enrich_company_fills_registry_fields(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    guard = SpendGuard(session, stage="enrich")

    company = enrich_company(session, JUBILEE_INN, config=config, guard=guard)

    assert company is not None
    assert company.registered_at == date(2016, 3, 12)
    assert company.headcount == 640
    assert company.revenue["2024"] > 0
    assert company.region_code == "77"
    assert company.status == "active"
    # Факт нужен при том, что дата лежит и в компании: пересчёт системы
    # на исторических данных идёт по append-only таблице фактов.
    fact = session.scalars(select(Fact)).one()
    assert fact.fact_type == FactType.COMPANY_REGISTRATION.value
    assert fact.occurred_at == date(2016, 3, 12)
    assert set(fact.payload) == {"registered_at", "ogrn", "okved", "status", "headcount", "revenue"}


def test_enrich_company_uses_cache(session, config):
    """Второй запрос той же компании не делается вообще — это и есть
    экономия: компания приезжает из разных источников по нескольку раз."""
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    guard = SpendGuard(session, stage="enrich")

    enrich_company(session, JUBILEE_INN, config=config, guard=guard)
    assert len(spend_rows(session)) == 1

    enrich_company(session, JUBILEE_INN, config=config, guard=guard)
    assert len(spend_rows(session)) == 1, "кэш не сработал — стадия платит дважды"


def test_enrich_company_force_ignores_cache(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    guard = SpendGuard(session, stage="enrich")

    enrich_company(session, JUBILEE_INN, config=config, guard=guard)
    enrich_company(session, JUBILEE_INN, config=config, guard=guard, force=True)
    assert len(spend_rows(session)) == 2


def test_enrich_company_ignores_stale_cache(session, config):
    """Данные старше cache_ttl_days перезапрашиваются: выручка и штат
    устаревают, а по ним считается бюджет клиента."""
    ttl = config.sources.source(SOURCE_NAME).cache_ttl_days
    company = repo.upsert_company(
        session, JUBILEE_INN, name="Ромашка", registered_at=date(2016, 3, 12)
    )
    company.updated_at = datetime.now(tz=None) - timedelta(days=ttl + 1)
    session.flush()

    enrich_company(session, JUBILEE_INN, config=config, guard=SpendGuard(session))
    assert len(spend_rows(session)) == 1


def test_enrich_company_does_not_call_api_for_fresh_shell(session, config):
    """Компания, у которой реестр уже отвечал, не перезапрашивается —
    даже если её строку только что трогал другой коллектор."""
    repo.upsert_company(session, PLAIN_INN, name="Ясный день", registered_at=date(2013, 6, 20))
    enrich_company(session, PLAIN_INN, config=config, guard=SpendGuard(session))
    assert spend_rows(session) == []


def test_enrich_company_unknown_inn(session, config):
    """Компании нет в реестре — это ответ, а не сбой. Деньги при этом
    потрачены и в журнал попадают."""
    unknown = "7707083893"
    result = enrich_company(session, unknown, config=config, guard=SpendGuard(session))
    assert result is None
    assert len(spend_rows(session)) == 1


def test_enrich_company_records_provider_and_key(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    stub = StubClient(fixture_record())
    guard = SpendGuard(session, stage="enrich")

    enrich_company(session, JUBILEE_INN, config=config, guard=guard, client=stub)

    row = spend_rows(session)[0]
    assert (row.provider, row.request_key, row.stage) == ("stub", JUBILEE_INN, "enrich")
    # Платный провайдер считается по цене из sources.yaml, фикстуры — нет.
    assert row.cost_rub == config.sources.source(SOURCE_NAME).cost_rub_per_call


def test_budget_exceeded_stops_enrichment(session, config):
    """BudgetExceeded не глушится: стадия обязана остановиться, а не
    дожечь остаток бюджета по оставшимся компаниям."""
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    stub = StubClient(fixture_record())
    guard = SpendGuard(session, stage="enrich")
    guard.budget_rub = 0.1  # дешевле одного вызова

    with pytest.raises(BudgetExceeded):
        enrich_company(session, JUBILEE_INN, config=config, guard=guard, client=stub)

    assert stub.calls == [], "запрос ушёл в API после исчерпания бюджета"
    assert repo.get_company(session, JUBILEE_INN).registered_at is None


def test_call_limit_exceeded_stops_enrichment(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    guard = SpendGuard(session, stage="enrich")
    guard.call_limit = 0

    with pytest.raises(BudgetExceeded):
        enrich_company(session, JUBILEE_INN, config=config, guard=guard)


def test_enrich_company_skips_disabled_source(session, config):
    """Источник выключен — работаем с тем, что есть, и не тратим ничего."""
    cfg = registry_config(config, mode="off", enabled=False)
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")

    company = enrich_company(session, JUBILEE_INN, config=cfg, guard=SpendGuard(session))
    assert company is not None and company.registered_at is None
    assert spend_rows(session) == []


def test_enrich_company_does_not_wipe_known_fields(session, config):
    """Реестр не отдаёт выручку по ликвидированной компании — уже известную
    затирать нельзя, иначе один источник обнуляет работу другого."""
    repo.upsert_company(
        session, LIQUIDATED_INN, name="Лунная соната", revenue={"2023": 900_000_000.0}
    )
    company = enrich_company(session, LIQUIDATED_INN, config=config, guard=SpendGuard(session))
    assert company.revenue == {"2023": 900_000_000.0}
    assert company.status == "liquidated"


# --------------------------------------------------------------- коллектор


def test_collector_targets_companies_without_registration(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    repo.upsert_company(session, PLAIN_INN, name="Ясный день")
    repo.upsert_company(session, LIQUIDATED_INN, name="Соната", registered_at=date(2016, 12, 30))
    session.commit()

    result = RegistryCollector(session, config, run_id="test").run()

    assert (result.count_in, result.count_out) == (2, 2)
    assert {f.inn for f in session.scalars(select(Fact))} == {JUBILEE_INN, PLAIN_INN}


def test_collector_writes_registry_fields_into_company(session, config):
    """Юбилей считается от company.registered_at — без переноса полей
    в карточку компании весь источник бесполезен."""
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    session.commit()

    RegistryCollector(session, config, run_id="test").run()

    company = repo.get_company(session, JUBILEE_INN)
    assert company.registered_at == date(2016, 3, 12)
    assert company.headcount == 640
    assert company.okved == "46.90"
    assert company.source == SOURCE_NAME


def test_collector_is_idempotent(session, config):
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    session.commit()
    collector = RegistryCollector(session, config, run_id="test")

    assert collector.run(inns=[JUBILEE_INN]).count_out == 1
    second = collector.run(inns=[JUBILEE_INN])
    assert second.count_out == 0
    assert second.details["duplicates"] == 1
    assert session.scalar(select(func.count()).select_from(Fact)) == 1


def test_collector_explicit_inns_ignore_registration_state(session, config):
    """Явный список — это ручной перезапрос: он идёт даже по компаниям,
    у которых дата регистрации уже известна."""
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка", registered_at=date(1999, 1, 1))
    session.commit()

    RegistryCollector(session, config, run_id="test").run(inns=[JUBILEE_INN])
    assert repo.get_company(session, JUBILEE_INN).registered_at == date(2016, 3, 12)


def test_collector_skips_unknown_inns(session, config):
    """Компании нет в реестре — остальные из пачки собираются как обычно."""
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    session.commit()

    result = RegistryCollector(session, config, run_id="test").run(
        inns=[JUBILEE_INN, "7707083893"]
    )
    assert result.count_out == 1
    assert len(spend_rows(session)) == 2  # спросили обе, нашли одну


def test_collector_stops_on_budget_but_keeps_paid_data(session, config, monkeypatch):
    """Стадия остановлена, но за уже полученные записи заплачено —
    выбрасывать их значит платить второй раз."""
    for inn in (JUBILEE_INN, PLAIN_INN):
        repo.upsert_company(session, inn, name="Тест")
    session.commit()

    collector = RegistryCollector(session, config, run_id="test")
    monkeypatch.setattr(
        collector, "guard", _guard_allowing(session, calls=1, stage=f"collect:{SOURCE_NAME}")
    )

    result = collector.run(inns=[JUBILEE_INN, PLAIN_INN])

    assert result.details.get("budget_exceeded") == 1
    assert repo.get_company(session, JUBILEE_INN).registered_at == date(2016, 3, 12)


def _guard_allowing(session, *, calls: int, stage: str) -> SpendGuard:
    guard = SpendGuard(session, stage=stage)
    guard.call_limit = calls
    return guard


def test_collector_is_registered():
    import gtm.collectors.registry  # noqa: F401
    from gtm.collectors.base import get_collector

    assert get_collector(SOURCE_NAME) is RegistryCollector


def test_collector_off_source_does_nothing(session, config):
    cfg = registry_config(config, mode="off", enabled=False)
    repo.upsert_company(session, JUBILEE_INN, name="Ромашка")
    session.commit()

    result = RegistryCollector(session, cfg, run_id="test").run()
    assert result.details.get("skipped_disabled") == 1
    assert session.scalar(select(func.count()).select_from(Company)) == 1
