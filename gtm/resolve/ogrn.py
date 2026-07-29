"""Что можно достать из ОГРН, не заплатив ни за один запрос.

ОГРН устроен так, что сам несёт год внесения записи и код региона:

    1 12 77 46 12345 6
    │  │  │  │   │   └ контрольный разряд
    │  │  │  │   └──── номер записи
    │  │  │  └──────── код налогового органа
    │  │  └─────────── код субъекта РФ
    │  └────────────── последние две цифры года внесения записи
    └───────────────── признак отнесения (1, 5 — ЮЛ)

Отсюда бесплатно берутся две вещи, за которые иначе платят реестру:
год для расчёта круглой годовщины и регион для фильтра.

ГЛАВНОЕ ОГРАНИЧЕНИЕ. ЕГРЮЛ заработал в июле 2002 года, и все существовавшие
до него компании получили ОГРН при перерегистрации в 2002-2004 годах. У них
год в ОГРН — год перерегистрации, а не основания: компания 1993 года выпуска
покажет 2002. Значит юбилеи 5, 10, 15 и 20 лет считаются надёжно, а 25 и
старше по ОГРН считать нельзя — на них нужна настоящая дата регистрации.

Функция `is_year_reliable` возвращает именно это различие, а не «да/нет
вообще»: вызывающий код обязан понимать, с чем имеет дело.
"""

from __future__ import annotations

import re

# Контрольный разряд: остаток от деления числа из старших разрядов на 11,
# взятый по модулю 10. Для ОГРН (13 знаков) — первые 12, для ОГРНИП (15) — 14.
_LENGTHS = {13: 12, 15: 14}

# Признак отнесения (первая цифра). 1 и 5 — государственная регистрация ЮЛ,
# 2 — записи о ЮЛ, внесённые по иным основаниям, 3 и 4 — ИП.
_LEGAL_ENTITY_PREFIXES = frozenset("15")

# ЕГРЮЛ запущен 1 июля 2002. Записи 2002-2004 годов массово относятся
# к перерегистрации ранее созданных компаний, поэтому год основания
# по ним не восстанавливается.
EGRUL_START_YEAR = 2002
FIRST_RELIABLE_YEAR = 2005

_OGRN_RE = re.compile(r"(?<!\d)(\d{15}|\d{13})(?!\d)")


def _digits(value: str | None) -> str:
    text = (value or "").strip()
    if not text.isascii() or not text.isdigit():
        return ""
    return text


def is_valid_ogrn(ogrn: str | None) -> bool:
    """Проверка контрольного разряда ОГРН (13 знаков) и ОГРНИП (15 знаков)."""
    value = _digits(ogrn)
    body_length = _LENGTHS.get(len(value))
    if body_length is None:
        return False
    if value[0] == "0":
        return False
    body, control = value[:body_length], value[body_length:]
    return str(int(body) % 11 % 10) == control


def is_legal_entity(ogrn: str | None) -> bool:
    """ЮЛ или ИП. Нас интересуют юрлица: ИП не арендуют зал на 800 человек."""
    value = _digits(ogrn)
    return bool(value) and value[0] in _LEGAL_ENTITY_PREFIXES


def registration_year(ogrn: str | None) -> int | None:
    """Год внесения записи в реестр — вторая и третья цифры ОГРН.

    Не путать с годом основания: см. ограничение в докстринге модуля.
    """
    if not is_valid_ogrn(ogrn):
        return None
    two_digits = int(_digits(ogrn)[1:3])
    # Все ОГРН выданы начиная с 2002 года, так что двузначный год однозначен.
    year = 2000 + two_digits
    return year if year >= EGRUL_START_YEAR else None


def region_code(ogrn: str | None) -> str | None:
    """Код субъекта РФ — четвёртая и пятая цифры. Бесплатный фильтр по региону."""
    if not is_valid_ogrn(ogrn):
        return None
    code = _digits(ogrn)[3:5]
    # «00» и «99» кодами субъектов не бывают: значит формат не тот, что ждём.
    return None if code in {"00", "99"} else code


def is_year_reliable(year: int | None) -> bool:
    """Можно ли считать год из ОГРН годом основания.

    2002-2004 — годы массовой перерегистрации, там год записи не совпадает
    с годом основания. Всё, что позже, совпадает.
    """
    return year is not None and year >= FIRST_RELIABLE_YEAR


def extract_ogrn(text: str | None) -> str | None:
    """Достать ОГРН из произвольного текста — реквизиты в подвале письма,
    выгрузка, страница компании."""
    for match in _OGRN_RE.finditer(text or ""):
        candidate = match.group(1)
        if is_valid_ogrn(candidate):
            return candidate
    return None
