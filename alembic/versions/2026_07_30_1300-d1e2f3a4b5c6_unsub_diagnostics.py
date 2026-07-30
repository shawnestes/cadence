"""unsub_diagnostics: failed unsubscribe lookups, recorded instead of user-facing 404s

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-07-30 13:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unsub_diagnostics",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant", sa.Text(), nullable=True),
        sa.Column("recipient_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_unsub_diagnostics_created_at", "unsub_diagnostics", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_unsub_diagnostics_created_at", table_name="unsub_diagnostics")
    op.drop_table("unsub_diagnostics")
