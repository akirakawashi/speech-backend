"""Speech Analytics: client segment and product line as searchable dimensions.

Owner decision S-21. These are the top two levels of the topic reference that
are not topics: the segment belongs to the client and the product to what is
being discussed, and both are properties of the NCC project rather than of the
conversation.

Two columns per dimension, not one. The value can come from the project -- which
an administrator has mapped, and which is therefore a fact -- or from the
conversation, where a language model inferred it. Storing only the value would
produce a report in which the two are indistinguishable, and such a report can
neither be checked nor defended.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0004_segment_product
Revises: sp_0003_topic_parent
Create Date: 2026-09-09
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0004_segment_product"
down_revision = "sp_0003_topic_parent"
branch_labels = None
depends_on = None

_SEGMENTS = "('b2b', 'individual')"
_PRODUCTS = "('wallet', 'mobile', 'broadband')"
_SOURCES = "('project', 'llm', 'unknown')"


def upgrade() -> None:
    # The project carries the authoritative mapping. Nullable: NCC does not know
    # it, an administrator enters it, and until then the call falls back to what
    # the conversation says.
    op.add_column("speech_ncc_projects", sa.Column("segment", sa.String(16), nullable=True))
    op.add_column("speech_ncc_projects", sa.Column("product", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_speech_projects_segment",
        "speech_ncc_projects",
        f"segment IS NULL OR segment IN {_SEGMENTS}",
    )
    op.create_check_constraint(
        "ck_speech_projects_product",
        "speech_ncc_projects",
        f"product IS NULL OR product IN {_PRODUCTS}",
    )

    op.add_column("speech_calls", sa.Column("segment", sa.String(16), nullable=True))
    op.add_column("speech_calls", sa.Column("product", sa.String(16), nullable=True))
    op.add_column(
        "speech_calls",
        sa.Column("segment_source", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "speech_calls",
        sa.Column("product_source", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.create_check_constraint(
        "ck_speech_calls_segment", "speech_calls", f"segment IS NULL OR segment IN {_SEGMENTS}"
    )
    op.create_check_constraint(
        "ck_speech_calls_product", "speech_calls", f"product IS NULL OR product IN {_PRODUCTS}"
    )
    op.create_check_constraint(
        "ck_speech_calls_segment_source", "speech_calls", f"segment_source IN {_SOURCES}"
    )
    op.create_check_constraint(
        "ck_speech_calls_product_source", "speech_calls", f"product_source IN {_SOURCES}"
    )

    # One index per dimension, leading with the value and ending with the date:
    # every filtered query also orders by date, and an index that stops at the
    # value leaves the sort to be done by hand.
    op.create_index("speech_calls_segment_idx", "speech_calls", ["segment", "started_at"])
    op.create_index("speech_calls_product_idx", "speech_calls", ["product", "started_at"])


def downgrade() -> None:
    op.drop_index("speech_calls_product_idx", table_name="speech_calls")
    op.drop_index("speech_calls_segment_idx", table_name="speech_calls")
    for name in (
        "ck_speech_calls_product_source",
        "ck_speech_calls_segment_source",
        "ck_speech_calls_product",
        "ck_speech_calls_segment",
    ):
        op.drop_constraint(name, "speech_calls", type_="check")
    for column in ("product_source", "segment_source", "product", "segment"):
        op.drop_column("speech_calls", column)

    for name in ("ck_speech_projects_product", "ck_speech_projects_segment"):
        op.drop_constraint(name, "speech_ncc_projects", type_="check")
    for column in ("product", "segment"):
        op.drop_column("speech_ncc_projects", column)
