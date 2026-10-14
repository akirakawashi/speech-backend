"""Текущий результат анализа и переиспользуемые неизменяемые снимки справочников."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.application.dto.analysis_storage import AnalysisBindingKind
from src.application.dto.calls import Product, Segment, Sentiment
from src.infrastructure.persistence.base import SpeechBase, utc_now
from src.infrastructure.persistence.models.speech import SpeechCall, SpeechTranscript


class SpeechAnalysisReference(SpeechBase):
    """Один снимок отправленных справочников; одинаковый хеш не создаёт новый документ."""

    __tablename__ = "speech_analysis_references"
    __table_args__ = (
        UniqueConstraint("schema_version", "fingerprint", name="uq_speech_analysis_reference_hash"),
        CheckConstraint("schema_version > 0", name="ck_speech_analysis_reference_version"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_speech_analysis_reference_hash"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_speech_analysis_reference_payload"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )


class SpeechCallAnalysis(SpeechBase):
    """Один актуальный разбор звонка; причина может быть получена отдельным запросом."""

    __tablename__ = "speech_call_analyses"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_speech_call_analysis_call"),
        ForeignKeyConstraint(
            ["call_id", "transcript_id"],
            [f"{SpeechTranscript.__tablename__}.call_id", f"{SpeechTranscript.__tablename__}.id"],
            name="fk_speech_call_analysis_transcript",
            ondelete="CASCADE",
        ),
        CheckConstraint("revision > 0", name="ck_speech_call_analysis_revision"),
        CheckConstraint(
            "binding_kind IN ('verified', 'legacy_observed')",
            name="ck_speech_call_analysis_binding",
        ),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('negative', 'neutral', 'positive')",
            name="ck_speech_call_analysis_sentiment",
        ),
        CheckConstraint(
            "llm_segment IS NULL OR llm_segment IN ('b2b', 'individual')",
            name="ck_speech_call_analysis_segment",
        ),
        CheckConstraint(
            "llm_product IS NULL OR llm_product IN ('wallet', 'mobile', 'broadband')",
            name="ck_speech_call_analysis_product",
        ),
        CheckConstraint(
            "char_length(call_reason) <= 200", name="ck_speech_call_analysis_reason_length"
        ),
        CheckConstraint(
            "ai_path IS NULL OR jsonb_typeof(ai_path) = 'array'",
            name="ck_speech_call_analysis_path",
        ),
        CheckConstraint(
            "ai_suggestion IS NULL OR jsonb_typeof(ai_suggestion) = 'array'",
            name="ck_speech_call_analysis_suggestion",
        ),
        CheckConstraint(
            "analysis_provenance IS NOT NULL OR reason_provenance IS NOT NULL",
            name="ck_speech_call_analysis_component",
        ),
        CheckConstraint(
            "analysis_provenance IS NULL OR jsonb_typeof(analysis_provenance) = 'object'",
            name="ck_speech_call_analysis_provenance",
        ),
        CheckConstraint(
            "reason_provenance IS NULL OR jsonb_typeof(reason_provenance) = 'object'",
            name="ck_speech_call_analysis_reason_provenance",
        ),
        CheckConstraint(
            "analysis_provenance IS NULL OR "
            "COALESCE(analysis_provenance->>'origin', '') IN ('llm', 'legacy_import')",
            name="ck_speech_call_analysis_origin",
        ),
        CheckConstraint(
            "reason_provenance IS NULL OR "
            "COALESCE(reason_provenance->>'origin', '') IN ('llm', 'legacy_import')",
            name="ck_speech_call_analysis_reason_origin",
        ),
        CheckConstraint(
            "binding_kind <> 'verified' OR ("
            "(analysis_provenance IS NULL OR "
            "COALESCE(analysis_provenance->>'origin', '') = 'llm') AND "
            "(reason_provenance IS NULL OR "
            "COALESCE(reason_provenance->>'origin', '') = 'llm'))",
            name="ck_speech_call_analysis_verified",
        ),
        CheckConstraint(
            "COALESCE(analysis_provenance->>'origin', '') <> 'llm' OR "
            "(analysis_reference_id IS NOT NULL AND sentiment IS NOT NULL)",
            name="ck_speech_call_analysis_computed",
        ),
        CheckConstraint(
            "COALESCE(reason_provenance->>'origin', '') <> 'llm' OR "
            "reason_reference_id IS NOT NULL",
            name="ck_speech_call_analysis_reason_computed",
        ),
        CheckConstraint(
            "(call_reason IS NOT NULL OR call_reason_code IS NOT NULL) = "
            "(reason_provenance IS NOT NULL)",
            name="ck_speech_call_analysis_reason_component",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_speech_call_analysis_time"),
        Index(
            "speech_call_analyses_node_idx",
            "ai_node_id",
            "call_id",
            postgresql_where=text("ai_node_id IS NOT NULL"),
        ),
        Index(
            "speech_call_analyses_reason_idx",
            "call_reason_code",
            "call_id",
            postgresql_where=text("call_reason_code IS NOT NULL"),
        ),
        Index("speech_call_analyses_reference_idx", "analysis_reference_id"),
        Index("speech_call_analyses_reason_reference_idx", "reason_reference_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SpeechCall.__tablename__}.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    binding_kind: Mapped[AnalysisBindingKind] = mapped_column(String(16), nullable=False)
    ai_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_classifier_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ai_path: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    ai_suggestion: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    ai_suggestion_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_classifier_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sentiment: Mapped[Sentiment | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_segment: Mapped[Segment | None] = mapped_column(String(16), nullable=True)
    llm_product: Mapped[Product | None] = mapped_column(String(16), nullable=True)
    call_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_reason_code: Mapped[str | None] = mapped_column(
        String(8),
        ForeignKey("speech_call_reasons.code", ondelete="RESTRICT"),
        nullable=True,
    )
    analysis_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_analysis_references.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_analysis_references.id", ondelete="RESTRICT"),
        nullable=True,
    )
    analysis_provenance: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    reason_provenance: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )
