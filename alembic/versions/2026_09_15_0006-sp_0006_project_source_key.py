"""Speech Analytics: projects known by a source key, not only by an NCC UUID.

The daily source since 2026-09-15 is a pair of reporting views in the NCC
database, and those views carry no project UUID: `changecallproject` is empty in
every row, and the REST API cannot list projects at all. A project of that
source is identified by the view it comes from. Nothing created project rows
before this, so every call had no project and the queue filter was empty.

`ncc_uuid` becomes optional and `source_key` is added; a row needs at least one
of the two. Directory sync asks NCC only about rows that have a UUID.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0006_project_source_key
Revises: sp_0005_speech_sessions
Create Date: 2026-09-15
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0006_project_source_key"
down_revision = "sp_0005_speech_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "speech_ncc_projects", sa.Column("source_key", sa.String(length=128), nullable=True)
    )
    # The name PostgreSQL gives an inline UNIQUE, as `ncc_uuid` got in sp_0001:
    # a model declared with `unique=True` and this migration then agree.
    op.create_unique_constraint(
        "speech_ncc_projects_source_key_key", "speech_ncc_projects", ["source_key"]
    )
    op.alter_column(
        "speech_ncc_projects", "ncc_uuid", existing_type=sa.String(length=64), nullable=True
    )
    op.create_check_constraint(
        "ck_speech_projects_identity",
        "speech_ncc_projects",
        "ncc_uuid IS NOT NULL OR source_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_speech_projects_identity", "speech_ncc_projects", type_="check")
    # A project known only by its source key keeps the key as its identifier
    # instead of being deleted: calls reference the row, and deleting it would
    # take the queue name of every one of them along.
    op.execute(
        "UPDATE speech_ncc_projects SET ncc_uuid = left(source_key, 64) WHERE ncc_uuid IS NULL"
    )
    op.alter_column(
        "speech_ncc_projects", "ncc_uuid", existing_type=sa.String(length=64), nullable=False
    )
    op.drop_constraint("speech_ncc_projects_source_key_key", "speech_ncc_projects", type_="unique")
    op.drop_column("speech_ncc_projects", "source_key")
