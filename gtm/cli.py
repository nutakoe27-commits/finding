"""Командный интерфейс.

Одна ежедневная команда (`gtm daily`) и отдельные команды для тяжёлых
разовых операций. Каждую стадию можно запустить поодиночке — при разборе
«почему список пустой» это оказывается нужным чаще, чем кажется.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gtm.config import load_config
from gtm.observability import format_run_summary, new_run_id, setup_logging
from gtm.settings import get_settings
from gtm.storage import repo
from gtm.storage.db import create_all, session_scope, sqlite_path

app = typer.Typer(help="Поиск заказчиков для конференц-площадки", no_args_is_help=True)
db_app = typer.Typer(help="База данных", no_args_is_help=True)
import_app = typer.Typer(help="Импорт данных из файлов", no_args_is_help=True)
expectations_app = typer.Typer(help="Ожидания — ядро системы", no_args_is_help=True)
feedback_app = typer.Typer(help="Обратная связь и калибровка", no_args_is_help=True)
quarantine_app = typer.Typer(help="Записи, не сведённые к компании", no_args_is_help=True)

app.add_typer(db_app, name="db")
app.add_typer(import_app, name="import")
app.add_typer(expectations_app, name="expectations")
app.add_typer(feedback_app, name="feedback")
app.add_typer(quarantine_app, name="quarantine")

console = Console()


def _today(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _config():
    return load_config()


@app.callback()
def main(log_level: str = typer.Option("INFO", "--log-level")) -> None:
    setup_logging(log_level)


# ------------------------------------------------------------------- db


def _alembic_config():
    from alembic.config import Config as AlembicConfig

    return AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


@db_app.command("init")
def db_init() -> None:
    """Создать схему из моделей и отметить её как актуальную для alembic.

    Отметка обязательна: без неё следующий `db upgrade` попытается создать
    уже существующие таблицы и упадёт. С ней он корректно накатывает только
    то, что появилось после.
    """
    from alembic import command

    create_all()
    command.stamp(_alembic_config(), "head")

    url = get_settings().database_url
    path = sqlite_path(url)
    # Путь в адресе относительный, значит зависит от текущего каталога.
    # Печатаем абсолютный, чтобы потом не искать, куда легла база.
    where = f"{url} ({path.resolve()})" if path else url
    console.print(f"[green]Схема создана[/green]: {where}")


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """Накатить миграции alembic."""
    from alembic import command

    command.upgrade(_alembic_config(), revision)
    console.print(f"[green]Миграции накатаны[/green]: {revision}")


# --------------------------------------------------------------- import


@import_app.command("manual")
def import_manual(
    file: Path = typer.Option(..., "--file", "-f", exists=True),
    contacts: bool = typer.Option(True, help="Импортировать и контакты тоже"),
) -> None:
    """Импорт CSV с компаниями и прошедшими мероприятиями.

    С этого начинается работа системы: 30 компаний, собранных руками,
    важнее любого коллектора, потому что на них проверяется, работают ли
    письма вообще.
    """
    from gtm.collectors.manual import ManualCollector, import_contacts

    config = _config()
    with session_scope() as session:
        result = ManualCollector(session, config, run_id=new_run_id()).run(path=file)
        n_contacts = import_contacts(session, file) if contacts else 0
    console.print(
        f"[green]Импортировано[/green]: фактов {result.count_out} из {result.count_in}, "
        f"контактов {n_contacts}"
    )
    if result.details:
        console.print(f"Подробности: {result.details}")


@import_app.command("suppression")
def import_suppression(file: Path = typer.Option(..., "--file", "-f", exists=True)) -> None:
    """Пополнить стоп-лист (колонки: inn, email, domain, reason, note)."""
    from gtm.filters.suppression import import_suppression_csv

    with session_scope() as session:
        n = import_suppression_csv(session, file)
    console.print(f"[green]В стоп-лист добавлено[/green]: {n}")


@import_app.command("outcomes")
def import_outcomes(file: Path = typer.Option(..., "--file", "-f", exists=True)) -> None:
    """Импорт исходов касаний (колонки: outreach_id, outcome, note)."""
    from gtm.feedback import import_outcomes_csv

    with session_scope() as session:
        stats = import_outcomes_csv(session, file)
    console.print(f"[green]Исходы записаны[/green]: {stats}")


# -------------------------------------------------------------- collect


@app.command("collect")
def collect(
    source: str = typer.Argument(
        ..., help="venue_pages | fns_open_data | manual | events_archive | registry | zakupki | hh"
    ),
    backfill: int | None = typer.Option(None, help="Разовый сбор архива за N лет назад"),
    check: bool = typer.Option(
        False, "--check", help="Только сверить адреса, ничего не сохранять (venue_pages)"
    ),
    only: str | None = typer.Option(None, "--only", help="Одна площадка по части названия"),
) -> None:
    """Собрать один источник."""
    from gtm.collectors import get_collector

    config = _config()
    run_id = new_run_id()
    with session_scope() as session:
        collector = get_collector(source)(session, config, run_id=run_id)
        if backfill is not None:
            if not hasattr(collector, "backfill"):
                raise typer.BadParameter(f"Источник {source} не поддерживает бэкфилл")
            result = collector.backfill(years=backfill)
        elif check:
            # Сверка адресов: обходим страницы, но ничего не пишем в базу.
            # Нужна перед первым боевым запуском — список площадок в конфиге
            # собран без доступа к сети, адреса надо подтвердить.
            list(collector.collect(check=True, only=only))
            console.print(collector.check_report())
            return
        else:
            kwargs = {"only": only} if only else {}
            result = collector.run(**kwargs)
    console.print(
        f"[green]{source}[/green]: получено {result.count_in}, новых фактов {result.count_out}"
    )
    if result.details:
        console.print(f"Подробности: {result.details}")


@app.command("inspect")
def inspect_cmd(
    path: Path = typer.Argument(..., exists=True, help="Скачанный набор: zip, xml, csv или json"),
    limit: int = typer.Option(3, "--limit", help="Сколько записей показать"),
) -> None:
    """Что лежит в скачанном наборе открытых данных.

    Имена полей в наборах ФНС меняются между выпусками, а разметка
    в config/sources.yaml — предположение. Эта команда печатает фактическую
    структуру файла, чтобы расхождение правилось одной строкой в конфиге,
    а не выглядело как «коллектор ничего не нашёл».
    """
    from gtm.collectors.fns_open_data import inspect_file

    console.print(inspect_file(path, limit=limit))


# --------------------------------------------------------- expectations


@expectations_app.command("build")
def expectations_build(today: str | None = typer.Option(None, "--today")) -> None:
    """Построить ожидания из накопленных фактов."""
    from gtm.expectations import build_from_facts

    config = _config()
    day = _today(today)
    with session_scope() as session:
        result = build_from_facts(session, config, today=day, run_id=new_run_id())
    console.print(
        f"[green]Ожидания[/green]: из {result.count_in} фактов создано {result.count_out}"
    )
    if result.details:
        console.print(f"Подробности: {result.details}")


@expectations_app.command("rescore")
def expectations_rescore(today: str | None = typer.Option(None, "--today")) -> None:
    """Пересчитать приоритеты — после правки весов в config/signals.yaml."""
    from gtm.expectations import rescore_all

    config = _config()
    with session_scope() as session:
        n = rescore_all(session, config, today=_today(today))
    console.print(f"[green]Пересчитано[/green]: {n}")


# --------------------------------------------------------------- стадии


@app.command("filter")
def filter_cmd(
    today: str | None = typer.Option(None, "--today"),
    limit: int | None = typer.Option(None),
) -> None:
    """Применить ICP к ожиданиям с открытым окном. Без внешних запросов."""
    from gtm.filters import run_filter

    config = _config()
    with session_scope() as session:
        result = run_filter(
            session, config, today=_today(today), run_id=new_run_id(), limit=limit
        )
    console.print(f"[green]Фильтр[/green]: {result.count_in} → {result.count_out}")
    rejects = {k: v for k, v in result.details.items() if isinstance(v, int)}
    for code, n in sorted(rejects.items(), key=lambda kv: -kv[1]):
        console.print(f"  отсев {code}: {n}")


@app.command("enrich")
def enrich_cmd(limit: int | None = typer.Option(None, "--limit")) -> None:
    """Собрать досье по прошедшим фильтр. Здесь начинаются траты."""
    from gtm.enrich import run_enrich

    config = _config()
    with session_scope() as session:
        result = run_enrich(session, config, run_id=new_run_id(), limit=limit)
        console.print(f"[green]Обогащено[/green]: {result.count_in} → {result.count_out}")
        from gtm.spend import daily_report

        console.print(daily_report(session))


@app.command("generate")
def generate_cmd(limit: int | None = typer.Option(None, "--limit")) -> None:
    """Написать письма по собранным досье."""
    from gtm.generate import run_generate

    config = _config()
    with session_scope() as session:
        result = run_generate(session, config, run_id=new_run_id(), limit=limit)
    console.print(f"[green]Писем[/green]: {result.count_in} → {result.count_out}")
    if result.details:
        console.print(f"Подробности: {result.details}")


@app.command("deliver")
def deliver_cmd(
    today: str | None = typer.Option(None, "--today"),
    limit: int | None = typer.Option(None, "--limit"),
    out_dir: Path | None = typer.Option(None, "--out-dir"),
) -> None:
    """Выгрузить утренний список менеджеру."""
    from gtm.deliver import run_deliver

    config = _config()
    with session_scope() as session:
        result, paths = run_deliver(
            session,
            config,
            today=_today(today),
            run_id=new_run_id(),
            limit=limit,
            out_dir=out_dir,
        )
    console.print(f"[green]В списке[/green]: {result.count_out}")
    for path in paths:
        console.print(f"  {path}")


@app.command("daily")
def daily(
    today: str | None = typer.Option(None, "--today"),
    no_collect: bool = typer.Option(False, "--no-collect", help="Только обработка накопленного"),
    enrich_limit: int | None = typer.Option(None),
    generate_limit: int | None = typer.Option(None),
    out_dir: Path | None = typer.Option(None, "--out-dir"),
) -> None:
    """Весь конвейер за один прогон. Это то, что вешается в cron."""
    from gtm.pipeline import run_daily

    config = _config()
    with session_scope() as session:
        report = run_daily(
            session,
            config,
            today=_today(today),
            collect=not no_collect,
            enrich_limit=enrich_limit,
            generate_limit=generate_limit,
            out_dir=out_dir,
        )
    console.print(f"[bold]{report['summary']}[/bold]")
    console.print(f"Потрачено: {report['spent_rub']:.2f} ₽ ({report['api_calls']} вызовов)")
    for path in report["files"]:
        console.print(f"  {path}")


# ------------------------------------------------------------- feedback


@feedback_app.command("record")
def feedback_record(
    outreach_id: int = typer.Argument(...),
    outcome: str = typer.Argument(..., help="no_reply | reply_positive | meeting | booked | lost"),
    note: str | None = typer.Option(None),
    suppress: bool = typer.Option(False, help="Добавить в стоп-лист"),
) -> None:
    """Зафиксировать исход касания.

    Без этого фильтр не дообучается: у площадки цикл короткий и результат
    бинарный, это редкая возможность считать веса по фактам, а не гадать.
    """
    from gtm.feedback import record_outcome

    with session_scope() as session:
        row = record_outcome(session, outreach_id, outcome, note=note, suppress=suppress)
    console.print(f"[green]Записано[/green]: касание {row.id} → {row.outcome}")


@feedback_app.command("funnel")
def feedback_funnel() -> None:
    """Воронка: отправлено → ответы → показы зала → брони."""
    from gtm.feedback import funnel

    with session_scope() as session:
        data = funnel(session)
    table = Table("Показатель", "Значение")
    for key, value in data.items():
        table.add_row(str(key), str(value))
    console.print(table)


@feedback_app.command("recalibrate")
def feedback_recalibrate() -> None:
    """Предложение по весам сигналов.

    Конфиг не переписывается автоматически: правка YAML программой затирает
    комментарии, а автоматический сдвиг весов без человека — чёрный ящик,
    который потом не отладить. Фрагмент вставляется руками.
    """
    from gtm.feedback import recalibrate

    config = _config()
    with session_scope() as session:
        report = recalibrate(session, config)

    table = Table("Сигнал", "Исходов", "Положительных", "Конверсия", "Вес", "Предлагается")
    for kind, row in report.per_kind.items():
        table.add_row(
            kind,
            str(row.get("n", 0)),
            str(row.get("positives", 0)),
            f"{row.get('conversion', 0):.1%}",
            f"{row.get('current_weight', 0):.2f}",
            f"{row.get('proposed_weight', 0):.2f}",
        )
    console.print(table)
    for kind, reason in report.skipped.items():
        console.print(f"[yellow]{kind}[/yellow]: {reason}")
    if report.yaml_snippet:
        console.print("\n[bold]Вставить в config/signals.yaml:[/bold]")
        console.print(report.yaml_snippet)


# ----------------------------------------------------------- quarantine


@quarantine_app.command("list")
def quarantine_list(limit: int = typer.Option(20, "--limit")) -> None:
    """Записи, которые не удалось свести к компании."""
    with session_scope() as session:
        rows = repo.pending_quarantine(session, limit=limit)
        table = Table("ID", "Источник", "Название", "Причина", "Кандидаты")
        for row in rows:
            candidates = ", ".join(
                f"{c.get('name')} ({c.get('inn')})" for c in (row.candidates or [])[:3]
            )
            table.add_row(
                str(row.id), row.source, row.raw_name or "—", row.reason, candidates or "—"
            )
        console.print(table)


@quarantine_app.command("resolve")
def quarantine_resolve(
    quarantine_id: int = typer.Argument(...),
    inn: str = typer.Argument(...),
) -> None:
    """Привязать карантинную запись к ИНН вручную."""
    from gtm.resolve import resolve_quarantined

    with session_scope() as session:
        n = resolve_quarantined(session, quarantine_id, inn)
    console.print(f"[green]Привязано[/green]: обновлено фактов {n}")


# ---------------------------------------------------------------- stats


@app.command("stats")
def stats(run_id: str | None = typer.Option(None, "--run-id")) -> None:
    """Сводка последнего прогона и трат по API.

    Когда система выдаст пустой список, здесь видно, на какой стадии
    обнулилось.
    """
    from gtm.resolve import resolve_stats
    from gtm.spend import daily_report

    with session_scope() as session:
        if run_id is None:
            last = session.query(repo.StageRun).order_by(repo.StageRun.id.desc()).first()
            run_id = last.run_id if last else None
        if run_id:
            rows = repo.run_summary(session, run_id)
            console.print(f"[bold]Прогон {run_id}[/bold]: {format_run_summary(rows)}")
            table = Table("Стадия", "Вход", "Выход", "Ошибка")
            for row in rows:
                table.add_row(row["stage"], str(row["in"]), str(row["out"]), row["error"] or "")
            console.print(table)
        else:
            console.print("[yellow]Прогонов ещё не было[/yellow]")
        console.print(daily_report(session))
        console.print(f"Сведение: {resolve_stats(session)}")


if __name__ == "__main__":  # pragma: no cover
    app()
