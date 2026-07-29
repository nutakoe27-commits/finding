"""Общие фикстуры.

Тесты идут на SQLite в памяти файла (не :memory:, чтобы работали несколько
соединений). Схема создаётся из метаданных, миграции проверяются отдельно.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gtm.config import Config, load_config
from gtm.storage.db import create_all, get_engine, get_session_factory
from gtm.storage.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def session(db_url: str) -> Iterator[Session]:
    create_all(db_url)
    factory = get_session_factory(db_url)
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()
        Base.metadata.drop_all(get_engine(db_url))


@pytest.fixture()
def config() -> Config:
    return load_config(REPO_ROOT / "config")


@pytest.fixture()
def today() -> date:
    """Фиксированная «сегодня» — конец июля: окно продаж корпоративного
    сезона открывается в августе, на этой дате строятся сценарии."""
    return date(2026, 7, 29)


@pytest.fixture()
def company_factory(session: Session):
    from gtm.storage.repo import upsert_company

    # ИНН по умолчанию проходит контрольную сумму — иначе резолвер отправит
    # компанию в карантин и тесты стадий будут врать.
    def _make(inn: str = "7701234560", **fields):
        fields.setdefault("name", f"ООО Тест {inn[-4:]}")
        fields.setdefault("name_norm", "тест")
        fields.setdefault("region_code", "77")
        fields.setdefault("city", "Москва")
        fields.setdefault("headcount", 500)
        fields.setdefault("revenue", {"2025": 1_500_000_000.0})
        fields.setdefault("registered_at", date(2011, 3, 15))
        fields.setdefault("status", "active")
        return upsert_company(session, inn, **fields)

    return _make
