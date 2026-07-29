"""Импорт руками собранного CSV.

Этап 1 всей системы: первая версия работает на файле из ~30 компаний,
которые проводили корпоратив в прошлом декабре. Этот коллектор не
выкидывается никогда — он же запасной путь, когда сломался внешний
источник, и способ добавить компанию, найденную глазами.

Разбор нарочно терпимый. Файл заполняет человек: даты приезжают как
«15.12.2025», «2025-12-19» и «декабрь 2025», участники — как «~600» и
«500-600». Мусор в одной колонке не должен убивать строку целиком:
строка собрана руками и стоит дорого. Ошибка ровно одна — строка,
по которой нельзя понять, о какой компании речь.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy.orm import Session

from gtm.collectors.base import Collector, CollectorError, RawFact, register
from gtm.observability import StageResult, get_logger
from gtm.settings import REPO_ROOT
from gtm.storage import repo
from gtm.storage.models import DatePrecision, FactType

SOURCE_NAME = "manual"

# Технические константы разбора файла. В конфиг не выносятся: это не
# бизнес-параметры, а свойства того, как Excel в русской локали сохраняет CSV.
_ENCODINGS = ("utf-8-sig", "cp1251")
_DELIMITERS = (";", ",", "\t")
# 16 hex-символов хеша: на файле в тысячи строк коллизия невозможна,
# а source_uid остаётся читаемым в SQL-выборке.
_UID_LENGTH = 16
_DEFAULT_PATH = "data/manual/companies.csv"

# Месяц по первым трём буквам — покрывает и «декабрь», и «декабря», и «дек».
_MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

_RE_DMY = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$")
_RE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RE_MONTH_YEAR_NUM = re.compile(r"^(\d{1,2})[.\-/](\d{4})$")
_RE_ISO_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_RE_MONTH_WORD = re.compile(r"^(?:(\d{1,2})\s+)?([а-яё]{3,})\.?\s*(\d{4})", re.IGNORECASE)


# ------------------------------------------------------------------ разбор


def parse_event_date(raw: str | None) -> tuple[date, str] | None:
    """Вернуть (дата, точность) или None, если распознать не удалось.

    «декабрь 2025» — это не 1 декабря, а «где-то в декабре»: точность
    едет дальше в payload, чтобы ожидание не притворялось точным.
    """
    if not raw:
        return None
    text = " ".join(str(raw).split()).strip()
    if not text:
        return None

    match = _RE_DMY.match(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        return _with_precision(year, month, day, DatePrecision.DAY)

    match = _RE_ISO.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _with_precision(year, month, day, DatePrecision.DAY)

    match = _RE_MONTH_YEAR_NUM.match(text)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        return _with_precision(year, month, 1, DatePrecision.MONTH)

    match = _RE_ISO_MONTH.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        return _with_precision(year, month, 1, DatePrecision.MONTH)

    match = _RE_MONTH_WORD.match(text)
    if match:
        day_raw, word, year_raw = match.groups()
        month = _MONTHS.get(word.lower()[:3])
        if month is None:
            return None
        if day_raw:
            return _with_precision(int(year_raw), month, int(day_raw), DatePrecision.DAY)
        return _with_precision(int(year_raw), month, 1, DatePrecision.MONTH)

    return None


def _with_precision(
    year: int, month: int, day: int, precision: DatePrecision
) -> tuple[date, str] | None:
    try:
        return date(year, month, day), precision.value
    except ValueError:
        # 31.02.2025 или 13-й месяц — опечатка. Ведём себя как с любой
        # нераспознанной датой: строка выживает, факт станет бездатным.
        return None


def parse_attendees(raw: str | None) -> int | None:
    """«~600», «600 чел.», «500-600» → 600. Мусор → None, а не исключение.

    Из диапазона берём верхнюю границу: занижать масштаб опаснее, чем
    завышать — по масштабу решается, наш это клиент или нет.
    """
    if raw is None:
        return None
    # «1 200» — разделитель тысяч пробелом (в том числе неразрывным);
    # склеиваем только цифру с цифрой, чтобы «500 - 600» осталось диапазоном.
    text = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", str(raw))
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if not numbers:
        return None
    top = max(numbers)
    return top if top > 0 else None


def is_valid_inn(value: str) -> bool:
    """Контрольная сумма ИНН. ИНН — канонический ключ компании, и опечатка
    в нём хуже, чем его отсутствие: строка с битым ИНН уйдёт в карантин
    по названию, а не создаст компанию-призрак."""
    if not value.isdigit():
        return False
    digits = [int(ch) for ch in value]
    if len(digits) == 10:
        return digits[9] == _inn_checksum(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
    if len(digits) == 12:  # ИП: две контрольные цифры
        first = _inn_checksum(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        second = _inn_checksum(digits, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        return digits[10] == first and digits[11] == second
    return False


def _inn_checksum(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights, strict=False)) % 11 % 10


def normalize_inn(raw: str | None) -> str | None:
    """Оставить только цифры и проверить контрольную сумму. Не прошло — None."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits if is_valid_inn(digits) else None


def normalize_name(raw: str | None) -> str:
    """Грубая нормализация названия — только для стабильного source_uid.

    Сведением названий к компаниям занимается resolve, здесь нужно лишь
    чтобы «ООО «Ромашка-Трейд»» и «ООО Ромашка-Трейд» дали один ключ.
    """
    if not raw:
        return ""
    text = str(raw).lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`]", "", text)
    return " ".join(text.split())


# -------------------------------------------------------------- строка CSV


class ManualRow(BaseModel):
    """Строка файла после разбора. Валидация на границе: дальше по системе
    едут уже date и int, а не «декабрь 2025» и «~600»."""

    model_config = ConfigDict(extra="ignore")

    row_number: int = 0
    inn: str | None = None
    company_name: str | None = None
    event_title: str | None = None
    event_date: date | None = None
    date_precision: str | None = None
    attendees: int | None = None
    venue: str | None = None
    city: str | None = None
    contact_name: str | None = None
    contact_position: str | None = None
    contact_email: str | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values: dict[str, Any] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue  # лишние колонки без заголовка
            # Пустая ячейка — это отсутствие значения, а не пустая строка.
            values[key] = (value.strip() or None) if isinstance(value, str) else value

        values["inn"] = normalize_inn(values.get("inn"))
        if not values["inn"] and not values.get("company_name"):
            raise ValueError("ни company_name, ни корректного inn — по строке не найти компанию")

        parsed = parse_event_date(values.get("event_date"))
        values["event_date"], values["date_precision"] = parsed or (None, None)
        values["attendees"] = parse_attendees(values.get("attendees"))
        return values

    @property
    def company_key(self) -> str:
        return self.inn or normalize_name(self.company_name)

    @property
    def source_uid(self) -> str:
        """Хеш от значимых полей, а не номер строки.

        Номер строки развалился бы при первой же вставке компании в
        середину файла: все нижние строки поехали бы и приехали в базу
        как новые факты.
        """
        parts = [
            self.company_key,
            self.event_date.isoformat() if self.event_date else "",
            normalize_name(self.event_title),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:_UID_LENGTH]

    def to_raw_fact(self) -> RawFact:
        if self.event_date is not None:
            return RawFact(
                source_uid=self.source_uid,
                fact_type=FactType.PAST_EVENT.value,
                company_name=self.company_name,
                inn=self.inn,
                occurred_at=self.event_date,
                payload={
                    "title": self.event_title,
                    "attendees": self.attendees,
                    "venue": self.venue,
                    "city": self.city,
                    "precision": self.date_precision,
                    "notes": self.notes,
                },
            )
        # Даты нет — ожидание из такой строки не построить. Но компанию в базу
        # завести надо: юбилей по ЕГРЮЛ и обогащение работают уже от неё.
        return RawFact(
            source_uid=self.source_uid,
            fact_type=FactType.GROWTH.value,
            company_name=self.company_name,
            inn=self.inn,
            payload={"notes": self.notes},
        )


# ------------------------------------------------------------ чтение файла


def _read_text(path: Path) -> str:
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise CollectorError(f"{path}: не читается ни как {' , '.join(_ENCODINGS)}")


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "_")


def _parse_rows(path: Path) -> tuple[list[ManualRow], int]:
    """Вернуть (строки, сколько пропущено). Пропущенные — только те, по
    которым нельзя понять компанию."""
    log = get_logger(f"gtm.collector.{SOURCE_NAME}")
    stream = io.StringIO(_read_text(path))
    header = stream.readline()
    stream.seek(0)
    # Разделитель угадываем по заголовку: Excel в русской локали пишет «;».
    reader = csv.DictReader(stream, delimiter=max(_DELIMITERS, key=header.count))
    reader.fieldnames = [_normalize_header(name) for name in reader.fieldnames or []]

    rows: list[ManualRow] = []
    skipped = 0
    for number, raw in enumerate(reader, start=2):  # 1-я строка — заголовок
        if not any((value or "").strip() for value in raw.values() if isinstance(value, str)):
            continue  # пустая строка в хвосте файла — не ошибка
        try:
            rows.append(ManualRow.model_validate({**raw, "row_number": number}))
        except (ValidationError, ValueError) as exc:
            skipped += 1
            log.warning("manual.row_skipped", path=str(path), row=number, error=str(exc))
    return rows, skipped


def load_rows(path: str | Path) -> list[ManualRow]:
    """Разобрать CSV. Битые строки пропускаются с предупреждением в логе."""
    return _parse_rows(Path(path))[0]


def import_contacts(session: Session, path: str | Path) -> int:
    """Разложить контакты из того же файла. Вернуть число импортированных.

    Отдельным проходом, а не внутри коллектора: факты append-only и
    повторный импорт их не трогает, а контакты, наоборот, дополняют и
    правят руками — их перезапись безопасна и нужна.
    """
    log = get_logger(f"gtm.collector.{SOURCE_NAME}")
    seen: set[tuple[str, str]] = set()
    imported = 0
    for row in load_rows(path):
        # Контакт цепляется к компании по ИНН; без ИНН и без «собаки» в
        # адресе писать всё равно некуда.
        if not row.inn or not row.contact_email or "@" not in row.contact_email:
            continue
        # Одна компания встречается в файле несколькими мероприятиями —
        # контакт при этом один.
        key = (row.inn, row.contact_email.lower())
        if key in seen:
            continue
        seen.add(key)
        if repo.get_company(session, row.inn) is None:
            # Заводим только оболочку: название из ЕГРЮЛ, если оно уже есть,
            # затирать данными из файла нельзя.
            repo.upsert_company(
                session,
                row.inn,
                name=row.company_name or row.inn,
                city=row.city,
                source=SOURCE_NAME,
            )
        repo.upsert_contact(
            session,
            inn=row.inn,
            email=row.contact_email,
            full_name=row.contact_name,
            position=row.contact_position,
            source=SOURCE_NAME,
            # Контакт из ручного файла собран под конкретное письмо —
            # это и есть тот, кому пишем.
            is_primary=True,
        )
        imported += 1
    session.commit()
    log.info("manual.contacts_imported", path=str(path), count=imported)
    return imported


# --------------------------------------------------------------- коллектор


@register
class ManualCollector(Collector):
    """CSV, собранный руками. С него система стартует и им же чинится."""

    name = SOURCE_NAME
    fact_type = FactType.PAST_EVENT.value

    #: сколько строк последнего запуска не удалось привязать к компании
    skipped_rows: int = 0

    def collect(self, *, path: str | Path | None = None) -> Iterable[RawFact]:
        csv_path = self._resolve_path(path)
        if not csv_path.exists():
            if path is not None:
                # Файл назвали явно — молчать нельзя, это опечатка оператора.
                raise CollectorError(f"Не найден CSV {csv_path}")
            # Файла по умолчанию может не быть: система стартует с примера,
            # рабочий файл появляется позже. Это не повод падать.
            self.log.warning("manual.file_missing", path=str(csv_path))
            self.skipped_rows = 0
            return []

        rows, self.skipped_rows = _parse_rows(csv_path)
        self.log.info(
            "manual.loaded", path=str(csv_path), rows=len(rows), skipped=self.skipped_rows
        )
        return [row.to_raw_fact() for row in rows]

    def run(self, **kwargs: Any) -> StageResult:
        result = super().run(**kwargs)
        if self.skipped_rows:
            # Потери должны быть видны в сводке запуска, а не только в логе.
            result.note("skipped_rows", self.skipped_rows)
        return result

    def _resolve_path(self, path: str | Path | None) -> Path:
        raw = Path(path or getattr(self.source_config, "default_path", None) or _DEFAULT_PATH)
        # Пути в sources.yaml заданы от корня проекта.
        return raw if raw.is_absolute() else REPO_ROOT / raw
