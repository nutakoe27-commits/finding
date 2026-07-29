"""Архив прошедших мероприятий.

Основа самого сильного сигнала. Компания провела конференцию в ноябре
прошлого года — с высокой вероятностью проведёт и в этом, и написать ей
надо в июле, за четыре месяца, пока площадку ещё не выбрали. Анонс этого
года бесполезен: когда мероприятие объявили публично, зал забронирован
два-четыре месяца назад.

Отсюда и форма работы: сбор за 2-3 года назад — разовая тяжёлая операция
(`backfill`), дальше база работает как календарь и регулярный прогон
догребает только последний год.

Асинхронность здесь ровно из-за бэкфилла: страниц за три года сотни, и
каждая ждёт сеть. Дальше по конвейеру async не идёт — `fetch` синхронный,
он просто оборачивает асинхронный сбор.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field

from gtm.collectors.base import Collector, CollectorError, RawFact, register
from gtm.observability import StageResult, get_logger
from gtm.settings import REPO_ROOT
from gtm.spend import SpendGuard
from gtm.storage.models import FactType

SOURCE_NAME = "events_archive"
TIMEPAD = "timepad"
FIXTURE = "fixture"

# Технические константы: свойства протокола Timepad и файловой фикстуры,
# а не бизнес-параметры. Пороги масштаба и глубина бэкфилла — в config.
_DEFAULT_BASE_URL = "https://api.timepad.ru/v1"
_EVENTS_PATH = "/events"
# Больше 100 событий за запрос Timepad не отдаёт.
_PAGE_LIMIT = 100
# Предохранитель от бесконечной пагинации, если total приедет неадекватным.
_MAX_PAGES = 100
# Столько страниц тянем одновременно. Больше — Timepad начинает отвечать 429,
# меньше — бэкфилл за три года превращается в получасовое ожидание сети.
_MAX_CONCURRENT_PAGES = 4
# 429 и 5xx — временные; на них ретрай осмыслен, на 400/404 — нет.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Поля, которых нет в кратком ответе, но без которых событие бесполезно.
_FIELDS = ("location", "organization", "registration_data", "categories", "ticket_types")
_DEFAULT_USER_AGENT = "gtm-research/0.1"
_DEFAULT_TIMEOUT = 30.0
# Регулярный прогон смотрит на год назад: годовщина считается от прошлогоднего
# мероприятия. Глубже лезет backfill, и то один раз.
_DEFAULT_WINDOW_DAYS = 365
_DEFAULT_BACKFILL_YEARS = 3
# Если порог масштаба в конфиге не задан — берём нижнюю границу ICP.
_MIN_ATTENDEES_FALLBACK = 200


async def _apause(seconds: float) -> None:
    """Отдельной функцией, чтобы тесты не ждали ретраев по-настоящему."""
    if seconds > 0:
        await asyncio.sleep(seconds)


# ------------------------------------------------------------------- запись


class EventRecord(BaseModel):
    """Прошедшее мероприятие до превращения в факт.

    Валидация на границе: дальше по системе едут date и int, а не
    «2025-11-13T10:00:00+0300» и словарь с лимитами билетов.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    source_uid: str = Field(min_length=1, max_length=256)
    title: str
    starts_at: date
    organizer_name: str
    # Ни один архив мероприятий ИНН не отдаёт — сведёт resolve по названию.
    organizer_inn: str | None = None
    attendees: int | None = None
    venue: str | None = None
    city: str = ""
    url: str = ""
    categories: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------- разбор


def _positive_int(value: Any) -> int | None:
    """Ноль и мусор — это «не знаем», а не «ноль человек»."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_starts_at(value: Any) -> date | None:
    """Timepad отдаёт «2025-11-13T10:00:00+0300». Час начала нам не нужен."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def parse_attendees(item: dict[str, Any]) -> int | None:
    """Сколько человек ожидалось на мероприятии.

    Фактическую явку Timepad не публикует. Ближайшее к масштабу — лимит
    регистраций, а если он не выставлен, сумма лимитов по типам билетов.
    Неизвестно — так и оставляем None: занижать масштаб опаснее, чем
    не знать его, потому что по масштабу решается, наш это клиент или нет.
    """
    registration = item.get("registration_data") or {}
    limit = _positive_int(registration.get("tickets_limit"))
    if limit is not None:
        return limit

    total = 0
    for ticket in item.get("ticket_types") or []:
        if not isinstance(ticket, dict):
            continue
        total += _positive_int(ticket.get("limit")) or 0
    return total or None


def _venue(location: dict[str, Any]) -> str | None:
    """Timepad не отделяет название площадки от адреса, и это не мешает:
    нам важно, где компания проводила прошлое мероприятие, в любом виде."""
    address = " ".join(str(location.get("address") or "").split())
    return address or None


def to_event_record(item: dict[str, Any], *, source: str = TIMEPAD) -> EventRecord | None:
    """Событие Timepad — в запись архива. None, если по нему не понять
    организатора или дату: без них ни ожидания, ни письма не построить."""
    if not isinstance(item, dict):
        return None
    event_id = str(item.get("id") or "").strip()
    starts_at = parse_starts_at(item.get("starts_at"))
    organization = item.get("organization") or {}
    organizer = " ".join(str(organization.get("name") or "").split())
    if not event_id or starts_at is None or not organizer:
        return None

    location = item.get("location") or {}
    categories = [
        " ".join(str(category.get("name")).split())
        for category in item.get("categories") or []
        if isinstance(category, dict) and category.get("name")
    ]
    return EventRecord(
        source=source,
        source_uid=f"{source}:{event_id}",
        title=" ".join(str(item.get("name") or "").split()) or f"Мероприятие {event_id}",
        starts_at=starts_at,
        organizer_name=organizer,
        attendees=parse_attendees(item),
        venue=_venue(location),
        city=" ".join(str(location.get("city") or "").split()),
        url=str(item.get("url") or ""),
        categories=categories,
    )


def to_raw_fact(record: EventRecord) -> RawFact:
    """Запись архива — в факт. Дальше по ней строится ожидание годовщины."""
    return RawFact(
        source_uid=record.source_uid,
        fact_type=FactType.PAST_EVENT.value,
        company_name=record.organizer_name,
        inn=record.organizer_inn,
        occurred_at=record.starts_at,
        payload={
            "title": record.title,
            "attendees": record.attendees,
            "venue": record.venue,
            "city": record.city,
            "url": record.url,
            "categories": list(record.categories),
            "organizer_name": record.organizer_name,
            "provider": record.source,
        },
    )


def backfill_windows(today: date, years: int) -> list[tuple[date, date]]:
    """Разбить период бэкфилла на смежные годовые интервалы, старые первыми.

    Одним запросом за три года ходить нельзя: пагинация Timepad уходит на
    сотни страниц, а упавший на середине сбор пришлось бы начинать заново.
    Годовые куски дают и обозримую глубину, и точку перезапуска.
    """
    if years < 1:
        raise ValueError(f"Глубина бэкфилла {years} лет — нечего собирать")
    edges = [today - relativedelta(years=y) for y in range(years, 0, -1)]
    windows: list[tuple[date, date]] = []
    for index, start in enumerate(edges):
        # Конец интервала — день перед началом следующего: без нахлёста,
        # чтобы одно и то же событие не приезжало дважды.
        end = edges[index + 1] - timedelta(days=1) if index + 1 < len(edges) else today
        windows.append((start, end))
    return windows


# --------------------------------------------------------------- провайдеры


class TimepadProvider:
    """Публичный API Timepad: афиша и архив мероприятий.

    Страницы тянутся параллельно с ограничением одновременности. Сортировка
    в запросе задаётся явно: выборка на стороне Timepad живая, и при
    «плавающем» порядке параллельные страницы перемешались бы между собой.
    """

    name = TIMEPAD

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        cities: list[str] | None = None,
        token: str | None = None,
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = 3,
        retry_backoff: float = 2.0,
        guard: SpendGuard | None = None,
        cost_rub: float = 0.0,
        log: Any | None = None,
    ) -> None:
        self.base_url = base_url
        self.cities = list(cities or [])
        self.token = token
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = max(1, retries)
        self.retry_backoff = retry_backoff
        self.guard = guard
        self.cost_rub = cost_rub
        self.log = log or get_logger(f"gtm.collector.{SOURCE_NAME}")

    def fetch(self, *, since: date, until: date) -> list[EventRecord]:
        """Синхронная обёртка: async живёт внутри этого модуля и не дальше."""
        return asyncio.run(self.fetch_async(since=since, until=until))

    async def fetch_async(self, *, since: date, until: date) -> list[EventRecord]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.token:
            # Публичная выборка работает и без токена, с ним выше лимиты.
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=self.timeout
        ) as client:
            # Первая страница нужна не только ради данных: из неё узнаём
            # total и только потом решаем, сколько страниц брать параллельно.
            first = await self._page(client, since=since, until=until, skip=0)
            items = list(first.get("values") or [])
            total = _positive_int(first.get("total")) or len(items)

            skips = list(range(_PAGE_LIMIT, min(total, _PAGE_LIMIT * _MAX_PAGES), _PAGE_LIMIT))
            if skips:
                semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)
                pages = await asyncio.gather(
                    *(
                        self._page_limited(semaphore, client, since=since, until=until, skip=skip)
                        for skip in skips
                    )
                )
                for page in pages:
                    items.extend(page.get("values") or [])

        # Дедуп по id: выборка живая, и на границе страниц одно и то же
        # событие приезжает дважды. gather сохраняет порядок задач, поэтому
        # результат детерминирован.
        records: dict[str, EventRecord] = {}
        for item in items:
            record = to_event_record(item, source=self.name)
            if record is not None:
                records.setdefault(record.source_uid, record)
        self.log.info(
            "timepad.fetched",
            since=since.isoformat(),
            until=until.isoformat(),
            total=total,
            events=len(records),
        )
        return list(records.values())

    # ------------------------------------------------------------- запросы

    async def _page_limited(
        self,
        semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
        *,
        since: date,
        until: date,
        skip: int,
    ) -> dict[str, Any]:
        async with semaphore:
            return await self._page(client, since=since, until=until, skip=skip)

    def _params(self, *, since: date, until: date, skip: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": _PAGE_LIMIT,
            "skip": skip,
            "starts_at_min": since.isoformat(),
            "starts_at_max": until.isoformat(),
            "sort": "+starts_at",
            "fields": ",".join(_FIELDS),
        }
        if self.cities:
            params["cities"] = ",".join(self.cities)
        return params

    def _journal(self, skip: int) -> Any:
        """Запросы к Timepad бесплатны, но в журнал попадают: по нему видно,
        что источник ходил, и он же ловит зациклившийся сбор."""
        if self.guard is None:
            return nullcontext()
        return self.guard.call(
            self.name, _EVENTS_PATH, cost_rub=self.cost_rub, request_key=f"skip={skip}"
        )

    async def _page(
        self, client: httpx.AsyncClient, *, since: date, until: date, skip: int
    ) -> dict[str, Any]:
        params = self._params(since=since, until=until, skip=skip)
        request_key = f"{since:%Y-%m-%d}..{until:%Y-%m-%d}#{skip}"
        last_error = ""

        for attempt in range(1, self.retries + 1):
            try:
                with self._journal(skip):
                    response = await client.get(_EVENTS_PATH, params=params)
            except httpx.HTTPError as exc:
                last_error = f"сеть: {exc}"
            else:
                if response.status_code in _RETRY_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise CollectorError(
                        f"Timepad: HTTP {response.status_code} на запрос "
                        f"{request_key!r} — {response.text[:200]}"
                    )
                else:
                    try:
                        return dict(response.json())
                    except (ValueError, TypeError) as exc:
                        raise CollectorError(
                            f"Timepad вернул не JSON на {request_key!r}: {exc}"
                        ) from exc
            if attempt < self.retries:
                self.log.warning(
                    "timepad.retry", attempt=attempt, key=request_key, error=last_error
                )
                # Линейный откат: нас тормозят лимитом, а не банят.
                await _apause(self.retry_backoff * attempt)

        raise CollectorError(
            f"Timepad не ответил на {request_key!r} за {self.retries} попыток: {last_error}"
        )


class FixtureProvider:
    """Offline: те же мероприятия, но из файла, без единого запроса в сеть.

    Offline — репетиция живого прогона: разбор, окно и отсев по масштабу
    те же самые. Отличается только откуда приехали записи.
    """

    name = FIXTURE

    def __init__(self, *, path: Path, log: Any | None = None) -> None:
        self.path = path
        self.log = log or get_logger(f"gtm.collector.{SOURCE_NAME}")

    def fetch(self, *, since: date, until: date) -> list[EventRecord]:
        records: dict[str, EventRecord] = {}
        for entry in self._entries():
            record = self._to_record(entry)
            if record is None:
                continue
            # Окно фильтруем здесь, а не в коллекторе: провайдер обязан
            # отдавать ровно запрошенный интервал, иначе бэкфилл по годовым
            # кускам собрал бы одно и то же несколько раз.
            if not since <= record.starts_at <= until:
                continue
            records.setdefault(record.source_uid, record)
        return list(records.values())

    def _entries(self) -> list[dict[str, Any]]:
        """Фикстуры может не быть — это не авария: offline включают, чтобы
        прогнать конвейер без внешних источников."""
        if not self.path.exists():
            self.log.warning("events.fixtures_missing", path=str(self.path))
            return []
        files = sorted(self.path.glob("*.json")) if self.path.is_dir() else [self.path]

        entries: list[dict[str, Any]] = []
        for file in files:
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                self.log.warning("events.fixture_unreadable", path=str(file), error=str(exc))
                continue
            items = data.get("events") if isinstance(data, dict) else data
            if not isinstance(items, list):
                self.log.warning("events.fixture_unexpected_shape", path=str(file))
                continue
            entries.extend(item for item in items if isinstance(item, dict))
        return entries

    def _to_record(self, entry: dict[str, Any]) -> EventRecord | None:
        uid = str(entry.get("id") or "").strip()
        starts_at = parse_starts_at(entry.get("starts_at"))
        organizer = " ".join(str(entry.get("organizer_name") or "").split())
        if not uid or starts_at is None or not organizer:
            self.log.warning("events.fixture_row_skipped", uid=uid or None)
            return None
        return EventRecord(
            source=self.name,
            source_uid=f"{self.name}:{uid}",
            title=" ".join(str(entry.get("title") or "").split()) or uid,
            starts_at=starts_at,
            organizer_name=organizer,
            organizer_inn=entry.get("organizer_inn"),
            attendees=_positive_int(entry.get("attendees")),
            venue=entry.get("venue"),
            city=str(entry.get("city") or ""),
            url=str(entry.get("url") or ""),
            categories=[str(c) for c in entry.get("categories") or []],
        )


# --------------------------------------------------------------- коллектор


@register
class EventsArchiveCollector(Collector):
    """Прошедшие мероприятия — сырьё для сигнала «годовщина мероприятия»."""

    name = SOURCE_NAME
    fact_type = FactType.PAST_EVENT.value

    #: сколько мероприятий последнего запуска отсеяно как мелкие
    skipped_small: int = 0

    def collect(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        providers: list[str] | None = None,
    ) -> Iterable[RawFact]:
        window_until = until or date.today()
        window_since = since or window_until - timedelta(days=_DEFAULT_WINDOW_DAYS)
        if window_since > window_until:
            raise CollectorError(
                f"Окно сбора вывернуто: {window_since} позже {window_until}"
            )

        records: dict[str, EventRecord] = {}
        for provider in self._providers(providers):
            for record in provider.fetch(since=window_since, until=window_until):
                records.setdefault(record.source_uid, record)

        threshold = self.min_attendees
        facts: list[RawFact] = []
        self.skipped_small = 0
        for record in records.values():
            # Самый дешёвый фильтр в системе: мелочь отсекается до записи в
            # базу, до сведения к ИНН и до любого платного обогащения.
            # Неизвестный масштаб — не повод отсекать: у части архивов
            # числа нет вовсе, а мероприятие может быть крупным.
            if record.attendees is not None and record.attendees < threshold:
                self.skipped_small += 1
                continue
            facts.append(to_raw_fact(record))

        self.log.info(
            "events.collected",
            events=len(facts),
            too_small=self.skipped_small,
            since=window_since.isoformat(),
            until=window_until.isoformat(),
            mode=self.mode,
        )
        return facts

    def run(self, **kwargs: Any) -> StageResult:
        result = super().run(**kwargs)
        if self.skipped_small:
            # Отсев должен быть виден в сводке запуска, а не только в логе.
            result.note("too_small", self.skipped_small)
        return result

    def backfill(self, *, years: int | None = None) -> StageResult:
        """Разовый тяжёлый сбор за N лет назад.

        Дальше база работает как календарь: годовщины уже посчитаны, и
        регулярному прогону остаётся последний год.
        """
        span = int(years or getattr(self.source_config, "backfill_years", 0) or 0)
        if span < 1:
            span = _DEFAULT_BACKFILL_YEARS
        windows = backfill_windows(date.today(), span)

        result = StageResult(stage=f"backfill:{self.name}")
        for window_since, window_until in windows:
            window = self.run(since=window_since, until=window_until)
            result.bump_in(window.count_in)
            result.bump_out(window.count_out)
            for key, value in window.details.items():
                result.note(key, value if isinstance(value, int) else 1)
        result.note("windows", len(windows))
        self.log.info(
            "events.backfilled",
            years=span,
            windows=len(windows),
            collected=result.count_in,
            new=result.count_out,
        )
        return result

    # ------------------------------------------------------------ настройки

    @property
    def min_attendees(self) -> int:
        """Порог масштаба на весь архив живёт в настройках timepad: это
        единственный живой провайдер, а фикстуры в offline подменяют
        именно его — значит, и отсекать должны одинаково."""
        raw = self._provider_settings(TIMEPAD).get("min_attendees")
        return _positive_int(raw) or _MIN_ATTENDEES_FALLBACK

    def _provider_settings(self, name: str) -> dict[str, Any]:
        providers = getattr(self.source_config, "providers", None) or {}
        settings = providers.get(name) if isinstance(providers, dict) else None
        return dict(settings) if isinstance(settings, dict) else {}

    def _enabled_provider_names(self) -> list[str]:
        providers = getattr(self.source_config, "providers", None) or {}
        if not isinstance(providers, dict):
            return []
        # YAML 1.1 читает `off` как False — ловушка на ровном месте.
        return [
            name
            for name, settings in providers.items()
            if not isinstance(settings, dict)
            or str(settings.get("mode", "live")).lower() not in {"off", "false"}
        ]

    def _fixtures_path(self) -> Path:
        raw = Path(getattr(self.source_config, "fixtures_path", None) or "data/fixtures/events")
        # Пути в sources.yaml заданы от корня проекта.
        return raw if raw.is_absolute() else REPO_ROOT / raw

    def _providers(self, names: list[str] | None) -> list[TimepadProvider | FixtureProvider]:
        if self.mode == "offline":
            return [FixtureProvider(path=self._fixtures_path(), log=self.log)]

        wanted = [n.strip() for n in (names or self._enabled_provider_names()) if n and n.strip()]
        built: list[TimepadProvider | FixtureProvider] = []
        for name in wanted:
            if name == TIMEPAD:
                built.append(self._timepad())
                continue
            # expomap и прочее — парсинг сайтов, у него своя цена аккуратности.
            # Молча пропускать нельзя: иначе источник «работает» и даёт ноль.
            self.log.warning("events.provider_not_implemented", provider=name)
        if not built:
            raise CollectorError(
                f"Для '{SOURCE_NAME}' не осталось ни одного рабочего провайдера "
                f"(просили: {wanted or 'по конфигу'}). Реализован только '{TIMEPAD}'."
            )
        return built

    def _timepad(self) -> TimepadProvider:
        settings = self._provider_settings(TIMEPAD)
        defaults = self.config.sources.defaults
        return TimepadProvider(
            base_url=str(settings.get("base_url") or _DEFAULT_BASE_URL),
            cities=list(settings.get("cities") or []),
            # Токена в Settings нет: публичная выборка работает без него.
            token=getattr(self.settings, "timepad_token", None),
            user_agent=str(defaults.get("user_agent") or _DEFAULT_USER_AGENT),
            timeout=float(defaults.get("timeout_seconds", _DEFAULT_TIMEOUT)),
            retries=max(1, int(defaults.get("retries", 3))),
            retry_backoff=float(defaults.get("retry_backoff_seconds", 2)),
            guard=self.guard,
            cost_rub=self.cost_per_call,
            log=self.log,
        )
