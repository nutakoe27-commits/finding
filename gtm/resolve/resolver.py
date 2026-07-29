"""Сведение записи из источника к канонической компании.

Правило одно: компания заводится только под ИНН, прошедший контрольную сумму.
Всё, что не свелось, уходит в карантин на ручной разбор — не теряется тихо
и не идёт в работу. Соблазн «завести компанию по одному имени» и есть
источник каши: через месяц у одной организации три карточки и три письма.

Карантин идемпотентен по (source, source_uid): повторный сбор не плодит
строки и не затирает результат ручного разбора.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gtm.observability import get_logger
from gtm.resolve.inn import is_valid_inn
from gtm.resolve.names import name_candidates, normalize_name
from gtm.storage import repo
from gtm.storage.models import Fact, Quarantine, QuarantineStatus

log = get_logger("gtm.resolve")

# Причины карантина. Читаются человеком в разборе, поэтому строки, а не enum:
# набор причин задаётся этой стадией и никуда больше не переиспользуется.
REASON_INVALID_INN = "invalid_inn"
REASON_NO_IDENTITY = "no_identity"
REASON_AMBIGUOUS = "ambiguous"
REASON_NOT_FOUND = "not_found"

_EXACT_MATCH_SCORE = 100.0


def resolve_company(
    session: Session,
    *,
    name: str | None,
    inn: str | None,
    source: str,
    source_uid: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Вернуть канонический ИНН записи или None, отправив её в карантин."""
    raw_inn = (inn or "").strip()
    raw_name = " ".join((name or "").split())

    if raw_inn:
        if not is_valid_inn(raw_inn):
            # Не «почти ИНН», а мусор: опечатка, обрезанное число, чужое поле.
            return _park(
                session,
                source=source,
                source_uid=source_uid,
                raw_name=raw_name,
                reason=REASON_INVALID_INN,
                payload=payload,
            )
        # Минимум полей: остальное (штат, выручка, ОКВЭД) допишет обогащение,
        # а источник фактов не должен затирать данные реестра.
        repo.upsert_company(
            session, raw_inn, name=raw_name or None, name_norm=normalize_name(raw_name)
        )
        return raw_inn

    if not raw_name:
        return _park(
            session,
            source=source,
            source_uid=source_uid,
            raw_name=None,
            reason=REASON_NO_IDENTITY,
            payload=payload,
        )

    exact = repo.find_companies_by_norm_name(session, normalize_name(raw_name))
    if len(exact) == 1:
        return exact[0].inn
    if len(exact) > 1:
        # Две компании с одинаковым нормализованным именем — выбор за человеком.
        return _park(
            session,
            source=source,
            source_uid=source_uid,
            raw_name=raw_name,
            reason=REASON_AMBIGUOUS,
            payload=payload,
            candidates=[_candidate(c, _EXACT_MATCH_SCORE) for c in exact],
        )

    fuzzy = name_candidates(session, raw_name)
    if fuzzy:
        # Похожее имя само по себе ничего не решает: «Ромашка» и «Ромашка Плюс»
        # бывают одной компанией, а бывают двумя. Автоматически — никогда.
        return _park(
            session,
            source=source,
            source_uid=source_uid,
            raw_name=raw_name,
            reason=REASON_AMBIGUOUS,
            payload=payload,
            candidates=[_candidate(c, score) for c, score in fuzzy],
        )

    return _park(
        session,
        source=source,
        source_uid=source_uid,
        raw_name=raw_name,
        reason=REASON_NOT_FOUND,
        payload=payload,
    )


def resolve_quarantined(session: Session, quarantine_id: int, inn: str) -> int:
    """Ручной разбор: привязать карантинную запись к ИНН. Вернуть число обновлённых фактов."""
    row = session.get(Quarantine, quarantine_id)
    if row is None:
        raise KeyError(f"Карантинная запись {quarantine_id} не найдена")
    if not is_valid_inn(inn):
        # Ручной ввод ошибается так же, как источник, а последствия те же.
        raise ValueError(f"ИНН {inn!r} не проходит контрольную сумму")

    if repo.get_company(session, inn) is None:
        repo.upsert_company(
            session,
            inn,
            name=row.raw_name or None,
            name_norm=normalize_name(row.raw_name or ""),
            source=row.source,
        )

    # Выборки фактов по (source, source_uid) в repo нет — запрос локальный.
    facts = session.scalars(
        select(Fact).where(Fact.source == row.source, Fact.source_uid == row.source_uid)
    )
    updated = 0
    for fact in facts:
        if fact.inn != inn:
            fact.inn = inn
            updated += 1

    row.status = QuarantineStatus.RESOLVED.value
    row.resolved_inn = inn
    session.flush()
    log.info("resolve.manual", quarantine_id=quarantine_id, inn=inn, facts=updated)
    return updated


def resolve_stats(session: Session) -> dict[str, int]:
    """Сводка по карантину: сколько ждёт разбора и сколько фактов висит без ИНН."""
    counts = dict(
        session.execute(select(Quarantine.status, func.count()).group_by(Quarantine.status)).all()
    )
    unresolved = session.scalar(select(func.count()).select_from(Fact).where(Fact.inn.is_(None)))
    return {
        "pending": int(counts.get(QuarantineStatus.PENDING.value, 0)),
        "resolved": int(counts.get(QuarantineStatus.RESOLVED.value, 0)),
        "discarded": int(counts.get(QuarantineStatus.DISCARDED.value, 0)),
        "unresolved_facts": int(unresolved or 0),
    }


def _candidate(company: Any, score: float) -> dict[str, Any]:
    return {"inn": company.inn, "name": company.name, "score": round(float(score), 1)}


def _park(
    session: Session,
    *,
    source: str,
    source_uid: str,
    raw_name: str | None,
    reason: str,
    payload: dict[str, Any] | None,
    candidates: list[dict[str, Any]] | None = None,
) -> str | None:
    """В карантин — либо вернуть ИНН, если эту запись уже разобрали руками.

    `add_quarantine` возвращает существующую строку по (source, source_uid),
    поэтому повторный сбор того же факта не создаёт дубль и не откатывает
    ручной разбор к None.
    """
    row = repo.add_quarantine(
        session,
        source=source,
        source_uid=source_uid,
        raw_name=raw_name,
        reason=reason,
        payload=payload,
        candidates=candidates,
    )
    if row.status == QuarantineStatus.RESOLVED.value and row.resolved_inn:
        return row.resolved_inn
    log.debug("resolve.quarantined", source=source, source_uid=source_uid, reason=reason)
    return None
