"""Ежедневный прогон: собрать → построить ожидания → отфильтровать →
обогатить → написать → выдать список.

Порядок стадий здесь не декоративный. Сначала бесплатное сужение воронки,
потом платные операции: 300 фактов → 40 после фильтра → 20 обогащённых →
12 писем. Обратный порядок сжёг бы бюджет за неделю.

Это единственное место, где стадии знают друг о друге. Сами модули стадий
между собой не связаны — в частности, фильтр ничего не знает о коллекторах,
и это инвариант, а не случайность.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.observability import format_run_summary, get_logger, new_run_id
from gtm.settings import get_settings
from gtm.storage import repo

# Источники в порядке сбора. Реестр идёт последним: он обогащает компании,
# которые до него добавили другие коллекторы.
COLLECT_ORDER = ("manual", "events_archive", "hh", "zakupki", "registry")


def collect_all(
    session: Session,
    config: Config,
    *,
    run_id: str,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Собрать все активные источники. Отключённые пропускаются молча —
    это штатное состояние, а не ошибка."""
    from gtm.collectors import get_collector

    log = get_logger("gtm.pipeline")
    names = sources or COLLECT_ORDER
    counts: dict[str, Any] = {}
    for name in names:
        try:
            source_cfg = config.sources.source(name)
        except KeyError:
            log.warning("collect.unknown_source", source=name)
            continue
        if not source_cfg.is_active:
            counts[name] = "off"
            continue
        collector = get_collector(name)(session, config, run_id=run_id)
        result = collector.run()
        counts[name] = result.count_out
    return counts


def run_daily(
    session: Session,
    config: Config,
    *,
    today: date | None = None,
    run_id: str | None = None,
    collect: bool = True,
    enrich_limit: int | None = None,
    generate_limit: int | None = None,
    deliver_limit: int | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Весь конвейер за один прогон. Это то, что вешается в cron."""
    from gtm.deliver import run_deliver
    from gtm.enrich import run_enrich
    from gtm.expectations import build_from_facts, rescore_all
    from gtm.filters import run_filter
    from gtm.generate import run_generate

    settings = get_settings()
    today = today or date.today()
    run_id = run_id or new_run_id()
    log = get_logger("gtm.pipeline")
    log.info("daily.start", run_id=run_id, today=today.isoformat())

    collected: dict[str, Any] = {}
    if collect:
        collected = collect_all(session, config, run_id=run_id)

    build_from_facts(session, config, today=today, run_id=run_id)
    rescore_all(session, config, today=today)
    session.commit()

    run_filter(session, config, today=today, run_id=run_id)
    session.commit()

    run_enrich(session, config, run_id=run_id, limit=enrich_limit)
    session.commit()

    run_generate(session, config, run_id=run_id, limit=generate_limit)
    session.commit()

    stages = repo.run_summary(session, run_id)
    summary_line = format_run_summary(stages)

    _, paths = run_deliver(
        session,
        config,
        today=today,
        run_id=run_id,
        limit=deliver_limit or settings.daily_delivery_limit,
        out_dir=out_dir,
        summary=summary_line,
    )
    session.commit()

    stages = repo.run_summary(session, run_id)
    spent, calls = repo.spend_today(session)
    log.info("daily.done", run_id=run_id, summary=format_run_summary(stages))

    return {
        "run_id": run_id,
        "today": today.isoformat(),
        "collected": collected,
        "stages": stages,
        "summary": format_run_summary(stages),
        "files": [str(p) for p in paths],
        "spent_rub": spent,
        "api_calls": calls,
    }
