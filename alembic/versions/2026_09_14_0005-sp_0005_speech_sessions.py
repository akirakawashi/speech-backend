"""Speech Analytics: server-side sessions for the browser (BFF).

The Keycloak client issued to this project is confidential and will not be
changed. A browser cannot hold a client secret, so the authorization-code
exchange has to happen on the server: the service talks to Keycloak, the
browser gets a cookie. That cookie needs somewhere to point.

A table rather than an encrypted cookie, and the difference is revocation. An
encrypted cookie needs no migration and no lookup, but it cannot be withdrawn:
whoever holds it stays in until it expires. Behind this door are recordings of
customer conversations, so "wait for the token to lapse" is not an answer. A
row is revoked with one UPDATE.

The primary key is a hash of the cookie value, not the value. A leaked dump
then yields no live sessions.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0005_speech_sessions
Revises: sp_0004_segment_product
Create Date: 2026-09-14
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0005_speech_sessions"
down_revision = "sp_0004_segment_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speech_sessions",
        # sha256 of the cookie value, hex -- 64 characters exactly.
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        # Roles resolved at login. Kept here so that an ordinary request costs
        # one indexed lookup rather than a round trip to Keycloak.
        sa.Column("roles", sa.ARRAY(sa.String(length=64)), nullable=False),
        # Only the refresh token. The access token is not needed once the roles
        # are resolved, and storing what is not needed is how a database
        # becomes worth stealing.
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Non-null means withdrawn. Not a DELETE: an administrator needs to see
        # that the session existed and when it was ended.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Every request looks a session up by id and must reject the expired and the
    # withdrawn. The partial index keeps that lookup on the live rows only.
    op.create_index(
        "speech_sessions_live_idx",
        "speech_sessions",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # "Show me everyone currently signed in" and "sign this person out
    # everywhere" are both queries by person, not by session.
    op.create_index("speech_sessions_sub_idx", "speech_sessions", ["sub"])


def downgrade() -> None:
    op.drop_index("speech_sessions_sub_idx", table_name="speech_sessions")
    op.drop_index("speech_sessions_live_idx", table_name="speech_sessions")
    op.drop_table("speech_sessions")
