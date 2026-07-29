"""Тесты импорта CSV.

Проверяем то, из-за чего импорт реально ломается в бою: человеческие
форматы дат и участников, битые строки и стабильность source_uid.
Последнее важнее всего: если uid поедет, повторный импорт того же файла
удвоит базу фактов.
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from gtm.collectors.manual import (
    ManualCollector,
    ManualRow,
    import_contacts,
    load_rows,
    parse_attendees,
    parse_event_date,
)
from gtm.storage import repo
from gtm.storage.models import Contact, Fact, FactType

HEADER = (
    "inn,company_name,event_title,event_date,attendees,venue,city,"
    "contact_name,contact_position,contact_email,notes"
)
EXAMPLE_CSV = Path(__file__).resolve().parent.parent / "data" / "manual" / "companies.example.csv"


def write_csv(path: Path, *rows: str, header: str = HEADER) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def fake_resolve(monkeypatch):
    """gtm.resolve пишется параллельно, а коллектору от него нужен только
    контракт «имя/ИНН → ИНН или None». Подставляем минимальную реализацию,
    чтобы тест проверял импорт, а не чужой резолвер."""
    module = types.ModuleType("gtm.resolve")

    def resolve_company(
        session, *, name=None, inn=None, source=None, source_uid=None, payload=None
    ):
        if not inn:
            return None
        repo.upsert_company(session, inn, name=name or inn, city=(payload or {}).get("city"))
        return inn

    module.resolve_company = resolve_company
    monkeypatch.setitem(sys.modules, "gtm.resolve", module)
    return module


# ------------------------------------------------------------------- даты


@pytest.mark.parametrize(
    ("raw", "expected", "precision"),
    [
        ("15.12.2025", date(2025, 12, 15), "day"),
        ("05.12.2025", date(2025, 12, 5), "day"),
        ("2025-12-19", date(2025, 12, 19), "day"),
        ("декабрь 2025", date(2025, 12, 1), "month"),
        ("Декабря 2025", date(2025, 12, 1), "month"),
        ("дек 2025", date(2025, 12, 1), "month"),
        ("5 декабря 2025", date(2025, 12, 5), "day"),
        ("12.2025", date(2025, 12, 1), "month"),
        ("2025-12", date(2025, 12, 1), "month"),
        ("  ноябрь   2025 ", date(2025, 11, 1), "month"),
    ],
)
def test_parse_event_date_formats(raw, expected, precision):
    assert parse_event_date(raw) == (expected, precision)


@pytest.mark.parametrize("raw", ["", None, "скоро", "как обычно в декабре", "31.02.2025", "2025"])
def test_parse_event_date_garbage(raw):
    """Нераспознанная и невозможная дата — не исключение: строка выживает,
    факт просто становится бездатным."""
    assert parse_event_date(raw) is None


# -------------------------------------------------------------- участники


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600", 600),
        ("~600", 600),
        ("600 чел.", 600),
        ("около 600 человек", 600),
        ("500-600", 600),
        ("500 - 600", 600),
        ("до 1500", 1500),
        ("1 200", 1200),
        ("1 200", 1200),
        ("много", None),
        ("", None),
        (None, None),
        ("0", None),
    ],
)
def test_parse_attendees(raw, expected):
    assert parse_attendees(raw) == expected


def test_attendees_takes_upper_bound_of_range():
    """Занижать масштаб опаснее, чем завышать: по нему решается, наш клиент
    или нет, а слишком крупных отсечёт фильтр вместимости."""
    assert parse_attendees("500-600") == 600


# ------------------------------------------------------------------ строки


def test_row_without_company_is_skipped(tmp_path):
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,",
        ",,Корпоратив без хозяина,15.12.2025,900,,Москва,,,,строка-сирота",
        "7712345671,АО Синий Кит-Тест,,,,,Москва,,,,",
    )
    rows = load_rows(path)
    assert [r.company_name for r in rows] == ["ООО Ромашка-Трейд", "АО Синий Кит-Тест"]


def test_broken_inn_does_not_kill_row(tmp_path):
    """ИНН с битой контрольной суммой — опечатка. ИНН отбрасываем (компанию-
    призрак заводить нельзя), но строка остаётся и уйдёт на сведение по имени."""
    path = write_csv(
        tmp_path / "c.csv",
        "7701234567,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,",
    )
    row = load_rows(path)[0]
    assert row.inn is None
    assert row.company_name == "ООО Ромашка-Трейд"


def test_row_with_only_inn_survives(tmp_path):
    path = write_csv(tmp_path / "c.csv", "7701234560,,,15.12.2025,600,,Москва,,,,")
    rows = load_rows(path)
    assert len(rows) == 1
    assert rows[0].inn == "7701234560"


def test_semicolon_and_cp1251_file(tmp_path):
    """Файл приезжает из Excel в русской локали: «;» и cp1251."""
    path = tmp_path / "excel.csv"
    text = "\n".join(
        [
            HEADER.replace(",", ";"),
            "7701234560;ООО Ромашка-Трейд;Корпоратив;15.12.2025;600;;Москва;;;;",
        ]
    )
    path.write_text(text + "\n", encoding="cp1251")
    rows = load_rows(path)
    assert len(rows) == 1
    assert rows[0].company_name == "ООО Ромашка-Трейд"
    assert rows[0].attendees == 600


def test_missing_optional_columns(tmp_path):
    """Необязательных колонок в файле может не быть вовсе."""
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,15.12.2025",
        header="inn,company_name,event_date",
    )
    row = load_rows(path)[0]
    assert row.event_date == date(2025, 12, 15)
    assert row.attendees is None and row.venue is None


# ------------------------------------------------------------------ факты


def test_dated_row_becomes_past_event():
    row = ManualRow.model_validate(
        {
            "inn": "7701234560",
            "company_name": "ООО Ромашка-Трейд",
            "event_title": "Корпоратив",
            "event_date": "декабрь 2025",
            "attendees": "~600",
            "venue": "Клуб Пример",
            "city": "Москва",
            "notes": "каждый год",
        }
    )
    fact = row.to_raw_fact()
    assert fact.fact_type == FactType.PAST_EVENT.value
    assert fact.occurred_at == date(2025, 12, 1)
    # Точность едет в payload, иначе ожидание притворится точным.
    assert fact.payload == {
        "title": "Корпоратив",
        "attendees": 600,
        "venue": "Клуб Пример",
        "city": "Москва",
        "precision": "month",
        "notes": "каждый год",
    }


def test_row_without_date_becomes_growth():
    """Ожидание отсюда не построить, но компанию завести надо: юбилей по
    ЕГРЮЛ и обогащение работают уже от неё."""
    row = ManualRow.model_validate(
        {"inn": "7701234560", "company_name": "ООО Ромашка-Трейд", "notes": "нашли руками"}
    )
    fact = row.to_raw_fact()
    assert fact.fact_type == FactType.GROWTH.value
    assert fact.occurred_at is None
    assert fact.payload == {"notes": "нашли руками"}


# --------------------------------------------------------- стабильность uid


def test_source_uid_ignores_row_position(tmp_path):
    first = write_csv(
        tmp_path / "a.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,",
        "7712345671,АО Синий Кит-Тест,Собрание,19.12.2025,800,,Москва,,,,",
    )
    with_insert = write_csv(
        tmp_path / "b.csv",
        "7723456782,ООО Первый Гвоздь,Корпоратив,01.12.2025,500,,Москва,,,,",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,",
        "7712345671,АО Синий Кит-Тест,Собрание,19.12.2025,800,,Москва,,,,",
    )
    before = {r.source_uid for r in load_rows(first)}
    after = {r.source_uid for r in load_rows(with_insert)}
    assert before < after and len(after) == 3


def test_source_uid_survives_edit_of_attendees(tmp_path):
    """Правка второстепенного поля не должна порождать новый факт."""
    original = load_rows(
        write_csv(
            tmp_path / "a.csv",
            "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,~600,,Москва,,,,",
        )
    )[0]
    edited = load_rows(
        write_csv(
            tmp_path / "b.csv",
            "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,650 чел.,,Москва,,,,",
        )
    )[0]
    assert original.source_uid == edited.source_uid


def test_uid_differs_for_different_events():
    def uid(**over):
        base = {
            "inn": "7701234560",
            "company_name": "ООО Ромашка-Трейд",
            "event_date": "15.12.2025",
        }
        return ManualRow.model_validate({**base, **over}).source_uid

    assert uid(event_title="Корпоратив") != uid(event_title="Конференция")
    assert uid(event_date="15.12.2025") != uid(event_date="15.12.2024")
    # Кавычки и регистр в названии не должны разводить одну строку на две.
    assert (
        ManualRow.model_validate({"company_name": 'ООО "Ромашка-Трейд"'}).source_uid
        == ManualRow.model_validate({"company_name": "ООО Ромашка-Трейд"}).source_uid
    )


# ------------------------------------------------------------- run на базе


def facts_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Fact))


def test_run_writes_facts_and_is_idempotent(session, config, tmp_path):
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,~600,,Москва,,,,",
        "7712345671,АО Синий Кит-Тест,Собрание,декабрь 2025,800,,Москва,,,,",
        "7723456782,ООО Первый Гвоздь,,,,,Москва,,,,без даты",
    )
    collector = ManualCollector(session, config, run_id="test")

    first = collector.run(path=str(path))
    assert (first.count_in, first.count_out) == (3, 3)
    assert facts_count(session) == 3

    second = collector.run(path=str(path))
    assert second.count_in == 3
    assert second.count_out == 0
    assert second.details["duplicates"] == 3
    assert facts_count(session) == 3

    types_ = {f.fact_type for f in session.scalars(select(Fact))}
    assert types_ == {FactType.PAST_EVENT.value, FactType.GROWTH.value}


def test_run_counts_skipped_rows(session, config, tmp_path):
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,",
        ",,Ничей корпоратив,15.12.2025,900,,Москва,,,,",
    )
    result = ManualCollector(session, config, run_id="test").run(path=str(path))
    assert result.count_out == 1
    assert result.details["skipped_rows"] == 1


def test_run_after_row_inserted_adds_only_new_fact(session, config, tmp_path):
    collector = ManualCollector(session, config, run_id="test")
    romashka = "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,"
    kit = "7712345671,АО Синий Кит-Тест,Собрание,19.12.2025,800,,Москва,,,,"
    collector.run(path=str(write_csv(tmp_path / "a.csv", romashka, kit)))
    result = collector.run(
        path=str(
            write_csv(
                tmp_path / "b.csv",
                "7723456782,ООО Первый Гвоздь,Корпоратив,01.12.2025,500,,Москва,,,,",
                romashka,
                kit,
            )
        )
    )
    assert result.count_out == 1
    assert facts_count(session) == 3


def test_unresolved_row_lands_without_inn(session, config, tmp_path):
    """Строка без ИНН всё равно даёт факт: сведением занимается resolve,
    терять её на импорте нельзя."""
    path = write_csv(
        tmp_path / "c.csv", ",ООО Фиолетовый Слон,Корпоратив,18.12.2025,400,,Москва,,,,"
    )
    result = ManualCollector(session, config, run_id="test").run(path=str(path))
    assert result.details["unresolved"] == 1
    fact = session.scalars(select(Fact)).one()
    assert fact.inn is None and fact.company_name_raw == "ООО Фиолетовый Слон"


def test_default_path_from_config(session, config, tmp_path, monkeypatch):
    """Путь в sources.yaml задан от корня проекта."""
    monkeypatch.setattr("gtm.collectors.manual.REPO_ROOT", tmp_path)
    default = tmp_path / "data" / "manual" / "companies.csv"
    default.parent.mkdir(parents=True)
    write_csv(default, "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,,,,")

    result = ManualCollector(session, config, run_id="test").run()
    assert result.count_out == 1


def test_missing_default_file_is_not_an_error(session, config, tmp_path, monkeypatch):
    """Рабочего файла может ещё не быть — система стартует с примера."""
    monkeypatch.setattr("gtm.collectors.manual.REPO_ROOT", tmp_path)
    result = ManualCollector(session, config, run_id="test").run()
    assert (result.count_in, result.count_out) == (0, 0)


def test_missing_explicit_path_raises(session, config, tmp_path):
    from gtm.collectors.base import CollectorError

    collector = ManualCollector(session, config, run_id="test")
    with pytest.raises(CollectorError):
        collector.run(path=str(tmp_path / "нет-такого.csv"))


# ---------------------------------------------------------------- контакты


def test_import_contacts(session, config, tmp_path):
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,"
        "Иванова Мария,HR-директор,M.Ivanova@Romashka.example,",
        "7712345671,АО Синий Кит-Тест,Собрание,19.12.2025,800,,Москва,Петров Сергей,CMO,,нет почты",
        ",ООО Фиолетовый Слон,Корпоратив,18.12.2025,400,,Москва,Зайцева Дарья,HR,d@slon.example,",
    )
    assert import_contacts(session, path) == 1

    contact = session.scalars(select(Contact)).one()
    assert contact.inn == "7701234560"
    assert contact.email == "m.ivanova@romashka.example"  # нормализован
    assert contact.position == "HR-директор"
    assert contact.is_primary is True
    # Компания-оболочка заведена, иначе контакт некуда цеплять.
    assert repo.get_company(session, "7701234560") is not None


def test_import_contacts_is_idempotent(session, config, tmp_path):
    """Компания встречается в файле несколькими мероприятиями, контакт при
    этом один — ни повтор строки, ни повтор импорта не плодят дублей."""
    path = write_csv(
        tmp_path / "c.csv",
        "7701234560,ООО Ромашка-Трейд,Корпоратив,15.12.2025,600,,Москва,"
        "Иванова Мария,HR,m@romashka.example,",
        "7701234560,ООО Ромашка-Трейд,Конференция,12.02.2026,900,,Москва,"
        "Иванова Мария,HR,m@romashka.example,",
    )
    assert import_contacts(session, path) == 1
    assert import_contacts(session, path) == 1
    assert session.scalar(select(func.count()).select_from(Contact)) == 1


def test_import_contacts_keeps_registry_name(session, config, tmp_path, company_factory):
    """Название из ЕГРЮЛ рукописным из файла не затирается."""
    company = company_factory("7701234560", name="ООО «РОМАШКА-ТРЕЙД»")
    path = write_csv(
        tmp_path / "c.csv", "7701234560,Ромашка,,,,,Москва,Иванова Мария,HR,m@romashka.example,"
    )
    import_contacts(session, path)
    assert repo.get_company(session, company.inn).name == "ООО «РОМАШКА-ТРЕЙД»"


# ------------------------------------------------------- файл-документация


def test_example_file_is_valid():
    """Пример — живая документация формата: он обязан разбираться целиком."""
    rows = load_rows(EXAMPLE_CSV)
    assert len(rows) >= 12
    assert all(row.company_key for row in rows)

    precisions = {row.date_precision for row in rows}
    assert {"day", "month"} <= precisions
    assert any(row.event_date is None for row in rows), "нужен пример строки без даты"
    assert any(row.attendees is None for row in rows), "нужен пример с невнятным числом гостей"
    assert any(row.inn is None for row in rows), "нужен пример строки без ИНН"
    assert len({row.source_uid for row in rows}) == len(rows), "uid должны быть различны"


def test_example_file_runs_end_to_end(session, config):
    collector = ManualCollector(session, config, run_id="test")
    result = collector.run(path=str(EXAMPLE_CSV))
    assert result.count_out == result.count_in >= 12
    assert collector.run(path=str(EXAMPLE_CSV)).count_out == 0
