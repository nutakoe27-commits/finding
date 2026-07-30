"""Подключение к базе и первый запуск.

Тесты здесь про один класс ошибок: система должна работать на свежем клоне,
где нет ничего, кроме того, что лежит в git. Каталог var/ в git не попадает,
и на этом спотыкалась первая же команда.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from gtm.storage.db import (
    DriverMissing,
    create_all,
    ensure_sqlite_dir,
    get_engine,
    sqlite_path,
)
from gtm.storage.models import Base


def test_sqlite_path_extracted_from_url(tmp_path: Path):
    target = tmp_path / "var" / "gtm.db"
    assert sqlite_path(f"sqlite:///{target}") == target


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "postgresql+psycopg://gtm:gtm@localhost:5432/gtm",
    ],
)
def test_non_file_urls_have_no_path(url):
    """Файлового пути у памяти и у Postgres нет — создавать каталог нечего."""
    assert sqlite_path(url) is None


def test_missing_parent_directory_is_created(tmp_path: Path):
    """SQLite не создаёт родительский каталог сам и падает с «unable to open
    database file» — сообщение, из которого причина не видна вовсе."""
    target = tmp_path / "var" / "gtm.db"
    assert not target.parent.exists()

    ensure_sqlite_dir(f"sqlite:///{target}")

    assert target.parent.is_dir()


def test_nested_directories_are_created(tmp_path: Path):
    target = tmp_path / "a" / "b" / "c" / "gtm.db"

    ensure_sqlite_dir(f"sqlite:///{target}")

    assert target.parent.is_dir()


def test_existing_directory_is_not_a_problem(tmp_path: Path):
    ensure_sqlite_dir(f"sqlite:///{tmp_path / 'gtm.db'}")
    ensure_sqlite_dir(f"sqlite:///{tmp_path / 'gtm.db'}")


def test_create_all_works_on_a_fresh_clone(tmp_path: Path):
    """Сквозной случай: ровно то, что делает `gtm db init` в каталоге,
    где var/ ещё нет."""
    url = f"sqlite:///{tmp_path / 'var' / 'gtm.db'}"

    create_all(url)

    tables = set(inspect(get_engine(url)).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_memory_url_still_works():
    """Тесты и разовые проверки ходят в память — путь к файлу там пустой,
    и создание каталога не должно на этом падать."""
    create_all("sqlite:///:memory:")


def test_missing_driver_says_what_to_do():
    """Голый «No module named psycopg» из глубины SQLAlchemy не подсказывает
    ничего: пользователь не знает, что драйвер у нас необязательный, и что
    адрес мог приехать из .env, а не из его команды."""
    pytest.importorskip("sqlalchemy")
    if _psycopg_installed():
        pytest.skip("драйвер установлен — проверять нечего")

    with pytest.raises(DriverMissing) as excinfo:
        get_engine("postgresql+psycopg://gtm:gtm@localhost:5432/gtm")

    message = str(excinfo.value)
    assert "psycopg" in message
    assert "[postgres]" in message, "нужна команда установки"
    assert "sqlite" in message, "нужен путь отступления"
    assert ".env" in message, "адрес мог приехать оттуда"


def _psycopg_installed() -> bool:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True
