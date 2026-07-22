"""addendum 002: recipients.engagement_tier + tier_computed_at

Revision ID: b7c8d9e0f1a2
Revises: 23300d63ec1e
Create Date: 2026-07-22 14:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "23300d63ec1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipients",
        sa.Column(
            "engagement_tier",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'neutral'"),
        ),
    )
    op.add_column(
        "recipients",
        sa.Column("tier_computed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_recipients_engagement_tier", "recipients", ["engagement_tier"]
    )


def downgrade() -> None:
    op.drop_index("ix_recipients_engagement_tier", table_name="recipients")
    op.drop_column("recipients", "tier_computed_at")
    op.drop_column("recipients", "engagement_tier")
