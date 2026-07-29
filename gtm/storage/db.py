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

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from gtm.settings import get_settings
from gtm.storage.models import Base

_SESSION_FACTORIES: dict[str, sessionmaker[Session]] = {}


@lru_cache(maxsize=8)
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, future=True, connect_args=connect_args)
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
