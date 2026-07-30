"""Подключение к БД.

Целевая база — PostgreSQL (полнотекстовый поиск по названиям компаний
при сведении записей и нормальная работа с JSON). SQLite поддерживается
для локальной разработки и тестов: объёмы это позволяют, а на CI
Postgres не всегда есть.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker

from gtm.settings import get_settings
from gtm.storage.models import Base

_SESSION_FACTORIES: dict[str, sessionmaker[Session]] = {}


def sqlite_path(url: str) -> Path | None:
    """Путь к файлу базы для файловых SQLite-адресов, иначе None."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def ensure_sqlite_dir(url: str) -> None:
    """Создать каталог под файл базы.

    SQLite не создаёт родительский каталог сам и падает с «unable to open
    database file». Каталог var/ в git не попадает (он в .gitignore), поэтому
    на свежем клоне первая же команда упиралась в это. Сообщение при том
    ничего не объясняет, так что дешевле создать каталог, чем объяснять.
    """
    path = sqlite_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


class DriverMissing(RuntimeError):
    """Драйвер под указанный адрес базы не установлен."""


# Что делать, если драйвер не стоит. Голый ModuleNotFoundError из глубины
# SQLAlchemy не подсказывает ничего: пользователь видит «No module named
# psycopg» и не знает, что psycopg у нас необязательная зависимость.
_DRIVER_HINTS = {
    "psycopg": "pip install -e '.[postgres]'",
    "psycopg2": "pip install -e '.[postgres]'",
}


@lru_cache(maxsize=8)
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    connect_args = {}
    if url.startswith("sqlite"):
        ensure_sqlite_dir(url)
        connect_args["check_same_thread"] = False
    try:
        engine = create_engine(url, future=True, connect_args=connect_args)
    except ModuleNotFoundError as exc:
        hint = _DRIVER_HINTS.get(exc.name or "", "")
        raise DriverMissing(
            f"Адрес базы {url!r} требует драйвер '{exc.name}', а он не установлен."
            + (f" Поставьте его: {hint}." if hint else "")
            + " Либо переключитесь на SQLite: GTM_DATABASE_URL=sqlite:///var/gtm.db"
            " (проверьте также файл .env — настройки читаются и оттуда)."
        ) from exc
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = get_engine(url)
    key = str(engine.url)
    if key not in _SESSION_FACTORIES:
        _SESSION_FACTORIES[key] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SESSION_FACTORIES[key]


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Транзакция на блок. Коммит на выходе, откат на исключении."""
    factory = get_session_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(url: str | None = None) -> None:
    """Создать схему из метаданных.

    Для первого запуска и тестов. Изменения схемы после этого идут
    через alembic (gtm/storage/migrations).
    """
    Base.metadata.create_all(get_engine(url))


def drop_all(url: str | None = None) -> None:
    Base.metadata.drop_all(get_engine(url))
