"""Проверка и извлечение ИНН.

ИНН — единственный канонический ключ компании, поэтому проверка настоящая,
по контрольным суммам ФНС, а не «десять цифр — сойдёт». Битая цифра в ИНН
из источника обязана уходить в карантин: если завести под неё компанию,
дубль потом не разлепить, а письмо уйдёт дважды.
"""

from __future__ import annotations

import re

# Веса контрольных разрядов (приказ ФНС о порядке присвоения ИНН).
# 10 знаков — юрлицо: один контрольный разряд. 12 знаков — ИП: два.
_WEIGHTS_10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

# Первые две цифры — код региона, «00» не выдаётся. Отдельная проверка нужна
# из-за 0000000000: контрольную сумму эта заглушка проходит, а в источниках
# она встречается регулярно.
_INVALID_REGION_PREFIX = "00"

# ИНН в тексте: ровно 10 или 12 цифр подряд. Границы обязательны — иначе
# внутри ОГРН (13 знаков) найдётся «валидный» десятизначный кусок.
_INN_RE = re.compile(r"(?<!\d)(\d{12}|\d{10})(?!\d)")
# То же, но рядом с явной пометкой. В подвале письма ИНН лежит вплотную
# к КПП, ОГРН и расчётному счёту — пометка снимает неоднозначность.
_MARKED_INN_RE = re.compile(r"(?:ИНН|INN)\D{0,10}(\d{12}|\d{10})(?!\d)", re.IGNORECASE)


def _control_digit(digits: str, weights: tuple[int, ...]) -> int:
    return sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11 % 10


def is_valid_inn(inn: str) -> bool:
    """Проверка контрольных сумм для 10-значного (юрлицо) и 12-значного (ИП) ИНН."""
    value = (inn or "").strip()
    # isdigit() пропускает «²» и восточные цифры, на которых int() падает.
    if not value.isascii() or not value.isdigit():
        return False
    if value.startswith(_INVALID_REGION_PREFIX):
        return False
    if len(value) == 10:
        return _control_digit(value[:9], _WEIGHTS_10) == int(value[9])
    if len(value) == 12:
        return _control_digit(value[:10], _WEIGHTS_11) == int(value[10]) and _control_digit(
            value[:11], _WEIGHTS_12
        ) == int(value[11])
    return False


def extract_inn(text: str) -> str | None:
    """Достать ИНН из произвольного текста: «ИНН 7707083893», реквизиты в подвале.

    Сначала числа с пометкой «ИНН», потом любые отдельно стоящие 10/12 цифр:
    иначе на строке «ОГРН … ИНН …» вернётся ОГРН, случайно прошедший проверку.
    """
    if not text:
        return None
    for pattern in (_MARKED_INN_RE, _INN_RE):
        for match in pattern.finditer(text):
            candidate = match.group(1)
            if is_valid_inn(candidate):
                return candidate
    return None
