"""Нормализация названий и поиск похожих компаний.

Название — не ключ, но единственная зацепка, когда источник не отдал ИНН.
Нормализация сводит «ООО "Ромашка-Инвест"», «Группа компаний Ромашка Инвест»
и «Ромашка Инвест» к одной строке. Дальше по ней ищутся кандидаты — но
только как подсказка человеку: свести компанию по одному имени нельзя.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from gtm.storage import repo
from gtm.storage.models import Company

# Порог нечёткого сравнения (rapidfuzz WRatio, шкала 0..100). Ловит опечатки
# и хвосты вроде «плюс»/«групп». Кандидаты всё равно уходят человеку в
# карантин, поэтому дешевле показать лишнее, чем пропустить дубль.
DEFAULT_MIN_SCORE = 88.0
DEFAULT_LIMIT = 5

# Организационно-правовые формы одним словом.
_OPF_TOKENS = frozenset(
    {
        "ооо",
        "оао",
        "ао",
        "пао",
        "зао",
        "нао",
        "ип",
        "гк",
        "ано",
        "нко",
        "фгуп",
        "гуп",
        "муп",
        "гбу",
        "фгбу",
        "llc",
        "ltd",
        "inc",
        "gmbh",
    }
)

# Те же формы прописью. Убираются целиком и до токенов: если вычеркнуть
# «акционерное общество», от «публичного акционерного общества» останется
# висящее «публичное». Порядок — от длинной формы к короткой.
_OPF_PHRASES = (
    "общество с ограниченной ответственностью",
    "непубличное акционерное общество",
    "публичное акционерное общество",
    "закрытое акционерное общество",
    "открытое акционерное общество",
    "limited liability company",
    "индивидуальный предприниматель",
    "акционерное общество",
    "группа компаний",
)

# Всё, кроме букв и цифр, — разделитель. Так «Ромашка-Инвест», «Ромашка Инвест»
# и «"Ромашка" Инвест» дают одну строку, а кавычки всех видов, точки в «Ltd.»
# и прочая пунктуация исчезают без отдельного списка символов.
_SEPARATORS_RE = re.compile(r"[^0-9a-zа-я]+")


def normalize_name(raw: str) -> str:
    """Название без ОПФ, кавычек, регистра, «ё» и пунктуации."""
    if not raw:
        return ""
    text = raw.lower().replace("ё", "е")
    text = _SEPARATORS_RE.sub(" ", text).strip()
    if not text:
        return ""

    stripped = text
    for phrase in _OPF_PHRASES:
        stripped = re.sub(rf"(?<!\S){re.escape(phrase)}(?!\S)", " ", stripped)
    tokens = [t for t in stripped.split() if t not in _OPF_TOKENS]
    # Если от названия после чистки не осталось ничего («ООО "АО"»), возвращаем
    # форму до вычёркивания: пустой name_norm совпал бы разом со всеми
    # компаниями, у которых имя тоже не пережило нормализацию.
    return " ".join(tokens) or text


def name_candidates(
    session: Session,
    name: str,
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[tuple[Company, float]]:
    """Похожие компании по нормализованному имени, по убыванию score.

    Перебирается вся таблица компаний. На масштабе задачи (тысячи строк,
    один прогон в сутки) это дешевле триграммного индекса, который пришлось
    бы поддерживать и который всё равно живёт только в PostgreSQL.
    """
    query = normalize_name(name)
    if not query:
        return []
    companies = [c for c in repo.iter_companies(session) if c.name_norm]
    if not companies:
        return []
    matches = process.extract(
        query,
        [c.name_norm for c in companies],
        scorer=fuzz.WRatio,
        limit=limit,
        score_cutoff=min_score,
    )
    return [(companies[index], float(score)) for _, score, index in matches]
