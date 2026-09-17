"""tap-7725 hosts and host sessions

The first accounts in this system, and the only ones. Guests deliberately get no row
here: the invite token is their whole credential.

Nothing existing is touched — no column is added to `events`, and `guests` is not
looked at, let alone re-keyed. Ownership of an event by a host is TAP-7726's migration,
kept separate so this one stays reviewable and so a rollback of either is clean.

Revision ID: cc20600801c7
Revises: 155528dee388
Create Date: 2026-09-16 20:58:23.920608

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc20600801c7"
down_revision: str | Sequence[str] | None = "155528dee388"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "hosts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_hosts_email"), "hosts", ["email"], unique=True)
    op.create_table(
        "host_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("host_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_host_sessions_token_hash"), "host_sessions", ["token_hash"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping `host_sessions` first is not cosmetic: its foreign key points at `hosts`.
    """
    op.drop_index(op.f("ix_host_sessions_token_hash"), table_name="host_sessions")
    op.drop_table("host_sessions")
    op.drop_index(op.f("ix_hosts_email"), table_name="hosts")
    op.drop_table("hosts")
