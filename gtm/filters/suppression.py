"""Стоп-лист: кому нельзя писать ни при каких условиях.

Отписавшиеся, действующие клиенты площадки, конкуренты и адреса, с которых
вернулся отскок. Проверка стоит один индексный запрос, поэтому стоит первой
в ICP: считать выручку компании, которой писать запрещено, незачем.

Ошибка здесь дороже пропущенного клиента: повторное письмо тому, кто просил
не писать, — это жалоба, а жалобы бьют по репутации домена, которую потом
восстанавливать месяцами.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gtm.filters.icp import Verdict
from gtm.observability import get_logger
from gtm.storage import repo
from gtm.storage.models import SuppressionReason

# Технические константы: свойства того, как Excel в русской локали пишет CSV.
_ENCODINGS = ("utf-8-sig", "cp1251")
_DELIMITERS = (";", ",", "\t")
# Длина ИНН: 10 у юрлица, 12 у ИП. Всё остальное — опечатка, и в стоп-листе
# она опаснее, чем в источнике: молча перестанет совпадать с компанией.
_INN_LENGTHS = (10, 12)

_KNOWN_REASONS = {reason.value for reason in SuppressionReason}


def _log():
    return get_logger("gtm.filters.suppression")


# ------------------------------------------------------------------ проверка


def check_suppression(session: Session, inn: str, email: str | None = None) -> Verdict:
    """Проверить ИНН и адрес по стоп-листу. `passed=False` — писать нельзя."""
    if not repo.is_suppressed(session, inn=inn or None, email=email):
        return Verdict(passed=True, reason="не в стоп-листе")

    matched: list[str] = []
    if inn and repo.is_suppressed(session, inn=inn):
        matched.append("ИНН")
    # repo.is_suppressed по адресу заодно сверяет домен. Разделять их вторым
    # запросом незачем: решение одно и то же — не писать.
    if email and repo.is_suppressed(session, email=email):
        matched.append("адрес")
    return Verdict(
        passed=False,
        code="suppressed",
        reason=f"В стоп-листе (совпадение по: {', '.join(matched) or 'ИНН'})",
        details={"matched_by": matched},
    )


def suppress_on_bounce(session: Session, email: str) -> None:
    """Письмо вернулось — адрес в стоп-лист.

    Повторные отправки на мёртвый ящик почтовые провайдеры считают признаком
    спамера, и репутация домена падает на всех адресатов сразу.
    """
    address = (email or "").strip().lower()
    if not address or "@" not in address:
        return
    if repo.is_suppressed(session, email=address):
        return
    repo.add_suppression(
        session,
        reason=SuppressionReason.BOUNCED.value,
        email=address,
        note="отскок письма",
    )
    session.flush()
    _log().info("suppression.bounced", email=address)


# -------------------------------------------------------------------- импорт


def _read_text(path: Path) -> str:
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: не читается ни как {', '.join(_ENCODINGS)}")


def _read_rows(path: Path) -> list[dict[str, Any]]:
    stream = io.StringIO(_read_text(path))
    header = stream.readline()
    stream.seek(0)
    # Разделитель угадываем по заголовку: Excel в русской локали пишет «;».
    reader = csv.DictReader(stream, delimiter=max(_DELIMITERS, key=header.count))
    reader.fieldnames = [
        name.strip().lstrip("﻿").lower() for name in reader.fieldnames or [] if name
    ]
    return [
        row
        for row in reader
        if any((value or "").strip() for value in row.values() if isinstance(value, str))
    ]


def _clean(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _clean_inn(value: Any, *, row: int) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) not in _INN_LENGTHS:
        _log().warning("suppression.bad_inn", row=row, value=str(value))
        return None
    return digits


def _clean_domain(value: Any) -> str | None:
    domain = _clean(value)
    if not domain:
        return None
    # «@example.ru» и «www.example.ru» — тот же домен; строку заполняет человек.
    return domain.lstrip("@").removeprefix("www.") or None


def _reason(value: Any, *, row: int) -> str:
    text = _clean(value)
    if text in _KNOWN_REASONS:
        return str(text)
    if text:
        _log().warning("suppression.unknown_reason", row=row, value=text)
    return SuppressionReason.MANUAL.value


def _already_known(
    session: Session, inn: str | None, email: str | None, domain: str | None
) -> bool:
    """Все идентификаторы строки уже в списке — добавлять нечего.

    Проверяем каждый отдельно: строка «тот же ИНН, новый адрес» должна
    попасть в базу, иначе адрес потеряется.
    """
    checks: list[bool] = []
    if inn:
        checks.append(repo.is_suppressed(session, inn=inn))
    if email:
        checks.append(repo.is_suppressed(session, email=email))
    if domain:
        # repo.is_suppressed сравнивает часть после «@» с колонкой domain.
        checks.append(repo.is_suppressed(session, email=f"x@{domain}"))
    return bool(checks) and all(checks)


def import_suppression_csv(session: Session, path: str | Path) -> int:
    """Пополнить стоп-лист из CSV (inn, email, domain, reason, note).

    Вернуть число добавленных записей. Повторный импорт того же файла ничего
    не добавляет: файл ведут руками и присылают целиком, дубли в нём — норма.
    """
    rows = _read_rows(Path(path))
    added = 0
    duplicates = 0
    skipped = 0
    for number, row in enumerate(rows, start=2):  # 1-я строка — заголовок
        inn = _clean_inn(row.get("inn"), row=number)
        email = _clean(row.get("email"))
        domain = _clean_domain(row.get("domain"))
        if not (inn or email or domain):
            # Без единого идентификатора запись никогда ни с чем не совпадёт.
            skipped += 1
            _log().warning("suppression.row_skipped", row=number)
            continue
        if _already_known(session, inn, email, domain):
            duplicates += 1
            continue
        repo.add_suppression(
            session,
            reason=_reason(row.get("reason"), row=number),
            inn=inn,
            email=email,
            domain=domain,
            note=str(row.get("note") or "").strip() or None,
        )
        added += 1
    session.flush()
    _log().info(
        "suppression.imported",
        path=str(path),
        added=added,
        duplicates=duplicates,
        skipped=skipped,
    )
    return added
