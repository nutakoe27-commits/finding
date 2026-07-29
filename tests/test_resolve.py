"""Тесты сведения записей к канонической компании.

Проверяется главное: под битый или отсутствующий ИНН компания не заводится,
а запись уходит в карантин ровно один раз, сколько бы раз коллектор ни
перезапускался.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from gtm.resolve import (
    extract_inn,
    is_valid_inn,
    name_candidates,
    normalize_name,
    resolve_company,
    resolve_quarantined,
    resolve_stats,
)
from gtm.storage import repo
from gtm.storage.models import FactType, Quarantine, QuarantineStatus

# Реальные ИНН: Сбербанк, Газпром, Яндекс, Лукойл.
SBER = "7707083893"
GAZP = "7736050003"
YNDX = "7736207543"
LKOH = "7708004767"
# 12-значные ИНН физлица/ИП.
IP_INN = "500100732259"
IP_INN_2 = "771234567859"


# ------------------------------------------------------------------- ИНН


@pytest.mark.parametrize("value", [SBER, GAZP, YNDX, LKOH, IP_INN, IP_INN_2, f"  {SBER}  "])
def test_is_valid_inn_accepts_real_checksums(value: str) -> None:
    assert is_valid_inn(value)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("7707083894", "испорчен контрольный разряд юрлица"),
        ("500100732258", "испорчен второй контрольный разряд ИП"),
        ("500100742259", "испорчен первый контрольный разряд ИП"),
        ("770708389", "девять цифр"),
        ("77070838931", "одиннадцать цифр"),
        ("1027700132195", "это ОГРН, а не ИНН"),
        ("0000000000", "заглушка: сумма сходится, но кода региона 00 не бывает"),
        ("77070838a3", "буква внутри"),
        ("770 708 3893", "пробелы внутри"),
        ("", "пусто"),
        ("   ", "пробелы"),
        ("７７０７０８３８９３", "полноширинные цифры"),
        ("²²²²²²²²²²", "надстрочные цифры проходят isdigit()"),
    ],
)
def test_is_valid_inn_rejects_broken(value: str, why: str) -> None:
    assert not is_valid_inn(value), why


def test_extract_inn_from_marked_text() -> None:
    assert extract_inn(f"ИНН {SBER}") == SBER
    assert extract_inn(f"инн: {IP_INN}") == IP_INN


def test_extract_inn_from_email_footer() -> None:
    footer = (
        "ООО «Ромашка»\n"
        f"ИНН/КПП {SBER}/770701001\n"
        "ОГРН 1027700132195\n"
        "р/с 40702810400000001234 в ПАО Сбербанк\n"
    )
    assert extract_inn(footer) == SBER


def test_extract_inn_prefers_marked_number() -> None:
    # Оба числа проходят контрольную сумму, но реквизитом помечено второе.
    text = f"Договор № {GAZP} от 01.02.2026. Заказчик, ИНН {SBER}."
    assert extract_inn(text) == SBER


def test_extract_inn_ignores_longer_numbers() -> None:
    # Внутри ОГРН и номера счёта не должно находиться «валидного» куска.
    assert extract_inn("ОГРН 1027700132195, р/с 40702810400000001234") is None


@pytest.mark.parametrize("text", ["", "нет никаких реквизитов", "ИНН не указан"])
def test_extract_inn_returns_none(text: str) -> None:
    assert extract_inn(text) is None


# --------------------------------------------------------------- названия


@pytest.mark.parametrize(
    "raw",
    [
        'ООО "Ромашка-Инвест"',
        "ООО «Ромашка Инвест»",
        "ЗАО 'Ромашка-Инвест'",
        "Ромашка Инвест",
        "  РОМАШКА   инвест  ",
        "ГК Ромашка-Инвест",
        "Группа компаний Ромашка-Инвест",
        'Общество с ограниченной ответственностью "Ромашка Инвест"',
        "Публичное акционерное общество «Ромашка-Инвест»",
        "Ромашка/Инвест",
    ],
)
def test_normalize_name_collapses_opf_and_punctuation(raw: str) -> None:
    assert normalize_name(raw) == "ромашка инвест"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('ООО "Пчёлка"', "пчелка"),
        ("Romashka LLC", "romashka"),
        ("Romashka Ltd.", "romashka"),
        ("ROMASHKA INC", "romashka"),
        ("ИП Иванов И.И.", "иванов и и"),
        ("АНО «Центр развития»", "центр развития"),
        ("ФГУП НИИ Точмаш", "нии точмаш"),
        ("ГБУ Школа № 1234", "школа 1234"),
        # «Акционер» не должен пострадать от вычёркивания «акционерное общество».
        ("ООО Акционер", "акционер"),
        ("", ""),
        ("«»", ""),
    ],
)
def test_normalize_name_cases(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_keeps_something_when_only_opf_left() -> None:
    # Иначе пустой name_norm совпал бы разом со всеми такими компаниями.
    assert normalize_name('ООО "АО"') == "ооо ао"


def _company(company_factory, inn: str, name: str):
    return company_factory(inn, name=name, name_norm=normalize_name(name))


def test_name_candidates_sorted_and_cut_off(session: Session, company_factory) -> None:
    _company(company_factory, SBER, "Ромашка Инвест")
    _company(company_factory, GAZP, "Ромашка Инвест Групп")
    _company(company_factory, YNDX, "Транснефть")

    found = name_candidates(session, 'ООО "Ромашка-Инвест"')
    scores = [score for _, score in found]

    assert [c.inn for c, _ in found] == [SBER, GAZP]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(100.0)


def test_name_candidates_respects_limit_and_threshold(session: Session, company_factory) -> None:
    _company(company_factory, SBER, "Ромашка Инвест")
    _company(company_factory, GAZP, "Ромашка Инвест Групп")

    assert len(name_candidates(session, "Ромашка Инвест", limit=1)) == 1
    assert [c.inn for c, _ in name_candidates(session, "Ромашка Инвест", min_score=99.0)] == [SBER]


def test_name_candidates_empty_cases(session: Session, company_factory) -> None:
    # Компания без name_norm не должна становиться кандидатом ни для кого.
    company_factory(SBER, name="Зимородок", name_norm="")

    assert name_candidates(session, "Зимородок") == []
    assert name_candidates(session, "") == []
    assert name_candidates(session, "«»") == []


# --------------------------------------------------------------- резолвер


def test_resolve_by_valid_inn_creates_company(session: Session) -> None:
    inn = resolve_company(
        session,
        name='ООО "Ромашка-Инвест"',
        inn=SBER,
        source="registry",
        source_uid="r-1",
    )

    company = repo.get_company(session, SBER)
    assert inn == SBER
    assert company is not None
    assert company.name == 'ООО "Ромашка-Инвест"'
    assert company.name_norm == "ромашка инвест"
    assert repo.pending_quarantine(session) == []


def test_resolved_company_becomes_findable_by_name(session: Session) -> None:
    # Ради этого и нужен name_norm: следующий источник придёт без ИНН.
    resolve_company(
        session, name='ООО "Ромашка-Инвест"', inn=SBER, source="registry", source_uid="r-1"
    )

    assert (
        resolve_company(session, name="Ромашка Инвест", inn=None, source="hh", source_uid="v-1")
        == SBER
    )


def test_invalid_inn_goes_to_quarantine(session: Session, company_factory) -> None:
    # Имя совпадает с существующей компанией — битый ИНН всё равно важнее:
    # это признак, что источник отдал не то поле.
    _company(company_factory, SBER, "Ромашка")

    result = resolve_company(
        session, name="Ромашка", inn="7707083894", source="hh", source_uid="v-2"
    )
    rows = repo.pending_quarantine(session)

    assert result is None
    assert len(rows) == 1
    assert rows[0].reason == "invalid_inn"
    assert rows[0].source_uid == "v-2"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_no_identity_goes_to_quarantine(session: Session, name: str | None) -> None:
    result = resolve_company(session, name=name, inn=None, source="hh", source_uid="v-3")
    rows = repo.pending_quarantine(session)

    assert result is None
    assert [r.reason for r in rows] == ["no_identity"]


def test_exact_name_match_resolves(session: Session, company_factory) -> None:
    _company(company_factory, SBER, "Ромашка Инвест")
    _company(company_factory, GAZP, "Транснефть")

    result = resolve_company(
        session, name='ГК "Ромашка-Инвест"', inn=None, source="events", source_uid="e-1"
    )

    assert result == SBER
    assert repo.pending_quarantine(session) == []


def test_two_exact_matches_are_ambiguous(session: Session, company_factory) -> None:
    _company(company_factory, SBER, "Ромашка")
    _company(company_factory, GAZP, "ООО «Ромашка»")

    result = resolve_company(session, name="Ромашка", inn=None, source="events", source_uid="e-2")
    row = repo.pending_quarantine(session)[0]

    assert result is None
    assert row.reason == "ambiguous"
    assert {c["inn"] for c in row.candidates} == {SBER, GAZP}
    assert all(c["score"] == 100.0 for c in row.candidates)


def test_fuzzy_match_never_resolves_silently(session: Session, company_factory) -> None:
    _company(company_factory, SBER, "Ромашка Инвест")

    result = resolve_company(
        session,
        name="Ромашка Инвест Групп",
        inn=None,
        source="events",
        source_uid="e-3",
        payload={"event": "Годовое собрание"},
    )
    row = repo.pending_quarantine(session)[0]

    assert result is None
    assert row.reason == "ambiguous"
    assert row.candidates[0]["inn"] == SBER
    assert 88.0 <= row.candidates[0]["score"] < 100.0
    assert row.payload == {"event": "Годовое собрание"}


def test_unknown_name_goes_to_quarantine_without_creating_company(session: Session) -> None:
    result = resolve_company(
        session, name="ООО «Зимородок»", inn=None, source="events", source_uid="e-4"
    )
    row = repo.pending_quarantine(session)[0]

    assert result is None
    assert row.reason == "not_found"
    assert row.raw_name == "ООО «Зимородок»"
    # Вот это и есть главное правило: компании по одному имени не заводятся.
    assert list(repo.iter_companies(session)) == []


def test_repeated_resolve_does_not_duplicate_quarantine(session: Session) -> None:
    for _ in range(3):
        assert (
            resolve_company(session, name="Зимородок", inn=None, source="hh", source_uid="v-5")
            is None
        )

    assert len(repo.pending_quarantine(session)) == 1


# ------------------------------------------------------------ ручной разбор


def _park_fact(session: Session, *, source: str = "hh", source_uid: str = "v-9") -> int:
    """Смоделировать путь коллектора: сведение не удалось — факт лёг без ИНН."""
    inn = resolve_company(session, name="Зимородок", inn=None, source=source, source_uid=source_uid)
    repo.upsert_fact(
        session,
        source=source,
        source_uid=source_uid,
        fact_type=FactType.VACANCY.value,
        inn=inn,
        company_name_raw="Зимородок",
    )
    return repo.pending_quarantine(session)[0].id


def test_resolve_quarantined_binds_facts(session: Session) -> None:
    quarantine_id = _park_fact(session)

    updated = resolve_quarantined(session, quarantine_id, SBER)
    row = session.get(Quarantine, quarantine_id)

    assert updated == 1
    assert row.status == QuarantineStatus.RESOLVED.value
    assert row.resolved_inn == SBER
    assert repo.get_company(session, SBER) is not None
    assert list(repo.unresolved_facts(session)) == []


def test_resolve_quarantined_is_idempotent(session: Session) -> None:
    quarantine_id = _park_fact(session)

    assert resolve_quarantined(session, quarantine_id, SBER) == 1
    # Повторный разбор уже привязанной записи ничего не меняет.
    assert resolve_quarantined(session, quarantine_id, SBER) == 0


def test_manual_resolution_survives_recollection(session: Session) -> None:
    # Коллектор перезапустился после ручного разбора: ИНН обязан вернуться,
    # а не улететь в карантин по второму кругу.
    quarantine_id = _park_fact(session)
    resolve_quarantined(session, quarantine_id, SBER)

    assert (
        resolve_company(session, name="Зимородок", inn=None, source="hh", source_uid="v-9") == SBER
    )
    assert len(repo.pending_quarantine(session)) == 0


def test_resolve_quarantined_rejects_bad_input(session: Session) -> None:
    quarantine_id = _park_fact(session)

    with pytest.raises(ValueError):
        resolve_quarantined(session, quarantine_id, "7707083894")
    with pytest.raises(KeyError):
        resolve_quarantined(session, quarantine_id + 1000, SBER)


def test_resolve_stats(session: Session) -> None:
    quarantine_id = _park_fact(session, source_uid="v-a")
    _park_fact(session, source_uid="v-b")
    _park_fact(session, source_uid="v-c")
    resolve_quarantined(session, quarantine_id, SBER)
    discarded = repo.pending_quarantine(session)[0]
    discarded.status = QuarantineStatus.DISCARDED.value
    session.flush()

    assert resolve_stats(session) == {
        "pending": 1,
        "resolved": 1,
        "discarded": 1,
        "unresolved_facts": 2,
    }
