"""Приоритет ожидания.

    score = вес типа × уверенность × сезонность × (1 + бонусы − штрафы)

Зачем вообще число: менеджер обрабатывает пятнадцать карточек в день, а
ожиданий с открытым окном может быть сорок. Score решает, какие пятнадцать.

Правило начисления одно и жёсткое: бонус даётся только когда данные его
подтверждают. Неизвестный масштаб — это штраф, а не «наверное, крупное»:
иначе воронка забивается мероприятиями на сто человек, ради которых
площадка в ЮВАО никому не интересна.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from gtm.config import Config
from gtm.observability import get_logger
from gtm.storage import repo
from gtm.storage.models import Company, Contact, Expectation, ExpectationKind

# Технические константы. Веса, бонусы и штрафы — в config/signals.yaml.
_MOSCOW_REGION = "77"
_MOSCOW_CITY = "москва"
# Общие ящики: письмо в них уходит в никуда, персональный контакт дороже.
_GENERIC_MAILBOXES = frozenset({"info", "sales", "mail", "office"})
_NON_WORD = re.compile(r"[^0-9a-zа-я]+")


def _normalize(text: str | None) -> str:
    """«Event-менеджер» и «event менеджер» — одно и то же: регистр, дефисы
    и ё не должны решать, засчитан бонус или нет."""
    if not text:
        return ""
    return _NON_WORD.sub(" ", str(text).lower().replace("ё", "е")).strip()


def _is_moscow(company: Company | None) -> bool:
    if company is None:
        return False
    if (company.region_code or "").strip() == _MOSCOW_REGION:
        return True
    return _MOSCOW_CITY in _normalize(company.city)


def _primary_contact(expectation: Expectation) -> Contact | None:
    """Сессию берём у самого ожидания: скоринг вызывают с уже загруженными
    объектами, тащить session в сигнатуру ради двух бонусов не нужно."""
    session = object_session(expectation)
    if session is None:
        return None
    return repo.primary_contact(session, expectation.inn)


def _is_event_manager(position: str | None, config: Config) -> bool:
    """Должности сверяем с теми же ключевыми словами, по которым ищем
    вакансии: это ровно те люди, которые решают по площадке."""
    normalized = _normalize(position)
    if not normalized:
        return False
    vacancy_cfg = config.signals.kinds.get(ExpectationKind.VACANCY_SIGNAL.value)
    keywords = vacancy_cfg.keywords if vacancy_cfg else []
    return any(word and word in normalized for word in (_normalize(k) for k in keywords))


def _is_generic_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    return email.strip().lower().split("@", 1)[0] in _GENERIC_MAILBOXES


def score_expectation(expectation: Expectation, company: Company | None, config: Config) -> float:
    """Оценка одного ожидания. Без обращений к внешним источникам."""
    kind_cfg = config.signals.kind(expectation.kind)
    scoring = config.signals.scoring
    bonuses = scoring.bonuses
    penalties = scoring.penalties
    evidence = expectation.evidence or {}

    modifier = 0.0
    attendees = expectation.expected_attendees
    if attendees is None:
        modifier -= penalties.get("attendees_unknown", 0.0)
    else:
        # Пороги накопительные: на 900 человек срабатывают оба — это и
        # «больше 500», и «больше 800», и второе важнее первого.
        if attendees > 500:
            modifier += bonuses.get("attendees_over_500", 0.0)
        if attendees > 800:
            modifier += bonuses.get("attendees_over_800", 0.0)

    if _is_moscow(company):
        modifier += bonuses.get("moscow_hq", 0.0)

    previous = evidence.get("previous_event") or {}
    if isinstance(previous, dict) and previous.get("venue"):
        # Знаем, где проводили раньше — есть с чем сравнивать в письме
        # и понятно, кого мы замещаем.
        modifier += bonuses.get("previous_venue_known", 0.0)

    if int(evidence.get("repeats") or 0) > 1:
        modifier += bonuses.get("repeat_event", 0.0)

    contact = _primary_contact(expectation)
    if contact is not None:
        if _is_event_manager(contact.position, config):
            modifier += bonuses.get("contact_is_event_manager", 0.0)
        if _is_generic_email(contact.email):
            modifier -= penalties.get("contact_generic_email", 0.0)

    season = config.signals.seasonality.factor(expectation.expected_at.month)
    score = kind_cfg.weight * expectation.confidence * season * (1.0 + modifier)
    # Отрицательный приоритет смысла не имеет: это всё ещё «в конец списка».
    return round(max(score, 0.0), 4)


def rescore_all(session: Session, config: Config, *, today: date) -> int:
    """Пересчитать приоритеты — после правки весов в config/signals.yaml.

    Трогаем только ожидания, дата которых ещё не прошла: приоритет нужен,
    чтобы выбрать, кому писать сегодня, а переписывать оценки архива
    бессмысленно и с ростом базы дорожает. В ежедневную выборку прошлое
    и так не попадает — окно закрывается не позже самой даты.
    """
    log = get_logger("gtm.expectations")
    stmt = select(Expectation).where(Expectation.expected_at >= today)
    changed = 0
    for expectation in session.scalars(stmt):
        company = repo.get_company(session, expectation.inn)
        try:
            expectation.score = score_expectation(expectation, company, config)
        except KeyError as exc:
            # Тип сигнала выкинули из конфига, а ожидания по нему остались.
            log.warning("rescore.unknown_kind", kind=expectation.kind, error=str(exc))
            continue
        changed += 1
    session.flush()
    log.info("expectations.rescored", count=changed, today=today.isoformat())
    return changed
