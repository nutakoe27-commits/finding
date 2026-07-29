"""Бесплатные наборы ФНС: универсум компаний и их размер.

Главное, что здесь проверяется, — терпимость к разметке. Точные имена полей
в наборах меняются между выпусками, и коллектор обязан это переживать: иначе
каждый новый выпуск ФНС выглядит как «система перестала находить компании».
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
from sqlalchemy import select

from gtm.collectors.fns_open_data import (
    DEFAULT_FIELDS,
    FnsOpenDataCollector,
    OpenDataError,
    inspect_file,
    iter_raw_records,
    parse_msp,
    parse_size,
    pick,
)
from gtm.storage import repo
from gtm.storage.models import Company, Fact, FactType


def ogrn(*, year: int, region: str = "77", prefix: int = 1, number: int = 12345) -> str:
    body = f"{prefix}{year % 100:02d}{region}46{number:05d}"
    return body + str(int(body) % 11 % 10)


# Настоящие ИНН с корректной контрольной суммой — резолвер битые отвергает.
INN_A = "7718260181"
INN_B = "7705590834"
INN_MICRO = "7727219937"


def msp_xml(records: list[dict]) -> bytes:
    """Синтетическая выгрузка. Кавычки в значениях экранируются — настоящие
    наборы ФНС отдают названия как «ООО &quot;Ромашка&quot;»."""
    parts = ["<Файл>"]
    for rec in records:
        attrs = " ".join(
            f"{k}={quoteattr(str(v))}" for k, v in rec.items() if k != "_nested"
        )
        nested = rec.get("_nested", "")
        parts.append(f"<Документ {attrs}>{nested}</Документ>")
    parts.append("</Файл>")
    return "".join(parts).encode("utf-8")


def zipped(name_to_bytes: dict[str, bytes], path: Path) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, blob in name_to_bytes.items():
            archive.writestr(name, blob)
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture()
def msp_archive(tmp_path: Path) -> Path:
    return zipped(
        {
            "msp-1.xml": msp_xml(
                [
                    {
                        "ИННЮЛ": INN_A,
                        "ОГРН": ogrn(year=2016),
                        "НаимОрганизации": 'ООО "Ромашка-Трейд"',
                        "КатСубМСП": "3",
                        "ДатаВклМСП": "10.08.2025",
                        "_nested": '<СвОКВЭД КодОКВЭД="46.90"/>',
                    },
                    {
                        "ИННЮЛ": INN_B,
                        "ОГРН": ogrn(year=2011, number=22222),
                        "НаимОрганизации": 'АО "Синий Кит"',
                        "КатСубМСП": "3",
                        "ДатаВклМСП": "10.08.2025",
                    },
                    {
                        "ИННЮЛ": INN_MICRO,
                        "ОГРН": ogrn(year=2021, number=33333),
                        "НаимОрганизации": 'ООО "Мелкий"',
                        "КатСубМСП": "1",
                    },
                ]
            )
        },
        tmp_path / "msp.zip",
    )


# ------------------------------------------------------------ чтение файлов


def test_reads_xml_records_from_zip(msp_archive: Path):
    rows = list(iter_raw_records(msp_archive))
    assert len(rows) == 3
    assert rows[0]["ИННЮЛ"] == INN_A
    # Атрибуты вложенных элементов попадают в тот же плоский словарь —
    # на этом и держится терпимость к структуре.
    assert rows[0]["КодОКВЭД"] == "46.90"


def test_reads_plain_csv(tmp_path: Path):
    path = tmp_path / "headcount.csv"
    path.write_text(f"ИНН;Численность\n{INN_A};640\n", encoding="utf-8")
    assert list(iter_raw_records(path)) == [{"ИНН": INN_A, "Численность": "640"}]


def test_reads_json_array_and_jsonl(tmp_path: Path):
    array = tmp_path / "a.json"
    array.write_text(json.dumps([{"ИНН": INN_A, "2110": 2140000000}]), encoding="utf-8")
    assert list(iter_raw_records(array))[0]["2110"] == "2140000000"

    lines = tmp_path / "b.jsonl"
    lines.write_text(
        json.dumps({"ИНН": INN_A, "2110": 1}) + "\n" + json.dumps({"ИНН": INN_B, "2110": 2}) + "\n",
        encoding="utf-8",
    )
    assert len(list(iter_raw_records(lines))) == 2


def test_missing_file_says_so_plainly(tmp_path: Path):
    with pytest.raises(OpenDataError, match="Не найден файл набора"):
        list(iter_raw_records(tmp_path / "нет-такого.zip"))


def test_unknown_files_in_archive_are_ignored(tmp_path: Path):
    path = zipped(
        {
            "readme.txt": "описание набора".encode(),
            "data.csv": f"ИНН;Год\n{INN_A};2025\n".encode(),
        },
        tmp_path / "mixed.zip",
    )
    assert list(iter_raw_records(path)) == [{"ИНН": INN_A, "Год": "2025"}]


# ------------------------------------------------------------------ разметка


def test_pick_ignores_case_and_padding():
    row = {" инн ": "123", "Прочее": ""}
    assert pick(row, ["ИНН"]) == "123"
    assert pick(row, ["Прочее"]) is None


def test_pick_takes_first_non_empty_candidate():
    row = {"НаимОрг": "", "НаимОрганизации": "Ромашка"}
    assert pick(row, ["НаимОрг", "НаимОрганизации"]) == "Ромашка"


# ------------------------------------------------------------ разбор записей


def test_msp_record_gets_year_and_region_from_ogrn():
    row = {"ИННЮЛ": INN_A, "ОГРН": ogrn(year=2016, region="50"), "НаимОрганизации": "Ромашка"}
    record = parse_msp(row, DEFAULT_FIELDS["msp"])
    assert record is not None
    # Год и регион взялись из ОГРН — без единого платного запроса.
    assert (record.registration_year, record.region_code) == (2016, "50")
    assert record.year_reliable is True


def test_msp_record_marks_unreliable_year_of_reregistration():
    row = {"ИННЮЛ": INN_A, "ОГРН": ogrn(year=2003), "НаимОрганизации": "Ромашка"}
    record = parse_msp(row, DEFAULT_FIELDS["msp"])
    assert record.registration_year == 2003
    assert record.year_reliable is False


def test_msp_record_rejects_broken_inn_and_missing_name():
    fields = DEFAULT_FIELDS["msp"]
    assert parse_msp({"ИННЮЛ": "7701234567", "НаимОрганизации": "Х"}, fields) is None
    assert parse_msp({"ИННЮЛ": INN_A}, fields) is None


def test_msp_record_rejects_individual_entrepreneur():
    """ИП в наборе есть, но зал на 800 человек они не арендуют."""
    body = "30450011600015"
    ip_ogrn = body + str(int(body) % 11 % 10)
    row = {"ИННЮЛ": INN_A, "ОГРН": ip_ogrn, "НаимОрганизации": "ИП Иванов"}
    assert parse_msp(row, DEFAULT_FIELDS["msp"]) is None


def test_size_records():
    headcount = parse_size(
        {"ИНН": INN_A, "Численность": "1 200"}, DEFAULT_FIELDS["headcount"], dataset="headcount"
    )
    assert headcount.headcount == 1200

    revenue = parse_size(
        {"ИНН": INN_A, "2110": "2 140 000,50", "Год": "2024"},
        DEFAULT_FIELDS["revenue"],
        dataset="revenue",
    )
    assert revenue.revenue == {"2024": 2140000.50}


def test_size_record_skips_rows_without_value():
    assert (
        parse_size({"ИНН": INN_A}, DEFAULT_FIELDS["headcount"], dataset="headcount") is None
    )


# ---------------------------------------------------------------- коллектор


def test_collect_builds_universe_and_skips_micro(session, config, msp_archive: Path):
    collector = FnsOpenDataCollector(session, config, run_id="test")

    result = collector.run(msp_path=msp_archive)

    assert result.count_out == 2, "микропредприятие должно отсеяться на входе"
    assert result.details["skipped_micro"] == 1
    inns = {c.inn for c in session.scalars(select(Company))}
    assert inns == {INN_A, INN_B}
    fact = session.scalars(select(Fact).where(Fact.inn == INN_A)).one()
    assert fact.fact_type == FactType.COMPANY_REGISTRATION.value
    assert fact.payload["registration_year"] == 2016
    assert fact.payload["registration_year_reliable"] is True
    assert fact.occurred_at == date(2025, 8, 10)


def test_ogrn_lands_in_the_company_not_only_in_the_fact(session, config, msp_archive: Path):
    """Движок ожиданий читает company.ogrn, а не payload факта. Если реквизиты
    останутся только в факте, сигнал юбилея не сработает вообще — а это
    единственное, что заменяет платный реестр."""
    FnsOpenDataCollector(session, config, run_id="test").run(msp_path=msp_archive)

    company = repo.get_company(session, INN_A)
    assert company.ogrn is not None
    assert company.okved == "46.90"
    assert company.region_code == "77"


def test_collect_is_idempotent(session, config, msp_archive: Path):
    """Набор перезаливают ежемесячно, и повторная заливка не должна плодить
    факты: source_uid держится на ИНН."""
    collector = FnsOpenDataCollector(session, config, run_id="test")
    collector.run(msp_path=msp_archive)

    second = FnsOpenDataCollector(session, config, run_id="test").run(msp_path=msp_archive)

    assert second.count_out == 0
    assert second.details["duplicates"] == 2


def test_collect_filters_by_region(session, config, tmp_path: Path):
    """Набор общероссийский, а нас интересуют Москва и область. Отсекать
    надо здесь: тащить в базу всю страну незачем."""
    archive = zipped(
        {
            "msp.xml": msp_xml(
                [
                    {
                        "ИННЮЛ": INN_A,
                        "ОГРН": ogrn(year=2016, region="66"),
                        "НаимОрганизации": "Уральский Клён",
                        "КатСубМСП": "3",
                    }
                ]
            )
        },
        tmp_path / "regions.zip",
    )

    result = FnsOpenDataCollector(session, config, run_id="test").run(msp_path=archive)

    assert result.count_out == 0
    assert result.details["skipped_region"] == 1


def test_size_datasets_only_touch_known_companies(session, config, msp_archive, tmp_path: Path):
    """Наборы размера дополняют универсум, а не создают его: иначе в базу
    попадут миллионы юрлиц без единого признака, что они нам интересны."""
    headcount = tmp_path / "hc.csv"
    unknown = "7736050003"
    headcount.write_text(
        f"ИНН;Численность\n{INN_A};640\n{unknown};9000\n", encoding="utf-8"
    )
    revenue = tmp_path / "rev.csv"
    revenue.write_text(f"ИНН;2110;Год\n{INN_A};2140000000;2024\n", encoding="utf-8")

    collector = FnsOpenDataCollector(session, config, run_id="test")
    collector.run(msp_path=msp_archive, headcount_path=headcount, revenue_path=revenue)

    company = repo.get_company(session, INN_A)
    assert company.headcount == 640
    assert company.revenue["2024"] == 2140000000.0
    assert repo.get_company(session, unknown) is None


def test_disabled_source_collects_nothing(session, config, msp_archive: Path):
    config.sources.model_extra["fns_open_data"]["enabled"] = False

    result = FnsOpenDataCollector(session, config, run_id="test").run(msp_path=msp_archive)

    assert result.details == {"skipped_disabled": 1}


# ---------------------------------------------------------------- инспекция


def test_inspect_shows_actual_field_names(msp_archive: Path):
    """Смысл команды: сверить разметку в конфиге с тем, что ФНС реально
    отдаёт в текущем выпуске набора."""
    report = inspect_file(msp_archive, limit=1)

    assert "msp-1.xml" in report
    assert "ИННЮЛ" in report and "КодОКВЭД" in report
    assert "Запись 1" in report


def test_inspect_hints_at_wrong_record_tag(tmp_path: Path):
    path = tmp_path / "other.xml"
    path.write_text("<Файл><Строка ИНН='1'/></Файл>", encoding="utf-8")

    report = inspect_file(path)

    # Тег не тот, но корень всё равно показан — иначе непонятно, что править.
    assert "Запись 1" in report or "record_tags" in report


def test_inspect_on_missing_file_does_not_raise(tmp_path: Path):
    assert "Не найден файл набора" in inspect_file(tmp_path / "нет.zip")
