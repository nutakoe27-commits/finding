"""Начальная схема: девять таблиц из gtm/storage/models.py.

Порядок создания повторяет порядок в моделях и одновременно совпадает
с порядком зависимостей по внешним ключам (company -> fact -> expectation
-> contact -> outreach), поэтому его нельзя тасовать произвольно.

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company",
        sa.Column("inn", sa.String(length=12), nullable=False),
        sa.Column("ogrn", sa.String(length=15), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("name_full", sa.String(length=1024), nullable=True),
        sa.Column("name_norm", sa.String(length=512), nullable=False),
        sa.Column("region_code", sa.String(length=3), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("okved", sa.String(length=16), nullable=True),
        sa.Column("registered_at", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=True),
        sa.Column("headcount_year", sa.Integer(), nullable=True),
        sa.Column("revenue", sa.JSON(), nullable=False),
        sa.Column("site", sa.String(length=512), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(inn) in (10, 12)", name="ck_company_inn_length"),
        sa.PrimaryKeyConstraint("inn"),
    )
    op.create_index("ix_company_name_norm", "company", ["name_norm"])
    op.create_index("ix_company_registered_at", "company", ["registered_at"])

    op.create_table(
        "fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_uid", sa.String(length=256), nullable=False),
        sa.Column("fact_type", sa.String(length=64), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("company_name_raw", sa.String(length=512), nullable=True),
        sa.Column("occurred_at", sa.Date(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inn"], ["company.inn"]),
        sa.PrimaryKeyConstraint("id"),
        # На этом ключе держится идемпотентность коллекторов.
        sa.UniqueConstraint("source", "source_uid", name="uq_fact_source_uid"),
    )
    op.create_index("ix_fact_inn", "fact", ["inn"])
    op.create_index("ix_fact_type_occurred", "fact", ["fact_type", "occurred_at"])

    op.create_table(
        "expectation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("expected_at", sa.Date(), nullable=False),
        sa.Column("expected_precision", sa.String(length=16), nullable=False),
        sa.Column("window_opens_at", sa.Date(), nullable=False),
        sa.Column("window_closes_at", sa.Date(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expected_attendees", sa.Integer(), nullable=True),
        sa.Column("source_fact_id", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("dossier", sa.JSON(), nullable=False),
        sa.Column("filter_verdict", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inn"], ["company.inn"]),
        sa.ForeignKeyConstraint(["source_fact_id"], ["fact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key", name="uq_expectation_dedup"),
    )
    # Главный запрос системы: «окно контакта открылось сегодня, статус new».
    op.create_index("ix_expectation_window", "expectation", ["window_opens_at", "status"])
    op.create_index("ix_expectation_status_score", "expectation", ["status", "score"])

    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=False),
        sa.Column("full_name", sa.String(length=256), nullable=True),
        sa.Column("position", sa.String(length=256), nullable=True),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("consent_status", sa.String(length=32), nullable=False),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inn"], ["company.inn"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inn", "email", name="uq_contact_inn_email"),
    )
    op.create_index("ix_contact_email", "contact", ["email"])

    op.create_table(
        "outreach",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("expectation_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("generated_by", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"]),
        sa.ForeignKeyConstraint(["expectation_id"], ["expectation.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_status_outcome", "outreach", ["status", "outcome"])

    op.create_table(
        "suppression",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("domain", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_suppression_inn", "suppression", ["inn"])
    op.create_index("ix_suppression_email", "suppression", ["email"])
    op.create_index("ix_suppression_domain", "suppression", ["domain"])

    op.create_table(
        "quarantine",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_uid", sa.String(length=256), nullable=False),
        sa.Column("raw_name", sa.String(length=512), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolved_inn", sa.String(length=12), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_uid", name="uq_quarantine_source_uid"),
    )
    op.create_index("ix_quarantine_status", "quarantine", ["status"])

    op.create_table(
        "api_spend",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=256), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("units", sa.Float(), nullable=False),
        sa.Column("cost_rub", sa.Float(), nullable=False),
        sa.Column("request_key", sa.String(length=256), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_spend_ts_provider", "api_spend", ["ts", "provider"])

    op.create_table(
        "stage_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("count_in", sa.Integer(), nullable=False),
        sa.Column("count_out", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stage_run_run_id", "stage_run", ["run_id"])


def downgrade() -> None:
    # Строго обратный порядок: иначе внешние ключи не дадут удалить таблицу.
    # Индексы уходят вместе с таблицами, отдельный drop_index не нужен.
    op.drop_table("stage_run")
    op.drop_table("api_spend")
    op.drop_table("quarantine")
    op.drop_table("suppression")
    op.drop_table("outreach")
    op.drop_table("contact")
    op.drop_table("expectation")
    op.drop_table("fact")
    op.drop_table("company")
