"""Speech Analytics: the operator's five-level classifier as a tree (SP-310).

Owner decisions 2026-09-15: the AI classifies a call along the same five levels
the operator uses in NCC (`uroven_1…5`), so that the two compare level by level
(S-22, revising S-20). The tree is the owner's file «Тематики (финал) вариант 2»
plus every path the operators set in NCC: the daily ingest adds a path the tree
lacks, and an administrator switches off what is noise.

A node is a level of a path. Its key is the label with the spelling evened out,
unique among the children of one parent. An empty level inside a path is a node
with an empty label, so that the levels keep their places. A node is never
deleted, only switched off: calls may point at it (ADM-01).

`speech_calls.operator_node_id` is the operator's classification — the node of
the last level of the path.

The tree is filled by `python -m src.presentation.cli.seed_classifier --apply` (the file),
by the daily ingest, and once by `python -m src.presentation.cli.classifier_backfill`
(the days the reporting views still hold).

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0009_classifier
Revises: sp_0008_call_disposition
Create Date: 2026-09-15
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0009_classifier"
down_revision = "sp_0008_call_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speech_classifier_nodes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # RESTRICT, not CASCADE: a node in use is switched off, never deleted.
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["speech_classifier_nodes.id"],
            name="fk_speech_classifier_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("level BETWEEN 1 AND 5", name="ck_speech_classifier_level"),
        sa.CheckConstraint("(parent_id IS NULL) = (level = 1)", name="ck_speech_classifier_root"),
        sa.CheckConstraint("source IN ('file', 'ncc')", name="ck_speech_classifier_source"),
    )
    # Two partial indexes rather than one on (parent_id, key): in a unique index
    # NULLs are distinct, and two roots with one key would both be accepted.
    op.create_index(
        "speech_classifier_root_key_uq",
        "speech_classifier_nodes",
        ["key"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "speech_classifier_child_key_uq",
        "speech_classifier_nodes",
        ["parent_id", "key"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )
    op.create_index(
        "speech_classifier_parent_idx", "speech_classifier_nodes", ["parent_id", "sort_order"]
    )

    op.add_column("speech_calls", sa.Column("operator_node_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "speech_calls_operator_node_id_fkey",
        "speech_calls",
        "speech_classifier_nodes",
        ["operator_node_id"],
        ["id"],
    )
    op.create_index(
        "speech_calls_operator_node_idx", "speech_calls", ["operator_node_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("speech_calls_operator_node_idx", table_name="speech_calls")
    op.drop_constraint("speech_calls_operator_node_id_fkey", "speech_calls", type_="foreignkey")
    op.drop_column("speech_calls", "operator_node_id")
    op.drop_index("speech_classifier_parent_idx", table_name="speech_classifier_nodes")
    op.drop_index("speech_classifier_child_key_uq", table_name="speech_classifier_nodes")
    op.drop_index("speech_classifier_root_key_uq", table_name="speech_classifier_nodes")
    op.drop_table("speech_classifier_nodes")
