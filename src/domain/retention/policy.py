"""How long each table keeps its rows, and what makes a row safe to delete.

`the retention job`. Seven tables in this schema are append-only and grow on every chat
turn. **Not one of them had a retention policy** -- no pruning code existed
anywhere in the backend. On a cluster shared with LLM whose measured connection
headroom is one, unbounded growth is not a housekeeping detail; it is
an input to the physical-split decision this task has to make.

The central point, and the reason this is a declaration rather than a `DELETE
... WHERE created_at < now() - interval`:

**The age column is not the safety column.** A row in `workflow_event_outbox` is
old the moment it is written, and it is safe to delete only once it has been
published. Pruning by `created_at` would delete pending work -- an event that
was never delivered, removed because it was slow. So each policy names the
column that proves the row's work is *finished*, and age is measured from that
column. A table whose completion column is NULL is a table with nothing to
prune, however old the rows are.

Two windows are correctness constraints rather than storage preferences, and
they are marked as such:

* `workflow_command_receipts` -- these are idempotency receipts. Delete one and
  a replayed command executes a second time. The window must exceed the longest
  interval in which a replay can arrive, not the interval that would be
  convenient.
* `audit_events` -- the security journal. A short window here is not tidiness,
  it is destroying the record of who did what. One year, and the number is a
  decision an owner may raise but should not lower without saying why.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

DAY_SECONDS: Final = 86_400


class RetentionPolicy(BaseModel):
    """Срок хранения завершённых строк и основание выбранного срока."""

    model_config = ConfigDict(frozen=True)

    table: str
    #: The column proving the row's work is finished. Rows where it is NULL are
    #: never pruned -- they are pending, not old.
    completion_column: str
    days: int = Field(ge=1)
    reason: str
    #: `storage` -- the window is a capacity choice and may be tuned.
    #: `correctness` -- shortening it changes behaviour, not just disk use.
    #: `compliance` -- shortening it destroys a record someone is entitled to.
    basis: Literal["storage", "correctness", "compliance"] = "storage"


RETENTION_POLICIES: tuple[RetentionPolicy, ...] = (
    RetentionPolicy(
        table="audit_events",
        completion_column="created_at",
        days=365,
        basis="compliance",
        reason="security journal; the record of who did what to which resource",
    ),
    RetentionPolicy(
        table="workflow_run_stages",
        completion_column="created_at",
        days=30,
        reason=(
            "per-stage timings, written on every turn and read by no code at all -- "
            "the largest purely diagnostic table in the schema. Thirty days is "
            "enough to investigate an incident and not enough to accumulate a year"
        ),
    ),
    RetentionPolicy(
        table="workflow_event_outbox",
        completion_column="published_at",
        days=14,
        reason="published events; an unpublished row is pending work and is never pruned",
    ),
    RetentionPolicy(
        table="bff_event_inbox",
        completion_column="processed_at",
        days=14,
        reason="processed inbound events; an unprocessed row is pending work",
    ),
    RetentionPolicy(
        table="bff_workflow_dispatch_outbox",
        completion_column="completed_at",
        days=14,
        reason="completed dispatches; an incomplete one is still owed a delivery",
    ),
    RetentionPolicy(
        table="workflow_event_delivery_acks",
        completion_column="acked_at",
        days=14,
        reason="delivery acknowledgements, useful only while the matching outbox row exists",
    ),
    RetentionPolicy(
        table="workflow_command_receipts",
        completion_column="created_at",
        days=90,
        basis="correctness",
        reason=(
            "idempotency receipts. Deleting one lets a replayed command run twice, "
            "so the window has to outlast the longest possible replay, not the "
            "longest convenient one"
        ),
    ),
    RetentionPolicy(
        table="workflow_runs",
        completion_column="completed_at",
        days=90,
        reason="terminal runs; a run without a completion time is still in flight",
    ),
    # --- Файлы беседы -------------------------------------------------------
    RetentionPolicy(
        table="conversation_files",
        completion_column="object_deleted_at",
        days=30,
        reason=(
            "надгробие: объект уже удалён, строка доживает месяц, чтобы панель и "
            "история объясняли, почему файл исчез. Само окно хранения файла — "
            "сто восемьдесят дней — исполняет `app.attachments.prune`, а не эта "
            "политика: она удаляет строку после удаления объекта, а не вместо него"
        ),
    ),
    RetentionPolicy(
        table="conversation_file_tables",
        completion_column="object_deleted_at",
        days=30,
        reason=(
            "листы файла беседы, переведённые в таблицы (ADR-0017). Объекты Parquet "
            "удаляет вместе с файлом `purge` или `app.attachments.prune` и ставит "
            "отметку на все строки файла; строка доживает месяц, как строка файла"
        ),
    ),
    RetentionPolicy(
        table="chat_attachment_jobs",
        completion_column="finished_at",
        days=30,
        reason=(
            "завершённые задания обработки. Задание без времени завершения ещё "
            "стоит в очереди, выполняется или ждёт человека — удаление по "
            "возрасту стёрло бы работу, которая не сделана"
        ),
    ),
    RetentionPolicy(
        table="conversation_document_chunks",
        completion_column="created_at",
        days=190,
        reason=(
            "страховка, а не основной механизм. Куски удаляет этап `purge` "
            "вместе с файлом, и в норме сюда ничего не доживает: файл живёт "
            "сто восемьдесят дней. Сто девяносто — тот же срок, что у "
            "lifecycle бакета: всё, что старше, пережило собственный файл, то "
            "есть уборка за ним умерла. Без этой строки такой кусок остался бы "
            "навсегда, и поиск цитировал бы документ, которого нет"
        ),
    ),
    # --- Speech Analytics ---------------------------------------------------
    RetentionPolicy(
        table="speech_audit_events",
        completion_column="occurred_at",
        days=365,
        basis="compliance",
        reason=(
            "who listened to a recording, who downloaded it, who exported a "
            "list. Same year as the main audit journal and for the "
            "same reason: shortening it destroys the record of access to "
            "personal data, which is the thing the journal exists to prove"
        ),
    ),
    RetentionPolicy(
        table="speech_jobs",
        completion_column="finished_at",
        days=30,
        reason=(
            "finished pipeline jobs. A job without a finish time is still "
            "queued, running or dead awaiting a human -- pruning by age would "
            "delete work that was never done"
        ),
    ),
)

POLICY_BY_TABLE: dict[str, RetentionPolicy] = {p.table: p for p in RETENTION_POLICIES}

#: Delete order for the Speech Analytics prune job, kept separately from
#: `DELETE_ORDER` in `src/infrastructure/retention/cleanup.py`.
#:
#: Not a stylistic split. That job opens the shared database and cannot reach
#: these tables at all -- Speech Analytics owns a database of its own. A single
#: order would read as one job pruning everything and would fail on the first
#: speech table with "relation does not exist", which looks like a broken
#: migration rather than a job pointed at the wrong database.
#:
#: Children before parents, same rule as the other order.
SPEECH_DELETE_ORDER: tuple[str, ...] = (
    "speech_jobs",
    "speech_audit_events",
)

#: Tables that are not append-only: rows are updated and deleted in the course
#: of ordinary use, and their size follows the business, not the clock. Named
#: explicitly so the gate can tell "considered and exempt" from "overlooked".
NOT_APPEND_ONLY: frozenset[str] = frozenset(
    {
        "agents",
        "conversations",
        "external_model_providers",
        "external_provider_models",
        "knowledge_documents",
        "knowledge_source_mappings",
        "messages",
        "model_showcase_access",
        "model_showcase_items",
        "prompt_versions",
        "tool_registry_entries",
        "users",
        # Speech Analytics: reference data and per-user state, edited in the course
        # of ordinary use. Their size follows the call centre, not the clock.
        "speech_ingest_cursors",
        "speech_ncc_projects",
        "speech_operators",
        "speech_settings",
        "speech_topics",
        "speech_users",
    }
)

#: Tables kept **forever, on purpose**, and that is a third answer -- not the
#: same as "not append-only".
#:
#: The two states above could not describe them honestly. These tables do grow
#: with the clock, so calling them not-append-only would be false; and they have
#: no deletion window, so a `RetentionPolicy` would have nothing to say. Left in
#: either category the decision would read as an oversight, which is precisely
#: what this file exists to prevent.
#:
#: Every entry is an owner's decision with a reason, and each is a candidate for
#: a window if Data Protection asks for one -- see open question 4 in
#: `aicistent-documentation/speech-analytics/04-security-data-protection.md`.
RETAINED_INDEFINITELY: dict[str, str] = {
    # Оценка диалога (specs/chat-session-rating). Причина — по-английски, как у
    # соседей: гейт data-retention печатает её, а консоль Windows без UTF-8
    # падала на кириллице (UnicodeEncodeError, проверка выпуска 2026-09-22).
    "conversation_ratings": (
        "one current rating per general-chat conversation: a number, ids, dates and "
        "the user's optional free-text comment about the assistant (decision "
        "2026-09-27). The comment is PII-masked by the guardrails sidecar before "
        "storage, erasable by its author while the conversation exists, and blanked "
        "when the user deletes the conversation; the number is kept after deletion "
        "(owner's decision 2026-09-22). Not the conversation's content. Candidate for "
        "a comment retention window if Data Protection asks"
    ),
    # История ревизий оценки (PHASE_2_DESIGN §4.3, §4.5): растёт с часами, окна
    # нет — та же третья категория, что у основной таблицы.
    "conversation_rating_revisions": (
        "append-only history of each rating's revisions: score, revision number, "
        "comment length and flags, answer count and last answer id at that moment, "
        "timestamp. No comment text, no conversation content. Kept as long as the "
        "rating itself (owner's decision 2026-09-27); rows leave only by cascade when "
        "the rating row is deleted"
    ),
    "speech_calls": (
        "call metadata: the searchable record of which conversations happened. "
        "Audio expires after 180 days; the card outlives it deliberately, so a "
        "transcript stays findable after the recording is gone (decision S-07)"
    ),
    "speech_transcripts": (
        "one row per call, carrying which engine produced the transcript. "
        "Deleting it would orphan the segments below it"
    ),
    "speech_transcript_segments": (
        "the transcripts themselves and the search index built from them. "
        "Retained indefinitely by owner decision S-07; this is the table Data "
        "Protection is most likely to want a window on, and adding one here is "
        "a policy entry plus an entry in the delete order, nothing more"
    ),
}
