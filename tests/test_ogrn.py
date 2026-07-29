"""ОГРН как бесплатный источник года регистрации и региона.

Смысл этих тестов не в проверке арифметики, а в фиксации границы надёжности:
год из ОГРН равен году основания только начиная с 2005. Если эта граница
однажды поедет, юбилеи начнут считаться от перерегистрации, и система будет
писать компаниям про несуществующие даты.
"""

from __future__ import annotations

import pytest

from gtm.resolve.ogrn import (
    extract_ogrn,
    is_legal_entity,
    is_valid_ogrn,
    is_year_reliable,
    region_code,
    registration_year,
)


def ogrn(*, prefix: int = 1, year: int, region: str = "77", tax: str = "46", number: int = 12345):
    """Собрать корректный ОГРН: контрольный разряд — остаток от деления
    старших 12 цифр на 11 по модулю 10."""
    body = f"{prefix}{year % 100:02d}{region}{tax}{number:05d}"
    assert len(body) == 12, body
    return body + str(int(body) % 11 % 10)


# --------------------------------------------------------- контрольный разряд


def test_valid_ogrn_passes():
    assert is_valid_ogrn(ogrn(year=2016)) is True


def test_wrong_control_digit_is_rejected():
    correct = ogrn(year=2016)
    broken = correct[:-1] + str((int(correct[-1]) + 1) % 10)
    assert is_valid_ogrn(broken) is False


@pytest.mark.parametrize(
    "value",
    ["", None, "12345", "102774612345", "10277461234599", "abcdefghijklm", "0027746123459"],
)
def test_malformed_values_are_rejected(value):
    assert is_valid_ogrn(value) is False


def test_ogrnip_of_15_digits_is_valid_but_not_a_legal_entity():
    body = "30450011600015"
    value = body + str(int(body) % 11 % 10)
    assert is_valid_ogrn(value) is True
    # ИП не арендуют зал на 800 человек — на этом признаке они и отсекаются.
    assert is_legal_entity(value) is False


def test_legal_entity_prefixes():
    assert is_legal_entity(ogrn(prefix=1, year=2016)) is True
    assert is_legal_entity(ogrn(prefix=5, year=2016)) is True


# ------------------------------------------------------------- год и регион


@pytest.mark.parametrize("year", [2002, 2005, 2011, 2016, 2021, 2026])
def test_registration_year_is_read_from_second_and_third_digits(year):
    assert registration_year(ogrn(year=year)) == year


def test_region_is_read_from_fourth_and_fifth_digits():
    assert region_code(ogrn(year=2016, region="50")) == "50"
    assert region_code(ogrn(year=2016, region="77")) == "77"


def test_broken_ogrn_gives_neither_year_nor_region():
    assert registration_year("1027746123458") is None
    assert region_code("1027746123458") is None


# ------------------------------------------- граница надёжности года основания


@pytest.mark.parametrize("year", [2002, 2003, 2004])
def test_years_of_mass_reregistration_are_not_reliable(year):
    """ЕГРЮЛ заработал в 2002, и все существовавшие компании получили ОГРН
    при перерегистрации. У них год записи не год основания: компания 1993 года
    покажет 2002. Юбилей по такому году был бы выдумкой."""
    assert is_year_reliable(registration_year(ogrn(year=year))) is False


@pytest.mark.parametrize("year", [2005, 2011, 2016, 2021])
def test_years_after_reregistration_are_reliable(year):
    assert is_year_reliable(registration_year(ogrn(year=year))) is True


def test_none_year_is_not_reliable():
    assert is_year_reliable(None) is False


# -------------------------------------------------------------- извлечение


def test_extract_ogrn_from_requisites_footer():
    value = ogrn(year=2016)
    text = f"ООО «Ромашка-Трейд», ИНН 7718260181, ОГРН {value}, КПП 771801001"
    assert extract_ogrn(text) == value


def test_extract_skips_broken_and_finds_valid():
    good = ogrn(year=2011)
    text = f"ОГРН 1027746123458 (опечатка), верный: {good}"
    assert extract_ogrn(text) == good


def test_extract_returns_none_when_nothing_valid():
    assert extract_ogrn("ИНН 7718260181, телефон 74951234567") is None
    assert extract_ogrn(None) is None
