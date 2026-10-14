"""Добавочное хранение актуального анализа без переноса прежних результатов.

Revision ID: sp_0017_analysis_storage
Revises: sp_0016_evaluation_jobs
Create Date: 2026-10-14

Писатели и читатели пока используют прежние колонки speech_calls. Миграция
создаёт только основание переноса; рабочие данные и очередь не переписываются.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "sp_0017_analysis_storage"
down_revision = "sp_0016_evaluation_jobs"
branch_labels = None
depends_on = None

_ANALYSIS_CHECKS = (
    ("ck_speech_call_analysis_revision", "revision > 0"),
    ("ck_speech_call_analysis_binding", "binding_kind IN ('verified', 'legacy_observed')"),
    (
        "ck_speech_call_analysis_sentiment",
        "sentiment IS NULL OR sentiment IN ('negative', 'neutral', 'positive')",
    ),
    (
        "ck_speech_call_analysis_segment",
        "llm_segment IS NULL OR llm_segment IN ('b2b', 'individual')",
    ),
    (
        "ck_speech_call_analysis_product",
        "llm_product IS NULL OR llm_product IN ('wallet', 'mobile', 'broadband')",
    ),
    ("ck_speech_call_analysis_reason_length", "char_length(call_reason) <= 200"),
    ("ck_speech_call_analysis_path", "ai_path IS NULL OR jsonb_typeof(ai_path) = 'array'"),
    (
        "ck_speech_call_analysis_suggestion",
        "ai_suggestion IS NULL OR jsonb_typeof(ai_suggestion) = 'array'",
    ),
    (
        "ck_speech_call_analysis_component",
        "analysis_provenance IS NOT NULL OR reason_provenance IS NOT NULL",
    ),
    (
        "ck_speech_call_analysis_provenance",
        "analysis_provenance IS NULL OR jsonb_typeof(analysis_provenance) = 'object'",
    ),
    (
        "ck_speech_call_analysis_reason_provenance",
        "reason_provenance IS NULL OR jsonb_typeof(reason_provenance) = 'object'",
    ),
    (
        "ck_speech_call_analysis_origin",
        "analysis_provenance IS NULL OR "
        "COALESCE(analysis_provenance->>'origin', '') IN ('llm', 'legacy_import')",
    ),
    (
        "ck_speech_call_analysis_reason_origin",
        "reason_provenance IS NULL OR "
        "COALESCE(reason_provenance->>'origin', '') IN ('llm', 'legacy_import')",
    ),
    (
        "ck_speech_call_analysis_verified",
        "binding_kind <> 'verified' OR ("
        "(analysis_provenance IS NULL OR "
        "COALESCE(analysis_provenance->>'origin', '') = 'llm') AND "
        "(reason_provenance IS NULL OR "
        "COALESCE(reason_provenance->>'origin', '') = 'llm'))",
    ),
    (
        "ck_speech_call_analysis_computed",
        "COALESCE(analysis_provenance->>'origin', '') <> 'llm' OR "
        "(analysis_reference_id IS NOT NULL AND sentiment IS NOT NULL)",
    ),
    (
        "ck_speech_call_analysis_reason_computed",
        "COALESCE(reason_provenance->>'origin', '') <> 'llm' OR reason_reference_id IS NOT NULL",
    ),
    (
        "ck_speech_call_analysis_reason_component",
        "(call_reason IS NOT NULL OR call_reason_code IS NOT NULL) = "
        "(reason_provenance IS NOT NULL)",
    ),
    ("ck_speech_call_analysis_time", "updated_at >= created_at"),
)


def upgrade() -> None:
    """Создать таблицы/ограничения после sp_0016; сохранить все существующие данные."""
    op.create_unique_constraint(
        "uq_speech_transcripts_call_identity", "speech_transcripts", ["call_id", "id"]
    )
    op.create_table(
        "speech_analysis_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schema_version", "fingerprint", name="uq_speech_analysis_reference_hash"
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_speech_analysis_reference_version"),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'", name="ck_speech_analysis_reference_hash"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_speech_analysis_reference_payload"
        ),
    )
    op.create_table(
        "speech_call_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("binding_kind", sa.String(16), nullable=False),
        sa.Column("ai_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_path", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("ai_suggestion", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("ai_suggestion_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sentiment", sa.String(16), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("llm_segment", sa.String(16), nullable=True),
        sa.Column("llm_product", sa.String(16), nullable=True),
        sa.Column("call_reason", sa.Text(), nullable=True),
        sa.Column("call_reason_code", sa.String(8), nullable=True),
        sa.Column("analysis_reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason_reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("analysis_provenance", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("reason_provenance", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id", name="uq_speech_call_analysis_call"),
        sa.ForeignKeyConstraint(["call_id"], ["speech_calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["call_id", "transcript_id"],
            ["speech_transcripts.call_id", "speech_transcripts.id"],
            name="fk_speech_call_analysis_transcript",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ai_node_id"], ["speech_classifier_nodes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ai_suggestion_node_id"], ["speech_classifier_nodes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["call_reason_code"], ["speech_call_reasons.code"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_reference_id"], ["speech_analysis_references.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reason_reference_id"], ["speech_analysis_references.id"], ondelete="RESTRICT"
        ),
        *(sa.CheckConstraint(expression, name=name) for name, expression in _ANALYSIS_CHECKS),
    )
    op.create_index(
        "speech_call_analyses_node_idx",
        "speech_call_analyses",
        ["ai_node_id", "call_id"],
        postgresql_where=sa.text("ai_node_id IS NOT NULL"),
    )
    op.create_index(
        "speech_call_analyses_reason_idx",
        "speech_call_analyses",
        ["call_reason_code", "call_id"],
        postgresql_where=sa.text("call_reason_code IS NOT NULL"),
    )
    op.create_index(
        "speech_call_analyses_reference_idx", "speech_call_analyses", ["analysis_reference_id"]
    )
    op.create_index(
        "speech_call_analyses_reason_reference_idx", "speech_call_analyses", ["reason_reference_id"]
    )


def downgrade() -> None:
    """Удалить только добавочное хранение и его FK; применять после остановки писателей.

    Side Effects:
        Новые результаты и снимки удаляются. Прежние колонки звонка и данные
        остальных таблиц сохраняются; это не откат переноса будущего этапа.
    """
    op.drop_table("speech_call_analyses")
    op.drop_table("speech_analysis_references")
    op.drop_constraint("uq_speech_transcripts_call_identity", "speech_transcripts", type_="unique")
