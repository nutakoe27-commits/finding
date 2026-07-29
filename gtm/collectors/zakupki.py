"""Госзакупки: 44-ФЗ и 223-ФЗ.

Бюджетные учреждения и компании с госучастием публикуют извещения об аренде
помещений под конференции, форумы, коллегии и съезды. Источник
структурированный, публичный, легальный — и, в отличие от анонсов,
показывает намерение ДО того, как площадка выбрана: извещение появляется
за месяц-два до мероприятия, а не после того, как зал уже забронирован.

ОГОВОРКА, КОТОРУЮ ПРИДЁТСЯ ОБЪЯСНЯТЬ КЛИЕНТУ. Госзаказчик не может
«получить письмо и подписать договор»: он обязан провести конкурентную
процедуру. Сигнал отсюда — это не лид в обычном смысле, а приглашение
поучаствовать в закупке: изучить документацию, подготовить заявку, пройти
по требованиям и конкурировать ценой, зачастую с фиксированной НМЦК.
Отсюда и короткое окно контакта (lead_days=30 в config/signals.yaml):
писать до публикации бессмысленно — предмет закупки ещё не сформулирован,
а после публикации счёт идёт на недели до окончания подачи заявок.
Считать эти ожидания такими же, как годовщина корпоратива, нельзя: тут
другой процесс продажи, и в отчёте их надо показывать отдельной строкой.

Зато сведение к компании здесь тривиальное: ИНН заказчика в извещении есть
всегда. Это единственный наш источник, который не нуждается в разборе
карантина по названию.

Прямой доступ к ЕИС с 2025 года требует токена или ЭЦП, поэтому источник
по умолчанию выключен (mode "off" в config/sources.yaml) и подключается
последним — через платного посредника. Все обращения идут через SpendGuard.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gtm.collectors.base import Collector, CollectorError, RawFact, register
from gtm.config import Config, SourceConfig
from gtm.observability import StageResult, get_logger
from gtm.settings import REPO_ROOT, Settings, get_settings
from gtm.spend import SpendGuard
from gtm.storage.models import FactType

SOURCE_NAME = "zakupki"

log = get_logger(f"gtm.collector.{SOURCE_NAME}")

# Умолчания на случай, если поле не заполнено в config/sources.yaml.
# Бизнес-параметры (ключевые слова, регион, законы, цена вызова) — оттуда.
_LIVE_MODE = "live"
_DEFAULT_PROVIDER = "damia"
_DEFAULT_FIXTURES_PATH = "data/fixtures/zakupki"
_FIXTURES_FILE = "notices.json"
_KNOWN_LAWS = ("44", "223")

# Технические константы: свойства протокола посредника и форматов ЕИС.
_DEFAULT_BASE_URL = "https://damia.ru/api-zakupki"
_SEARCH_PATH = "/zsearch"
_TIMEOUT_SECONDS = 30.0
_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
# 429 и 5xx — временные; на них ретрай осмыслен, на 400/403 — нет.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Предохранитель пагинации. У платного источника зациклившийся обход стоит
# денег, а не только времени, поэтому потолок жёсткий и низкий.
_MAX_PAGES = 10
# Регулярный прогон смотрит на месяц назад: окно контакта по закупке — 30
# дней, извещение старше этого срока обсуждать уже не с кем.
_DEFAULT_WINDOW_DAYS = 30


def _pause(seconds: float) -> None:
    """Отдельной функцией, чтобы тесты не ждали ретраев по-настоящему."""
    if seconds > 0:
        time.sleep(seconds)


# --------------------------------------------------------- число участников

# Единицы измерения аудитории. Это не бизнес-параметр, а морфология языка:
# по этому списку число «привязывается» к людям, а не к деньгам, метрам и
# датам, которых в тексте закупки всегда больше.
_ATTENDEE_UNITS = (
    r"участник\w*"
    r"|человек(?![\w-])"
    r"|людей"
    r"|чел\."
    r"|гост(?:ь|я|ей|ям|ями|е)(?![\w-])"
    r"|делегат\w*"
    r"|слушател\w*"
    r"|посетител\w*"
    r"|зрител\w*"
    r"|персон\w*"
    r"|мест(?:о|а|ам|ах)?(?![\w-])"
)
# «1 200», «1200», «1,5» (последнее осмысленно только с «тыс.»).
# \s здесь важен целиком: разряды в выгрузках ЕИС разделены неразрывным
# пробелом, а не обычным.
_NUMBER = r"\d{1,3}(?:\s\d{3})+|\d+(?:[.,]\d+)?"
# Между числом и единицей допускается ровно одно слово («1000 посадочных
# мест»). Два и больше — уже не связка, а совпадение: «12 ноября для гостей»
# дало бы 12 участников и выкинуло бы крупный форум как мелкий.
_ATTENDEES_RE = re.compile(
    rf"(?P<lo>{_NUMBER})"
    rf"(?:\s*[-–—]\s*(?P<hi>{_NUMBER}))?"
    rf"\s*(?P<mult>тыс\.|тыс(?:яч\w*)?(?![\w-]))?"
    rf"\s*(?:[а-яё]+\s+)?"
    rf"(?:{_ATTENDEE_UNITS})",
    re.IGNORECASE,
)
_THOUSAND = 1000
# Границы правдоподобия. Меньше двадцати — это не аудитория конференции,
# а число дней, столов или порядковый номер; больше ста тысяч — опечатка
# или площадь. И то и другое лучше отдать как «не знаем».
_MIN_PLAUSIBLE_ATTENDEES = 20
_MAX_PLAUSIBLE_ATTENDEES = 100_000


def _number(raw: str | None) -> float | None:
    if not raw:
        return None
    # Пробелы внутри числа — разрядные разделители, в том числе неразрывные.
    text = "".join(ch for ch in raw if not ch.isspace())
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_attendees(text: str | None) -> int | None:
    """Ожидаемое число участников из текста извещения. Не нашли — None.

    Число засчитывается, только если оно прямо привязано к людям:
    «форум на 800 участников», «зал на 1000 посадочных мест». Всё остальное
    отбрасывается, потому что в описании закупки чисел много и почти все
    они не про людей — «800 кв. м», «800 000 рублей», «12 ноября», «3 дня».
    Занизить масштаб хуже, чем не знать его: по масштабу решается, наш это
    клиент или нет, а неизвестное число фильтр умеет обрабатывать отдельно.

    Если чисел несколько («1000 мест и три аудитории по 120 мест»), берём
    наибольшее: зал должен вместить всех, кого перечислили.
    """
    if not text:
        return None

    best: int | None = None
    for match in _ATTENDEES_RE.finditer(str(text)):
        # Верхняя граница диапазона: «на 250-300 участников» значит, что
        # площадка должна выдержать 300.
        value = _number(match.group("hi")) or _number(match.group("lo"))
        if value is None:
            continue
        if match.group("mult"):
            value *= _THOUSAND
        number = int(round(value))
        if not _MIN_PLAUSIBLE_ATTENDEES <= number <= _MAX_PLAUSIBLE_ATTENDEES:
            continue
        if best is None or number > best:
            best = number
    return best


# ------------------------------------------------------------ разбор полей


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _first(*values: Any) -> str | None:
    """Первое непустое значение: у посредников одно и то же поле называется
    по-разному от метода к методу."""
    for value in values:
        if isinstance(value, list | tuple):
            value = value[0] if value else None
        text = " ".join(str(value).split()) if value is not None else ""
        if text:
            return text
    return None


def _to_float(value: Any) -> float | None:
    """Цена приезжает и числом, и строкой «1 850 000,00»."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        number = _number(str(value)) or 0.0
    return number if number > 0 else None


def parse_notice_date(value: Any) -> date | None:
    """Даты в извещениях: ISO, «ДД.ММ.ГГГГ» и ISO со временем публикации."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%d.%m.%Y").date()
    except ValueError:
        return None


def normalize_law(value: Any) -> str | None:
    """«44-ФЗ», «Федеральный закон № 223-ФЗ», 223 — к «44»/«223».

    Всё прочее (615-ПП по капремонту, отменённый 94-ФЗ) — не наш процесс:
    там другая процедура, и объяснять её клиенту нечем.
    """
    if isinstance(value, dict):
        value = value.get("Наим") or value.get("Код") or value.get("name")
    digits = _digits(value)
    for law in _KNOWN_LAWS:
        if digits == law:
            return law
    return None


def _notice_url(notice_id: str) -> str:
    """Ссылка на извещение в ЕИС.

    Прямой путь зависит от способа закупки (ea44, ok44, 223 живут по разным
    адресам), а способ посредник отдаёт не всегда. Поиск по реестровому
    номеру открывает нужное извещение при любом способе — и не превращается
    в битую ссылку в карточке у менеджера.
    """
    return (
        "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
        f"?searchString={notice_id}"
    )


# ------------------------------------------------------------------ запись


class NoticeRecord(BaseModel):
    """Извещение о закупке, приведённое к одному виду.

    Валидация на границе: дальше по системе едут date, float и «44»/«223»,
    а не «19.07.2026» и «Федеральный закон № 223-ФЗ».
    """

    model_config = ConfigDict(extra="forbid")

    notice_id: str = Field(min_length=1, max_length=64)
    law: Literal["44", "223"]
    customer_name: str = Field(min_length=1, max_length=512)
    # Не проверяем контрольную сумму здесь: битый ИНН должен доехать до
    # resolve и лечь в карантин с причиной, а не исчезнуть вместе с извещением.
    customer_inn: str | None = None
    title: str = Field(min_length=1, max_length=1024)
    published_at: date
    # Дата мероприятия, если она указана в сроках исполнения. Часто её нет —
    # тогда ожидание строит стадия сигналов от даты публикации.
    event_date: date | None = None
    price: float | None = None
    attendees: int | None = None
    region: str = ""
    url: str = ""

    @field_validator("customer_inn", mode="before")
    @classmethod
    def _inn_digits(cls, value: Any) -> Any:
        return _digits(value) or None

    @field_validator("law", mode="before")
    @classmethod
    def _law_code(cls, value: Any) -> Any:
        return normalize_law(value) or value


def to_raw_fact(record: NoticeRecord) -> RawFact:
    """Извещение — в факт. occurred_at — дата публикации: именно она
    датирует сам факт, а ожидаемая дата мероприятия едет в payload."""
    return RawFact(
        source_uid=f"{SOURCE_NAME}:{record.notice_id}",
        fact_type=FactType.TENDER.value,
        company_name=record.customer_name,
        inn=record.customer_inn,
        occurred_at=record.published_at,
        payload={
            "notice_id": record.notice_id,
            "law": record.law,
            "title": record.title,
            "customer_name": record.customer_name,
            "published_at": record.published_at.isoformat(),
            "event_date": record.event_date.isoformat() if record.event_date else None,
            "price": record.price,
            "attendees": record.attendees,
            "region": record.region,
            "url": record.url,
        },
    )


def _notice(
    *,
    notice_id: Any,
    law: Any,
    customer_name: Any,
    customer_inn: Any = None,
    title: Any,
    description: Any = "",
    published_at: Any,
    event_date: Any = None,
    price: Any = None,
    region: Any = "",
    url: Any = None,
) -> NoticeRecord | None:
    """Собрать запись из разнородных полей. None — если извещение бесполезно.

    Одна сборка на все источники: offline обязан быть репетицией живого
    прогона, а не отдельной веткой разбора.
    """
    uid = _first(notice_id)
    name = _first(customer_name)
    subject = _first(title)
    published = parse_notice_date(published_at)
    code = normalize_law(law)
    if not uid or not name or not subject or published is None or code is None:
        log.warning(
            "zakupki.notice_skipped",
            notice_id=uid,
            law=_first(law),
            has_customer=bool(name),
            has_date=published is not None,
        )
        return None

    text = subject if not description else f"{subject}\n{description}"
    try:
        return NoticeRecord(
            notice_id=uid,
            law=code,
            customer_name=name,
            customer_inn=customer_inn,
            title=subject,
            published_at=published,
            event_date=parse_notice_date(event_date),
            price=_to_float(price),
            # Числа участников отдельным полем нет ни у посредника, ни в ЕИС:
            # оно живёт в тексте предмета закупки и достаётся только оттуда.
            attendees=parse_attendees(text),
            region=_first(region) or "",
            url=_first(url) or _notice_url(uid),
        )
    except ValidationError as exc:
        log.warning("zakupki.notice_invalid", notice_id=uid, error=str(exc))
        return None


# ------------------------------------------------------- разбор ответов API
# Разбор отделён от сети: только так его можно проверить на зафиксированном
# JSON, не поднимая ни одного соединения.


def parse_damia_notice(
    data: dict[str, Any], *, notice_id: str | None = None
) -> NoticeRecord | None:
    """Одно извещение damia (api-zakupki) → запись.

    Реестровый номер лежит в ключе ответа, а внутри записи его может
    не быть — поэтому он передаётся отдельно.
    """
    if not isinstance(data, dict):
        return None
    customer = data.get("Заказчик")
    customer = customer if isinstance(customer, dict) else {"Наим": customer}
    return _notice(
        notice_id=_first(data.get("РегНомер"), data.get("Номер"), notice_id),
        law=_first(data.get("Закон"), data.get("ФЗ"), data.get("Тип")),
        customer_name=_first(
            customer.get("НаимСокр"), customer.get("Наим"), data.get("НаимЗаказчика")
        ),
        customer_inn=_first(customer.get("ИНН"), data.get("ИННЗаказчика")),
        title=_first(data.get("Наим"), data.get("ЗакНаим"), data.get("Предмет")),
        description=_first(data.get("Описание"), data.get("ОбъектЗакупки")) or "",
        published_at=_first(data.get("ДатаПубл"), data.get("ДатаРазм")),
        event_date=_first(
            data.get("ДатаНачИсп"), data.get("ДатаМероприятия"), data.get("СрокНачИсп")
        ),
        price=data.get("НачЦена") if data.get("НачЦена") is not None else data.get("НМЦК"),
        region=_first(data.get("Регион"), data.get("КодРегион")),
        url=_first(data.get("Ссылка"), data.get("url")),
    )


def parse_search_response(data: Any) -> list[NoticeRecord]:
    """Ответ поиска → записи.

    Посредник отвечает словарём {реестровый номер: извещение}; некоторые
    методы заворачивают то же самое в список. Битая запись не должна
    уносить с собой всю выдачу, за которую уже заплачено.
    """
    if isinstance(data, dict):
        items: list[tuple[str | None, Any]] = [(str(key), value) for key, value in data.items()]
    elif isinstance(data, list):
        items = [(None, value) for value in data]
    else:
        return []

    records: dict[str, NoticeRecord] = {}
    for key, item in items:
        if not isinstance(item, dict):
            continue
        record = parse_damia_notice(item, notice_id=key)
        if record is not None:
            records.setdefault(record.notice_id, record)
    return list(records.values())


# ------------------------------------------------------------------ клиенты


class ZakupkiClient(Protocol):
    """Что умеет источник закупок.

    `provider` нужен не логике, а журналу трат и логу: без него не видно,
    кто именно ходил за извещениями.
    """

    provider: str

    def search(
        self, *, keywords: list[str], region: str, since: date, until: date
    ) -> list[NoticeRecord]: ...


class DamiaZakupkiClient:
    """Посредник damia: поиск извещений по ключевым словам.

    Источник платный, поэтому каждый запрос идёт через SpendGuard —
    и удачный, и неудачный: у посредника списывается и то, и другое.
    """

    provider = _DEFAULT_PROVIDER

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _TIMEOUT_SECONDS,
        retries: int = _RETRIES,
        backoff: float = _RETRY_BACKOFF_SECONDS,
        user_agent: str = "gtm-research/0.1",
        guard: SpendGuard | None = None,
        cost_rub: float = 0.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            # Клиент без ключа бесполезен, но упасть он должен здесь,
            # а не первым платным вызовом посреди стадии.
            raise CollectorError(f"{SOURCE_NAME}/{self.provider}: не задан ключ доступа")
        self.api_key = api_key
        self.base_url = base_url
        self.retries = max(1, int(retries))
        self.backoff = float(backoff)
        self.guard = guard
        self.cost_rub = float(cost_rub)
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent, "Accept": "application/json"}
        )

    def search(
        self, *, keywords: list[str], region: str, since: date, until: date
    ) -> list[NoticeRecord]:
        found: dict[str, NoticeRecord] = {}
        for word in keywords:
            for page in range(1, _MAX_PAGES + 1):
                records = parse_search_response(
                    self._request(word, region=region, since=since, until=until, page=page)
                )
                fresh = [r for r in records if r.notice_id not in found]
                for record in fresh:
                    found[record.notice_id] = record
                # Останавливаемся не только на пустой странице, но и когда
                # новых номеров не появилось: если параметр пагинации
                # посредник не понимает, он молча отдаёт первую страницу
                # снова — и обход крутится за наши деньги.
                if not fresh:
                    break
        return list(found.values())

    def _journal(self, request_key: str) -> Any:
        if self.guard is None:
            return nullcontext()
        return self.guard.call(
            self.provider, _SEARCH_PATH, cost_rub=self.cost_rub, request_key=request_key
        )

    def _request(
        self, keyword: str, *, region: str, since: date, until: date, page: int
    ) -> Any:
        params = {
            "q": keyword,
            "key": self.api_key,
            "region": region,
            "dfrom": since.isoformat(),
            "dto": until.isoformat(),
            "page": page,
        }
        request_key = f"{keyword}#{page}"
        last_error = ""

        for attempt in range(1, self.retries + 1):
            try:
                with self._journal(request_key):
                    response = self._client.get(f"{self.base_url}{_SEARCH_PATH}", params=params)
            except httpx.HTTPError as exc:
                last_error = f"сеть: {exc}"
            else:
                if response.status_code in _RETRY_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise CollectorError(
                        f"{self.provider}: HTTP {response.status_code} на запрос "
                        f"{request_key!r} — {response.text[:200]}"
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise CollectorError(
                            f"{self.provider} вернул не JSON на {request_key!r}: {exc}"
                        ) from exc
            if attempt < self.retries:
                log.warning("zakupki.retry", attempt=attempt, key=request_key, error=last_error)
                # Линейный откат: нас тормозят лимитом, а не банят.
                _pause(self.backoff * attempt)

        raise CollectorError(
            f"{self.provider} не ответил на {request_key!r} за {self.retries} попыток: "
            f"{last_error}"
        )


class FixtureZakupkiClient:
    """Извещения из файла: ни одного запроса в сеть и ни одного рубля.

    Режим по умолчанию. Источник выключен, пока не решён вопрос с токеном,
    но конвейер целиком обязан подниматься и проверяться и без него.

    Ключевые слова здесь не применяются: фикстура — это уже результат
    поиска, изображать полнотекстовый поиск по ней значит проверять
    не то, что работает в бою. Окно и регион отсекаются по-настоящему:
    их обещает отдавать и живой клиент.
    """

    provider = "fixture"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def search(
        self, *, keywords: list[str], region: str, since: date, until: date
    ) -> list[NoticeRecord]:
        wanted = _digits(region) or str(region or "").strip()
        found: dict[str, NoticeRecord] = {}
        for entry in self._entries():
            record = self._to_record(entry)
            if record is None:
                continue
            if not since <= record.published_at <= until:
                continue
            if wanted and record.region and record.region != wanted:
                continue
            found.setdefault(record.notice_id, record)
        log.info(
            "zakupki.fixtures_read",
            path=str(self.path),
            notices=len(found),
            keywords=len(keywords),
        )
        return list(found.values())

    def _entries(self) -> list[dict[str, Any]]:
        """Фикстуры может не быть — это не авария: offline включают, чтобы
        прогнать конвейер без внешних источников."""
        if not self.path.exists():
            log.warning("zakupki.fixtures_missing", path=str(self.path))
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("zakupki.fixture_unreadable", path=str(self.path), error=str(exc))
            return []
        items = data.get("notices") if isinstance(data, dict) else data
        if not isinstance(items, list):
            log.warning("zakupki.fixture_unexpected_shape", path=str(self.path))
            return []
        return [item for item in items if isinstance(item, dict)]

    def _to_record(self, entry: dict[str, Any]) -> NoticeRecord | None:
        # Число участников в фикстуре не проставлено намеренно: оно достаётся
        # из текста тем же разбором, что и в живом режиме.
        return _notice(
            notice_id=entry.get("notice_id"),
            law=entry.get("law"),
            customer_name=entry.get("customer_name"),
            customer_inn=entry.get("customer_inn"),
            title=entry.get("title"),
            description=entry.get("description") or "",
            published_at=entry.get("published_at"),
            event_date=entry.get("event_date"),
            price=entry.get("price"),
            region=entry.get("region"),
            url=entry.get("url"),
        )


def _fixtures_path(source: SourceConfig) -> Path:
    raw = Path(str(getattr(source, "fixtures_path", None) or _DEFAULT_FIXTURES_PATH))
    # Пути в sources.yaml заданы от корня проекта.
    directory = raw if raw.is_absolute() else REPO_ROOT / raw
    return directory / _FIXTURES_FILE if directory.suffix != ".json" else directory


def get_zakupki_client(
    config: Config, settings: Settings | None = None, *, guard: SpendGuard | None = None
) -> ZakupkiClient:
    """Клиент закупок по конфигу.

    Не режим live или нет ключа — фикстуры. Переключение всегда с записью
    в лог: когда в базе не окажется ни одной закупки, причина должна
    находиться за десять секунд, а не выясняться разбором конфига.

    `guard` передаёт коллектор: платные запросы обязаны идти через учёт,
    а живёт SpendGuard на стадии, а не в клиенте.
    """
    settings = settings or get_settings()
    source = config.sources.source(SOURCE_NAME)
    fixtures = _fixtures_path(source)

    if source.mode != _LIVE_MODE:
        log.info("zakupki.fixtures", reason="mode", mode=source.mode, path=str(fixtures))
        return FixtureZakupkiClient(fixtures)

    provider = str(getattr(source, "provider", "") or _DEFAULT_PROVIDER).strip().lower()
    if provider != _DEFAULT_PROVIDER:
        # Опечатка в конфиге: молча уйти на фикстуры значит подсунуть
        # выдуманные закупки вместо настоящих.
        raise CollectorError(
            f"{SOURCE_NAME}: неизвестный provider '{provider}', реализован "
            f"только '{_DEFAULT_PROVIDER}'"
        )
    # Ключи api-zakupki и api-egrul у damia разные, но выдают их вместе:
    # специфичный имеет приоритет, общий — запасной.
    key = settings.zakupki_token or settings.damia_key
    if not key:
        log.warning("zakupki.fixtures", reason="no_key", provider=provider, path=str(fixtures))
        return FixtureZakupkiClient(fixtures)

    defaults = config.sources.defaults or {}
    return DamiaZakupkiClient(
        key,
        base_url=str(getattr(source, "base_url", None) or _DEFAULT_BASE_URL),
        timeout=float(defaults.get("timeout_seconds", _TIMEOUT_SECONDS)),
        retries=int(defaults.get("retries", _RETRIES)),
        backoff=float(defaults.get("retry_backoff_seconds", _RETRY_BACKOFF_SECONDS)),
        user_agent=str(defaults.get("user_agent", "gtm-research/0.1")),
        guard=guard,
        cost_rub=float(source.cost_rub_per_call),
    )


# --------------------------------------------------------------- коллектор


@register
class ZakupkiCollector(Collector):
    """Извещения о закупках — сырьё для сигнала tender.

    Источник выключен по умолчанию (mode "off"): без токена ЕИС или ключа
    посредника ходить некуда. Выключенный коллектор не делает ни одного
    запроса — базовый `run` останавливается до `collect`.
    """

    name = SOURCE_NAME
    fact_type = FactType.TENDER.value

    #: сколько извещений последнего запуска отсеяно как мелкие
    skipped_small: int = 0
    #: сколько отсеяно по чужому закону (не из config/sources.yaml)
    skipped_other_law: int = 0

    def collect(
        self, *, since: date | None = None, until: date | None = None
    ) -> Iterable[RawFact]:
        # Счётчики отсева сбрасываем до похода в источник: иначе прерванный
        # по бюджету прогон покажет в сводке цифры предыдущего.
        self.skipped_small = 0
        self.skipped_other_law = 0

        window_until = until or date.today()
        window_since = since or window_until - timedelta(days=self._window_days())
        if window_since > window_until:
            raise CollectorError(f"Окно сбора вывернуто: {window_since} позже {window_until}")

        keywords = self._keywords()
        if not keywords:
            raise CollectorError(
                f"Для '{SOURCE_NAME}' не задано ни одного ключевого слова "
                f"в config/sources.yaml — искать нечего"
            )

        client = get_zakupki_client(self.config, self.settings, guard=self.guard)
        notices = client.search(
            keywords=keywords,
            region=self._region(),
            since=window_since,
            until=window_until,
        )

        laws = self._laws()
        threshold = self._min_attendees()
        facts: list[RawFact] = []
        for notice in notices:
            # Закон определяет процедуру, а её клиенту объяснять: набор
            # законов задан конфигом, и чужой сюда попадать не должен.
            if notice.law not in laws:
                self.skipped_other_law += 1
                continue
            # Самый дешёвый фильтр в системе: «зал на 120 человек» отсекается
            # до записи в базу и до любого обогащения. Неизвестный масштаб
            # не отсекаем — у большинства извещений числа нет вовсе.
            if notice.attendees is not None and notice.attendees < threshold:
                self.skipped_small += 1
                continue
            facts.append(to_raw_fact(notice))

        self.log.info(
            "zakupki.collected",
            notices=len(facts),
            too_small=self.skipped_small,
            other_law=self.skipped_other_law,
            provider=client.provider,
            since=window_since.isoformat(),
            until=window_until.isoformat(),
            mode=self.mode,
        )
        return facts

    def run(self, **kwargs: Any) -> StageResult:
        result = super().run(**kwargs)
        # Отсев должен быть виден в сводке запуска, а не только в логе.
        if self.skipped_small:
            result.note("too_small", self.skipped_small)
        if self.skipped_other_law:
            result.note("other_law", self.skipped_other_law)
        return result

    # ------------------------------------------------------------ настройки

    def _keywords(self) -> list[str]:
        raw = getattr(self.source_config, "keywords", None) or []
        return [" ".join(str(w).split()) for w in raw if str(w).strip()]

    def _laws(self) -> tuple[str, ...]:
        raw = getattr(self.source_config, "laws", None) or _KNOWN_LAWS
        laws = tuple(law for value in raw if (law := normalize_law(value)))
        return laws or _KNOWN_LAWS

    def _region(self) -> str:
        return str(getattr(self.source_config, "region", "") or "").strip()

    def _window_days(self) -> int:
        days = int(getattr(self.source_config, "search_period_days", 0) or 0)
        return days if days > 0 else _DEFAULT_WINDOW_DAYS

    def _min_attendees(self) -> int:
        """Нижняя граница масштаба — общая для всей системы, из config/icp.yaml:
        зал на сто человек не наш, кто бы его ни арендовал."""
        return int(self.config.icp.scale.min_attendees)
