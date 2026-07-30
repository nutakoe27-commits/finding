"""Логи и счётчики стадий.

Каждая стадия логирует счётчик на входе и выходе. Когда система однажды
выдаст пустой список, здесь за десять секунд видно, на какой стадии
обнулилось: «собрано 340 → прошло фильтр 45 → обогащено 20 → писем 12».
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from gtm.settings import get_settings
from gtm.storage import repo

_CONFIGURED = False


def setup_logging(level: str | None = None, as_json: bool | None = None) -> None:
    global _CONFIGURED
    settings = get_settings()
    level = level or settings.log_level
    as_json = settings.log_json if as_json is None else as_json

    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if as_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    # Чужие библиотеки на INFO печатают своё: alembic — «setup plugin ...»
    # и «Context impl SQLiteImpl», httpx — строку на каждый запрос. К делу это
    # не относится, содержательные строки мы пишем сами, а в случае httpx
    # в лог ещё и утекают адреса запросов. На DEBUG не глушим — там нужно.
    if logging.getLogger().level > logging.DEBUG:
        for noisy in ("alembic", "httpx", "httpcore", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str = "gtm") -> Any:
    if not _CONFIGURED:
        setup_logging()
    return structlog.get_logger(name)


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


@dataclass
class StageResult:
    """Счётчики стадии. `details` — куда уходят отсеянные и почему."""

    stage: str
    count_in: int = 0
    count_out: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def bump_in(self, n: int = 1) -> None:
        self.count_in += n

    def bump_out(self, n: int = 1) -> None:
        self.count_out += n

    def note(self, key: str, n: int = 1) -> None:
        self.details[key] = self.details.get(key, 0) + n


@contextmanager
def stage(session: Session, run_id: str, name: str, count_in: int = 0) -> Iterator[StageResult]:
    """Обёртка стадии: пишет StageRun в БД и лог на выходе."""
    log = get_logger("gtm.stage")
    row = repo.start_stage(session, run_id, name, count_in)
    result = StageResult(stage=name, count_in=count_in)
    try:
        yield result
    except Exception as exc:
        repo.finish_stage(
            session, row, count_out=result.count_out, count_in=result.count_in, error=str(exc)
        )
        session.commit()
        log.error("stage.failed", stage=name, run_id=run_id, error=str(exc))
        raise
    repo.finish_stage(
        session,
        row,
        count_out=result.count_out,
        count_in=result.count_in,
        details=result.details,
    )
    log.info(
        "stage.done",
        stage=name,
        run_id=run_id,
        count_in=result.count_in,
        count_out=result.count_out,
        **{k: v for k, v in result.details.items() if isinstance(v, int)},
    )


def format_run_summary(rows: list[dict[str, Any]]) -> str:
    """«собрано 340 → фильтр 45 → обогащено 20 → писем 12».

    Стадия, которая получила записи на вход и не выпустила ни одной, пишется
    как «11→0»: иначе в сводке она выглядит нулём, неотличимым от «на входе
    ничего не было», и обрыв конвейера читается как отсутствие клиентов.
    """
    if not rows:
        return "нет данных по запуску"
    parts = []
    for row in rows:
        count_in, count_out = row.get("in") or 0, row["out"]
        parts.append(f"{row['stage']} {count_in}→0" if count_in and not count_out
                     else f"{row['stage']} {count_out}")
    line = " → ".join(parts)
    failed = [r["stage"] for r in rows if r.get("error")]
    if failed:
        line += f"  [ошибки: {', '.join(failed)}]"
    stopped = [r["stage"] for r in rows if (r.get("details") or {}).get("stop_reason")]
    if stopped:
        line += f"  [оборвалось: {', '.join(stopped)}]"
    return line


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """«1 вызов», «2 вызова», «11 вызовов» — согласование по русским правилам."""
    tail_100 = abs(count) % 100
    tail_10 = abs(count) % 10
    if 11 <= tail_100 <= 14:
        return many
    if tail_10 == 1:
        return one
    if 2 <= tail_10 <= 4:
        return few
    return many
