"""Speech Analytics: the reason a customer called, for the contact centre (S-32).

Owner decision 2026-09-24. The analysis stage now also says why the customer
called — one phrase from the customer's side and one code of a reference the
contact centre works with (`K01…K99`, the owner's sample of 2026-09-24). It is
the model's reading of the masked transcript, not the operator's classification
in NCC and not the topic of the conversation.

`speech_call_reasons` is the reference. Codes never change and are never
reused: a code is switched off, not deleted, because calls keep pointing at it.
The rows are loaded from a file (`python -m src.presentation.cli.seed_call_reasons
--apply`), as the classifier is — not here, so edits stay visible line by line.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0011_call_reasons
Revises: sp_0010_ai_classification
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0011_call_reasons"
down_revision = "sp_0010_ai_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speech_call_reasons",
        sa.Column("code", sa.String(length=8), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.CheckConstraint("code ~ '^K[0-9]{2}$'", name="ck_speech_call_reasons_code"),
    )

    op.add_column("speech_calls", sa.Column("call_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_speech_calls_call_reason_length",
        "speech_calls",
        "char_length(call_reason) <= 200",
    )
    op.add_column("speech_calls", sa.Column("call_reason_code", sa.String(length=8), nullable=True))
    # RESTRICT: a code that calls point at is switched off, never deleted.
    op.create_foreign_key(
        "speech_calls_call_reason_code_fkey",
        "speech_calls",
        "speech_call_reasons",
        ["call_reason_code"],
        ["code"],
        ondelete="RESTRICT",
    )
    # The report groups the calls of a period by reason, and narrows by date.
    op.create_index(
        "speech_calls_call_reason_idx", "speech_calls", ["call_reason_code", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("speech_calls_call_reason_idx", table_name="speech_calls")
    op.drop_constraint("speech_calls_call_reason_code_fkey", "speech_calls", type_="foreignkey")
    op.drop_column("speech_calls", "call_reason_code")
    op.drop_constraint("ck_speech_calls_call_reason_length", "speech_calls", type_="check")
    op.drop_column("speech_calls", "call_reason")
    op.drop_table("speech_call_reasons")
