"""Вакансии с hh.ru.

ИСТОЧНИК ВЫКЛЮЧЕН: публичный API hh.ru закрыт. В config/sources.yaml стоит
mode "off", вместе с ним выключен и тип сигнала vacancy_signal — иначе движок
ждал бы фактов, которых не будет.

Модуль оставлен не как мёртвый код, а как рабочая заготовка: структура у всех
job-board API одна и та же, и под SuperJob или Habr Career переписывается
только метод collect. Всё остальное — пагинация, ретраи, отсев кадровых
агентств, стабильный source_uid — переносится как есть.

Компания открывает вакансию event-менеджера или руководителя внутренних
коммуникаций — значит, строит событийную функцию, значит, события будут.
Сигнал средней силы, зато источник бесплатный, публичный и срабатывает
задолго до того, как мероприятие появится в анонсах: людей нанимают раньше,
чем бронируют зал.

ИНН hh не отдаёт вовсе, есть только название работодателя. Сводить к
компании будет resolve; здесь задача одна — не потерять и не приукрасить.

Синхронно и в один поток: за проход выходит несколько десятков запросов
к бесплатному API, выигрыш от асинхронности здесь нулевой, а отладка сложнее.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from gtm.collectors.base import Collector, CollectorError, RawFact, register
from gtm.observability import StageResult
from gtm.settings import REPO_ROOT
from gtm.storage.models import FactType

SOURCE_NAME = "hh"
SIGNAL_KIND = "vacancy_signal"

# Технические константы: свойства протокола hh, а не бизнес-параметры.
_DEFAULT_BASE_URL = "https://api.hh.ru"
_VACANCIES_PATH = "/vacancies"
# 429 и 5xx — временные; на них ретрай осмыслен, на 400/404 — нет.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# hh отдаёт максимум 2000 позиций на выборку, дальше страницы пустые.
_MAX_OFFSET = 2000
# Больше 30 дней в параметре period hh не принимает — вернёт 400.
_MAX_PERIOD_DAYS = 30
_AGENCY_EMPLOYER_TYPE = "agency"

_NON_WORD = re.compile(r"[^0-9a-zа-я]+")


def _pause(seconds: float) -> None:
    """Отдельной функцией, чтобы тесты не ждали ретраев по-настоящему."""
    if seconds > 0:
        time.sleep(seconds)


# ------------------------------------------------------------------ разбор


def _normalize(text: str | None) -> str:
    """Свести к «event менеджер»: регистр, дефисы и кавычки не должны
    решать, нашли мы ключевое слово или нет."""
    if not text:
        return ""
    return _NON_WORD.sub(" ", str(text).lower().replace("ё", "е")).strip()


def matched_keywords(title: str | None, keywords: list[str]) -> list[str]:
    """Какие из ключевых слов реально видны в названии вакансии."""
    normalized = _normalize(title)
    if not normalized:
        return []
    return [word for word in keywords if _normalize(word) and _normalize(word) in normalized]


def parse_published_at(value: Any) -> date | None:
    """hh отдаёт «2026-07-20T12:00:00+0300». Час публикации нам не нужен."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def is_agency(item: dict[str, Any]) -> bool:
    """Кадровое агентство ищет не себе: мероприятие будет у его клиента,
    а кто этот клиент — вакансия не говорит. Такой факт только шумит."""
    employer = item.get("employer") or {}
    return str(employer.get("type") or "").lower() == _AGENCY_EMPLOYER_TYPE


def to_raw_fact(
    item: dict[str, Any], keywords: list[str], query: str | None = None
) -> RawFact | None:
    """Вакансию — в факт. None, если по записи не понять компанию.

    `query` — ключевое слово, по которому hh нашёл вакансию: он ищет с
    учётом морфологии, и «менеджеру по мероприятиям» наше буквальное
    сравнение не поймает, а сигнал там есть.
    """
    if not isinstance(item, dict):
        return None
    vacancy_id = str(item.get("id") or "").strip()
    employer = item.get("employer") or {}
    employer_name = " ".join(str(employer.get("name") or "").split())
    if not vacancy_id or not employer_name:
        return None

    title = item.get("name")
    found = matched_keywords(title, keywords)
    if not found and query:
        found = [query]
    area = item.get("area") or {}
    published_at = item.get("published_at")

    return RawFact(
        source_uid=f"{SOURCE_NAME}:{vacancy_id}",
        fact_type=FactType.VACANCY.value,
        company_name=employer_name,
        occurred_at=parse_published_at(published_at),
        payload={
            "vacancy_id": vacancy_id,
            "title": title,
            "employer_name": employer_name,
            "employer_id": str(employer.get("id")) if employer.get("id") else None,
            "published_at": published_at,
            # alternate_url — та ссылка, которую откроет человек в карточке.
            "url": item.get("alternate_url") or item.get("url"),
            "area": area.get("name") or area.get("id"),
            "keywords": found,
        },
    )


# --------------------------------------------------------------- коллектор


@register
class HHCollector(Collector):
    """Поиск вакансий по ключевым словам сигнала vacancy_signal."""

    name = SOURCE_NAME
    fact_type = FactType.VACANCY.value

    #: сколько вакансий последнего запуска отсеяно как агентские
    skipped_agencies: int = 0

    def collect(
        self, *, keywords: list[str] | None = None, period_days: int | None = None
    ) -> Iterable[RawFact]:
        words = [w.strip() for w in (keywords or self._keywords()) if w and w.strip()]
        if not words:
            raise CollectorError(
                f"Для '{SIGNAL_KIND}' не задано ни одного ключевого слова "
                f"в config/signals.yaml — искать нечего"
            )

        if self.mode == "offline":
            found = self._fixture_items()
        else:
            found = self._search(words, period_days)

        self.skipped_agencies = 0
        facts: dict[str, RawFact] = {}
        for item, query in found:
            if not isinstance(item, dict):
                continue
            if is_agency(item):
                self.skipped_agencies += 1
                continue
            fact = to_raw_fact(item, words, query)
            if fact is None:
                continue
            previous = facts.get(fact.source_uid)
            if previous is None:
                facts[fact.source_uid] = fact
                continue
            # Одна вакансия находится сразу по нескольким словам — это одна
            # вакансия, но знать надо про все совпадения.
            merged = list(previous.payload["keywords"])
            merged += [w for w in fact.payload["keywords"] if w not in merged]
            previous.payload["keywords"] = merged

        self.log.info(
            "hh.collected",
            vacancies=len(facts),
            agencies=self.skipped_agencies,
            keywords=len(words),
            mode=self.mode,
        )
        return list(facts.values())

    def run(self, **kwargs: Any) -> StageResult:
        result = super().run(**kwargs)
        if self.skipped_agencies:
            # Отсев должен быть виден в сводке запуска, а не только в логе.
            result.note("agencies", self.skipped_agencies)
        return result

    # ------------------------------------------------------------- источник

    def _keywords(self) -> list[str]:
        return list(self.config.signals.kind(SIGNAL_KIND).keywords)

    def _opt(self, key: str, default: Any) -> Any:
        value = getattr(self.source_config, key, None)
        return default if value is None else value

    def _search(
        self, keywords: list[str], period_days: int | None
    ) -> list[tuple[dict[str, Any], str | None]]:
        """Обойти выдачу по каждому ключевому слову постранично."""
        defaults = self.config.sources.defaults
        base_url = str(self._opt("base_url", _DEFAULT_BASE_URL))
        per_page = int(self._opt("per_page", 100))
        max_pages = int(self._opt("max_pages", 10))
        period = int(period_days or self._opt("search_period_days", _MAX_PERIOD_DAYS))
        if period > _MAX_PERIOD_DAYS:
            self.log.warning("hh.period_clamped", requested=period, limit=_MAX_PERIOD_DAYS)
            period = _MAX_PERIOD_DAYS
        rps = float(self._opt("rate_limit_rps", 0) or 0)
        gap = 1.0 / rps if rps > 0 else 0.0

        headers = {
            # Без внятного User-Agent hh отвечает 403 — это его требование,
            # а не вежливость.
            "User-Agent": self.settings.hh_user_agent,
            "Accept": "application/json",
        }
        found: list[tuple[dict[str, Any], str | None]] = []
        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=float(defaults.get("timeout_seconds", 30)),
        ) as client:
            for word in keywords:
                for page in range(max_pages):
                    data = self._get_page(
                        client,
                        {
                            "text": word,
                            # Ищем по названию вакансии: слово из описания
                            # («у нас бывают корпоративы») сигналом не является.
                            "search_field": "name",
                            "area": self._opt("area", 1),
                            "per_page": per_page,
                            "page": page,
                            "period": period,
                        },
                    )
                    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
                    found.extend((item, word) for item in items)
                    pages = int(data.get("pages") or 0)
                    if not items or page + 1 >= pages or (page + 1) * per_page >= _MAX_OFFSET:
                        break
                    _pause(gap)
        return found

    def _get_page(self, client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
        defaults = self.config.sources.defaults
        retries = max(1, int(defaults.get("retries", 3)))
        backoff = float(defaults.get("retry_backoff_seconds", 2))
        request_key = f"{params['text']}#{params['page']}"
        last_error = ""

        for attempt in range(1, retries + 1):
            try:
                with self.guard.call(
                    SOURCE_NAME,
                    _VACANCIES_PATH,
                    cost_rub=self.cost_per_call,
                    request_key=request_key,
                ):
                    response = client.get(_VACANCIES_PATH, params=params)
            except httpx.HTTPError as exc:
                last_error = f"сеть: {exc}"
            else:
                if response.status_code == 403:
                    # Ретраить бесполезно: пока не сменится заголовок, будет 403.
                    raise CollectorError(
                        "hh.ru ответил 403. Обычно это User-Agent: сейчас "
                        f"'{self.settings.hh_user_agent}', задайте свой "
                        f"в GTM_HH_USER_AGENT с контактом."
                    )
                if response.status_code in _RETRY_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise CollectorError(
                        f"hh.ru: HTTP {response.status_code} на запрос "
                        f"{request_key!r} — {response.text[:200]}"
                    )
                else:
                    try:
                        return dict(response.json())
                    except (ValueError, TypeError) as exc:
                        raise CollectorError(
                            f"hh.ru вернул не JSON на {request_key!r}: {exc}"
                        ) from exc
            if attempt < retries:
                self.log.warning("hh.retry", attempt=attempt, key=request_key, error=last_error)
                # Линейный откат: у бесплатного API нас тормозят, а не банят.
                _pause(backoff * attempt)

        raise CollectorError(
            f"hh.ru не ответил на {request_key!r} за {retries} попыток: {last_error}"
        )

    # -------------------------------------------------------------- offline

    def _fixture_items(self) -> list[tuple[dict[str, Any], str | None]]:
        """Режим offline: читаем сохранённую выдачу, в сеть не ходим.

        Фикстуры может не быть — это не повод падать: offline включают,
        чтобы прогнать конвейер без внешних источников.
        """
        raw = getattr(self.source_config, "fixtures_path", None)
        if not raw:
            self.log.info("hh.offline_without_fixtures", source=SOURCE_NAME)
            return []
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path  # пути в sources.yaml заданы от корня проекта
        if not path.exists():
            self.log.warning("hh.fixtures_missing", path=str(path))
            return []

        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        items: list[tuple[dict[str, Any], str | None]] = []
        for file in files:
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                self.log.warning("hh.fixture_unreadable", path=str(file), error=str(exc))
                continue
            page = data.get("items") if isinstance(data, dict) else data
            if not isinstance(page, list):
                self.log.warning("hh.fixture_unexpected_shape", path=str(file))
                continue
            items.extend((item, None) for item in page)
        return items
