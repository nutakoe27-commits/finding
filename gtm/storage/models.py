"""Модель данных.

Шесть основных таблиц (company, fact, expectation, contact, outreach,
suppression) плюс три служебных (quarantine, api_spend, stage_run).

Два инварианта, которые нельзя нарушать:

1. `fact` — append-only. Строки не перезаписываются и не удаляются.
   Это страховка: когда через месяц выяснится, что фильтр отсекал не тех,
   всё пересчитывается на исторических данных, без повторных обращений к API.
2. ИНН — единственный канонический ключ компании. Всё, что не удалось
   свести к ИНН, уходит в `quarantine`, а не теряется тихо и не идёт в работу.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------
# Перечисления. Хранятся в БД строками — так их видно в SQL-выборках
# и не требуется миграция при добавлении значения.
# --------------------------------------------------------------------------


StrEnum = enum.StrEnum


class FactType(StrEnum):
    """Тип сырого факта из источника."""

    PAST_EVENT = "past_event"  # компания провела мероприятие
    COMPANY_REGISTRATION = "company_registration"  # дата регистрации из ЕГРЮЛ
    VACANCY = "vacancy"  # вакансия event-менеджера и т.п.
    TENDER = "tender"  # закупка по 44-ФЗ / 223-ФЗ
    VENUE_LOSS = "venue_loss"  # площадка закрылась / сменила профиль
    GROWTH = "growth"  # рост штата, новый офис, инвестиции
    NEWS = "news"


class ExpectationKind(StrEnum):
    """Тип ожидания. Веса и окна контакта задаются в config/signals.yaml."""

    EVENT_ANNIVERSARY = "event_anniversary"  # годовщина мероприятия
    COMPANY_JUBILEE = "company_jubilee"  # круглая годовщина компании
    TENDER = "tender"
    VACANCY_SIGNAL = "vacancy_signal"
    VENUE_LOSS = "venue_loss"
    GROWTH = "growth"
    MANUAL = "manual"


class ExpectationStatus(StrEnum):
    NEW = "new"  # создано, фильтр не применялся
    FILTERED_OUT = "filtered_out"  # не прошло ICP
    READY = "ready"  # прошло фильтр, ждёт обогащения
    ENRICHED = "enriched"  # досье собрано
    DRAFTED = "drafted"  # письмо сгенерировано
    DELIVERED = "delivered"  # попало в список менеджеру
    CONTACTED = "contacted"  # письмо отправлено
    WON = "won"  # бронь
    LOST = "lost"
    SNOOZED = "snoozed"  # отложено до следующего цикла


class DatePrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"


class ConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"


class OutreachStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    BOUNCED = "bounced"
    REPLIED = "replied"
    SKIPPED = "skipped"


class Outcome(StrEnum):
    """Исход касания. Цикл у площадки короткий, результат бинарный —
    именно поэтому по этим данным можно реально дообучать фильтр."""

    PENDING = "pending"
    NO_REPLY = "no_reply"
    REPLY_NEGATIVE = "reply_negative"
    REPLY_POSITIVE = "reply_positive"
    MEETING = "meeting"
    VIEWING = "viewing"  # приехали смотреть зал
    BOOKED = "booked"
    LOST = "lost"


class SuppressionReason(StrEnum):
    OPTED_OUT = "opted_out"
    CLIENT = "client"
    COMPETITOR = "competitor"
    MANUAL = "manual"
    BOUNCED = "bounced"


class QuarantineStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DISCARDED = "discarded"


# --------------------------------------------------------------------------
# Таблицы
# --------------------------------------------------------------------------


class Company(Base):
    """Компания. Первичный ключ — ИНН, всё остальное сводится к нему."""

    __tablename__ = "company"

    inn: Mapped[str] = mapped_column(String(12), primary_key=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    name: Mapped[str] = mapped_column(String(512))
    name_full: Mapped[str | None] = mapped_column(String(1024))
    # Нормализованное название: без ОПФ, кавычек, регистра. По нему ищем
    # кандидатов при сведении записей из источников.
    name_norm: Mapped[str] = mapped_column(String(512), default="")
    region_code: Mapped[str | None] = mapped_column(String(3))
    city: Mapped[str | None] = mapped_column(String(128))
    okved: Mapped[str | None] = mapped_column(String(16))
    # Сигнал круглой годовщины считается арифметикой от этой даты.
    registered_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="active")
    headcount: Mapped[int | None] = mapped_column(Integer)
    headcount_year: Mapped[int | None] = mapped_column(Integer)
    # {"2024": 1234567.0, "2023": ...} — выручка по годам
    revenue: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    site: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    facts: Mapped[list[Fact]] = relationship(back_populates="company")
    expectations: Mapped[list[Expectation]] = relationship(back_populates="company")
    contacts: Mapped[list[Contact]] = relationship(back_populates="company")

    __table_args__ = (
        Index("ix_company_name_norm", "name_norm"),
        Index("ix_company_registered_at", "registered_at"),
        CheckConstraint("length(inn) in (10, 12)", name="ck_company_inn_length"),
    )


class Fact(Base):
    """Сырой факт из источника. Append-only.

    Идемпотентность коллекторов держится на уникальном ключе
    (source, source_uid): повторный сбор не создаёт дублей.
    """

    __tablename__ = "fact"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    source_uid: Mapped[str] = mapped_column(String(256))
    fact_type: Mapped[str] = mapped_column(String(64))
    inn: Mapped[str | None] = mapped_column(ForeignKey("company.inn"))
    # Название как пришло из источника — до сведения к ИНН.
    company_name_raw: Mapped[str | None] = mapped_column(String(512))
    # Когда факт произошёл (дата мероприятия, публикации вакансии, закупки).
    occurred_at: Mapped[date | None] = mapped_column(Date)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Когда мы этот факт увидели.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company | None] = relationship(back_populates="facts")

    __table_args__ = (
        UniqueConstraint("source", "source_uid", name="uq_fact_source_uid"),
        Index("ix_fact_type_occurred", "fact_type", "occurred_at"),
        Index("ix_fact_inn", "inn"),
    )


class Expectation(Base):
    """Ядро системы: «у компании X примерно тогда-то ожидается мероприятие».

    Все типы сигналов сводятся к этой сущности. Ежедневная работа —
    один запрос: ожидания, у которых окно контакта открылось сегодня
    и с которыми ещё не работали.
    """

    __tablename__ = "expectation"

    id: Mapped[int] = mapped_column(primary_key=True)
    inn: Mapped[str] = mapped_column(ForeignKey("company.inn"))
    kind: Mapped[str] = mapped_column(String(64))
    expected_at: Mapped[date] = mapped_column(Date)
    expected_precision: Mapped[str] = mapped_column(String(16), default=DatePrecision.MONTH.value)
    # expected_at минус lead_days из config/signals.yaml
    window_opens_at: Mapped[date] = mapped_column(Date)
    window_closes_at: Mapped[date] = mapped_column(Date)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default=ExpectationStatus.NEW.value)
    # Масштаб мероприятия — ключевой критерий ICP.
    expected_attendees: Mapped[int | None] = mapped_column(Integer)
    source_fact_id: Mapped[int | None] = mapped_column(ForeignKey("fact.id"))
    # Чем обосновано ожидание: прошлое мероприятие, площадка, дата.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Досье (звено 3). Заполняется только для прошедших фильтр.
    dossier: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    filter_verdict: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # inn|kind|YYYY-MM — защита от повторного создания того же ожидания.
    dedup_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    company: Mapped[Company] = relationship(back_populates="expectations")
    outreaches: Mapped[list[Outreach]] = relationship(back_populates="expectation")

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_expectation_dedup"),
        Index("ix_expectation_window", "window_opens_at", "status"),
        Index("ix_expectation_status_score", "status", "score"),
    )


class Contact(Base):
    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    inn: Mapped[str] = mapped_column(ForeignKey("company.inn"))
    full_name: Mapped[str | None] = mapped_column(String(256))
    position: Mapped[str | None] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256))
    phone: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(64))
    consent_status: Mapped[str] = mapped_column(String(32), default=ConsentStatus.UNKNOWN.value)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="contacts")

    __table_args__ = (
        UniqueConstraint("inn", "email", name="uq_contact_inn_email"),
        Index("ix_contact_email", "email"),
    )


class Outreach(Base):
    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(primary_key=True)
    expectation_id: Mapped[int] = mapped_column(ForeignKey("expectation.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id"))
    channel: Mapped[str] = mapped_column(String(32), default="email")
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default=OutreachStatus.DRAFT.value)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(32), default=Outcome.PENDING.value)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    expectation: Mapped[Expectation] = relationship(back_populates="outreaches")

    __table_args__ = (Index("ix_outreach_status_outcome", "status", "outcome"),)


class Suppression(Base):
    """Стоп-лист. Проверяется до генерации письма и до отправки."""

    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(primary_key=True)
    inn: Mapped[str | None] = mapped_column(String(12))
    email: Mapped[str | None] = mapped_column(String(256))
    domain: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str] = mapped_column(String(32), default=SuppressionReason.MANUAL.value)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_suppression_inn", "inn"),
        Index("ix_suppression_email", "email"),
        Index("ix_suppression_domain", "domain"),
    )


class Quarantine(Base):
    """Записи, которые не удалось свести к ИНН. Разбираются руками."""

    __tablename__ = "quarantine"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    source_uid: Mapped[str] = mapped_column(String(256))
    raw_name: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(128))
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default=QuarantineStatus.PENDING.value)
    resolved_inn: Mapped[str | None] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source", "source_uid", name="uq_quarantine_source_uid"),
        Index("ix_quarantine_status", "status"),
    )


class ApiSpend(Base):
    """Журнал платных запросов. Без него счёт в конце месяца — сюрприз,
    а понять, какая стадия его сожгла, будет нечем."""

    __tablename__ = "api_spend"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    provider: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str | None] = mapped_column(String(256))
    stage: Mapped[str | None] = mapped_column(String(64))
    units: Mapped[float] = mapped_column(Float, default=1.0)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    request_key: Mapped[str | None] = mapped_column(String(256))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_api_spend_ts_provider", "ts", "provider"),)


class StageRun(Base):
    """Счётчики на входе и выходе каждой стадии.

    Когда система однажды выдаст пустой список, здесь за десять секунд
    видно, на какой стадии обнулилось.
    """

    __tablename__ = "stage_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    count_in: Mapped[int] = mapped_column(Integer, default=0)
    count_out: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_stage_run_run_id", "run_id"),)
