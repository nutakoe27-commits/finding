"""Досье по ожиданию — третье звено.

Обогащение стоит платных запросов, поэтому идёт только по прошедшим фильтр
и только сверху вниз по score: бюджет должен уходить на первые двадцать
ожиданий, а не размазываться по всем сорока.

Задача досье — дать генератору конкретный повод и честно перечислить, чего
мы не знаем. Отсюда два правила, которые важнее полноты:

1. Пустое поле — None. Ничего не достраиваем «по среднему»: выдуманное
   число участников в письме это не стилистика, а сгоревший лид.
2. `gaps` — явный список дыр. Он нужен и генератору (чтобы не сослаться
   на то, чего нет), и менеджеру, которому эти дыры закрывать звонком.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.observability import StageResult, get_logger, stage
from gtm.spend import BudgetExceeded, SpendGuard
from gtm.storage import repo
from gtm.storage.models import Company, Expectation, ExpectationKind, ExpectationStatus

STAGE_NAME = "enrich"

# Технические константы. Бизнес-параметры (пороги, веса, окна) — в config/*.yaml,
# здесь только формулировки для человека и разметка адресов.
#
# Месяцы в предложном падеже: повод читает человек, а не парсер.
_MONTHS_PREP = (
    "январе",
    "феврале",
    "марте",
    "апреле",
    "мае",
    "июне",
    "июле",
    "августе",
    "сентябре",
    "октябре",
    "ноябре",
    "декабре",
)

# Общие ящики. Тот же список, что в скоринге: письмо в info@ уходит в никуда,
# и генератор должен знать, что обращение будет безличным.
_GENERIC_MAILBOXES = frozenset({"info", "sales", "mail", "office"})

# Дыры называем словами менеджера, а не именами полей: этот список попадёт
# и в карточку утреннего списка.
_GAP_NO_CONTACT = "нет контакта"
_GAP_NO_ATTENDEES = "неизвестно число участников"
_GAP_NO_VENUE = "неизвестна площадка"
_GAP_NO_COMPANY_SIZE = "неизвестен размер компании"


# ------------------------------------------------------------------ мелочи


def _positive_int(value: Any) -> int | None:
    """Ноль и мусор — это «не знаем», а не «ноль человек»."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_date(value: Any) -> date | None:
    """Даты в evidence лежат строками — JSON другого типа не знает."""
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _month_year(value: date) -> str:
    return f"{_MONTHS_PREP[value.month - 1]} {value.year}"


def _years_word(years: int) -> str:
    """«15 лет», но «21 год» и «22 года». Круглые даты из конфига дают
    только «лет», однако повод собирается и из ручных evidence."""
    if 11 <= years % 100 <= 14:
        return "лет"
    last = years % 10
    if last == 1:
        return "год"
    if last in (2, 3, 4):
        return "года"
    return "лет"


def _is_generic_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    return email.strip().lower().split("@", 1)[0] in _GENERIC_MAILBOXES


# --------------------------------------------------------------- компания


def _needs_registry(company: Company | None) -> bool:
    """Штат, выручка и дата регистрации — то, ради чего вообще платят реестру:
    без них письму не на что опереться, а юбилей не посчитать.

    Повторный запрос по той же компании не страшен: у реестра свой кэш
    по cache_ttl_days, и в него этот вызов и упрётся.
    """
    if company is None:
        return True
    return company.headcount is None or not company.revenue or company.registered_at is None


def _refresh_from_registry(
    session: Session, config: Config, inn: str, *, guard: SpendGuard
) -> Company | None:
    """Дозапрос в реестр. BudgetExceeded не глушим: он обязан остановить стадию.

    Импорт ленивый — коллекторы тянут за собой сеть и клиентов провайдеров,
    стадии обогащения они не нужны.
    """
    log = get_logger("gtm.enrich")
    try:
        from gtm.collectors.registry import enrich_company
    except ImportError as exc:
        log.warning(
            "enrich.registry_unavailable",
            inn=inn,
            error=str(exc),
            note="досье собирается без дозапроса в реестр",
        )
        return None
    return enrich_company(session, inn, config=config, guard=guard)


def _revenue_last(revenue: Any) -> float | None:
    """Выручка за самый свежий известный год. Без года — числу в письме
    всё равно не место, а фильтру хватает величины."""
    if not isinstance(revenue, dict) or not revenue:
        return None
    years = [key for key in revenue if str(key).isdigit()]
    if not years:
        return None
    value = revenue[max(years, key=int)]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _company_block(company: Company | None, inn: str) -> dict[str, Any]:
    if company is None:
        return {
            "inn": inn,
            "name": None,
            "headcount": None,
            "revenue_last": None,
            "registered_at": None,
            "city": None,
            "age_years": None,
        }
    registered_at = company.registered_at
    return {
        "inn": company.inn,
        "name": company.name,
        "headcount": _positive_int(company.headcount),
        "revenue_last": _revenue_last(company.revenue),
        "registered_at": registered_at.isoformat() if registered_at else None,
        "city": company.city,
        "age_years": relativedelta(date.today(), registered_at).years if registered_at else None,
    }


# ---------------------------------------------------------------- событие


def _event_block(expectation: Expectation) -> dict[str, Any]:
    """Что мы знаем о самом мероприятии.

    Для годовщины это прошлогоднее мероприятие — именно на него ссылается
    письмо. Для закупки дата мероприятия названа в самой закупке, и она же
    стала expected_at.
    """
    evidence = expectation.evidence or {}
    previous = evidence.get("previous_event")
    previous = previous if isinstance(previous, dict) else {}
    tender = evidence.get("tender")
    tender = tender if isinstance(tender, dict) else {}

    event_date = _as_date(previous.get("date"))
    if event_date is None and tender:
        event_date = expectation.expected_at
    repeats = _positive_int(evidence.get("repeats"))
    attendees = _positive_int(previous.get("attendees")) or _positive_int(
        expectation.expected_attendees
    )
    return {
        "title": previous.get("title") or tender.get("title") or None,
        "date": event_date.isoformat() if event_date else None,
        "attendees": attendees,
        "venue": previous.get("venue") or None,
        # Повтор — то же мероприятие в разные годы, а не два факта об одном.
        "is_repeat": bool(repeats and repeats > 1),
        "years_repeated": repeats,
    }


# ----------------------------------------------------------------- контакт


def _contact_block(session: Session, inn: str) -> dict[str, Any]:
    contact = repo.primary_contact(session, inn)
    if contact is None:
        return {"full_name": None, "position": None, "email": None, "is_generic": None}
    return {
        "full_name": contact.full_name,
        "position": contact.position,
        "email": contact.email,
        "is_generic": _is_generic_email(contact.email),
    }


# -------------------------------------------------------------------- повод


def _anniversary_reason(event: dict[str, Any]) -> str | None:
    """«в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо».

    Каждая часть необязательна: без даты повода нет вовсе, без числа
    участников и площадки фраза просто короче — но не врёт.
    """
    event_date = _as_date(event.get("date"))
    if event_date is None:
        return None
    parts = [f"в {_month_year(event_date)} проводили"]
    title = event.get("title")
    parts.append(f"«{title}»" if title else "мероприятие")
    if event.get("attendees"):
        # Тильда не украшение: архивы дают порядок величины, а не список гостей.
        parts.append(f"на ~{event['attendees']} человек")
    if event.get("venue"):
        parts.append(f"в {event['venue']}")
    return " ".join(parts)


def _jubilee_reason(expectation: Expectation, evidence: dict[str, Any]) -> str | None:
    """«в марте 2026 компании исполняется 15 лет»."""
    years = _positive_int(evidence.get("years"))
    if years is None:
        return None
    return (
        f"в {_month_year(expectation.expected_at)} компании исполняется "
        f"{years} {_years_word(years)}"
    )


def _reason(expectation: Expectation, event: dict[str, Any]) -> str:
    """Повод одной фразой.

    Два основных типа собираются здесь заново: у них формулировка попадает
    в письмо почти дословно и должна деградировать по частям. Остальные
    берут готовую фразу из evidence — её пишет тот же билдер, который знает
    про этот тип сигнала всё.
    """
    evidence = expectation.evidence or {}
    reason: str | None = None
    if expectation.kind == ExpectationKind.EVENT_ANNIVERSARY.value:
        reason = _anniversary_reason(event)
    elif expectation.kind == ExpectationKind.COMPANY_JUBILEE.value:
        reason = _jubilee_reason(expectation, evidence)
    if reason:
        return reason
    fallback = evidence.get("reason")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return f"ожидаем мероприятие в {_month_year(expectation.expected_at)}"


# --------------------------------------------------------------------- дыры


def _gaps(
    company: dict[str, Any], event: dict[str, Any], contact: dict[str, Any]
) -> list[str]:
    """Чего не хватает. Порядок — от того, что блокирует письмо, к тому,
    что его лишь обедняет."""
    gaps: list[str] = []
    if not contact.get("email"):
        gaps.append(_GAP_NO_CONTACT)
    if event.get("attendees") is None:
        gaps.append(_GAP_NO_ATTENDEES)
    if event.get("venue") is None:
        gaps.append(_GAP_NO_VENUE)
    if company.get("headcount") is None and company.get("revenue_last") is None:
        gaps.append(_GAP_NO_COMPANY_SIZE)
    return gaps


# ------------------------------------------------------------------ досье


def build_dossier(
    session: Session, config: Config, expectation: Expectation, *, guard: SpendGuard
) -> dict[str, Any]:
    """Собрать досье по одному ожиданию.

    Структура фиксирована — её читает генератор писем. Единственное платное
    место здесь — дозапрос в реестр, и он делается только когда в карточке
    компании действительно нет штата, выручки или даты регистрации.
    """
    company = repo.get_company(session, expectation.inn)
    if _needs_registry(company):
        refreshed = _refresh_from_registry(session, config, expectation.inn, guard=guard)
        company = refreshed or repo.get_company(session, expectation.inn)

    company_block = _company_block(company, expectation.inn)
    event = _event_block(expectation)
    contact = _contact_block(session, expectation.inn)
    return {
        "company": company_block,
        "event": event,
        "contact": contact,
        "reason": _reason(expectation, event),
        "gaps": _gaps(company_block, event, contact),
        "enriched_at": datetime.now(UTC).isoformat(),
    }


# ----------------------------------------------------------------- стадия


def run_enrich(
    session: Session, config: Config, *, run_id: str, limit: int | None = None
) -> StageResult:
    """Обогатить прошедшие фильтр — по убыванию приоритета, не больше limit.

    Дойти до конца списка не обязано: при исчерпании дневного бюджета стадия
    останавливается на месте, а всё, за что уже заплачено, остаётся в базе.
    """
    log = get_logger("gtm.enrich")
    if limit is not None and limit <= 0:
        # repo трактует limit=0 как «без лимита». Для стадии, которая тратит
        # деньги, `--limit 0` обязан означать «ничего», а не «всё».
        targets: list[Expectation] = []
    else:
        targets = list(
            repo.expectations_by_status(session, [ExpectationStatus.READY.value], limit=limit)
        )

    with stage(session, run_id, STAGE_NAME, count_in=len(targets)) as result:
        guard = SpendGuard(session, stage=STAGE_NAME)
        for expectation in targets:
            try:
                dossier = build_dossier(session, config, expectation, guard=guard)
            except BudgetExceeded as exc:
                result.details["stopped"] = "budget_exceeded"
                result.details["stop_reason"] = str(exc)
                result.details["not_enriched"] = len(targets) - result.count_out
                log.warning(
                    "enrich.budget_exceeded",
                    run_id=run_id,
                    enriched=result.count_out,
                    left=len(targets) - result.count_out,
                    error=str(exc),
                )
                break
            expectation.dossier = dossier
            repo.set_status(session, expectation, ExpectationStatus.ENRICHED.value)
            result.bump_out()
            if dossier["gaps"]:
                result.note("with_gaps")
        session.flush()
    return result
