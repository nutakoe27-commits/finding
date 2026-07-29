"""Бесплатные наборы открытых данных ФНС: универсум компаний и их размер.

Три набора закрывают то, за что иначе платят реестру:

* реестр МСП (rmsp.nalog.gov.ru) — список юрлиц с ИНН, ОГРН, ОКВЭД и регионом.
  Из ОГРН арифметикой берётся год регистрации, то есть сигнал круглой
  годовщины считается вообще без платных запросов (см. gtm/resolve/ogrn.py);
* сведения о среднесписочной численности — штат для фильтра по размеру;
* ГИР БО (bo.nalog.gov.ru) — выручка для того же фильтра.

Наборы приходят файлами, а не по API: пользователь скачивает архив, коллектор
читает его с диска. Поэтому здесь нет ни HTTP-клиента, ни трат — и, что важнее,
нет зависимости от чужого аптайма в момент прогона.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ РЕЕСТРА МСП. В него по определению не входят крупные
компании, а на них приходятся мероприятия на 800-1500 человек — лучший для нас
сегмент. Универсум получается с дырой, и закрывается она не здесь, а сбором
истории мероприятий со страниц площадок-конкурентов (venue_pages).

ПРО РАЗМЕТКУ ПОЛЕЙ. Точные имена полей в наборах ФНС меняются между
выпусками, а проверить их на реальном файле при написании модуля возможности
не было. Поэтому читатель терпим: он собирает все атрибуты записи в плоский
словарь, а соответствие полей задаётся списками кандидатов в
config/sources.yaml. Расхождение правится в конфиге за одну строку, а команда
`gtm inspect <файл>` печатает фактическую структуру скачанного архива.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from gtm.collectors.base import Collector, RawFact, register
from gtm.resolve.inn import is_valid_inn
from gtm.resolve.ogrn import is_legal_entity, is_year_reliable, region_code, registration_year
from gtm.settings import REPO_ROOT
from gtm.storage.models import FactType

# Тег, внутри которого лежит одна запись. У наборов ФНС это «Документ»,
# но список расширяемый — дешевле перечислить варианты, чем угадывать.
DEFAULT_RECORD_TAGS = ("Документ", "Document", "row", "item")

# Разметка по умолчанию — предположение, а не проверенный факт.
# Первое найденное имя из списка и берётся.
DEFAULT_FIELDS: dict[str, dict[str, list[str]]] = {
    "msp": {
        "inn": ["ИННЮЛ", "ИНН", "inn"],
        "ogrn": ["ОГРН", "ogrn"],
        "name": ["НаимОрганизации", "НаимОрг", "НаимПолнЮЛ", "НаимСокрЮЛ", "name"],
        "category": ["КатСубМСП", "category"],
        "okved": ["КодОКВЭД", "okved"],
        "region": ["КодРегион", "region"],
        "city": ["Город", "НаимГород", "city"],
        "included_at": ["ДатаВклМСП", "included_at"],
    },
    "headcount": {
        "inn": ["ИНН", "inn", "ИННЮЛ"],
        "headcount": ["Численность", "СЧР", "headcount", "value"],
        "year": ["Год", "year", "Период"],
    },
    "revenue": {
        "inn": ["ИНН", "inn"],
        "revenue": ["2110", "Выручка", "revenue", "value"],
        "year": ["Год", "year", "period"],
    },
}

# Категории субъектов МСП: 1 — микро, 2 — малое, 3 — среднее.
# Микропредприятия отсекаем на входе: зал на 250+ человек им не нужен,
# а объём набора они раздувают в разы.
MICRO_CATEGORY = "1"


class _Rec(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MspRecord(_Rec):
    inn: str
    ogrn: str | None = None
    name: str
    category: str | None = None
    okved: str | None = None
    region_code: str | None = None
    city: str | None = None
    included_at: date | None = None
    # Год регистрации, вычисленный из ОГРН, и признак его надёжности.
    registration_year: int | None = None
    year_reliable: bool = False


class SizeRecord(_Rec):
    inn: str
    headcount: int | None = None
    revenue: dict[str, float] = Field(default_factory=dict)


class OpenDataError(RuntimeError):
    pass


# ------------------------------------------------------------- чтение файлов


def _members(path: Path) -> Iterator[tuple[str, bytes]]:
    """Отдать содержимое файла или всех файлов внутри zip.

    Наборы ФНС приходят архивами с сотнями файлов внутри; распаковывать их
    на диск незачем, читаем потоком.
    """
    if not path.exists():
        raise OpenDataError(f"Не найден файл набора: {path}")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                yield info.filename, archive.read(info)
        return
    yield path.name, path.read_bytes()


def _flatten_xml(element: ElementTree.Element) -> dict[str, str]:
    """Все атрибуты поддерева одной записи в плоском словаре.

    Нужно именно так: в наборах ФНС значимые поля разбросаны по вложенным
    элементам (ОКВЭД в одном, регион в другом), а их точная структура
    меняется между выпусками. Плоский словарь переживает такие изменения,
    жёсткий путь по дереву — нет.
    """
    flat: dict[str, str] = {}
    for node in element.iter():
        for key, value in node.attrib.items():
            # Первое вхождение важнее: основной ОКВЭД идёт раньше дополнительных.
            flat.setdefault(key, value)
        if node.text and node.text.strip():
            flat.setdefault(node.tag, node.text.strip())
    return flat


def iter_raw_records(
    path: str | Path, *, record_tags: Iterable[str] = DEFAULT_RECORD_TAGS
) -> Iterator[dict[str, str]]:
    """Записи набора в виде плоских словарей, независимо от формата файла.

    Поддерживаются xml, csv и json — этого достаточно для всех трёх наборов
    и не требует угадывать формат заранее.
    """
    tags = set(record_tags)
    for name, blob in _members(Path(path)):
        suffix = Path(name).suffix.lower()
        if suffix == ".xml":
            yield from _iter_xml(blob, tags)
        elif suffix in {".csv", ".tsv"}:
            yield from _iter_csv(blob, delimiter="\t" if suffix == ".tsv" else None)
        elif suffix in {".json", ".jsonl"}:
            yield from _iter_json(blob)
        # Прочие файлы в архиве (readme, схемы) молча пропускаем.


def _iter_xml(blob: bytes, tags: set[str]) -> Iterator[dict[str, str]]:
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError as exc:
        raise OpenDataError(f"Не удалось разобрать XML: {exc}") from exc
    found = False
    for element in root.iter():
        # Пространство имён в теге отбрасываем: {ns}Документ -> Документ.
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in tags:
            found = True
            yield _flatten_xml(element)
    if not found:
        # Записи не нашлись — вероятно, тег другой. Отдаём корень целиком,
        # чтобы gtm inspect показал, что там на самом деле лежит.
        yield _flatten_xml(root)


def _iter_csv(blob: bytes, *, delimiter: str | None = None) -> Iterator[dict[str, str]]:
    text = blob.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ";"
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        yield {k: v for k, v in row.items() if k and v}


def _iter_json(blob: bytes) -> Iterator[dict[str, str]]:
    text = blob.decode("utf-8", errors="replace").strip()
    if not text:
        return
    if text[0] == "[":
        payload = json.loads(text)
        rows = payload if isinstance(payload, list) else [payload]
    elif text[0] == "{" and "\n" in text and text.count("{") > 1:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if isinstance(row, dict):
            yield {str(k): ("" if v is None else str(v)) for k, v in row.items()}


# ------------------------------------------------------------------ разметка


def pick(row: dict[str, str], candidates: Iterable[str]) -> str | None:
    """Первое непустое значение из перечисленных имён.

    Сравнение без учёта регистра и пробелов: в наборах попадается и «ИНН»,
    и «инн», и «ИНН ».
    """
    normalized = {key.strip().lower(): value for key, value in row.items()}
    for candidate in candidates:
        value = normalized.get(candidate.strip().lower())
        if value not in (None, ""):
            return str(value).strip()
    return None


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _fields(config, dataset: str) -> dict[str, list[str]]:
    """Разметка из конфига поверх значений по умолчанию."""
    source = config.sources.model_extra.get("fns_open_data") or {}
    configured = ((source.get(dataset) or {}).get("fields")) or {}
    merged = {key: list(value) for key, value in DEFAULT_FIELDS[dataset].items()}
    for key, value in configured.items():
        merged[key] = list(value) if isinstance(value, list) else [value]
    return merged


def parse_msp(row: dict[str, str], fields: dict[str, list[str]]) -> MspRecord | None:
    """Одна запись реестра МСП. Битые и нерелевантные — None, без исключений:
    в наборе миллионы строк, падать на каждой странности нельзя."""
    inn = pick(row, fields["inn"])
    if not inn or not is_valid_inn(inn):
        return None
    name = pick(row, fields["name"])
    if not name:
        return None
    ogrn = pick(row, fields["ogrn"])
    # ИП отсекаем здесь же: зал на 800 человек они не арендуют.
    if ogrn and not is_legal_entity(ogrn):
        return None
    year = registration_year(ogrn) if ogrn else None
    return MspRecord(
        inn=inn,
        ogrn=ogrn,
        name=name,
        category=pick(row, fields["category"]),
        okved=pick(row, fields["okved"]),
        region_code=(region_code(ogrn) if ogrn else None) or pick(row, fields["region"]),
        city=pick(row, fields["city"]),
        included_at=_to_date(pick(row, fields["included_at"])),
        registration_year=year,
        year_reliable=is_year_reliable(year),
    )


def parse_size(
    row: dict[str, str], fields: dict[str, list[str]], *, dataset: str
) -> SizeRecord | None:
    inn = pick(row, fields["inn"])
    if not inn or not is_valid_inn(inn):
        return None
    if dataset == "headcount":
        headcount = _to_int(pick(row, fields["headcount"]))
        if headcount is None:
            return None
        return SizeRecord(inn=inn, headcount=headcount)
    revenue = _to_float(pick(row, fields["revenue"]))
    if revenue is None:
        return None
    year = pick(row, fields["year"]) or str(date.today().year - 1)
    return SizeRecord(inn=inn, revenue={str(_to_int(year) or year): revenue})


# --------------------------------------------------------------- инспекция


def inspect_file(path: str | Path, *, limit: int = 3) -> str:
    """Что лежит в скачанном наборе: файлы внутри архива и поля первых записей.

    Нужно ровно для одного: сверить разметку в config/sources.yaml с тем, что
    ФНС реально отдаёт в текущем выпуске набора. Без этого расхождение схемы
    выглядит как «коллектор ничего не нашёл» и отлаживается наугад.
    """
    target = Path(path)
    lines = [f"Файл: {target}"]
    try:
        names = [name for name, _ in _members(target)]
    except OpenDataError as exc:
        return f"{lines[0]}\n{exc}"
    lines.append(f"Файлов внутри: {len(names)}")
    lines.extend(f"  {name}" for name in names[:10])
    if len(names) > 10:
        lines.append(f"  ... и ещё {len(names) - 10}")

    lines.append("")
    shown = 0
    for row in iter_raw_records(target):
        lines.append(f"Запись {shown + 1}: полей {len(row)}")
        for key, value in list(row.items())[:40]:
            preview = value if len(value) <= 60 else value[:57] + "..."
            lines.append(f"  {key} = {preview}")
        shown += 1
        if shown >= limit:
            break
    if not shown:
        lines.append("Записей не найдено — вероятно, другой тег записи.")
        lines.append("Проверьте fns_open_data.record_tags в config/sources.yaml.")
    return "\n".join(lines)


# --------------------------------------------------------------- коллектор


def _resolve_path(raw: str | Path | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


@register
class FnsOpenDataCollector(Collector):
    """Универсум компаний и их размер из бесплатных наборов ФНС.

    Порядок важен: сначала реестр МСП создаёт компании, потом наборы
    численности и выручки дополняют уже созданные. Компании, которых нет
    в универсуме, наборы размера не создают — иначе в базу попадут миллионы
    юрлиц без единого признака, что они нам интересны.
    """

    name = "fns_open_data"
    fact_type = FactType.COMPANY_REGISTRATION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.skipped_micro = 0
        self.skipped_region = 0
        self.size_updates = 0
        self._headcount_path: Path | None = None
        self._revenue_path: Path | None = None
        # Разобранные записи держим до конца run(): ОГРН и ОКВЭД надо положить
        # в саму компанию, а компании появятся только после записи фактов.
        self._msp_records: dict[str, MspRecord] = {}

    @property
    def _source(self) -> dict[str, Any]:
        return self.config.sources.model_extra.get("fns_open_data") or {}

    def _record_tags(self) -> tuple[str, ...]:
        configured = self._source.get("record_tags")
        return tuple(configured) if configured else DEFAULT_RECORD_TAGS

    def _path_for(self, dataset: str, override: str | Path | None) -> Path | None:
        """Путь к набору или None, если файла нет.

        Отсутствие набора — штатная ситуация: у клиента может быть скачан
        один файл из трёх. Падать на этом нельзя, но и молчать не надо.
        """
        path = _resolve_path(override or (self._source.get(dataset) or {}).get("path"))
        if path is None:
            self.log.info(f"fns.{dataset}_skipped", reason="путь к набору не задан")
            return None
        if not path.exists():
            self.log.warning(f"fns.{dataset}_missing", path=str(path))
            return None
        return path

    def collect(
        self,
        *,
        msp_path: str | Path | None = None,
        headcount_path: str | Path | None = None,
        revenue_path: str | Path | None = None,
    ) -> Iterable[RawFact]:
        # Пути наборов размера запоминаем, но применяем не здесь: компаний
        # в базе ещё нет — их создаст запись фактов в run(). Отсюда и порядок.
        self._headcount_path = self._path_for("headcount", headcount_path)
        self._revenue_path = self._path_for("revenue", revenue_path)

        msp = self._path_for("msp", msp_path)
        return self._collect_msp(msp) if msp is not None else []

    def _collect_msp(self, path: Path) -> list[RawFact]:
        fields = _fields(self.config, "msp")
        allowed_regions = set(self.config.icp.geo.company_regions)
        drop_micro = bool((self._source.get("msp") or {}).get("skip_micro", True))
        facts: list[RawFact] = []
        seen: set[str] = set()

        for row in iter_raw_records(path, record_tags=self._record_tags()):
            record = parse_msp(row, fields)
            if record is None:
                continue
            if drop_micro and record.category == MICRO_CATEGORY:
                self.skipped_micro += 1
                continue
            # Фильтр по региону — здесь, а не в стадии фильтрации: набор
            # общероссийский, и тащить в базу всю страну незачем.
            if allowed_regions and record.region_code not in allowed_regions:
                self.skipped_region += 1
                continue
            if record.inn in seen:
                continue
            seen.add(record.inn)
            self._msp_records[record.inn] = record
            facts.append(
                RawFact(
                    source_uid=f"msp:{record.inn}",
                    fact_type=FactType.COMPANY_REGISTRATION.value,
                    company_name=record.name,
                    inn=record.inn,
                    occurred_at=record.included_at,
                    payload={
                        "ogrn": record.ogrn,
                        "okved": record.okved,
                        "region_code": record.region_code,
                        "city": record.city,
                        "msp_category": record.category,
                        "registration_year": record.registration_year,
                        "registration_year_reliable": record.year_reliable,
                        "dataset": "msp",
                    },
                )
            )
        self.log.info(
            "fns.msp_collected",
            kept=len(facts),
            skipped_micro=self.skipped_micro,
            skipped_region=self.skipped_region,
        )
        return facts

    def _apply_size(self, path: Path, dataset: str) -> None:
        """Дополнить известные компании штатом или выручкой."""
        from gtm.storage import repo

        fields = _fields(self.config, dataset)
        updated = 0
        for row in iter_raw_records(path, record_tags=self._record_tags()):
            record = parse_size(row, fields, dataset=dataset)
            if record is None:
                continue
            company = repo.get_company(self.session, record.inn)
            if company is None:
                continue
            if record.headcount is not None:
                company.headcount = record.headcount
                company.headcount_year = date.today().year
            if record.revenue:
                merged = dict(company.revenue or {})
                merged.update(record.revenue)
                company.revenue = merged
            updated += 1
        self.session.flush()
        self.size_updates += updated
        self.log.info(f"fns.{dataset}_applied", updated=updated)

    def _apply_company_fields(self) -> None:
        """Перенести ОГРН, ОКВЭД и регион из разобранных записей в компании.

        Без этого шага ОГРН остаётся только в payload факта, а движок
        ожиданий читает company.ogrn — и сигнал юбилея не срабатывает вовсе.
        Резолвер создаёт компанию по минимуму (ИНН и название), дополнять её
        реквизитами — дело источника.
        """
        from gtm.storage import repo

        for inn, record in self._msp_records.items():
            if repo.get_company(self.session, inn) is None:
                continue
            repo.upsert_company(
                self.session,
                inn,
                ogrn=record.ogrn,
                okved=record.okved,
                region_code=record.region_code,
                city=record.city,
                name=record.name,
                source=self.name,
            )
        self.session.flush()

    def run(self, **kwargs: Any) -> Any:
        result = super().run(**kwargs)
        self._apply_company_fields()
        # Только теперь компании из реестра МСП есть в базе, и наборы штата
        # и выручки могут их дополнить. Раньше дополнять было нечего.
        for path, dataset in (
            (self._headcount_path, "headcount"),
            (self._revenue_path, "revenue"),
        ):
            if path is not None:
                self._apply_size(path, dataset)
        self.session.commit()
        if self.skipped_micro:
            result.details["skipped_micro"] = self.skipped_micro
        if self.skipped_region:
            result.details["skipped_region"] = self.skipped_region
        if self.size_updates:
            result.details["size_updates"] = self.size_updates
        return result
