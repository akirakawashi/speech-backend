"""Таблицы, которыми владеет Speech Analytics.

Живут в собственной базе на общем инстансе Postgres, поэтому наследуют
`SpeechBase`, а не общий `Base` — обоснование в `src/infrastructure/persistence/base.py`.

Проектная документация: `aicistent-documentation/speech-analytics/`,
раздел 3 дизайна. Протокол очереди целиком — в ADR очереди; таблица
`speech_jobs` ниже обязана ему соответствовать.

Перечисления — строковые колонки с проверочным ограничением, а не собственные
типы Postgres. Так принято в этом репозитории (`Message.role`), и на то есть
причина: `ALTER TYPE ... ADD VALUE` не выполняется внутри транзакции, то есть
добавление одного значения в перечисление превращает миграцию в особый случай.
"""

from __future__ import annotations

import uuid
from datetime import (
    date as dt_date,
    datetime,
)
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.application.dto.calls import (
    AnalysisStatus,
    AudioStatus,
    CallSource,
    ClassifierSource,
    DimensionSource,
    JobKind,
    JobLane,
    JobStatus,
    ProcessingStatus,
    Product,
    RoleMethod,
    Segment,
    Sentiment,
    Speaker,
)
from src.infrastructure.persistence.base import SpeechBase, utc_now


def _in(column: str, values: type[object]) -> str:
    """Условие `column IN (...)` из значений перечисления.

    Собирается из самого перечисления, чтобы ограничение в базе не разошлось с
    тем, что пишет код: разойдясь, оно превратится в отказ вставки на значении,
    которое приложение считает совершенно нормальным.
    """
    members = ", ".join(f"'{member.value}'" for member in values)  # type: ignore[attr-defined]
    return f"{column} IN ({members})"


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #


class SpeechNccProject(SpeechBase):
    """Проект NCC: входящая очередь или исходящий обзвон.

    `ingest_enabled` — рубильник сбора на проект (требование ING-08). Умолчание
    «включено»: новый проект колл-центра должен начать собираться сам, иначе
    его звонки тихо отсутствуют в поиске, а заметить это можно только по
    жалобе.
    """

    __tablename__ = "speech_ncc_projects"
    __table_args__ = (
        CheckConstraint(
            "segment IS NULL OR " + _in("segment", Segment), name="ck_speech_projects_segment"
        ),
        CheckConstraint(
            "product IS NULL OR " + _in("product", Product), name="ck_speech_projects_product"
        ),
        CheckConstraint(
            "ncc_uuid IS NOT NULL OR source_key IS NOT NULL", name="ck_speech_projects_identity"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: UUID проекта в NCC. Есть у проекта, известного по REST; у проекта из
    #: представления отчётной базы его нет — представление UUID не несёт
    #: (`changecallproject` пуст у всех строк, проверено 2026-09-15).
    ncc_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    #: Ключ, по которому проект заводит суточный приём:
    #: `reporting_view:<представление>`. Хотя бы одно из двух обязательно.
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    data_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    #: Сегмент клиента и линейка продукта этой очереди. NCC их не знает — это
    #: решение колл-центра, и заводит его администратор. Пусто до тех пор, и
    #: тогда значение звонка выводится из разговора (решение S-21).
    segment: Mapped[Segment | None] = mapped_column(String(16), nullable=True)
    product: Mapped[Product | None] = mapped_column(String(16), nullable=True)
    ingest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechOperator(SpeechBase):
    """Оператор колл-центра.

    Естественный ключ — `login`, а не `ncc_uuid`. Причина в источниках: логин
    приходит и из перечисления звонков, и из справочника сотрудников, а UUID —
    только из второго. Заводя оператора по UUID, мы не смогли бы записать
    звонок оператора, которого ещё не синхронизировали.
    """

    __tablename__ = "speech_operators"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ncc_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    login: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechTopic(SpeechBase):
    """Тема обращения из справочника.

    `code` стабилен и на латинице: именно его возвращает классификатор, и
    переименование темы в интерфейсе не должно ломать уже размеченные звонки.
    `llm_hint` — подсказка классификатору, когда выбирать эту тему; без неё
    близкие темы («Не работает интернет» и «Медленный интернет») различаются
    только названием, чего модели мало.

    Тема, уже назначенная звонкам, не удаляется, а деактивируется: удаление
    переписало бы историю разметки (требование ADM-01).
    """

    __tablename__ = "speech_topics"
    __table_args__ = (Index("speech_topics_parent_idx", "parent_id", "sort_order"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Крупная тема, если эта — уточнение. Два уровня, а не плоский путь:
    #: справочник владельца даёт 11 крупных тем и 88 уточнений, и уточнение
    #: уникально только вместе с родителем — восемь листов встречаются под
    #: двумя разными темами сразу.
    #:
    #: `RESTRICT`, а не `CASCADE`: удаление крупной темы не должно молча уносить
    #: её уточнения, а вместе с ними и разметку каждого звонка, который ими
    #: пользовался. Тема в обращении деактивируется, а не удаляется (ADM-01).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_topics.id", ondelete="RESTRICT"), nullable=True
    )
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#2ee6b8")
    llm_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechClassifierNode(SpeechBase):
    """Узел классификатора обращений — пятиуровневого дерева оператора (SP-310).

    Уровень 1 — корень; у остальных родитель — узел уровнем выше. Ключ — название
    после выравнивания написания (`label_key` из классификатора), и под одним
    родителем он не повторяется. У пустого уровня в середине пути название и
    ключ пустые: так уровни не сдвигаются.

    Узел не удаляется, а выключается: на него могут ссылаться звонки (ADM-01).
    """

    __tablename__ = "speech_classifier_nodes"
    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 5", name="ck_speech_classifier_level"),
        CheckConstraint("(parent_id IS NULL) = (level = 1)", name="ck_speech_classifier_root"),
        CheckConstraint(_in("source", ClassifierSource), name="ck_speech_classifier_source"),
        # Два частичных индекса, а не один по (parent_id, key): NULL в уникальном
        # индексе различны, и два корня с одним ключом прошли бы оба.
        Index(
            "speech_classifier_root_key_uq",
            "key",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
        Index(
            "speech_classifier_child_key_uq",
            "parent_id",
            "key",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
        Index("speech_classifier_parent_idx", "parent_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_classifier_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[ClassifierSource] = mapped_column(String(16), nullable=False)
    #: У узла файла — место в файле; у узла из NCC — после всех узлов файла.
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Когда путь встретился впервые: у узла из NCC это день, когда оператор его
    #: поставил и приём его увидел.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("now()"),
        nullable=False,
    )


class SpeechCallReason(SpeechBase):
    """Код справочника причин обращения для контакт-центра (S-32).

    Коды `K01…K99` неизменны и не переиспользуются. Код не удаляется, а
    выключается: на него ссылаются звонки, и их причина должна читаться так же,
    как в день разбора. Строки загружает `src.presentation.cli.seed_call_reasons` из файла.
    """

    __tablename__ = "speech_call_reasons"
    __table_args__ = (CheckConstraint("code ~ '^K[0-9]{2}$'", name="ck_speech_call_reasons_code"),)

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Указание модели — границы кода и правила выбора. Человеку не показывается.
    hint: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("now()"),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Звонок
# --------------------------------------------------------------------------- #


class SpeechCall(SpeechBase):
    """Один звонок, он же одна сессия NCC.

    Номер клиента хранится только маской для показа и хешем для группировки
    (требование ING-02). Сырой номер не хранится нигде, включая
    `source_snapshot`: снимок строки источника маскируется при записи. Это
    решение ИБ, а не оптимизация — оно убирает целый класс утечки, потому что
    утечь нечему.

    `completed_at` — колонка безопасности для политик хранения. Возраст строки
    не говорит, закончилась ли работа над ней; `NULL` здесь означает «не
    трогать», и это единственный честный ответ для звонка в обработке.
    """

    __tablename__ = "speech_calls"
    __table_args__ = (
        CheckConstraint(_in("audio_status", AudioStatus), name="ck_speech_calls_audio_status"),
        CheckConstraint(
            _in("processing_status", ProcessingStatus), name="ck_speech_calls_processing_status"
        ),
        CheckConstraint(
            _in("analysis_status", AnalysisStatus), name="ck_speech_calls_analysis_status"
        ),
        CheckConstraint(
            "sentiment IS NULL OR " + _in("sentiment", Sentiment),
            name="ck_speech_calls_sentiment",
        ),
        CheckConstraint(
            "segment IS NULL OR " + _in("segment", Segment), name="ck_speech_calls_segment"
        ),
        CheckConstraint(
            "product IS NULL OR " + _in("product", Product), name="ck_speech_calls_product"
        ),
        CheckConstraint(
            _in("segment_source", DimensionSource), name="ck_speech_calls_segment_source"
        ),
        CheckConstraint(
            _in("product_source", DimensionSource), name="ck_speech_calls_product_source"
        ),
        Index("speech_calls_segment_idx", "segment", "started_at"),
        Index("speech_calls_product_idx", "product", "started_at"),
        CheckConstraint("duration_s >= 0", name="ck_speech_calls_duration_non_negative"),
        CheckConstraint("ended_at >= started_at", name="ck_speech_calls_interval_ordered"),
        Index("speech_calls_started_idx", "started_at"),
        Index("speech_calls_operator_idx", "operator_id", "started_at"),
        Index("speech_calls_project_idx", "project_id", "started_at"),
        Index("speech_calls_topic_idx", "topic_id", "started_at"),
        Index("speech_calls_ai_topic_idx", "ai_topic_id", "started_at"),
        Index("speech_calls_disposition_idx", "disposition", "started_at"),
        Index("speech_calls_operator_node_idx", "operator_node_id", "started_at"),
        Index("speech_calls_ai_node_idx", "ai_node_id", "started_at"),
        CheckConstraint(
            "char_length(call_reason) <= 200", name="ck_speech_calls_call_reason_length"
        ),
        Index("speech_calls_call_reason_idx", "call_reason_code", "started_at"),
        CheckConstraint(
            "ai_topic_id IS NULL OR ai_topic_label IS NULL",
            name="ck_speech_calls_ai_topic_one_of",
        ),
        Index("speech_calls_status_idx", "processing_status", "started_at"),
        # Частичный индекс для удаления по сроку: он читает только звонки, у
        # которых аудио ещё лежит, а таких меньшинство от всего архива.
        Index(
            "speech_calls_audio_retention_idx",
            "started_at",
            postgresql_where="audio_status = 'stored'",
        ),
        Index("speech_calls_client_hash_idx", "client_number_hash"),
        CheckConstraint(_in("source", CallSource), name="ck_speech_calls_source"),
        Index("speech_calls_source_idx", "source", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: Источник звонка (фича 14): отчёты разделены по нему (S-35), воркер по нему
    #: выбирает, откуда качать запись.
    source: Mapped[CallSource] = mapped_column(
        String(16), nullable=False, server_default=text("'ncc'")
    )
    #: Ссылка на запись у источника, если она — секрет сама по себе: у Телфина
    #: это постоянная ссылка без аутентификации. Живёт до скачивания записи и
    #: стирается этапом `ingest` при любом его исходе; наружу не отдаётся ни
    #: одной ручкой, в снимок не пишется.
    source_audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_ncc_projects.id"), nullable=True
    )
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_operators.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Классификация оператора — узел классификатора обращений (SP-310): узел
    #: последнего уровня пути `uroven_1…5`, уровни выше — его предки.
    operator_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_classifier_nodes.id"), nullable=True
    )
    client_number_mask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_number_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Сегмент и продукт — **со своим источником каждый**. У значения из проекта
    #: и у догадки языковой модели разная цена ошибки, и отчёт, в котором они
    #: неотличимы, нельзя ни проверить, ни защитить (решение S-21). Тот же
    #: приём, что с `role_method` у ролей реплик.
    segment: Mapped[Segment | None] = mapped_column(String(16), nullable=True)
    segment_source: Mapped[DimensionSource] = mapped_column(
        String(16), nullable=False, server_default=text("'unknown'")
    )
    product: Mapped[Product | None] = mapped_column(String(16), nullable=True)
    product_source: Mapped[DimensionSource] = mapped_column(
        String(16), nullable=False, server_default=text("'unknown'")
    )

    audio_status: Mapped[AudioStatus] = mapped_column(
        String(32), nullable=False, default=AudioStatus.PENDING
    )
    audio_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    audio_channels: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    audio_stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audio_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        String(32), nullable=False, default=ProcessingStatus.NEW
    )
    analysis_status: Mapped[AnalysisStatus] = mapped_column(
        String(32), nullable=False, default=AnalysisStatus.PENDING
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_topics.id"), nullable=True
    )
    sentiment: Mapped[Sentiment | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Тема по предложению ИИ помощника (SP-309): тема справочника по суждению
    #: модели — или новая формулировка, которой в справочнике нет. Ровно одно
    #: из двух: у темы справочника подпись берётся из справочника.
    ai_topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_topics.id"), nullable=True
    )
    ai_topic_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Классификация ИИ (SP-310): узел классификатора, до которого путь из ответа
    #: модели нашёлся в дереве; уровни выше — его предки.
    ai_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_classifier_nodes.id"), nullable=True
    )
    #: Тема по предложению ИИ помощника путём (SP-309 поверх SP-310): названия
    #: уровней, любое из которых может быть формулировкой модели. Узел — если
    #: весь путь есть в дереве.
    ai_suggestion: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    ai_suggestion_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_classifier_nodes.id"), nullable=True
    )
    #: Причина обращения (S-32): зачем звонил клиент — фраза со стороны клиента и
    #: код справочника. Оценка модели по расшифровке, а не запись
    #: оператора. Пусто — звонок не разобран с причиной.
    call_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_reason_code: Mapped[str | None] = mapped_column(
        String(8),
        ForeignKey("speech_call_reasons.code", ondelete="RESTRICT"),
        nullable=True,
    )

    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


# --------------------------------------------------------------------------- #
# Расшифровка
# --------------------------------------------------------------------------- #


class SpeechTranscript(SpeechBase):
    """Расшифровка звонка — ровно одна на звонок.

    Полного текста здесь нет намеренно: он собирается из реплик. Дубль стоил бы
    несколько гигабайт в год и, что хуже, создавал бы второй источник правды,
    расходящийся с репликами.

    `created_at` — момент, когда записан текст. Переобработка удаляет
    расшифровку и создаёт заново, поэтому по нему же судят, свежее ли оценка
    звонка, чем его текст (`src.infrastructure.persistence.quality_transcript`).

    `model_digest` хранится, потому что качество распознавания привязано к
    конкретной сборке движка: при разборе плохой расшифровки первый вопрос —
    какой моделью она сделана.
    """

    __tablename__ = "speech_transcripts"
    __table_args__ = (
        CheckConstraint(_in("role_method", RoleMethod), name="ck_speech_transcripts_role_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speech_calls.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="ru")
    role_method: Mapped[RoleMethod] = mapped_column(
        String(32), nullable=False, default=RoleMethod.NONE
    )
    duration_s: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    segments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SpeechTranscriptSegment(SpeechBase):
    """Реплика: кто, когда и что сказал.

    Полнотекстовый индекс построен по реплике, а не по звонку. Один проход по
    нему даёт сразу три величины, которые показывает интерфейс: число
    совпадений, число разговоров и таймкоды цитат для перемотки.

    Текст один — `text_raw`, как его дал Whisper: обезличивания нет, второй
    копии с масками тоже. По нему построен индекс, его же видят экран, модели и
    выгрузки.

    `search_vector` — генерируемая колонка, а не поле, обновляемое приложением:
    индекс пересчитывается в той же транзакции, что и текст, и состояния
    «текст обновлён, индекс отстал» не существует.
    """

    __tablename__ = "speech_transcript_segments"
    __table_args__ = (
        UniqueConstraint("transcript_id", "seq", name="uq_speech_segments_transcript_seq"),
        CheckConstraint(_in("speaker", Speaker), name="ck_speech_segments_speaker"),
        CheckConstraint("end_ms >= start_ms", name="ck_speech_segments_interval_ordered"),
        CheckConstraint("start_ms >= 0", name="ck_speech_segments_start_non_negative"),
        Index("speech_segments_fts_idx", "search_vector", postgresql_using="gin"),
        Index("speech_segments_call_idx", "call_id", "seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_transcripts.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_calls.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[Speaker] = mapped_column(String(16), nullable=False, default=Speaker.UNKNOWN)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text_raw: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('russian', text_raw)", persisted=True),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Очередь заданий (протокол — в ADR очереди)
# --------------------------------------------------------------------------- #


class SpeechJob(SpeechBase):
    """Задание конвейера.

    Уникальность `(call_id, kind)` — ключ идемпотентности: у звонка не бывает
    двух заданий одного этапа. Повторная обработка выполняется обновлением
    строки в `queued`, а не вставкой второй.

    Частичные индексы под два единственных запроса, которые к таблице ходят
    часто: захват смотрит только ожидающие, сборщик просроченных — только
    выполняющиеся. Полные индексы по тем же колонкам обслуживали бы заодно и
    завершённые задания, которых со временем становится большинство.
    """

    __tablename__ = "speech_jobs"
    __table_args__ = (
        UniqueConstraint("call_id", "kind", name="uq_speech_jobs_call_kind"),
        CheckConstraint(_in("kind", JobKind), name="ck_speech_jobs_kind"),
        CheckConstraint(_in("status", JobStatus), name="ck_speech_jobs_status"),
        CheckConstraint(_in("lane", JobLane), name="ck_speech_jobs_lane"),
        CheckConstraint("attempts >= 0", name="ck_speech_jobs_attempts_non_negative"),
        CheckConstraint("max_attempts > 0", name="ck_speech_jobs_max_attempts_positive"),
        CheckConstraint("lease_seconds > 0", name="ck_speech_jobs_lease_positive"),
        Index(
            "speech_jobs_claim_idx",
            "lane",
            "priority",
            "created_at",
            postgresql_where="status = 'queued'",
        ),
        Index(
            "speech_jobs_running_idx",
            "locked_at",
            postgresql_where="status = 'running'",
        ),
        Index("speech_jobs_call_idx", "call_id"),
        Index("speech_jobs_status_idx", "status", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_calls.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[JobKind] = mapped_column(String(32), nullable=False)
    lane: Mapped[JobLane] = mapped_column(String(16), nullable=False)
    status: Mapped[JobStatus] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    #: Меньше — раньше. realtime = 0, backfill = 100.
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Состояние сбора, настройки, журнал, пользователи
# --------------------------------------------------------------------------- #


class SpeechIngestCursor(SpeechBase):
    """Докуда дошёл сбор. Одна строка на источник.

    Курсор по **дате**, а не watermark по времени последнего звонка: сбор
    суточный (решение S-19), и единица работы здесь — сутки целиком.

    Идемпотентность он не обеспечивает — её дают уникальные ключи `session_id`
    и `(call_id, kind)`. Он отвечает на другой вопрос: за какие даты прогон
    вообще запускался. Без него «за эту дату звонков не было» неотличимо от
    «за эту дату прогон не запускался», а это нормальный выходной против
    пропущенных суток.
    """

    __tablename__ = "speech_ingest_cursors"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_processed_day: Mapped[dt_date | None] = mapped_column(Date, nullable=True)
    backfill_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `server_default`, а не только `default`: умолчание на стороне Python
    # знает лишь ORM, а в эти таблицы пишут и прямым SQL — суточный прогон,
    # операторские команды, восстановление. Колонка `NOT NULL` без умолчания в
    # схеме превращает любой такой `INSERT` в отказ по несуществующей причине.
    backfill_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    backfill_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechSetting(SpeechBase):
    """Настройки, которые меняет администратор из интерфейса.

    В базе, а не в окружении: их правит человек через интерфейс, и правка не
    должна требовать выкатки. Умолчания — в `src/domain/calls/contracts.py`.
    """

    __tablename__ = "speech_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechAuditEvent(SpeechBase):
    """Журнал действий с персональными данными.

    Фиксируется **факт** доступа, а не то, что было получено: раскрытый текст,
    содержимое разговора и персональные данные в `payload` не попадают. Журнал,
    хранящий то, ради ограничения доступа к чему он ведётся, — вторая копия
    той же утечки.
    """

    __tablename__ = "speech_audit_events"
    __table_args__ = (
        Index("speech_audit_time_idx", "occurred_at"),
        Index("speech_audit_type_idx", "event_type", "occurred_at"),
        Index("speech_audit_actor_idx", "actor_sub", "occurred_at"),
        Index("speech_audit_call_idx", "call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_roles: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_calls.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class SpeechUser(SpeechBase):
    """Кого сервис видел.

    Источник правды по ролям — Keycloak; здесь лежит только последнее
    увиденное значение, чтобы администратор мог понять, с какими правами
    человек заходил, не открывая Keycloak.
    """

    __tablename__ = "speech_users"

    sub: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roles_last_seen: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SpeechSession(SpeechBase):
    """Сессия BFF: браузер держит куку, сервер — всё остальное.

    Таблица, а не зашифрованная кука, и это выбор с ценой. Кука не требует
    ни миграции, ни выборки на запрос, но её **нельзя отозвать**: уволенный
    сотрудник ходит до истечения срока, а в этом сервисе за доступом лежат
    разговоры клиентов. Строка отзывается одним `UPDATE`.

    Токен доступа здесь не хранится намеренно. После входа он не нужен: роли
    уже разобраны и лежат в `roles`, а наш API к Keycloak на каждый запрос не
    обращается. Хранится только `refresh_token` — им сессия продлевается и
    перечитываются группы, если человека перевели.

    В `id` лежит **хеш** значения куки, а не само значение. Утечка дампа базы
    тогда не даёт живых сессий: по хешу куку не восстановить.
    """

    __tablename__ = "speech_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sub: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Непустое — сессия погашена. Не удаление строки: администратору нужно
    #: видеть, что вход был и когда его прекратили.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


# --------------------------------------------------------------------------- #
# Оценка качества
# --------------------------------------------------------------------------- #


class SpeechCallEvaluation(SpeechBase):
    """Оценка звонка по скрипту — одна на звонок и редакцию скрипта.

    Вердикты этапов и возражения лежат в `jsonb`, а не в отдельных таблицах:
    читаются они всегда вместе с оценкой, а сводка разворачивает их одним
    `jsonb_array_elements`. Отдельные таблицы дали бы три вставки на звонок ради
    запроса, который нужен одному отчёту.

    Балл и «полностью корректный» хранятся, хотя выводятся из этапов: по ним
    считают сводку, и выводить их заново из `jsonb` в каждом запросе значит
    держать одну арифметику в двух местах. Считает её `src/application/quality/evaluation.py`.

    Звонок, к которому скрипт не применим, тоже получает строку — без баллов.
    Иначе команда оценки брала бы его снова при каждом запуске.
    """

    __tablename__ = "speech_call_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "call_id", "script_version", name="uq_speech_call_evaluations_call_script"
        ),
        CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100", name="ck_speech_call_evaluations_score"
        ),
        CheckConstraint(
            "script_applies OR (score IS NULL AND all_passed IS NULL)",
            name="ck_speech_call_evaluations_not_applicable",
        ),
        CheckConstraint(
            "(failure_reason IS NULL AND script_applies IS NOT NULL) OR "
            "(failure_reason IS NOT NULL AND script_applies IS NULL AND score IS NULL "
            "AND passed_count IS NULL AND stage_count IS NULL AND all_passed IS NULL)",
            name="ck_speech_call_evaluations_failure",
        ),
        # Сводка читает одну редакцию скрипта за раз.
        Index("speech_call_evaluations_script_idx", "script_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speech_calls.id", ondelete="CASCADE"), nullable=False
    )
    #: Редакция скрипта (`CallScript.version`), по которой поставлена оценка.
    script_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Модель, которая оценивала: при разборе спорной оценки первый вопрос — чья она.
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: None — оценка не получена; failure_reason объясняет отказ без балла.
    script_applies: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcript_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Доля засчитанных этапов, проценты.
    score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    passed_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    stage_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    all_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    objections: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Момент, когда прочитаны реплики, по которым поставлена оценка. Оценка
    #: старше `speech_transcripts.created_at` устарела: звонок переобработали, и
    #: её ссылки ведут в прежние реплики. Такую не показывают и ставят заново.
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


__all__ = [
    "SpeechAuditEvent",
    "SpeechCall",
    "SpeechCallEvaluation",
    "SpeechClassifierNode",
    "SpeechIngestCursor",
    "SpeechJob",
    "SpeechNccProject",
    "SpeechOperator",
    "SpeechSession",
    "SpeechSetting",
    "SpeechTopic",
    "SpeechTranscript",
    "SpeechTranscriptSegment",
    "SpeechUser",
]
