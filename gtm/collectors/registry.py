"""Реестр юрлиц (ЕГРЮЛ).

Источник даёт две разные по назначению вещи.

Первая — дату регистрации. Из неё арифметикой считается круглая годовщина
компании: 10, 15, 20, 25 лет — почти всегда большое мероприятие, и
конкурентов на этом сигнале практически нет, потому что им никто
не пользуется. Второе — штат и выручку: по ним фильтр решает, есть ли
у компании бюджет на зал такого масштаба.

Источник платный, отсюда два правила, которые нельзя нарушать:

1. Каждый вызов идёт через SpendGuard. BudgetExceeded не глушится — он
   обязан остановить стадию, а не дать ей дожечь остаток бюджета.
2. Повторный запрос той же компании не делается, пока не истёк
   cache_ttl_days. Это не оптимизация, а прямая экономия: одна и та же
   компания приезжает из разных источников по нескольку раз за прогон.

Без ключа и вне режима live источник работает на фикстурах. Система
обязана целиком подниматься и проверяться без единого платного вызова.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gtm.collectors.base import Collector, CollectorError, RawFact, register
from gtm.config import Config, SourceConfig
from gtm.observability import StageResult, get_logger
from gtm.resolve.inn import is_valid_inn
from gtm.resolve.names import normalize_name
from gtm.settings import REPO_ROOT, Settings, get_settings
from gtm.spend import SpendGuard
from gtm.storage import repo
from gtm.storage.models import Company, FactType

SOURCE_NAME = "registry"

# Умолчания на случай, если поле не заполнено в config/sources.yaml.
# Бизнес-параметры (провайдер, стоимость вызова, TTL кэша) берутся оттуда.
_LIVE_MODE = "live"
_DEFAULT_PROVIDER = "dadata"
_DEFAULT_FIXTURES_PATH = "data/fixtures/registry"
_FIXTURES_FILE = "companies.json"
_DEFAULT_CACHE_TTL_DAYS = 90

# Технические константы: свойства сети и форматов, не бизнес-логика.
_TIMEOUT_SECONDS = 30.0
_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
# Повторяем только то, что чинится повтором: перегрузку и отказ шлюза.
# 4xx — это неверный ключ или неверный запрос, повтор их не вылечит.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Сколько кандидатов просить у подсказок по названию: список читает человек
# при разборе карантина, длиннее десятка он бесполезен.
_SUGGEST_COUNT = 10
# Код строки «Выручка» формы 2 бухотчётности — им размечены финансы
# и у damia, и у ofdata.
_REVENUE_CODE = "2110"
# Даты в реестрах — календарные, по московскому времени. dadata отдаёт их
# миллисекундами эпохи, и в UTC полночь по Москве превращается в предыдущий
# день. Для дат до середины 90-х возможна погрешность в сутки: переходы
# на летнее время тогда менялись чаще, чем их успевали записать в tzdata.
try:
    from zoneinfo import ZoneInfo

    _REGISTRY_TZ: Any = ZoneInfo("Europe/Moscow")
except Exception:  # tzdata может отсутствовать в минимальном образе
    _REGISTRY_TZ = timezone(timedelta(hours=3))

# Статус компании: провайдеры пишут его тремя разными способами, а
# config/icp.yaml сравнивает с одной строкой. Порядок значим — «ликвидирован»
# и «в стадии ликвидации» это разные вещи, а начинаются одинаково.
_STATUS_UNKNOWN = "unknown"
_STATUS_MARKERS: tuple[tuple[str, str], ...] = (
    ("действующ", "active"),
    ("active", "active"),
    ("в стадии ликвидации", "liquidating"),
    ("ликвидируется", "liquidating"),
    ("liquidating", "liquidating"),
    ("ликвидирован", "liquidated"),
    ("liquidated", "liquidated"),
    ("прекращ", "liquidated"),
    ("исключен", "liquidated"),
    ("реорганиз", "reorganizing"),
    ("reorganizing", "reorganizing"),
    ("банкрот", "bankrupt"),
    ("bankrupt", "bankrupt"),
)


# --------------------------------------------------------------- разбор полей


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_status(raw: Any) -> str:
    """Статус компании к единому словарю.

    dadata пишет ACTIVE, damia — «Действующее», ofdata — {"Наим": "Действующая"}.
    Фильтр должен видеть одну строку, иначе allowed_statuses в icp.yaml
    придётся перечислять по каждому провайдеру.
    """
    if isinstance(raw, dict):
        raw = raw.get("Наим") or raw.get("Наименование") or raw.get("name")
    text = str(raw or "").strip().lower()
    if not text:
        # Не «active»: молча считать компанию живой опаснее, чем отсеять её
        # с внятной причиной — письмо ликвидированному юрлицу необратимо.
        return _STATUS_UNKNOWN
    for marker, status in _STATUS_MARKERS:
        if marker in text:
            return status
    return _STATUS_UNKNOWN


def parse_registry_date(value: Any) -> date | None:
    """Дата регистрации приезжает миллисекундами эпохи (dadata), ISO
    и «ДД.ММ.ГГГГ» (российские реестры). Нераспознанное — None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1000, tz=_REGISTRY_TZ).date()
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%d.%m.%Y").date()
    except ValueError:
        return None


def _year_key(value: Any) -> str | None:
    """Ключ-год из финансовой разметки. Всё, что не похоже на год, — не год:
    рядом с годами в тех же словарях лежат «ОтчетДата» и коды форм."""
    text = str(value)
    if len(text) == 4 and text.isdigit() and 1990 <= int(text) <= 2100:
        return text
    return None


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_revenue(raw: Any) -> dict[str, float]:
    """Выручка по годам: {"2024": 2140000000.0}.

    У damia и ofdata финансы размечены кодами строк бухотчётности
    ({"2024": {"2110": ...}}), у некоторых ответов — просто числом за год.
    """
    if not isinstance(raw, dict):
        return {}
    revenue: dict[str, float] = {}
    for key, item in raw.items():
        year = _year_key(key)
        if year is None:
            continue
        value = item.get(_REVENUE_CODE) if isinstance(item, dict) else item
        number = _to_float(value)
        if number is not None:
            revenue[year] = number
    return revenue


def parse_headcount(raw: Any) -> tuple[int | None, int | None]:
    """(штат, год). Штат приходит числом или разметкой по годам —
    в последнем случае берём самый свежий год."""
    if isinstance(raw, dict):
        years = {y: raw[k] for k in raw if (y := _year_key(k))}
        if not years:
            return None, None
        year = max(years)
        value = _to_float(years[year])
        return (int(value) if value else None), int(year)
    value = _to_float(raw)
    return (int(value) if value else None), None


def _first_str(*values: Any) -> str | None:
    """Первое непустое значение. Списки (телефоны, сайты) приходят от
    провайдеров и пустыми, и с одним элементом, и с десятком."""
    for value in values:
        if isinstance(value, list | tuple):
            value = value[0] if value else None
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return None


# ------------------------------------------------------------------- запись


class CompanyRecord(BaseModel):
    """Запись реестра, приведённая к одному виду для всех провайдеров.

    Валидация на границе: дальше по системе едут date, int и dict, а не
    «19.07.2002» и {"Наим": "Действующая"}.
    """

    model_config = ConfigDict(extra="forbid")

    inn: str
    ogrn: str | None = None
    name: str
    name_full: str | None = None
    registered_at: date | None = None
    region_code: str | None = None
    city: str | None = None
    okved: str | None = None
    status: str = _STATUS_UNKNOWN
    headcount: int | None = None
    headcount_year: int | None = None
    revenue: dict[str, float] = Field(default_factory=dict)
    site: str | None = None

    @field_validator("inn", "ogrn", mode="before")
    @classmethod
    def _only_digits(cls, value: Any) -> Any:
        return _digits(value) or None

    @field_validator("inn")
    @classmethod
    def _check_inn(cls, value: str) -> str:
        # ИНН — единственный канонический ключ компании. Битый ключ хуже
        # отсутствующего: под него заведётся компания-призрак, которую
        # потом не разлепить с настоящей.
        if not is_valid_inn(value):
            raise ValueError(f"ИНН {value!r} не проходит контрольную сумму")
        return value

    @field_validator("region_code", mode="before")
    @classmethod
    def _region_prefix(cls, value: Any) -> Any:
        # Регион приезжает и кодом «77», и КЛАДР-идентификатором
        # «7700000000000» — значимы только первые две цифры.
        return _digits(value)[:2] or None

    @field_validator("revenue", mode="before")
    @classmethod
    def _revenue_dict(cls, value: Any) -> Any:
        return parse_revenue(value) if isinstance(value, dict) else {}

    def to_company_fields(self) -> dict[str, Any]:
        """Поля для repo.upsert_company.

        Пустые значения там отбрасываются, поэтому провайдер, не отдавший
        выручку, не обнулит выручку, полученную из другого источника.
        """
        return {
            "name": self.name,
            "name_full": self.name_full,
            "name_norm": normalize_name(self.name),
            "ogrn": self.ogrn,
            "region_code": self.region_code,
            "city": self.city,
            "okved": self.okved,
            "registered_at": self.registered_at,
            "status": self.status,
            "headcount": self.headcount,
            "headcount_year": self.headcount_year,
            "revenue": self.revenue,
            "site": self.site,
            "source": SOURCE_NAME,
        }

    def to_raw_fact(self) -> RawFact:
        """Регистрация — событие одноразовое, поэтому source_uid это ИНН:
        повторный сбор по той же компании не создаёт второй факт."""
        return RawFact(
            source_uid=self.inn,
            fact_type=FactType.COMPANY_REGISTRATION.value,
            company_name=self.name,
            inn=self.inn,
            occurred_at=self.registered_at,
            payload={
                "registered_at": self.registered_at.isoformat() if self.registered_at else None,
                "ogrn": self.ogrn,
                "okved": self.okved,
                "status": self.status,
                "headcount": self.headcount,
                "revenue": self.revenue,
            },
        )


# ------------------------------------------------------- разбор ответов API
# Разбор отделён от сети: только так его можно проверить на зафиксированном
# JSON, не поднимая ни одного соединения.


def parse_dadata_party(suggestion: dict[str, Any]) -> CompanyRecord | None:
    """Одна подсказка dadata (/findById/party, /suggest/party) → запись."""
    if not isinstance(suggestion, dict):
        return None
    data = suggestion.get("data") or {}
    inn = _digits(data.get("inn"))
    if not is_valid_inn(inn):
        return None

    names = data.get("name") or {}
    state = data.get("state") or {}
    address = (data.get("address") or {}).get("data") or {}
    finance = data.get("finance") or {}

    revenue: dict[str, float] = {}
    year = _year_key(finance.get("year"))
    # revenue — выручка, income — доходы. У большинства юрлиц заполнено
    # только одно из двух, и для оценки бюджета годится любое.
    amount = _to_float(finance.get("revenue")) or _to_float(finance.get("income"))
    if year and amount:
        revenue[year] = amount

    headcount = _to_float(data.get("employee_count"))
    return CompanyRecord(
        inn=inn,
        ogrn=data.get("ogrn"),
        name=_first_str(names.get("short_with_opf"), names.get("short"), suggestion.get("value"))
        or inn,
        name_full=_first_str(
            names.get("full_with_opf"), names.get("full"), suggestion.get("unrestricted_value")
        ),
        registered_at=parse_registry_date(state.get("registration_date")),
        region_code=address.get("region_kladr_id"),
        city=_first_str(address.get("city"), address.get("settlement"), address.get("region")),
        okved=_first_str(data.get("okved")),
        status=normalize_status(state.get("status")),
        headcount=int(headcount) if headcount else None,
        # Года штата dadata не отдаёт; отчётный год финансов — ближайшая
        # честная оценка, а без года число штата нечем датировать.
        headcount_year=int(year) if (headcount and year) else None,
        revenue=revenue,
        site=None,  # подсказки сайт не возвращают
    )


def parse_damia_company(data: dict[str, Any], *, inn: str | None = None) -> CompanyRecord | None:
    """Запись damia (api-egrul) → запись реестра.

    ИНН у damia лежит в ключе ответа, а внутри записи его может не быть —
    поэтому он передаётся отдельно.
    """
    if not isinstance(data, dict):
        return None
    # Часть ответов завёрнута в «ЮЛ»/«ИП» — внутри та же плоская запись.
    for wrapper in ("ЮЛ", "ИП"):
        if isinstance(data.get(wrapper), dict):
            data = data[wrapper]
            break

    value = _digits(data.get("ИНН") or inn)
    if not is_valid_inn(value):
        return None

    address = _as_dict(data.get("Адрес") or data.get("ЮрАдрес"))
    headcount, headcount_year = parse_headcount(data.get("СЧР"))
    return CompanyRecord(
        inn=value,
        ogrn=data.get("ОГРН"),
        name=_first_str(data.get("НаимСокрЮЛ"), data.get("НаимСокр"), data.get("НаимПолнЮЛ"))
        or value,
        name_full=_first_str(data.get("НаимПолнЮЛ"), data.get("НаимПолн")),
        registered_at=parse_registry_date(data.get("ДатаРег") or data.get("ДатаОГРН")),
        region_code=_ru_region(address),
        city=_first_str(address.get("НасПункт"), address.get("Город"), address.get("Регион")),
        okved=_ru_okved(data.get("ОКВЭД")),
        status=normalize_status(data.get("Статус")),
        headcount=headcount,
        headcount_year=headcount_year,
        revenue=parse_revenue(data.get("Финансы") or data.get("Выручка")),
        site=_ru_site(data),
    )


def parse_ofdata_company(data: dict[str, Any]) -> CompanyRecord | None:
    """Блок `data` ответа ofdata (/v2/company, элемент /v2/search) → запись."""
    if not isinstance(data, dict):
        return None
    value = _digits(data.get("ИНН"))
    if not is_valid_inn(value):
        return None

    address = _as_dict(data.get("ЮрАдрес") or data.get("Адрес"))
    headcount, headcount_year = parse_headcount(data.get("СЧР"))
    return CompanyRecord(
        inn=value,
        ogrn=data.get("ОГРН"),
        name=_first_str(data.get("НаимСокр"), data.get("НаимПолн")) or value,
        name_full=_first_str(data.get("НаимПолн")),
        registered_at=parse_registry_date(data.get("ДатаРег")),
        region_code=_ru_region(address),
        city=_first_str(address.get("НасПункт"), address.get("Город")),
        okved=_ru_okved(data.get("ОКВЭД")),
        status=normalize_status(data.get("Статус")),
        headcount=headcount,
        headcount_year=headcount_year or _year_key(data.get("СЧРГод")),
        revenue=parse_revenue(data.get("Финансы")),
        site=_ru_site(data),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ru_region(address: dict[str, Any]) -> str | None:
    """Регион в российских ответах — то строка «77», то {"Код": "77", ...}."""
    region = address.get("Регион")
    if isinstance(region, dict):
        return _first_str(region.get("Код"))
    return _first_str(region, address.get("КодРегион"))


def _ru_okved(value: Any) -> str | None:
    return _first_str(value.get("Код") if isinstance(value, dict) else value)


def _ru_site(data: dict[str, Any]) -> str | None:
    return _first_str(_as_dict(data.get("Контакты")).get("Сайт"), data.get("Сайт"))


# ------------------------------------------------------------------ клиенты


class RegistryClient(Protocol):
    """Что умеет источник реестра.

    `provider` и `lookup_endpoint` нужны не логике, а журналу трат:
    без них в api_spend не видно, кто и куда ходил.
    """

    provider: str
    lookup_endpoint: str

    def lookup(self, inn: str) -> CompanyRecord | None: ...

    def suggest(self, name: str) -> list[CompanyRecord]: ...


def _load_fixtures(path: Path) -> dict[str, CompanyRecord]:
    """Файл вида {ИНН: запись}.

    Ошибка в нём — ошибка разработчика, а не источника: ломаемся сразу и
    с именем записи, вместо того чтобы молча отдать половину компаний.
    """
    if not path.exists():
        raise CollectorError(f"Не найдены фикстуры реестра: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CollectorError(f"{path}: ожидался словарь вида {{инн: запись}}")
    records: dict[str, CompanyRecord] = {}
    for inn, item in raw.items():
        if not isinstance(item, dict):
            raise CollectorError(f"{path}: запись {inn} — не словарь")
        try:
            record = CompanyRecord.model_validate({**item, "inn": inn})
        except ValidationError as exc:
            raise CollectorError(f"{path}: запись {inn} не разбирается: {exc}") from exc
        records[record.inn] = record
    return records


class FixtureClient:
    """Реестр из файла.

    Режим по умолчанию: без ключей система обязана подниматься и
    проверяться целиком, иначе её нельзя ни развернуть, ни протестировать.
    """

    provider = "fixture"
    lookup_endpoint = "fixtures/companies.json"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._records: dict[str, CompanyRecord] | None = None

    @property
    def records(self) -> dict[str, CompanyRecord]:
        if self._records is None:
            self._records = _load_fixtures(self.path)
        return self._records

    def lookup(self, inn: str) -> CompanyRecord | None:
        return self.records.get(_digits(inn))

    def suggest(self, name: str) -> list[CompanyRecord]:
        query = normalize_name(name)
        if not query:
            return []
        found = [
            record
            for record in self.records.values()
            if query in normalize_name(record.name) or normalize_name(record.name) in query
        ]
        return found[:_SUGGEST_COUNT]


class _HttpRegistryClient:
    """Общая обвязка платных провайдеров: ключ, таймаут, повтор, частота.

    Три клиента различаются адресом, авторизацией и разбором ответа —
    работа с сетью у них одна и та же.
    """

    provider = "http"
    base_url = ""
    lookup_endpoint = "/"
    suggest_endpoint = "/"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        retries: int = _RETRIES,
        backoff: float = _RETRY_BACKOFF_SECONDS,
        rate_limit_rps: float = 0.0,
        user_agent: str = "gtm-research/0.1",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            # Клиент без ключа бесполезен, но упасть он должен здесь,
            # а не первым платным вызовом посреди стадии.
            raise CollectorError(f"registry/{self.provider}: не задан ключ доступа")
        self.api_key = api_key
        self.retries = max(1, int(retries))
        self.backoff = float(backoff)
        self._min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._last_call = 0.0
        self.log = get_logger(f"gtm.collector.{SOURCE_NAME}")
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        gap = time.monotonic() - self._last_call
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last_call = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Ответ провайдера разобранным JSON. None — компании нет в реестре."""
        last_error = ""
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if response.status_code == httpx.codes.NOT_FOUND:
                    return None  # «не найдено» — это ответ, а не сбой
                if response.status_code not in _RETRY_STATUSES:
                    if response.is_error:
                        raise CollectorError(
                            f"{self.provider} {url}: HTTP {response.status_code} "
                            f"{response.text[:200]}"
                        )
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise CollectorError(f"{self.provider} {url}: ответ не JSON") from exc
                last_error = f"HTTP {response.status_code}"
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        raise CollectorError(
            f"{self.provider} {url}: нет ответа за {self.retries} попыток ({last_error})"
        )


class DadataClient(_HttpRegistryClient):
    """Подсказки dadata. Дёшево и полно по адресу и статусу;
    финансы отдаёт не по всем компаниям."""

    provider = "dadata"
    base_url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
    lookup_endpoint = "/findById/party"
    suggest_endpoint = "/suggest/party"

    def __init__(self, token: str, *, secret: str | None = None, **kwargs: Any) -> None:
        super().__init__(token, **kwargs)
        self._client.headers["Authorization"] = f"Token {token}"
        self._client.headers["Content-Type"] = "application/json"
        if secret:
            # Нужен «очистке», подсказкам — нет. Передаём, если задан.
            self._client.headers["X-Secret"] = secret

    def lookup(self, inn: str) -> CompanyRecord | None:
        data = self._request(
            "POST", f"{self.base_url}{self.lookup_endpoint}", json={"query": inn, "count": 1}
        )
        return _first_dadata_party(data)

    def suggest(self, name: str) -> list[CompanyRecord]:
        data = self._request(
            "POST",
            f"{self.base_url}{self.suggest_endpoint}",
            json={"query": name, "count": _SUGGEST_COUNT},
        )
        return _dadata_parties(data)


def _dadata_parties(data: Any) -> list[CompanyRecord]:
    suggestions = (data or {}).get("suggestions") or []
    parsed = (parse_dadata_party(item) for item in suggestions)
    return [record for record in parsed if record is not None]


def _first_dadata_party(data: Any) -> CompanyRecord | None:
    """Головная организация.

    Филиалы приезжают с тем же ИНН, но со своим адресом и без финансов —
    выбрать по ошибке филиал значит отфильтровать компанию по чужим данным.
    """
    suggestions = (data or {}).get("suggestions") or []
    ordered = sorted(
        suggestions,
        key=lambda item: (item.get("data") or {}).get("branch_type", "MAIN") != "MAIN",
    )
    for item in ordered:
        record = parse_dadata_party(item)
        if record is not None:
            return record
    return None


class DamiaClient(_HttpRegistryClient):
    """damia (api-egrul). Ключ передаётся параметром запроса,
    ответ приходит словарём {ИНН: запись}."""

    provider = "damia"
    base_url = "https://damia.ru/api-egrul"
    lookup_endpoint = "/req"
    suggest_endpoint = "/search"

    def lookup(self, inn: str) -> CompanyRecord | None:
        data = self._request(
            "GET",
            f"{self.base_url}{self.lookup_endpoint}",
            params={"req": inn, "key": self.api_key},
        )
        records = _keyed_by_inn(data, parse_damia_company, limit=1)
        return records[0] if records else None

    def suggest(self, name: str) -> list[CompanyRecord]:
        data = self._request(
            "GET",
            f"{self.base_url}{self.suggest_endpoint}",
            params={"q": name, "key": self.api_key, "count": _SUGGEST_COUNT},
        )
        return _keyed_by_inn(data, parse_damia_company, limit=_SUGGEST_COUNT)


def _keyed_by_inn(
    data: Any,
    parse: Callable[..., CompanyRecord | None],
    *,
    limit: int | None = None,
) -> list[CompanyRecord]:
    """Российские реестры отвечают словарём {ИНН: запись}: ИНН лежит
    в ключе, а внутри записи его может и не быть."""
    if not isinstance(data, dict):
        return []
    records: list[CompanyRecord] = []
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        record = parse(item, inn=key)
        if record is not None:
            records.append(record)
        if limit is not None and len(records) >= limit:
            break
    return records


class OfdataClient(_HttpRegistryClient):
    """ofdata. Отдаёт бухотчётность по годам — лучший источник выручки."""

    provider = "ofdata"
    base_url = "https://api.ofdata.ru/v2"
    lookup_endpoint = "/company"
    suggest_endpoint = "/search"

    def lookup(self, inn: str) -> CompanyRecord | None:
        data = self._request(
            "GET",
            f"{self.base_url}{self.lookup_endpoint}",
            params={"key": self.api_key, "inn": inn},
        )
        payload = _ofdata_payload(data)
        return parse_ofdata_company(payload) if isinstance(payload, dict) else None

    def suggest(self, name: str) -> list[CompanyRecord]:
        data = self._request(
            "GET",
            f"{self.base_url}{self.suggest_endpoint}",
            params={
                "key": self.api_key,
                "query": name,
                "by": "name",
                "obj": "org",
                "limit": _SUGGEST_COUNT,
            },
        )
        payload = _ofdata_payload(data) or {}
        rows = payload.get("Записи") or []
        parsed = (parse_ofdata_company(row) for row in rows if isinstance(row, dict))
        return [record for record in parsed if record is not None]


def _ofdata_payload(data: Any) -> Any:
    """У ofdata статус запроса лежит в `meta`, а не в коде HTTP: ошибочный
    ответ приходит с HTTP 200 и его легко принять за пустой результат."""
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") or {}
    if str(meta.get("status", "ok")).lower() == "error":
        raise CollectorError(f"ofdata: {meta.get('message') or 'ошибка запроса'}")
    return data.get("data")


# -------------------------------------------------------- выбор реализации


def _fixtures_path(source: SourceConfig) -> Path:
    raw = Path(str(getattr(source, "fixtures_path", None) or _DEFAULT_FIXTURES_PATH))
    # Пути в sources.yaml заданы от корня проекта.
    directory = raw if raw.is_absolute() else REPO_ROOT / raw
    return directory / _FIXTURES_FILE


def _http_options(config: Config, source: SourceConfig) -> dict[str, Any]:
    defaults = config.sources.defaults or {}
    return {
        "timeout": float(defaults.get("timeout_seconds", _TIMEOUT_SECONDS)),
        "retries": int(defaults.get("retries", _RETRIES)),
        "backoff": float(defaults.get("retry_backoff_seconds", _RETRY_BACKOFF_SECONDS)),
        "rate_limit_rps": float(getattr(source, "rate_limit_rps", 0) or 0),
        "user_agent": str(defaults.get("user_agent", "gtm-research/0.1")),
    }


def get_registry_client(config: Config, settings: Settings | None = None) -> RegistryClient:
    """Клиент реестра по конфигу.

    Не режим live или нет ключа — фикстуры. Молча переключаться нельзя:
    когда в базе не окажется дат регистрации, причина должна находиться
    в логе за десять секунд, а не выясняться разбором конфига.
    """
    settings = settings or get_settings()
    source = config.sources.source(SOURCE_NAME)
    log = get_logger(f"gtm.collector.{SOURCE_NAME}")
    fixtures = _fixtures_path(source)

    if source.mode != _LIVE_MODE:
        log.info("registry.fixtures", reason="mode", mode=source.mode, path=str(fixtures))
        return FixtureClient(fixtures)

    provider = str(getattr(source, "provider", "") or _DEFAULT_PROVIDER).strip().lower()
    keys = {
        "dadata": settings.dadata_token,
        "damia": settings.damia_key,
        "ofdata": settings.ofdata_key,
    }
    if provider not in keys:
        # Опечатка в конфиге: молча уйти на фикстуры значит подсунуть
        # выдуманные компании вместо настоящих.
        raise CollectorError(
            f"registry: неизвестный provider '{provider}', известные: {sorted(keys)}"
        )
    if not keys[provider]:
        log.warning("registry.fixtures", reason="no_key", provider=provider, path=str(fixtures))
        return FixtureClient(fixtures)

    options = _http_options(config, source)
    if provider == "dadata":
        return DadataClient(keys[provider], secret=settings.dadata_secret, **options)
    if provider == "damia":
        return DamiaClient(keys[provider], **options)
    return OfdataClient(keys[provider], **options)


# ------------------------------------------------------------- обогащение


def _call_cost(client: RegistryClient, source: SourceConfig) -> float:
    """Фикстуры денег не стоят, но в журнал попадают всё равно: по api_spend
    должно быть видно, что стадия действительно ходила за компанией."""
    return 0.0 if isinstance(client, FixtureClient) else float(source.cost_rub_per_call)


def _is_fresh(company: Company | None, ttl_days: int) -> bool:
    """Есть ли смысл идти в API.

    Свежий updated_at сам по себе ничего не значит: строку трогает любой
    коллектор. Признак того, что реестр по этой компании уже отвечал, —
    заполненная дата регистрации; без неё только что заведённая оболочка
    выглядела бы «свежей» и не обогатилась бы никогда.
    """
    if company is None or company.registered_at is None or ttl_days <= 0:
        return False
    updated = company.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)  # SQLite отдаёт время без зоны
    return updated >= datetime.now(UTC) - timedelta(days=ttl_days)


def _store(session: Session, record: CompanyRecord) -> Company:
    """Реестровые поля — в компанию, факт регистрации — в fact.

    Факт нужен при том, что дата лежит и в company: таблица фактов
    append-only, и пересчёт системы на исторических данных идёт по ней.
    """
    company = repo.upsert_company(session, record.inn, **record.to_company_fields())
    raw = record.to_raw_fact()
    repo.upsert_fact(
        session,
        source=SOURCE_NAME,
        source_uid=raw.source_uid,
        fact_type=raw.fact_type,
        inn=record.inn,
        company_name_raw=record.name,
        occurred_at=raw.occurred_at,
        payload=raw.payload,
    )
    session.flush()
    return company


def enrich_company(
    session: Session,
    inn: str,
    *,
    config: Config,
    guard: SpendGuard,
    force: bool = False,
    client: RegistryClient | None = None,
) -> Company | None:
    """Добрать реестровые данные по компании. Вернуть обновлённую или None.

    Кэш обязателен: пока не истёк cache_ttl_days, повторный запрос не
    делается вообще. Одна компания приезжает из разных источников по
    нескольку раз за прогон, и без кэша стадия платит за каждый.

    `client` передаётся, когда стадия обогащает пачку компаний: клиент
    строится один раз на всю пачку, а не на каждую компанию.
    """
    log = get_logger(f"gtm.collector.{SOURCE_NAME}")
    source = config.sources.source(SOURCE_NAME)
    company = repo.get_company(session, inn)

    if not source.is_active:
        log.debug("registry.disabled", inn=inn, mode=source.mode)
        return company

    ttl_days = int(getattr(source, "cache_ttl_days", _DEFAULT_CACHE_TTL_DAYS) or 0)
    if not force and _is_fresh(company, ttl_days):
        log.debug("registry.cache_hit", inn=inn, ttl_days=ttl_days)
        return company

    client = client or get_registry_client(config)
    # BudgetExceeded отсюда не ловится: стадия обязана остановиться.
    with guard.call(
        client.provider,
        client.lookup_endpoint,
        cost_rub=_call_cost(client, source),
        request_key=inn,
    ):
        record = client.lookup(inn)

    if record is None:
        log.info("registry.not_found", inn=inn, provider=client.provider)
        return None
    return _store(session, record)


# -------------------------------------------------------------- коллектор


def _inns_without_registration(session: Session) -> list[str]:
    """Кого спрашивать по умолчанию: пока даты регистрации нет,
    юбилей не посчитать. Выборки нет в repo — запрос локальный."""
    return list(session.scalars(select(Company.inn).where(Company.registered_at.is_(None))))


@register
class RegistryCollector(Collector):
    """ЕГРЮЛ по списку ИНН.

    Идёт последним в порядке сбора: обогащает компании, которых до него
    завели остальные коллекторы.
    """

    name = SOURCE_NAME
    fact_type = FactType.COMPANY_REGISTRATION.value

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._records: list[CompanyRecord] = []

    def collect(self, *, inns: list[str] | None = None) -> Iterable[RawFact]:
        client = get_registry_client(self.config, self.settings)
        cost = _call_cost(client, self.source_config)
        targets = list(inns) if inns is not None else _inns_without_registration(self.session)

        self._records = []
        facts: list[RawFact] = []
        for inn in targets:
            with self.guard.call(
                client.provider, client.lookup_endpoint, cost_rub=cost, request_key=inn
            ):
                record = client.lookup(inn)
            if record is None:
                self.log.info("registry.not_found", inn=inn, provider=client.provider)
                continue
            self._records.append(record)
            facts.append(record.to_raw_fact())

        self.log.info(
            "registry.collected", asked=len(targets), found=len(facts), provider=client.provider
        )
        return facts

    def run(self, **kwargs: Any) -> StageResult:
        result = super().run(**kwargs)
        # Базовый run сводит факт к ИНН, но штат, выручку и дату регистрации
        # в карточку компании не переносит, а юбилей считается именно от
        # company.registered_at. Пишем и после BudgetExceeded: за уже
        # полученные записи заплачено, терять их незачем.
        for record in self._records:
            repo.upsert_company(self.session, record.inn, **record.to_company_fields())
        if self._records:
            self.session.commit()
            result.note("companies_updated", len(self._records))
        return result
