"""Тест на расхождение моделей и миграций.

Схему в проекте можно получить двумя путями: create_all из метаданных
(тесты, первый запуск) и alembic upgrade (прод). Если эти два пути
разъедутся, тесты останутся зелёными на одной схеме, а прод поедет
на другой — и узнаем мы об этом по падению в бою.

Поэтому здесь схема строится строго миграциями, а потом сверяется
с Base.metadata: таблицы, колонки, типы, nullable, первичные ключи,
индексы и уникальные ограничения.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext

from gtm.settings import reset_settings_cache
from gtm.storage.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Служебная таблица самого alembic: в моделях её нет и быть не должно.
VERSION_TABLE = "alembic_version"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


def _shape_from_database(url: str) -> dict[str, dict]:
    """Форма схемы, как её видит inspect в реальной базе."""
    engine = sa.create_engine(url)
    try:
        insp = sa.inspect(engine)
        shape = {}
        for table in insp.get_table_names():
            if table == VERSION_TABLE:
                continue
            shape[table] = {
                "columns": {
                    c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns(table)
                },
                "pk": tuple(insp.get_pk_constraint(table)["constrained_columns"]),
                "indexes": {i["name"]: tuple(i["column_names"]) for i in insp.get_indexes(table)},
                "unique": {
                    u["name"]: tuple(u["column_names"])
                    for u in insp.get_unique_constraints(table)
                },
            }
        return shape
    finally:
        engine.dispose()


def _shape_from_metadata(metadata: sa.MetaData) -> dict[str, dict]:
    """Та же форма, посчитанная по моделям. Сравнивать имеет смысл только
    одинаково устроенные структуры, поэтому обе стороны сводим к одному виду."""
    shape = {}
    for table in metadata.tables.values():
        shape[table.name] = {
            "columns": {c.name: (str(c.type), c.nullable) for c in table.columns},
            "pk": tuple(c.name for c in table.primary_key.columns),
            "indexes": {i.name: tuple(c.name for c in i.columns) for i in table.indexes},
            "unique": {
                c.name: tuple(col.name for col in c.columns)
                for c in table.constraints
                if isinstance(c, sa.UniqueConstraint)
            },
        }
    return shape


@pytest.fixture()
def migrated_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Пустая база, накатанная миграциями.

    URL передаётся через GTM_DATABASE_URL — ровно так же, как в проде,
    так что тест заодно проверяет, что env.py действительно читает настройки.
    """
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("GTM_DATABASE_URL", url)
    reset_settings_cache()
    try:
        command.upgrade(_alembic_config(), "head")
        yield url
    finally:
        reset_settings_cache()


def test_upgrade_creates_database_at_env_url(migrated_url: str, tmp_path: Path) -> None:
    """env.py берёт URL из настроек, а не из alembic.ini."""
    assert (tmp_path / "migrated.db").exists()


def test_all_nine_tables_created(migrated_url: str) -> None:
    tables = set(_shape_from_database(migrated_url))
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 9


def test_migrated_schema_matches_models(migrated_url: str) -> None:
    """Главная проверка: забыли миграцию — тест красный."""
    assert _shape_from_database(migrated_url) == _shape_from_metadata(Base.metadata)


def test_autogenerate_sees_no_pending_changes(migrated_url: str) -> None:
    """Дублирующая проверка глазами самого alembic: с compare_type=True
    она ловит и смену типа колонки, которую сравнение форм может проглядеть."""
    engine = sa.create_engine(migrated_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == []


def test_drift_in_models_is_detected(migrated_url: str) -> None:
    """Проверка, что предыдущие тесты не вырожденные: добавляем в модели
    колонку, миграции для которой нет, — расхождение обязано вылезти."""
    drifted = sa.MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(drifted)
    drifted.tables["company"].append_column(sa.Column("loyalty_tier", sa.String(16)))

    assert _shape_from_database(migrated_url) != _shape_from_metadata(drifted)

    engine = sa.create_engine(migrated_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), drifted)
    engine.dispose()
    assert any("loyalty_tier" in repr(d) for d in diff)


def test_indexes_and_unique_constraints_survive_migration(migrated_url: str) -> None:
    """Индексы и уникальные ключи автогенератор роняет чаще всего, а без
    uq_fact_source_uid ломается идемпотентность коллекторов."""
    shape = _shape_from_database(migrated_url)
    assert shape["fact"]["unique"]["uq_fact_source_uid"] == ("source", "source_uid")
    assert shape["expectation"]["unique"]["uq_expectation_dedup"] == ("dedup_key",)
    assert shape["quarantine"]["unique"]["uq_quarantine_source_uid"] == ("source", "source_uid")
    assert shape["contact"]["unique"]["uq_contact_inn_email"] == ("inn", "email")
    # Индекс под главный ежедневный запрос: окно открылось + статус.
    assert shape["expectation"]["indexes"]["ix_expectation_window"] == (
        "window_opens_at",
        "status",
    )


def test_foreign_keys_point_to_company_inn(migrated_url: str) -> None:
    """ИНН — единственный канонический ключ компании; миграция обязана
    сохранять эти связи, иначе факты будут висеть в воздухе."""
    engine = sa.create_engine(migrated_url)
    insp = sa.inspect(engine)
    for table in ("fact", "expectation", "contact"):
        targets = {
            (fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in insp.get_foreign_keys(table)
        }
        assert ("company", ("inn",)) in targets
    engine.dispose()


def test_downgrade_base_removes_all_tables_and_upgrade_repeats(migrated_url: str) -> None:
    """downgrade должен работать, а не быть заглушкой: после отката
    остаётся только служебная таблица версий, и накатить можно заново."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")

    engine = sa.create_engine(migrated_url)
    assert set(sa.inspect(engine).get_table_names()) <= {VERSION_TABLE}
    engine.dispose()

    command.upgrade(cfg, "head")
    assert _shape_from_database(migrated_url) == _shape_from_metadata(Base.metadata)


def test_offline_mode_emits_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Офлайн-режим нужен, чтобы накат на прод можно было сначала прочитать
    глазами. Подключение при этом не открывается — база не создаётся."""
    url = f"sqlite:///{tmp_path / 'never-created.db'}"
    monkeypatch.setenv("GTM_DATABASE_URL", url)
    reset_settings_cache()
    try:
        command.upgrade(_alembic_config(), "head", sql=True)
    finally:
        reset_settings_cache()

    sql = capsys.readouterr().out
    assert "CREATE TABLE company" in sql
    assert "CREATE TABLE expectation" in sql
    assert not (tmp_path / "never-created.db").exists()
