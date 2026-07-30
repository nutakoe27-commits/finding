"""История мероприятий со страниц площадок-конкурентов.

ЗАЧЕМ ЭТО ГЛАВНЫЙ БЕСПЛАТНЫЙ ИСТОЧНИК. Страницы «прошедшие мероприятия»,
«наши клиенты» и фотоотчёты у крупных московских залов — готовый список
компаний, проводивших большие события в Москве, с масштабом и датами.
Отдел маркетинга конкурента уже отобрал за нас нужный сегмент, и отсюда
берётся самый сильный сигнал системы: годовщина мероприятия.

ПОЧЕМУ ЭВРИСТИКИ, А НЕ СЕЛЕКТОРЫ ПОД КАЖДЫЙ САЙТ. Селекторы под конкретную
вёрстку точнее, но их полтора десятка, они ломаются на каждом редизайне,
и написать их, не видя страниц, нельзя. Эвристики по тексту переживают
редизайн и деградируют предсказуемо: находят меньше, а не врут больше.
Шум здесь не опасен — всё, что не свелось к компании по ИНН, уходит
в карантин на ручной разбор, а не в письма.

ПРО ВЕЖЛИВОСТЬ. Обходим только публичные страницы, читаем robots.txt,
держим пол-запроса в секунду и указываем контакт в User-Agent. Архив
собирается раз в квартал, спешить некуда, а нагрузка на чужой сайт должна
быть незаметной.

HTML разбирается на stdlib html.parser: нужны только текст и ссылки,
ради этого тащить внешний парсер незачем.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from gtm.collectors.base import Collector, RawFact, register
from gtm.config import VenueSite
from gtm.storage.models import FactType

# Теги, содержимое которых в текст не идёт.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head"})

# Ссылки с такими словами в адресе или подписи ведут к спискам мероприятий.
_EVENT_LINK_RE = re.compile(
    r"event|meropriyat|meropriat|конферен|клиент|client|portfolio|case|kejs|"
    r"проект|project|otchet|отчет|отчёт|photo|foto|галере|galer|news|novosti",
    re.IGNORECASE,
)

# Организационно-правовые формы — самый надёжный признак названия компании
# в свободном тексте.
_COMPANY_RE = re.compile(
    r"(?P<opf>ООО|ОАО|АО|ПАО|ЗАО|НАО|ГК|Группа компаний|Холдинг|Концерн|Корпорация)\s+"
    # В кавычках — берём всё до закрывающей: «Синий Кит» это одно название,
    # а нежадное совпадение обрезало бы его до «Синий».
    r"(?:[«\"'](?P<quoted>[^»\"']{2,60})[»\"']"
    # Без кавычек — подряд идущие слова с большой буквы: «ООО Ромашка Трейд».
    # Следующее слово со строчной («провело», «организовало») уже не название.
    r"|(?P<plain>[А-ЯЁA-Z][\w\-.]*(?:[\s-]+[А-ЯЁA-Z][\w\-.]*){0,3}))",
    re.UNICODE,
)
# Название в кавычках-ёлочках без ОПФ: «Ромашка-Трейд» провела конференцию.
_QUOTED_RE = re.compile(r"«([А-ЯЁA-Z][^»]{2,60})»")

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}
_MONTH_RE = re.compile("|".join(sorted(_MONTHS, key=len, reverse=True)), re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")

# «на 800 человек», «800 участников», «более 1200 гостей»
_ATTENDEES_RE = re.compile(
    r"(?:на|более|свыше|около|до)?\s*(\d[\d\s ]{1,6})\s*"
    r"(?:человек|участник\w*|гост\w*|персон\w*|делегат\w*|зрител\w*)",
    re.IGNORECASE,
)
# Ловушки: площадь и деньги числами того же порядка.
_TRAP_RE = re.compile(r"\d[\d\s ]{1,9}\s*(?:кв\.?\s*м|м2|м²|руб|₽|тыс|млн)", re.IGNORECASE)


class _TextAndLinks(HTMLParser):
    """Текст и ссылки страницы. Больше от HTML ничего не нужно."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        # Блочные теги дают перевод строки: иначе соседние карточки
        # мероприятий склеятся в одну строку и разъедутся эвристики.
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._anchor).strip()))
            self._href = None
            self._anchor = []
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # Переводы строк внутри одного текстового узла схлопываем: в исходнике
        # HTML карточка мероприятия часто разбита на строки для читаемости,
        # и без этого одна карточка разъезжается на две — дата отрывается
        # от названия компании. Границы строк дают блочные теги, не отступы.
        cleaned = " ".join(data.split())
        if cleaned:
            self.chunks.append(cleaned + (" " if data[-1:].isspace() else ""))
        elif data:
            # Узел из одних пробелов всё равно разделяет слова соседних тегов.
            self.chunks.append(" ")
        if self._href is not None and cleaned:
            self._anchor.append(cleaned)

    @property
    def text(self) -> str:
        raw = unescape("".join(self.chunks))
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


@dataclass
class PageResult:
    """Итог одной страницы — он же строка отчёта для режима --check."""

    url: str
    status: int | None = None
    error: str | None = None
    text_length: int = 0
    events: int = 0

    def summary(self) -> str:
        if self.error:
            return f"{self.url} — ошибка: {self.error}"
        return f"{self.url} — {self.status}, текста {self.text_length}, событий {self.events}"


@dataclass
class SiteReport:
    venue: str
    pages: list[PageResult] = field(default_factory=list)

    @property
    def events(self) -> int:
        return sum(page.events for page in self.pages)


def parse_attendees(text: str) -> int | None:
    """Число участников из фразы. Площадь и рубли за участников не берём."""
    for match in _ATTENDEES_RE.finditer(text):
        # Проверяем, не попало ли совпадение внутрь «800 кв. м» или «800 тыс. руб».
        window = text[match.start() : match.end() + 12]
        if _TRAP_RE.search(window):
            continue
        digits = match.group(1).replace(" ", "").replace(" ", "")
        if not digits.isdigit():
            continue
        value = int(digits)
        # Ниже десяти — обычно номер зала или этаж; выше 100 000 — не мероприятие.
        if 10 <= value <= 100_000:
            return value
    return None


def parse_event_date(text: str, *, today: date | None = None) -> tuple[date, str] | None:
    """Дата мероприятия из текста. Точность — день, месяц или год.

    Год без месяца тоже годится: для сигнала годовщины важен месяц, а если
    и его нет, ожидание построится с грубой точностью и низким приоритетом.
    """
    today = today or date.today()
    year_match = _YEAR_RE.search(text)
    if not year_match:
        return None
    year = int(year_match.group(1))
    if year > today.year:
        return None
    month_match = _MONTH_RE.search(text)
    if not month_match:
        return date(year, 1, 1), "year"
    key = next(k for k in _MONTHS if month_match.group(0).lower().startswith(k[:4]))
    month = _MONTHS[key]
    day_match = re.search(r"(?<!\d)([12]?\d|3[01])\s*" + re.escape(month_match.group(0)), text)
    if day_match:
        try:
            return date(year, month, int(day_match.group(1))), "day"
        except ValueError:
            pass
    return date(year, month, 1), "month"


def company_names(text: str) -> list[str]:
    """Названия компаний в строке. Сначала с ОПФ — они надёжнее."""
    found: list[str] = []
    for match in _COMPANY_RE.finditer(text):
        core = (match.group("quoted") or match.group("plain") or "").strip(" .,-")
        if len(core) < 2:
            continue
        name = f"{match.group('opf')} {core}"
        if name not in found:
            found.append(name)
    if not found:
        for match in _QUOTED_RE.finditer(text):
            candidate = match.group(1).strip(" .,")
            # Кавычки в тексте чаще обрамляют название мероприятия, а не
            # компании, поэтому берём только если рядом нет слов о событии.
            if not re.search(r"конференц|форум|саммит|фестивал|премия", candidate, re.I):
                found.append(candidate)
    return found


def extract_events(text: str, *, min_attendees: int = 0) -> list[dict]:
    """Кандидаты в мероприятия из текста страницы.

    Работаем построчно: карточка мероприятия на такой странице почти всегда
    укладывается в одну-две строки, а межстрочный контекст даёт больше ложных
    связок, чем находок.
    """
    events: list[dict] = []
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if len(line) < 12:
            continue
        names = company_names(line)
        if not names:
            continue
        # Число участников и дата часто в соседней строке — смотрим окно.
        window = " ".join(lines[index : index + 2])
        attendees = parse_attendees(window)
        parsed = parse_event_date(window)
        if parsed is None and attendees is None:
            # Ни даты, ни масштаба — это просто упоминание компании, не событие.
            continue
        if attendees is not None and attendees < min_attendees:
            continue
        occurred_at, precision = parsed if parsed else (None, None)
        for name in names[:2]:
            events.append(
                {
                    "company_name": name,
                    "occurred_at": occurred_at,
                    "precision": precision,
                    "attendees": attendees,
                    "line": line[:300],
                }
            )
    return events


@register
class VenuePagesCollector(Collector):
    """Обход страниц площадок-конкурентов и отраслевых каталогов."""

    name = "venue_pages"
    fact_type = FactType.PAST_EVENT

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reports: list[SiteReport] = []
        self._robots: dict[str, RobotFileParser | None] = {}
        self._pages_fetched = 0

    @property
    def crawl(self):
        return self.config.venues.crawl

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": self.crawl.user_agent},
            timeout=self.crawl.timeout_seconds,
            follow_redirects=True,
        )

    def _pause(self) -> None:
        rps = max(self.crawl.rate_limit_rps, 0.01)
        time.sleep(1.0 / rps)

    def _allowed(self, client: httpx.Client, url: str) -> bool:
        """robots.txt. Недоступный robots.txt трактуем как разрешение —
        так же поступают обычные краулеры, и это не обход запрета."""
        if not self.crawl.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                response = client.get(f"{root}/robots.txt")
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None
            except httpx.HTTPError:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        return True if parser is None else parser.can_fetch(self.crawl.user_agent, url)

    def _skip(self, url: str) -> bool:
        return any(fragment in url for fragment in self.config.venues.skip_domains)

    def _start_urls(self, site: VenueSite) -> list[str]:
        if site.pages:
            return list(site.pages)
        base = site.site.rstrip("/")
        return [base] + [base + path for path in self.crawl.discover_paths]

    def _crawl_site(
        self, client: httpx.Client, site: VenueSite, *, check_only: bool
    ) -> tuple[SiteReport, list[RawFact]]:
        report = SiteReport(venue=site.name)
        facts: list[RawFact] = []
        min_attendees = self.config.icp.scale.min_attendees
        queue: list[tuple[str, int]] = [(url, 0) for url in self._start_urls(site)]
        visited: set[str] = set()

        while queue and len(visited) < self.crawl.max_pages_per_site:
            if self._pages_fetched >= self.crawl.max_pages_total:
                self.log.warning(
                    "venue_pages.budget_reached",
                    fetched=self._pages_fetched,
                    limit=self.crawl.max_pages_total,
                    venue=site.name,
                )
                # Явно, а не молча: иначе недобранные площадки выглядят как
                # «на их сайтах ничего нет».
                report.pages.append(
                    PageResult(url=site.site, error="лимит страниц на прогон исчерпан")
                )
                break
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            page = PageResult(url=url)
            if self._skip(url) or not self._allowed(client, url):
                page.error = "пропущено (robots.txt или стоп-лист)"
                report.pages.append(page)
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                page.error = type(exc).__name__
                report.pages.append(page)
                self._pause()
                continue
            page.status = response.status_code
            self._pages_fetched += 1
            self._pause()
            if response.status_code != 200:
                report.pages.append(page)
                continue

            parser = _TextAndLinks()
            parser.feed(response.text)
            text = parser.text
            page.text_length = len(text)

            events = extract_events(text, min_attendees=min_attendees)
            page.events = len(events)
            report.pages.append(page)

            if not check_only:
                facts.extend(self._to_facts(site, url, events))

            if depth < self.crawl.max_depth:
                for href, anchor in parser.links:
                    if not _EVENT_LINK_RE.search(f"{href} {anchor}"):
                        continue
                    absolute = urljoin(url, href)
                    if urlparse(absolute).netloc != urlparse(site.site).netloc:
                        continue
                    if absolute not in visited:
                        queue.append((absolute.split("#")[0], depth + 1))
        return report, facts

    def _to_facts(self, site: VenueSite, url: str, events: Iterable[dict]) -> list[RawFact]:
        facts: list[RawFact] = []
        for event in events:
            # source_uid из площадки, названия компании и даты: страница может
            # переехать по адресу, а запись останется той же.
            occurred = event["occurred_at"]
            key = f"{site.name}|{event['company_name']}|{occurred or 'nodate'}"
            # hashlib, а не встроенный hash(): тот рандомизирован между
            # запусками, и повторный обход создавал бы дубли вместо no-op.
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            facts.append(
                RawFact(
                    source_uid=f"venue:{digest}",
                    fact_type=FactType.PAST_EVENT.value,
                    company_name=event["company_name"],
                    occurred_at=occurred,
                    payload={
                        "venue": site.name,
                        "attendees": event["attendees"],
                        "precision": event["precision"],
                        "city": "Москва",
                        "url": url,
                        "excerpt": event["line"],
                        "provider": "venue_pages",
                    },
                )
            )
        return facts

    def collect(self, *, check: bool = False, only: str | None = None) -> Iterator[RawFact]:
        targets = self.config.venues.targets()
        if only:
            targets = [site for site in targets if only.lower() in site.name.lower()]
        if not targets:
            self.log.warning("venue_pages.no_targets", reason="config/venues.yaml пуст")
            return

        with self._client() as client:
            for site in targets:
                report, facts = self._crawl_site(client, site, check_only=check)
                self.reports.append(report)
                self.log.info(
                    "venue_pages.site",
                    venue=site.name,
                    pages=len(report.pages),
                    events=report.events,
                )
                yield from facts

    def check_report(self) -> str:
        """Отчёт для сверки адресов из config/venues.yaml с реальностью."""
        lines: list[str] = []
        for report in self.reports:
            lines.append(f"\n{report.venue}: событий {report.events}")
            for page in report.pages:
                lines.append(f"  {page.summary()}")
            if not report.events:
                lines.append("  ВНИМАНИЕ: ни одного события — проверьте адреса страниц")
        return "\n".join(lines) if lines else "Нечего обходить: config/venues.yaml пуст"
