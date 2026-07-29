"""Точка входа alembic.

URL берётся из gtm.settings, а не из alembic.ini: иначе GTM_DATABASE_URL
управлял бы приложением, но не миграциями, и в какой-то момент миграции
поедут не в ту базу.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from gtm.settings import get_settings
from gtm.storage.models import Base

config = context.config

# Автогенерация сравнивает схему в базе именно с этими метаданными.
target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def _configure(**kwargs: object) -> None:
    url = _database_url()
    context.configure(
        target_metadata=target_metadata,
        # SQLite не умеет большинство ALTER-ов. Без batch-режима любая
        # будущая миграция со сменой типа или удалением колонки на SQLite
        # (то есть на всех тестах и локальной разработке) упадёт.
        render_as_batch=url.startswith("sqlite"),
        # Иначе смена типа колонки в моделях не попадёт в автогенерацию
        # и тест на расхождение поймает её только по факту.
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Сгенерировать SQL без подключения — для ревью и ручного наката на прод."""
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
