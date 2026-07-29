"""Утренний список менеджеру — пятое звено.

Единственное, что от всей системы видит человек. Отсюда требования, которых
у остальных стадий нет:

1. Выбор уже сделан. Список отсортирован по приоритету и обрезан по лимиту,
   менеджер идёт сверху вниз и не решает, кому писать сегодня.
2. Дыры напечатаны в карточке. Про неизвестное число участников лучше узнать
   до звонка, чем в середине разговора.
3. Повторный прогон в тот же день ничего не дублирует: карточки уходят
   в статус `delivered`, а уже записанный файл не переписывается пустым.

Стадия ничего не считает и никуда не ходит: всё, что печатается, собрано
предыдущими звеньями и лежит в базе.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.observability import StageResult, get_logger, stage
from gtm.settings import get_settings
from gtm.storage import repo
from gtm.storage.models import Contact, Expectation, ExpectationStatus, Outreach, OutreachStatus

STAGE_NAME = "deliver"

# Технические константы. Лимит списка — в settings, порог отсечения —
# в config/signals.yaml; здесь только формат карточки и файлов.
#
# Месяцы в родительном падеже: «20 ноября 2026» читает человек.
_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Порядок колонок CSV фиксирован: файл уезжает в импорт CRM, где поля
# сопоставляют руками и один раз.
_CSV_COLUMNS = (
    "expectation_id",
    "outreach_id",
    "inn",
    "company",
    "reason",
    "expected_at",
    "expected_attendees",
    "previous_venue",
    "score",
    "contact",
    "subject",
    "body",
    "gaps",
)
# BOM: без него Excel открывает кириллицу кракозябрами, а открывают именно им.
_CSV_ENCODING = "utf-8-sig"
_GAPS_SEPARATOR = "; "


@dataclass
class DeliveryItem:
    """Одна карточка утреннего списка — всё, что нужно для одного касания."""

    expectation_id: int
    inn: str
    company: str
    reason: str
    expected_at: date
    expected_attendees: int | None
    previous_venue: str | None
    score: float
    contact: str | None  # «Имя, должность <email>» или None
    subject: str
    body: str
    gaps: list[str]
    # Не в исходном контракте, но без номера касания строка «gtm feedback
    # record» в подвале нерабочая, а без обратной связи не дообучается фильтр.
    outreach_id: int | None = None


# ------------------------------------------------------------------ мелочи


def _positive_int(value: Any) -> int | None:
    """Ноль и мусор — это «не знаем», а не «ноль человек»."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ru_date(value: date) -> str:
    return f"{value.day} {_MONTHS_GEN[value.month - 1]} {value.year}"


def _block(source: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """Досье и evidence приезжают из JSON-колонки: там может лежать что угодно."""
    block = (source or {}).get(name)
    return block if isinstance(block, dict) else {}


def _letter(session: Session, expectation_id: int) -> Outreach | None:
    """Последнее непустое письмо по ожиданию.

    Отправленные и пропущенные не берём: в утреннем списке место только тому,
    что менеджеру ещё предстоит отправить. Отдельной функции в repo нет —
    запрос локальный.
    """
    stmt = (
        select(Outreach)
        .where(
            Outreach.expectation_id == expectation_id,
            Outreach.status.in_([OutreachStatus.DRAFT.value, OutreachStatus.APPROVED.value]),
        )
        .order_by(Outreach.id.desc())
    )
    for row in session.scalars(stmt):
        if (row.body or "").strip():
            return row
    return None


def _contact_of(session: Session, expectation: Expectation, letter: Outreach) -> Contact | None:
    """Кому адресовано письмо. Если генератор выбрал конкретный контакт —
    печатаем его, иначе основной по компании."""
    if letter.contact_id is not None:
        contact = session.get(Contact, letter.contact_id)
        if contact is not None:
            return contact
    return repo.primary_contact(session, expectation.inn)


def _contact_line(contact: Contact | None) -> str | None:
    """«Анна Петрова, event-менеджер <a.petrova@romashka.ru>».

    Ничего не выдумываем: нет имени — остаётся адрес, нет адреса — остаётся
    имя, нет ничего — None, и карточка честно скажет, что контакта нет.
    """
    if contact is None:
        return None
    who = ", ".join(part for part in (contact.full_name, contact.position) if part)
    email = (contact.email or "").strip()
    if who and email:
        return f"{who} <{email}>"
    return who or email or None


# ------------------------------------------------------------------ карточка


def _item(
    session: Session, expectation: Expectation, letter: Outreach, contact: Contact | None
) -> DeliveryItem:
    """Собрать карточку. Источник данных — досье, evidence и карточка компании;
    ни одного внешнего запроса здесь быть не должно."""
    dossier = expectation.dossier or {}
    evidence = expectation.evidence or {}
    event = _block(dossier, "event")
    previous = _block(evidence, "previous_event")

    company = repo.get_company(session, expectation.inn)
    name = company.name if company else _block(dossier, "company").get("name")

    reason = dossier.get("reason") or evidence.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = f"мероприятие ожидается {_ru_date(expectation.expected_at)}"

    gaps = dossier.get("gaps")

    return DeliveryItem(
        expectation_id=expectation.id,
        inn=expectation.inn,
        company=name or expectation.inn,
        reason=reason.strip(),
        expected_at=expectation.expected_at,
        expected_attendees=(
            _positive_int(expectation.expected_attendees) or _positive_int(event.get("attendees"))
        ),
        previous_venue=event.get("venue") or previous.get("venue") or None,
        score=float(expectation.score or 0.0),
        contact=_contact_line(contact),
        subject=(letter.subject or "").strip(),
        body=(letter.body or "").strip(),
        gaps=[str(g) for g in gaps] if isinstance(gaps, list) else [],
        outreach_id=letter.id,
    )


def _select(
    session: Session, config: Config, *, today: date, limit: int | None
) -> tuple[list[DeliveryItem], dict[str, int]]:
    """Отбор карточек плюс счётчики отсева — вторые нужны только стадии.

    Порядок проверок от дешёвых к дорогим здесь тоже соблюдается, но экономим
    уже не деньги, а запросы: письмо и контакт достаём только для тех,
    кто прошёл по приоритету и дате.
    """
    min_score = config.signals.scoring.min_score_to_deliver
    limit = get_settings().daily_delivery_limit if limit is None else limit
    notes: dict[str, int] = {}

    def note(key: str) -> None:
        notes[key] = notes.get(key, 0) + 1

    items: list[DeliveryItem] = []
    for expectation in repo.expectations_by_status(session, [ExpectationStatus.DRAFTED.value]):
        if (expectation.score or 0.0) < min_score:
            note("below_min_score")
            continue
        if expectation.expected_at < today:
            # Пока письмо лежало в черновиках, мероприятие прошло. Писать
            # «планируете ли вы» про вчерашнее — худшее, что можно сделать.
            note("event_passed")
            continue
        letter = _letter(session, expectation.id)
        if letter is None:
            note("no_letter")
            continue
        contact = _contact_of(session, expectation, letter)
        if repo.is_suppressed(
            session, inn=expectation.inn, email=contact.email if contact else None
        ):
            # Стоп-лист мог пополниться уже после генерации письма, а список —
            # последняя точка перед отправкой.
            note("suppressed")
            continue
        items.append(_item(session, expectation, letter, contact))

    items.sort(key=lambda i: (-i.score, i.expected_at, i.expectation_id))
    cut = max(limit, 0)
    if len(items) > cut:
        notes["over_limit"] = len(items) - cut
        items = items[:cut]
    return items, notes


def build_daily(
    session: Session, config: Config, *, today: date, limit: int | None = None
) -> list[DeliveryItem]:
    """Карточки на сегодня: готовые письма с приоритетом не ниже порога,
    сверху вниз, не больше лимита. Статусы не трогает — это делает стадия."""
    items, _ = _select(session, config, today=today, limit=limit)
    return items


# --------------------------------------------------------------- markdown


def _quote(text: str) -> list[str]:
    """Текст письма — цитатой, чтобы его нельзя было спутать с инструкцией."""
    lines = (text or "").strip().splitlines() or [""]
    return [f"> {line}".rstrip() for line in lines]


def _card(index: int, item: DeliveryItem) -> list[str]:
    scale = (
        f"~{item.expected_attendees} человек"
        if item.expected_attendees
        else "масштаб неизвестен"
    )
    ids = f"**ИНН:** {item.inn} · ожидание #{item.expectation_id}"
    if item.outreach_id:
        ids += f" · касание #{item.outreach_id}"

    lines = [
        f"## {index}. {item.company} · приоритет {item.score:.2f}",
        "",
        f"- **Повод:** {item.reason}",
        f"- **Когда:** {_ru_date(item.expected_at)}, {scale}",
        f"- **Прошлый зал:** {item.previous_venue or 'неизвестен'}",
        f"- **Контакт:** {item.contact or 'не найден, искать руками'}",
        f"- {ids}",
        f"- **Тема письма:** {item.subject}",
        "",
        *_quote(item.body),
    ]
    if item.gaps:
        # Дыры видно до звонка, а не после: именно их менеджер закрывает первым
        # вопросом, и именно на них ломается разговор, если о них не знать.
        lines += ["", f"**Чего не знаем:** {_GAPS_SEPARATOR.join(item.gaps)}"]
    lines.append("")
    return lines


def _footer() -> list[str]:
    return [
        "---",
        "",
        "## Что делать со списком",
        "",
        "Идти сверху вниз: карточки уже отсортированы по приоритету, выбирать не нужно.",
        "Перед звонком закрыть строку «Чего не знаем» — эти дыры всплывут в разговоре.",
        "Письмо отправляет менеджер со своего ящика, текст выше можно править.",
        "",
        "Любой исход (и молчание через неделю тоже) фиксировать:",
        "",
        "```",
        "gtm feedback record <номер касания> reply_positive",
        "gtm feedback record <номер касания> no_reply",
        "```",
        "",
        "Без этого фильтр не дообучается: цикл у площадки короткий, а результат",
        "бинарный — это редкая возможность считать веса по фактам, а не гадать.",
        "",
    ]


def render_markdown(
    items: list[DeliveryItem], *, today: date, summary: str | None = None
) -> str:
    """Список для чтения человеком. Сводка конвейера в шапке — чтобы пустой
    или короткий день был объяснён сразу, а не после похода в `gtm stats`."""
    lines = [f"# Утренний список — {_ru_date(today)}", ""]
    if summary:
        lines += [f"**Конвейер:** {summary}", ""]
    lines += [f"**Карточек:** {len(items)}", ""]

    if not items:
        lines += [
            "Сегодня писать некому: подходящих ожиданий с готовыми письмами нет.",
            "Где обнулилась воронка, видно в `gtm stats`.",
            "",
        ]
        return "\n".join(lines)

    lines.append("---")
    lines.append("")
    for index, item in enumerate(items, start=1):
        lines += _card(index, item)
    lines += _footer()
    return "\n".join(lines)


# -------------------------------------------------------------------- csv


def render_csv(items: list[DeliveryItem]) -> str:
    """Плоский CSV под импорт в CRM: одна карточка — одна строка, без вложенности."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for item in items:
        writer.writerow(
            [
                item.expectation_id,
                item.outreach_id or "",
                item.inn,
                item.company,
                item.reason,
                item.expected_at.isoformat(),
                item.expected_attendees if item.expected_attendees else "",
                item.previous_venue or "",
                round(item.score, 4),
                item.contact or "",
                item.subject,
                item.body,
                _GAPS_SEPARATOR.join(item.gaps),
            ]
        )
    return buffer.getvalue()


# ----------------------------------------------------------------- стадия


def run_deliver(
    session: Session,
    config: Config,
    *,
    today: date,
    run_id: str,
    limit: int | None = None,
    out_dir: Path | None = None,
    summary: str | None = None,
) -> tuple[StageResult, list[Path]]:
    """Записать утренний список в out/ и перевести карточки в `delivered`.

    Файлы пишутся до смены статуса: упадёт запись — карточки останутся
    черновиками и уедут в завтрашний список, а не пропадут молча.
    """
    log = get_logger("gtm.deliver")
    candidates = repo.expectations_by_status(session, [ExpectationStatus.DRAFTED.value])
    items, notes = _select(session, config, today=today, limit=limit)

    directory = Path(out_dir) if out_dir else get_settings().output_dir
    md_path = directory / f"{today:%Y-%m-%d}.md"
    csv_path = directory / f"{today:%Y-%m-%d}.csv"

    with stage(session, run_id, STAGE_NAME, count_in=len(candidates)) as result:
        for key, count in notes.items():
            result.note(key, count)

        if not items and md_path.exists():
            # Повторный прогон в тот же день: всё уже delivered. Перезапись
            # пустым списком стёрла бы менеджеру утреннюю работу.
            result.details["skipped"] = "already_delivered"
            log.info("deliver.already_delivered", run_id=run_id, path=str(md_path))
            return result, [p for p in (md_path, csv_path) if p.exists()]

        directory.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(items, today=today, summary=summary), encoding="utf-8")
        csv_path.write_text(render_csv(items), encoding=_CSV_ENCODING)

        for item in items:
            expectation = session.get(Expectation, item.expectation_id)
            if expectation is not None:
                repo.set_status(session, expectation, ExpectationStatus.DELIVERED.value)
            result.bump_out()
        session.flush()

    if not items:
        # Пустой список — не ошибка, но повод посмотреть, где сузилась воронка.
        log.warning("deliver.empty", run_id=run_id, candidates=len(candidates), **notes)
    return result, [md_path, csv_path]
