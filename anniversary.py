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

    @property
    def is_open(self) -> bool:
        return self.window_opens <= self.today <= self.window_closes

    @property
    def days_to_open(self) -> int:
        return (self.window_opens - self.today).days

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
    )


# --------------------------------------------------------------------- API


class TimepadError(RuntimeError):
    pass


def fetch_events(
    *,
    token: str | None,
    since: date,
    until: date,
    city: str,
    limit_total: int,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Прошедшие события за период. Постранично, с внятными ошибками."""
    headers = {"User-Agent": "gtm-research/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    collected: list[dict[str, Any]] = []
    skip = 0
    with httpx.Client(timeout=30, headers=headers) as client:
        while len(collected) < limit_total:
            params: dict[str, Any] = {
                "limit": min(PAGE_SIZE, limit_total - len(collected)),
                "skip": skip,
                "cities[]": city,
                "starts_at_min": since.isoformat(),
                "starts_at_max": until.isoformat(),
                "sort": "-starts_at",
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
            values = payload.get("values") if isinstance(payload, dict) else None
            if not values:
                break
            collected.extend(values)
            if verbose:
                total = payload.get("total")
                print(f"получено {len(collected)} из {total or '?'}", file=sys.stderr)
            if len(values) < params["limit"]:
                break
            skip += len(values)
    return collected


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
            years = ", ".join(str(e.starts_at.year) for e in reversed(exp.history))
            lines.append(f"   Повторяется {exp.repeats} г.: {years} — уверенность выше")
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
    parser.add_argument("--max-events", type=int, default=1000, help="потолок на запрос")
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

    try:
        raw_events = fetch_events(
            token=token,
            since=since,
            until=today,
            city=args.city,
            limit_total=args.max_events,
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

    print(f"Событий получено: {len(raw_events)}")
    print(f"Разобрано: {len(parsed)} (у {len(with_scale)} известно число участников)")
    print(f"Крупных (от {args.min_attendees} чел.): {len(big)}")
    if not with_scale and parsed:
        print(
            "\nВНИМАНИЕ: ни у одного события не нашлось числа участников.\n"
            "Скорее всего, поля в ответе называются иначе. Пришлите вывод:\n"
            "  python anniversary.py --raw\n"
        )
    print()

    expectations = rank(build_expectations(big, today=today))
    print(format_table(expectations, today=today))

    if args.csv:
        write_csv(expectations, args.csv)
        print(f"Сохранено: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
