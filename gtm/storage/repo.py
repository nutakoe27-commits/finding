"""Доступ к данным. Все стадии ходят в БД только через эти функции.

Ключевое здесь — `upsert_fact`: коллекторы обязаны быть идемпотентными.
Упало посередине, перезапустили — дублей не появилось. Достигается
естественным ключом (source, source_uid) и вставкой с игнором конфликта.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gtm.storage.models import (
    ApiSpend,
    Company,
    Contact,
    Expectation,
    ExpectationStatus,
    Fact,
    Outcome,
    Outreach,
    Quarantine,
    QuarantineStatus,
    StageRun,
    Suppression,
)

# ------------------------------------------------------------------ company


def get_company(session: Session, inn: str) -> Company | None:
    return session.get(Company, inn)


def upsert_company(session: Session, inn: str, **fields: Any) -> Company:
    """Создать или обновить компанию по ИНН.

    Пустые значения не затирают уже известные: источник, который не отдал
    выручку, не должен обнулять выручку, полученную из другого источника.
    """
    company = session.get(Company, inn)
    if company is None:
        company = Company(inn=inn, name=fields.get("name") or inn)
        session.add(company)
    for key, value in fields.items():
        if value in (None, "", {}, []):
            continue
        if not hasattr(company, key):
            continue
        setattr(company, key, value)
    session.flush()
    return company


def find_companies_by_norm_name(session: Session, name_norm: str) -> list[Company]:
    stmt = select(Company).where(Company.name_norm == name_norm)
    return list(session.scalars(stmt))


def iter_companies(session: Session, limit: int | None = None) -> Sequence[Company]:
    stmt = select(Company)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


# --------------------------------------------------------------------- fact


def upsert_fact(
    session: Session,
    *,
    source: str,
    source_uid: str,
    fact_type: str,
    inn: str | None = None,
    company_name_raw: str | None = None,
    occurred_at: date | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[Fact, bool]:
    """Вернуть (факт, создан_ли). Повторный вызов с тем же ключом — no-op.

    Таблица append-only: существующая строка не перезаписывается, даже если
    payload в источнике изменился. Иначе теряется возможность пересчитать
    систему на исторических данных.
    """
    existing = session.scalar(
        select(Fact).where(Fact.source == source, Fact.source_uid == source_uid)
    )
    if existing is not None:
        # Единственное исключение: если факт был без ИНН, а теперь сведён —
        # это не изменение факта, а результат резолвинга.
        if existing.inn is None and inn is not None:
            existing.inn = inn
            session.flush()
        return existing, False

    fact = Fact(
        source=source,
        source_uid=source_uid,
        fact_type=fact_type,
        inn=inn,
        company_name_raw=company_name_raw,
        occurred_at=occurred_at,
        payload=payload or {},
    )
    session.add(fact)
    session.flush()
    return fact, True


def facts_by_type(
    session: Session,
    fact_type: str,
    *,
    since: date | None = None,
    only_resolved: bool = True,
) -> Sequence[Fact]:
    stmt = select(Fact).where(Fact.fact_type == fact_type)
    if since is not None:
        stmt = stmt.where(Fact.occurred_at >= since)
    if only_resolved:
        stmt = stmt.where(Fact.inn.is_not(None))
    return list(session.scalars(stmt.order_by(Fact.occurred_at)))


def unresolved_facts(session: Session, limit: int | None = None) -> Sequence[Fact]:
    stmt = select(Fact).where(Fact.inn.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


# -------------------------------------------------------------- expectation


def make_dedup_key(inn: str, kind: str, expected_at: date) -> str:
    """Одно ожидание на компанию/тип/месяц. Два источника, показывающие
    на один и тот же декабрьский корпоратив, не должны давать два письма."""
    return f"{inn}|{kind}|{expected_at:%Y-%m}"


def upsert_expectation(
    session: Session,
    *,
    inn: str,
    kind: str,
    expected_at: date,
    window_opens_at: date,
    window_closes_at: date,
    confidence: float,
    expected_precision: str = "month",
    expected_attendees: int | None = None,
    source_fact_id: int | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[Expectation, bool]:
    """Вернуть (ожидание, создано_ли).

    При совпадении ключа обновляем только доказательную базу и уверенность,
    и только вверх: второй сигнал о том же событии усиливает ожидание,
    но не сбрасывает уже пройденные стадии.
    """
    key = make_dedup_key(inn, kind, expected_at)
    existing = session.scalar(select(Expectation).where(Expectation.dedup_key == key))
    if existing is not None:
        if confidence > existing.confidence:
            existing.confidence = confidence
        if expected_attendees and not existing.expected_attendees:
            existing.expected_attendees = expected_attendees
        if evidence:
            merged = dict(existing.evidence or {})
            merged.update(evidence)
            existing.evidence = merged
        session.flush()
        return existing, False

    expectation = Expectation(
        inn=inn,
        kind=kind,
        expected_at=expected_at,
        expected_precision=expected_precision,
        window_opens_at=window_opens_at,
        window_closes_at=window_closes_at,
        confidence=confidence,
        expected_attendees=expected_attendees,
        source_fact_id=source_fact_id,
        evidence=evidence or {},
        status=ExpectationStatus.NEW.value,
        dedup_key=key,
    )
    session.add(expectation)
    session.flush()
    return expectation, True


def expectations_by_status(
    session: Session,
    statuses: Iterable[str],
    *,
    limit: int | None = None,
    order_by_score: bool = True,
) -> Sequence[Expectation]:
    stmt = select(Expectation).where(Expectation.status.in_(list(statuses)))
    if order_by_score:
        stmt = stmt.order_by(Expectation.score.desc().nullslast())
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def open_window_expectations(
    session: Session,
    today: date,
    *,
    statuses: Iterable[str] = (ExpectationStatus.NEW.value,),
    limit: int | None = None,
) -> Sequence[Expectation]:
    """Ежедневный запрос системы: у кого окно контакта открыто сегодня.

    Это и есть вся регулярная работа. Всё остальное — наполнение таблицы.
    """
    stmt = (
        select(Expectation)
        .where(
            Expectation.window_opens_at <= today,
            Expectation.window_closes_at >= today,
            Expectation.status.in_(list(statuses)),
        )
        .order_by(Expectation.score.desc().nullslast(), Expectation.expected_at)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def set_status(session: Session, expectation: Expectation, status: str) -> None:
    expectation.status = status
    session.flush()


# ------------------------------------------------------------------ contact


def upsert_contact(
    session: Session,
    *,
    inn: str,
    email: str | None,
    full_name: str | None = None,
    position: str | None = None,
    phone: str | None = None,
    source: str | None = None,
    is_primary: bool = False,
) -> Contact:
    email_norm = (email or "").strip().lower() or None
    existing = session.scalar(
        select(Contact).where(Contact.inn == inn, Contact.email == email_norm)
    )
    if existing is None:
        existing = Contact(inn=inn, email=email_norm)
        session.add(existing)
    for key, value in (
        ("full_name", full_name),
        ("position", position),
        ("phone", phone),
        ("source", source),
    ):
        if value:
            setattr(existing, key, value)
    if is_primary:
        existing.is_primary = True
    session.flush()
    return existing


def primary_contact(session: Session, inn: str) -> Contact | None:
    stmt = (
        select(Contact)
        .where(Contact.inn == inn, Contact.email.is_not(None))
        .order_by(Contact.is_primary.desc(), Contact.id)
    )
    return session.scalars(stmt).first()


# -------------------------------------------------------------- suppression


def is_suppressed(session: Session, *, inn: str | None = None, email: str | None = None) -> bool:
    """Стоп-лист проверяется до генерации письма и ещё раз до отправки."""
    if not inn and not email:
        return False
    conditions = []
    if inn:
        conditions.append(Suppression.inn == inn)
    if email:
        email_norm = email.strip().lower()
        conditions.append(Suppression.email == email_norm)
        if "@" in email_norm:
            conditions.append(Suppression.domain == email_norm.split("@", 1)[1])
    return session.scalar(select(func.count()).select_from(Suppression).where(or_(*conditions))) > 0


def add_suppression(
    session: Session,
    *,
    reason: str,
    inn: str | None = None,
    email: str | None = None,
    domain: str | None = None,
    note: str | None = None,
) -> Suppression:
    row = Suppression(
        inn=inn,
        email=(email or "").strip().lower() or None,
        domain=(domain or "").strip().lower() or None,
        reason=reason,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------- outreach


def create_outreach(
    session: Session,
    *,
    expectation_id: int,
    contact_id: int | None,
    subject: str,
    body: str,
    generated_by: str,
    status: str = "draft",
) -> Outreach:
    row = Outreach(
        expectation_id=expectation_id,
        contact_id=contact_id,
        subject=subject,
        body=body,
        generated_by=generated_by,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def record_outcome(
    session: Session, outreach_id: int, outcome: str, note: str | None = None
) -> Outreach:
    row = session.get(Outreach, outreach_id)
    if row is None:
        raise KeyError(f"Касание {outreach_id} не найдено")
    row.outcome = outcome
    row.outcome_at = datetime.now(UTC)
    if note:
        row.notes = f"{row.notes}\n{note}" if row.notes else note
    if outcome in {Outcome.REPLY_POSITIVE.value, Outcome.REPLY_NEGATIVE.value}:
        row.replied_at = row.replied_at or datetime.now(UTC)
        row.status = "replied"
    session.flush()
    return row


def outcomes_by_kind(session: Session) -> dict[str, dict[str, int]]:
    """Сводка исходов в разрезе типа сигнала — вход для пересчёта весов."""
    stmt = (
        select(Expectation.kind, Outreach.outcome, func.count())
        .join(Expectation, Outreach.expectation_id == Expectation.id)
        .group_by(Expectation.kind, Outreach.outcome)
    )
    result: dict[str, dict[str, int]] = {}
    for kind, outcome, count in session.execute(stmt):
        result.setdefault(kind, {})[outcome] = count
    return result


# --------------------------------------------------------------- quarantine


def add_quarantine(
    session: Session,
    *,
    source: str,
    source_uid: str,
    raw_name: str | None,
    reason: str,
    payload: dict[str, Any] | None = None,
    candidates: list[Any] | None = None,
) -> Quarantine:
    existing = session.scalar(
        select(Quarantine).where(Quarantine.source == source, Quarantine.source_uid == source_uid)
    )
    if existing is not None:
        return existing
    row = Quarantine(
        source=source,
        source_uid=source_uid,
        raw_name=raw_name,
        reason=reason,
        payload=payload or {},
        candidates=candidates or [],
    )
    session.add(row)
    session.flush()
    return row


def pending_quarantine(session: Session, limit: int | None = None) -> Sequence[Quarantine]:
    stmt = select(Quarantine).where(Quarantine.status == QuarantineStatus.PENDING.value)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


# --------------------------------------------------------- spend / stage_run


def log_api_spend(
    session: Session,
    *,
    provider: str,
    endpoint: str | None,
    stage: str | None,
    units: float = 1.0,
    cost_rub: float = 0.0,
    request_key: str | None = None,
    ok: bool = True,
) -> ApiSpend:
    row = ApiSpend(
        provider=provider,
        endpoint=endpoint,
        stage=stage,
        units=units,
        cost_rub=cost_rub,
        request_key=request_key,
        ok=ok,
    )
    session.add(row)
    session.flush()
    return row


def spend_today(session: Session, day: date | None = None) -> tuple[float, int]:
    """(потрачено рублей, число вызовов) за сутки — для дневного лимита."""
    day = day or datetime.now(UTC).date()
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    stmt = select(func.coalesce(func.sum(ApiSpend.cost_rub), 0.0), func.count()).where(
        ApiSpend.ts >= start
    )
    total, count = session.execute(stmt).one()
    return float(total or 0.0), int(count or 0)


def start_stage(session: Session, run_id: str, stage: str, count_in: int = 0) -> StageRun:
    row = StageRun(run_id=run_id, stage=stage, count_in=count_in)
    session.add(row)
    session.flush()
    return row


def finish_stage(
    session: Session,
    row: StageRun,
    *,
    count_out: int,
    count_in: int | None = None,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> StageRun:
    row.finished_at = datetime.now(UTC)
    row.count_out = count_out
    if count_in is not None:
        row.count_in = count_in
    if details:
        row.details = details
    if error:
        row.error = error
    session.flush()
    return row


def run_summary(session: Session, run_id: str) -> list[dict[str, Any]]:
    stmt = select(StageRun).where(StageRun.run_id == run_id).order_by(StageRun.started_at)
    return [
        {
            "stage": r.stage,
            "in": r.count_in,
            "out": r.count_out,
            "error": r.error,
            "details": r.details,
        }
        for r in session.scalars(stmt)
    ]
