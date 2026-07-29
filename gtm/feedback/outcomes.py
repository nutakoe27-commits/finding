"""Исходы касаний: что случилось после письма.

Цикл продажи у площадки короткий, а результат бинарный — забронировали или
нет. Это редкая возможность считать веса сигналов по фактам, а не гадать,
и ради неё система хранит всю историю. Поэтому фиксируется любой исход,
включая молчание: `no_reply` — такой же результат, как `booked`, и без него
конверсия типа сигнала считается по одним только удачам.

Второе, что делает этот модуль, — синхронизирует статус ожидания. Бронь
закрывает ожидание победой, отказ — поражением; всё промежуточное статус
не трогает, потому что встреча и показ зала — это ещё не результат.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gtm.observability import get_logger
from gtm.storage import repo
from gtm.storage.models import (
    Expectation,
    ExpectationStatus,
    Outcome,
    Outreach,
    OutreachStatus,
    SuppressionReason,
)

# Технические константы: свойства того, как Excel в русской локали пишет CSV.
# Дублируют константы импорта стоп-листа сознательно — тащить приватные
# хелперы из чужого модуля дороже, чем повторить десять строк.
_ENCODINGS = ("utf-8-sig", "cp1251")
_DELIMITERS = (";", ",", "\t")

_KNOWN_OUTCOMES = {outcome.value for outcome in Outcome}

# Исход → статус ожидания. Всё, чего здесь нет, статус не меняет: встреча
# и показ зала — ещё не результат, а перевод ожидания в терминальный статус
# вынул бы его из работы раньше времени.
_STATUS_BY_OUTCOME = {
    Outcome.BOOKED.value: ExpectationStatus.WON.value,
    Outcome.REPLY_NEGATIVE.value: ExpectationStatus.LOST.value,
    Outcome.LOST.value: ExpectationStatus.LOST.value,
}

# Статусы касания, которые уже означают «письмо ушло».
_SENT_STATUSES = frozenset(
    {OutreachStatus.SENT.value, OutreachStatus.BOUNCED.value, OutreachStatus.REPLIED.value}
)
_UNSENT_STATUSES = frozenset({OutreachStatus.DRAFT.value, OutreachStatus.APPROVED.value})

# Ступени воронки. Исход — самая дальняя точка, до которой дошло касание,
# поэтому бронь засчитывается и в ответы, и в показы зала. Это структура
# воронки, а не настраиваемый порог, — потому и в коде, а не в конфиге.
_REPLIED_OUTCOMES = frozenset(
    {
        Outcome.REPLY_NEGATIVE.value,
        Outcome.REPLY_POSITIVE.value,
        Outcome.MEETING.value,
        Outcome.VIEWING.value,
        Outcome.BOOKED.value,
    }
)
_POSITIVE_OUTCOMES = frozenset(
    {
        Outcome.REPLY_POSITIVE.value,
        Outcome.MEETING.value,
        Outcome.VIEWING.value,
        Outcome.BOOKED.value,
    }
)
_VIEWING_OUTCOMES = frozenset({Outcome.VIEWING.value, Outcome.BOOKED.value})


def _log():
    return get_logger("gtm.feedback")


# ------------------------------------------------------------------- запись


def _validate_outcome(value: Any) -> str:
    """Опечатка в исходе тихо испортит калибровку: тип сигнала недосчитается
    конверсии, а понять это по весам через месяц уже нельзя."""
    outcome = str(value or "").strip().lower()
    if outcome not in _KNOWN_OUTCOMES:
        raise ValueError(
            f"Неизвестный исход '{value}'. Допустимые: {', '.join(sorted(_KNOWN_OUTCOMES))}"
        )
    return outcome


def _mark_sent(row: Outreach, outcome: str) -> None:
    """Записанный исход — единственное доказательство, что письмо ушло.

    Отправляет его менеджер со своего ящика, система этого не видит. Без
    отметки воронка показывала бы ноль отправленных при десятке броней.
    Уже проставленное время отправки не трогаем: оно точнее нашей догадки.
    """
    if outcome == Outcome.PENDING.value:
        return
    if row.sent_at is None:
        row.sent_at = datetime.now(UTC)
    if row.status in _UNSENT_STATUSES:
        row.status = OutreachStatus.SENT.value


def _suppress_company(session: Session, inn: str, note: str | None) -> None:
    """Явный отказ от рассылки. Повторная запись того же ИНН не нужна:
    стоп-лист проверяется на совпадение, а не на количество строк."""
    if repo.is_suppressed(session, inn=inn):
        return
    repo.add_suppression(
        session,
        reason=SuppressionReason.OPTED_OUT.value,
        inn=inn,
        note=note or "явный отказ от рассылки",
    )


def record_outcome(
    session: Session,
    outreach_id: int,
    outcome: str,
    note: str | None = None,
    suppress: bool = False,
) -> Outreach:
    """Зафиксировать исход касания и подтянуть за ним статус ожидания.

    `suppress=True` — адресат попросил больше не писать: компания уходит
    в стоп-лист, и ни один следующий сигнал по ней до письма не дойдёт.
    """
    value = _validate_outcome(outcome)
    row = repo.record_outcome(session, outreach_id, value, note=note)
    _mark_sent(row, value)

    expectation = session.get(Expectation, row.expectation_id)
    status = _STATUS_BY_OUTCOME.get(value)
    if expectation is not None and status is not None:
        repo.set_status(session, expectation, status)
    if suppress and expectation is not None:
        _suppress_company(session, expectation.inn, note)

    session.flush()
    _log().info(
        "outcome.recorded",
        outreach_id=row.id,
        outcome=value,
        expectation_status=expectation.status if expectation else None,
        suppressed=suppress,
    )
    return row


# ------------------------------------------------------------------- импорт


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


def _outreach_id(value: Any) -> int | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits else None


def import_outcomes_csv(session: Session, path: str | Path) -> dict[str, int]:
    """Импорт исходов из таблицы (outreach_id, outcome, note).

    Менеджер ведёт результаты в таблице, а не в командной строке, — значит
    таблицу надо уметь принять целиком. Плохая строка не роняет импорт:
    иначе один опечатанный исход отменит работу за неделю. Причина отказа
    видна в счётчиках и в логе.
    """
    stats = {"rows": 0, "applied": 0, "skipped": 0, "not_found": 0, "bad_outcome": 0}
    for number, row in enumerate(_read_rows(Path(path)), start=2):  # 1-я строка — заголовок
        stats["rows"] += 1
        outreach_id = _outreach_id(row.get("outreach_id"))
        if outreach_id is None:
            stats["skipped"] += 1
            _log().warning("outcomes.row_skipped", row=number, reason="нет outreach_id")
            continue
        note = str(row.get("note") or "").strip() or None
        try:
            record_outcome(session, outreach_id, row.get("outcome") or "", note=note)
        except ValueError:
            stats["bad_outcome"] += 1
            _log().warning("outcomes.bad_outcome", row=number, value=row.get("outcome"))
            continue
        except KeyError:
            stats["not_found"] += 1
            _log().warning("outcomes.not_found", row=number, outreach_id=outreach_id)
            continue
        stats["applied"] += 1
    session.flush()
    _log().info("outcomes.imported", path=str(path), **stats)
    return stats


# ------------------------------------------------------------------ воронка


def _is_sent(row: Outreach) -> bool:
    return row.sent_at is not None or row.status in _SENT_STATUSES


def _happened_at(row: Outreach) -> date | None:
    stamp = row.sent_at or row.created_at
    return stamp.date() if stamp is not None else None


def _rate(numerator: int, denominator: int) -> float:
    """Пустая воронка — это ноль, а не деление на ноль: в первые недели
    работы система показывает именно её, и падать на этом нельзя."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def funnel(session: Session, *, since: date | None = None) -> dict[str, Any]:
    """Сводка воронки: отправлено → ответы → положительные → показы → брони.

    Считаем в Python, а не в SQL: касаний десяток в день, полный проход
    стоит копейки, зато правила ступеней читаются одним куском и не зависят
    от того, как диалект сравнивает даты со временем.
    """
    counters = {"sent": 0, "replies": 0, "positive": 0, "viewings": 0, "booked": 0}
    for row in session.scalars(select(Outreach)):
        if not _is_sent(row):
            continue
        if since is not None:
            happened = _happened_at(row)
            if happened is None or happened < since:
                continue
        counters["sent"] += 1
        outcome = row.outcome
        if outcome in _REPLIED_OUTCOMES:
            counters["replies"] += 1
        if outcome in _POSITIVE_OUTCOMES:
            counters["positive"] += 1
        if outcome in _VIEWING_OUTCOMES:
            counters["viewings"] += 1
        if outcome == Outcome.BOOKED.value:
            counters["booked"] += 1

    return {
        **counters,
        "reply_rate": _rate(counters["replies"], counters["sent"]),
        "positive_rate": _rate(counters["positive"], counters["replies"]),
        "viewing_rate": _rate(counters["viewings"], counters["positive"]),
        "booking_rate": _rate(counters["booked"], counters["viewings"]),
        "sent_to_booked": _rate(counters["booked"], counters["sent"]),
    }
