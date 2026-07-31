"""Шаг 1: годовщина мероприятия — самый сильный сигнал.

ЛОГИКА. Компания провела конференцию в ноябре прошлого года — значит с высокой
вероятностью проведёт и в этом. Писать ей надо не когда появится анонс (тогда
площадка уже забронирована два-четыре месяца назад), а за 3-5 месяцев до
ожидаемой даты, пока зал ещё выбирают.

Скрипт делает ровно это и ничего больше: берёт из Timepad прошедшие московские
мероприятия, оставляет крупные, группирует по организатору, считает ожидаемую
дату повторения и печатает, кому и когда писать. Ни базы, ни писем, ни
конвейера — их смысл проверять только после того, как окажется, что список
осмысленный.

ПРО НАДЁЖНОСТЬ ПОЛЕЙ. Точную структуру ответа Timepad я проверить не мог:
из окружения, где писался скрипт, нет доступа в сеть. Поэтому разбор ответа
терпим к вариантам, а ключ `--raw` печатает сырой JSON первого события —
по нему расхождение чинится правкой одной строки в EVENT_FIELDS.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

API_URL = "https://api.timepad.ru/v1/events"

# Timepad отдаёт часть полей только по явному запросу. Если API ответит 400
# на неизвестное поле, скрипт повторит запрос без этого параметра.
EVENT_FIELDS = [
    "id",
    "name",
    "starts_at",
    "url",
    "organization",
    "location",
    "registration_data",
    "categories",
    "description_short",
    "ticket_types",
]

# Максимум записей за один запрос. У Timepad это 100.
PAGE_SIZE = 100

# За сколько дней до ожидаемой даты открывается окно контакта и сколько
# держится. 120 дней — это «написать в июле про ноябрьскую конференцию»:
# площадку ещё не выбрали, но планирование уже началось.
LEAD_DAYS = 120
WINDOW_DAYS = 60

MONTHS_NOM = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


# --------------------------------------------------------------------- модель


@dataclass
class Event:
    """Одно прошедшее мероприятие, приведённое к тому, что нам нужно."""

    event_id: str
    title: str
    starts_at: date
    organizer: str
    organizer_id: str | None
    attendees: int | None
    attendees_source: str
    city: str | None
    url: str | None
    # Категории Timepad — самый точный доступный признак того, деловое это
    # мероприятие или концерт. Точнее любых догадок по названию организатора.
    categories: list[str] = field(default_factory=list)


@dataclass
class Expectation:
    """Ожидание повторения: кому, когда и с какого числа писать."""

    organizer: str
    organizer_id: str | None
    last_event: Event
    repeats: int
    max_attendees: int | None
    expected_at: date
    window_opens: date
    window_closes: date
    # Дата, относительно которой считалось окно. Хранится явно, а не берётся
    # из системных часов: иначе прогон «на заданное число» показывал бы окна
    # относительно сегодня, а тесты ломались бы на следующий день.
    today: date
    history: list[Event] = field(default_factory=list)
    # Другие месяцы этого же организатора — после схлопывания в одну строку.
    other_occasions: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.window_opens <= self.today <= self.window_closes

    @property
    def days_to_open(self) -> int:
        return (self.window_opens - self.today).days

    @property
    def categories(self) -> list[str]:
        return self.last_event.categories

    @property
    def confidence(self) -> float:
        """Повторяемость — единственное, что здесь реально повышает уверенность.

        Одно мероприятие может оказаться разовым. Три в один и тот же месяц
        разных лет — это уже традиция, и она повторится.
        """
        base = 0.7 + 0.1 * (self.repeats - 1)
        return min(base, 0.95)


# ------------------------------------------------------------------- разбор


def _first(source: Any, *paths: str) -> Any:
    """Первое непустое значение по списку путей вида "a.b.c"."""
    for path in paths:
        value: Any = source
        for key in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


# Выше этого числа «заявленные места» — не масштаб, а заглушка: организаторы
# ставят гигантский лимит, когда ограничения нет. В живом прогоне так вылезли
# «270020 человек» на дне открытых дверей. Такое число хуже отсутствующего:
# оно выглядит как факт и попадает в письмо.
IMPLAUSIBLE_ATTENDEES = 20_000


def parse_attendees(raw: dict[str, Any]) -> tuple[int | None, str]:
    """Сколько человек было. Возвращает (число, откуда взято).

    Timepad не отдаёт фактическую посещаемость прошедших событий, поэтому
    берём ближайшее к правде: сколько билетов продано или сколько мест было
    заявлено. Ничего не нашли — None, и в списке это будет честно помечено.
    Выдумывать число нельзя: оно попадёт в письмо и сожжёт лид.
    """
    sold = _first(raw, "registration_data.sold_count", "registration_data.tickets_sold")
    if isinstance(sold, int) and sold > 0:
        return sold, "продано билетов"

    limit = _first(raw, "registration_data.tickets_limit", "registration_data.places_limit")
    if isinstance(limit, int) and limit > 0:
        if limit >= IMPLAUSIBLE_ATTENDEES:
            # Заглушка «лимита нет», а не масштаб. Честнее не знать.
            return None, "лимит не задан"
        return limit, "заявлено мест"

    # Иногда лимит лежит в типах билетов — суммируем.
    ticket_types = raw.get("ticket_types")
    if isinstance(ticket_types, list):
        total = sum(
            t.get("limit") or 0 for t in ticket_types if isinstance(t, dict) and t.get("limit")
        )
        if total > 0:
            return total, "сумма по типам билетов"

    return None, "неизвестно"


def parse_categories(raw: dict[str, Any]) -> list[str]:
    """Названия категорий события.

    Timepad кладёт их списком словарей; берём человекочитаемое имя — по нему
    и фильтруем, потому что числовые id пришлось бы выяснять отдельно.
    """
    values = raw.get("categories")
    if not isinstance(values, list):
        return []
    names = []
    for item in values:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def parse_event(raw: dict[str, Any]) -> Event | None:
    """Событие Timepad -> наша модель. Мусор и события без организатора — None."""
    organizer = _first(raw, "organization.name", "organization.subdomain")
    starts_raw = raw.get("starts_at")
    if not organizer or not starts_raw:
        return None
    try:
        starts_at = datetime.fromisoformat(str(starts_raw)).date()
    except ValueError:
        return None

    attendees, source = parse_attendees(raw)
    return Event(
        event_id=str(raw.get("id") or ""),
        title=str(raw.get("name") or "без названия").strip(),
        starts_at=starts_at,
        organizer=str(organizer).strip(),
        organizer_id=str(_first(raw, "organization.id") or "") or None,
        attendees=attendees,
        attendees_source=source,
        city=_first(raw, "location.city", "location.address"),
        url=raw.get("url"),
        categories=parse_categories(raw),
    )


# --------------------------------------------------------------------- API


class TimepadError(RuntimeError):
    pass


def _shift_years(value: date, years: int) -> date:
    """Сдвиг на календарный год, а не на 365 дней: иначе за пару лет
    накапливается ошибка в сутки и границы сезона уезжают."""
    return date(value.year - years, value.month, min(value.day, 28))


def target_ranges(today: date, years: int, horizon_days: int = 0) -> list[tuple[date, date]]:
    """Периоды прошлых лет, чьё повторение попадает в открытое окно контакта.

    Диапазон выводится из самой механики окна, а не назначается на глаз.
    Окно открыто, когда до ожидаемой даты остаётся от WINDOW_DAYS до LEAD_DAYS
    дней. Значит писать сегодня надо про мероприятия, которые повторятся
    в промежутке [today + 60, today + 120] — а их прошлогодние оригиналы
    лежат ровно в том же промежутке год назад.

    Это два месяца, а не шесть. Разница принципиальная: шестимесячный период
    московских событий не выбрать никаким потолком, и обрезка съедает именно
    тот конец, ради которого всё затевается. Дважды на этом обжигались:
    сперва с сортировкой по убыванию, потом по возрастанию.

    `horizon_days` расширяет дальний край — чтобы заранее видеть, кому писать
    через месяц-другой.
    """
    first = today + timedelta(days=WINDOW_DAYS)
    last = today + timedelta(days=LEAD_DAYS + horizon_days)
    return [(_shift_years(first, k), _shift_years(last, k)) for k in range(1, years + 1)]


def split_into_chunks(since: date, until: date, chunk_days: int) -> list[tuple[date, date]]:
    """Порезать период на куски.

    Потолок применяется к запросу, поэтому единственный способ не потерять
    данные — спрашивать короткими отрезками и по каждому видеть, всё ли забрали.
    """
    chunks = []
    start = since
    while start <= until:
        end = min(start + timedelta(days=chunk_days - 1), until)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def fetch_events(
    *,
    token: str | None,
    ranges: list[tuple[date, date]],
    city: str,
    limit_per_range: int,
    chunk_days: int = 7,
    verbose: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """События за указанные периоды. Возвращает (события, предупреждения).

    Предупреждения — про упёршиеся в потолок периоды. Молчаливое обрезание
    выборки выглядит как «мероприятий столько и есть» и уводит в неверные
    выводы: именно на этом первый прогон и показал две недели вместо двух лет.
    """
    headers = {"User-Agent": "gtm-research/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    collected: list[dict[str, Any]] = []
    warnings: list[str] = []
    with httpx.Client(timeout=30, headers=headers) as client:
        for since, until in ranges:
            chunks = split_into_chunks(since, until, chunk_days)
            fetched_here = 0
            available_here = 0
            missed_chunks = 0
            for chunk_since, chunk_until in chunks:
                got, total = _fetch_range(
                    client,
                    since=chunk_since,
                    until=chunk_until,
                    city=city,
                    limit_total=limit_per_range,
                    token=token,
                    verbose=verbose,
                )
                collected.extend(got)
                fetched_here += len(got)
                # `total` API сообщает сам — это единственный честный ответ
                # на вопрос «всё ли мы забрали». Без него обрезанная выборка
                # выглядит как «столько мероприятий и есть».
                available_here += total if total is not None else len(got)
                if total is not None and len(got) < total:
                    missed_chunks += 1
                if verbose:
                    print(
                        f"  {chunk_since}..{chunk_until}: {len(got)} из {total}",
                        file=sys.stderr,
                    )
            coverage = 100 * fetched_here / available_here if available_here else 100
            line = (
                f"период {since}..{until}: получено {fetched_here} из {available_here} "
                f"({coverage:.0f}% охвата, кусков {len(chunks)})"
            )
            if missed_chunks:
                line += (
                    f" — НЕ ВСЁ: в {missed_chunks} кусках упёрлись в потолок, "
                    "уменьшите --chunk-days или поднимите --max-events"
                )
            warnings.append(line)
    return collected, warnings


def _fetch_range(
    client: httpx.Client,
    *,
    since: date,
    until: date,
    city: str,
    limit_total: int,
    token: str | None,
    verbose: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    """Вернуть (события, сколько их всего по мнению API)."""
    collected: list[dict[str, Any]] = []
    reported_total: int | None = None
    skip = 0
    while len(collected) < limit_total:
        params: dict[str, Any] = {
            "limit": min(PAGE_SIZE, limit_total - len(collected)),
            "skip": skip,
            "cities[]": city,
            "starts_at_min": since.isoformat(),
            "starts_at_max": until.isoformat(),
            # По возрастанию: период узкий, и так пагинация идёт от начала
            # сезона к концу, ничего не теряя на границе.
            "sort": "starts_at",
            "fields[]": EVENT_FIELDS,
        }
        response = client.get(API_URL, params=params)

        # Неизвестное поле — единственная ошибка, которую лечим сами:
        # состав fields[] мог измениться, а всё остальное в запросе верно.
        if response.status_code == 400 and "fields[]" in params:
            if verbose:
                print("API не принял fields[], повторяю без них", file=sys.stderr)
            params.pop("fields[]")
            response = client.get(API_URL, params=params)

        if response.status_code in (401, 403):
            # Текст ответа здесь важнее любых наших предположений: это
            # единственное место, где API сам объясняет, что ему не так.
            body = response.text.strip()[:500] or "(пустой ответ)"
            sent_token = "да" if token else "нет"
            raise TimepadError(
                f"Timepad ответил {response.status_code}.\n"
                f"Токен отправлялся: {sent_token}\n"
                f"Ответ API: {body}\n\n"
                "Что делать:\n"
                "  1. Проверить, дело ли в токене вообще:\n"
                '     curl -sS -i "https://api.timepad.ru/v1/events?limit=1" | head -25\n'
                "  2. Если и там отказ — нужен личный токен (dev.timepad.ru),\n"
                "     передать через --token <токен> или TIMEPAD_TOKEN.\n"
                "  3. Если без токена приходит 200, а с нашим запросом нет —\n"
                "     дело в параметрах запроса, пришлите этот вывод."
            )
        if response.status_code != 200:
            raise TimepadError(
                f"Timepad ответил {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        # `total` — сколько событий по этому запросу есть у API вообще.
        # Единственный честный ответ на вопрос «всё ли мы забрали».
        if isinstance(payload, dict) and isinstance(payload.get("total"), int):
            reported_total = payload["total"]
        values = payload.get("values") if isinstance(payload, dict) else None
        if not values:
            break
        collected.extend(values)
        if len(values) < params["limit"]:
            break
        skip += len(values)
    return collected, reported_total


def diagnose(*, token: str | None, city: str, since: date, until: date) -> int:
    """Три запроса по нарастающей: где именно ломается.

    Отделяет «API вообще требует токен» от «наш конкретный запрос не нравится».
    Без этого разделения непонятно, идти за токеном или чинить параметры.
    """
    probes: list[tuple[str, dict[str, Any], dict[str, str]]] = [
        ("простейший запрос, без токена", {"limit": 1}, {}),
    ]
    if token:
        probes.append(
            (
                "простейший запрос, с токеном",
                {"limit": 1},
                {"Authorization": f"Bearer {token}"},
            )
        )
    full = {
        "limit": 1,
        "cities[]": city,
        "starts_at_min": since.isoformat(),
        "starts_at_max": until.isoformat(),
        "sort": "-starts_at",
        "fields[]": EVENT_FIELDS,
    }
    probes.append(
        (
            "наш полный запрос" + (" с токеном" if token else " без токена"),
            full,
            {"Authorization": f"Bearer {token}"} if token else {},
        )
    )

    print(f"Токен: {'задан' if token else 'НЕ задан'}\n")
    ok_any = False
    for label, params, extra in probes:
        headers = {"User-Agent": "gtm-research/0.1", **extra}
        try:
            with httpx.Client(timeout=30, headers=headers) as client:
                response = client.get(API_URL, params=params)
        except httpx.HTTPError as exc:
            print(f"[сеть не дошла] {label}: {exc}")
            continue
        mark = "OK " if response.status_code == 200 else "ОТКАЗ"
        print(f"[{mark}] {label}: HTTP {response.status_code}")
        if response.status_code == 200:
            ok_any = True
            payload = response.json()
            total = payload.get("total") if isinstance(payload, dict) else None
            print(f"        событий всего по запросу: {total}")
        else:
            print(f"        ответ: {response.text.strip()[:300] or '(пусто)'}")
        print()

    if not ok_any:
        print(
            "Ни один запрос не прошёл — дело не в наших параметрах, а в доступе.\n"
            "Нужен токен: dev.timepad.ru, затем --token <токен> либо TIMEPAD_TOKEN."
        )
        return 2
    print("Хотя бы один запрос прошёл. Если полный запрос при этом отказал —\n"
          "дело в параметрах, пришлите этот вывод, поправлю.")
    return 0


# Организаторы, которые сами являются площадками или прокатчиками: у них свой
# зал, арендовать наш они не будут. На Timepad их много — платформа билетная,
# и её основная аудитория как раз культурные центры, кино и театры.
VENUE_MARKERS = (
    # площадки и учреждения культуры
    "кинотеатр", "театр", "музей", "галере", "библиотек", "историчк", "культурн",
    "дом культуры", "дом книги", "книжн", "клуб", "парк", "лектори", "филармони",
    "консерватор", "усадьб", "храм", "цирк", "планетари", "зоопарк", "концертн",
    "арена", "стадион", "выставочн", "экспоцентр", "конгресс", "коворкинг",
    "лофт", "площадк", "особняк", "центр им", "дворец",
    # кино: прокатчики и школы устраивают показы, а не корпоративы
    "кино", "фильмофонд", "киношкол", "киноклуб", "прокатн",
    # образование и просвещение — их мероприятия это лекции, не корпоративы
    "школа", "курсы", "академи", "институт", "университет", "лекторий",
    "гимнази", "колледж", "цдт", "гбоу", "приемная комиссия", "вгик",
    # ДК, КДЦ и концертные организации — протекли в живом прогоне
    "дк ", "кдц", "москонцерт", "палата", "мастерская", "хор", "оркестр",
    "спектакл", "филармон", "дом кино", "кинематограф",
    # фестивали как организаторы — это событие, а не компания-заказчик
    "фестиваль", "fest", "выставка",
)


def looks_like_venue(organizer: str) -> bool:
    """Похоже ли, что организатор — сам площадка, а не заказчик.

    Эвристика грубая и намеренно не приговор: организаторы помечаются, а не
    выбрасываются молча. Но без неё список забивают культурные центры и
    кинотеатры, у которых «330 мест» — это вместимость их собственного зала,
    а не масштаб корпоративного мероприятия.
    """
    lowered = organizer.lower()
    return any(marker in lowered for marker in VENUE_MARKERS)


# ------------------------------------------------------------- ожидания


def next_anniversary(last: date, today: date) -> date:
    """Та же дата через год. Если она уже прошла — ещё через год.

    Ожидание всегда смотрит вперёд: писать про мероприятие, которое уже
    состоялось, смысла нет.
    """
    candidate = date(last.year + 1, last.month, min(last.day, 28))
    while candidate < today:
        candidate = date(candidate.year + 1, candidate.month, candidate.day)
    return candidate


def build_expectations(events: list[Event], *, today: date) -> list[Expectation]:
    """Сгруппировать по организатору и месяцу, посчитать ожидаемое повторение.

    Группировка именно по месяцу, а не просто по организатору: компания может
    проводить и весеннюю конференцию, и декабрьский корпоратив — это два разных
    повода и два разных письма.
    """
    groups: dict[tuple[str, int], list[Event]] = {}
    for event in events:
        groups.setdefault((event.organizer, event.starts_at.month), []).append(event)

    expectations: list[Expectation] = []
    for (organizer, _month), group in groups.items():
        group.sort(key=lambda e: e.starts_at, reverse=True)
        last = group[0]
        expected = next_anniversary(last.starts_at, today)
        opens = expected - timedelta(days=LEAD_DAYS)
        known = [e.attendees for e in group if e.attendees]
        expectations.append(
            Expectation(
                organizer=organizer,
                organizer_id=last.organizer_id,
                last_event=last,
                # Повтор — это мероприятие того же организатора в том же месяце
                # разных лет. Два события одного года традицией не считаются.
                repeats=len({e.starts_at.year for e in group}),
                max_attendees=max(known) if known else None,
                expected_at=expected,
                window_opens=opens,
                window_closes=min(expected, opens + timedelta(days=WINDOW_DAYS)),
                today=today,
                history=group,
            )
        )
    return expectations


def dedupe_by_organizer(expectations: list[Expectation]) -> list[Expectation]:
    """Одна компания — одна строка.

    Группировка по месяцу верна по смыслу: весенний форум и декабрьский
    корпоратив — разные поводы. Но для списка, по которому ищут контакты,
    три строки одной компании это шум. Оставляем ближайшее по окну и самое
    крупное, остальные месяцы уходят в примечание.
    """
    best: dict[str, Expectation] = {}
    extra: dict[str, list[str]] = {}
    for exp in expectations:
        key = exp.organizer_id or exp.organizer
        current = best.get(key)
        if current is None:
            best[key] = exp
            continue
        # Открытое окно важнее размера: писать надо тому, кому пора.
        better = (exp.is_open, exp.max_attendees or 0) > (
            current.is_open,
            current.max_attendees or 0,
        )
        loser, winner = (current, exp) if better else (exp, current)
        best[key] = winner
        extra.setdefault(key, []).append(
            f"{MONTHS_NOM[loser.expected_at.month - 1]} {loser.expected_at.year}"
        )
    for key, months in extra.items():
        best[key].other_occasions = sorted(set(months))
    return list(best.values())


def category_histogram(expectations: list[Expectation]) -> list[tuple[str, int]]:
    """Какие категории встречаются и сколько раз.

    Нужно, чтобы подобрать фильтр по фактам, а не на глаз: один прогон
    показывает словарь категорий, дальше он превращается в --categories.
    """
    counts: dict[str, int] = {}
    for exp in expectations:
        for name in exp.categories or ["(без категории)"]:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def filter_by_categories(
    expectations: list[Expectation], keep: list[str]
) -> list[Expectation]:
    """Оставить только те, у кого категория совпадает с одной из указанных."""
    wanted = [k.strip().lower() for k in keep if k.strip()]
    if not wanted:
        return expectations
    return [
        exp
        for exp in expectations
        if any(w in c.lower() for c in exp.categories for w in wanted)
    ]


def rank(expectations: list[Expectation]) -> list[Expectation]:
    """Сначала те, кому писать пора, потом по масштабу и повторяемости."""
    return sorted(
        expectations,
        key=lambda e: (
            not e.is_open,
            -(e.max_attendees or 0),
            -e.repeats,
            e.window_opens,
        ),
    )


# ------------------------------------------------------------------- вывод


def format_table(expectations: list[Expectation], *, today: date) -> str:
    if not expectations:
        return "Ничего не найдено. Проверьте период, город и порог по числу участников."

    lines = []
    open_now = [e for e in expectations if e.is_open]
    lines.append(f"Найдено ожиданий: {len(expectations)}, из них писать пора: {len(open_now)}")
    lines.append("")

    for index, exp in enumerate(expectations, start=1):
        last = exp.last_event
        scale = (
            f"~{exp.max_attendees} чел. ({last.attendees_source})"
            if exp.max_attendees
            else "масштаб неизвестен"
        )
        when = f"{MONTHS_NOM[exp.expected_at.month - 1]} {exp.expected_at.year}"
        if exp.is_open:
            timing = "ПИСАТЬ СЕЙЧАС"
        elif exp.days_to_open > 0:
            timing = f"писать через {exp.days_to_open} дн. (с {exp.window_opens})"
        else:
            timing = f"окно закрылось {exp.window_closes}"

        lines.append(f"{index}. {exp.organizer}  [{timing}]")
        lines.append(f"   Было: «{last.title}» — {last.starts_at}, {scale}")
        if exp.repeats > 1:
            years = ", ".join(sorted({str(e.starts_at.year) for e in exp.history}))
            lines.append(f"   Повторяется {exp.repeats} г.: {years} — уверенность выше")
        if exp.other_occasions:
            lines.append(f"   Ещё мероприятия этой компании: {', '.join(exp.other_occasions)}")
        if exp.categories:
            lines.append(f"   Категории: {', '.join(exp.categories)}")
        lines.append(f"   Ожидаем: {when}. Уверенность {exp.confidence:.2f}")
        if last.url:
            lines.append(f"   {last.url}")
        lines.append("")
    return "\n".join(lines)


def write_csv(expectations: list[Expectation], path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "организатор", "прошлое_мероприятие", "дата", "участников",
                "источник_числа", "ожидаем", "писать_с", "окно_до",
                "повторов", "уверенность", "ссылка",
            ]
        )
        for exp in expectations:
            writer.writerow(
                [
                    exp.organizer,
                    exp.last_event.title,
                    exp.last_event.starts_at,
                    exp.max_attendees or "",
                    exp.last_event.attendees_source,
                    exp.expected_at,
                    exp.window_opens,
                    exp.window_closes,
                    exp.repeats,
                    f"{exp.confidence:.2f}",
                    exp.last_event.url or "",
                ]
            )


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Шаг 1: кто проводил крупные мероприятия и когда им писать",
    )
    parser.add_argument("--token", help="токен Timepad (или переменная TIMEPAD_TOKEN)")
    parser.add_argument("--years", type=int, default=2, help="за сколько лет назад смотреть")
    parser.add_argument("--city", default="Москва")
    parser.add_argument(
        "--min-attendees",
        type=int,
        default=250,
        help="ниже этого порога мероприятие не наше: там мы проигрываем по локации",
    )
    parser.add_argument(
        "--keep-unknown",
        action="store_true",
        help="оставлять события с неизвестным числом участников (по умолчанию отбрасываются)",
    )
    parser.add_argument(
        "--max-events", type=int, default=2000, help="потолок событий на один кусок периода"
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=7,
        help="длина куска, которым нарезается период: короче — меньше риск обрезки",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=0,
        help="заглянуть дальше открытого окна на N дней (кому писать через месяц-другой)",
    )
    parser.add_argument(
        "--categories",
        help="оставить только эти категории Timepad, через запятую "
             "(сначала посмотрите --show-categories)",
    )
    parser.add_argument(
        "--show-categories",
        action="store_true",
        help="напечатать, какие категории встречаются и как часто — по ним подбирается фильтр",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="не схлопывать несколько мероприятий одной компании в одну строку",
    )
    parser.add_argument(
        "--keep-venues",
        action="store_true",
        help="оставить организаторов-площадок (кинотеатры, ДК) — по умолчанию отброшены",
    )
    parser.add_argument(
        "--full-archive",
        action="store_true",
        help="тянуть весь период подряд вместо целевых сезонов (медленно и обрезается)",
    )
    parser.add_argument(
        "--today",
        help="считать окна на заданную дату ГГГГ-ММ-ДД (для проверки, по умолчанию сегодня)",
    )
    parser.add_argument("--csv", help="сохранить результат в CSV")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="напечатать сырой JSON первого события и выйти — для сверки полей",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="диагностика доступа: три запроса по нарастающей, видно где ломается",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    import os

    token = args.token or os.environ.get("TIMEPAD_TOKEN")
    # --today принимает календарную дату, а не момент времени: часовой пояс
    # тут не при чём, поэтому naive-разбор здесь осознанный.
    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date()  # noqa: DTZ007
        if args.today
        else datetime.now().astimezone().date()
    )
    since = date(today.year - args.years, today.month, 1)

    if args.check:
        return diagnose(token=token, city=args.city, since=since, until=today)

    ranges = (
        [(since, today)]
        if args.full_archive
        else target_ranges(today, args.years, args.horizon)
    )
    try:
        raw_events, warnings = fetch_events(
            token=token,
            ranges=ranges,
            city=args.city,
            limit_per_range=args.max_events,
            chunk_days=args.chunk_days,
            verbose=args.verbose,
        )
    except TimepadError as exc:
        print(f"\nОШИБКА: {exc}\n", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"\nОШИБКА СЕТИ: {exc}\n", file=sys.stderr)
        return 3

    if args.raw:
        if not raw_events:
            print("API не вернул ни одного события — проверьте период и город.")
            return 1
        print(json.dumps(raw_events[0], ensure_ascii=False, indent=2))
        return 0

    parsed = [e for e in (parse_event(r) for r in raw_events) if e is not None]
    with_scale = [e for e in parsed if e.attendees is not None]
    big = [e for e in parsed if e.attendees and e.attendees >= args.min_attendees]
    if args.keep_unknown:
        big += [e for e in parsed if e.attendees is None]

    venues = [e for e in big if looks_like_venue(e.organizer)]
    if not args.keep_venues:
        big = [e for e in big if not looks_like_venue(e.organizer)]

    print("Периоды запроса: " + ", ".join(f"{a}..{b}" for a, b in ranges))
    print(f"Событий получено: {len(raw_events)}")
    print(f"Разобрано: {len(parsed)} (у {len(with_scale)} известно число участников)")
    print(f"Крупных (от {args.min_attendees} чел.): {len(big) + len(venues)}")
    if venues:
        action = "оставлены" if args.keep_venues else "отброшены"
        print(f"  из них площадок и прокатчиков: {len(venues)} — {action} (--keep-venues)")
    for warning in warnings:
        print(f"  {warning}")
    if not with_scale and parsed:
        print(
            "\nВНИМАНИЕ: ни у одного события не нашлось числа участников.\n"
            "Скорее всего, поля в ответе называются иначе. Пришлите вывод:\n"
            "  python anniversary.py --raw\n"
        )
    print()

    expectations = build_expectations(big, today=today)

    if args.show_categories:
        print("Категории среди крупных мероприятий (по убыванию частоты):\n")
        for name, count in category_histogram(expectations):
            print(f"  {count:5}  {name}")
        print("\nОтобрать деловые можно так:")
        print("  python anniversary.py --categories 'бизнес,ит,конференц'")
        return 0

    if args.categories:
        before = len(expectations)
        expectations = filter_by_categories(expectations, args.categories.split(","))
        print(f"Фильтр по категориям: {before} -> {len(expectations)}\n")

    if not args.no_dedupe:
        expectations = dedupe_by_organizer(expectations)

    expectations = rank(expectations)
    print(format_table(expectations, today=today))

    if args.csv:
        write_csv(expectations, args.csv)
        print(f"Сохранено: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
