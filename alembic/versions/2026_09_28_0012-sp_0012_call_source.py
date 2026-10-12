"""Speech Analytics: a second source of calls — outbound calls from Telphin (feature 14).

Owner decisions 2026-09-28: S-35 (the same pipeline, separate reporting) and
S-36 (what is collected). Until now every call came from NCC and nothing said
so; now a call carries its source, because the reports are split by it and the
worker picks the downloader by it.

`source_audio_url` holds a recording link that is a secret by itself — at
Telphin a permanent link to the recording without authentication. It lives
from the listing until the recording is fetched: the `ingest` stage clears it
whatever the outcome. No endpoint returns it and the snapshot never holds it.

Existing calls are NCC calls: the server default fills them without a rewrite
of the table (Postgres 11+ keeps a constant default in the catalogue).

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0012_call_source
Revises: sp_0011_call_reasons
Create Date: 2026-09-28
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0012_call_source"
down_revision = "sp_0011_call_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "speech_calls",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="ncc"),
    )
    op.create_check_constraint(
        "ck_speech_calls_source", "speech_calls", "source IN ('ncc', 'telphin')"
    )
    # Reports and the interface narrow by source first, then by date.
    op.create_index("speech_calls_source_idx", "speech_calls", ["source", "started_at"])
    op.add_column("speech_calls", sa.Column("source_audio_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("speech_calls", "source_audio_url")
    op.drop_index("speech_calls_source_idx", table_name="speech_calls")
    op.drop_constraint("ck_speech_calls_source", "speech_calls", type_="check")
    op.drop_column("speech_calls", "source")
