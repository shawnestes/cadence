"""seg3a: contact_activity_daily (L2 rollup) and segment_tier_snapshot tables.

Revision ID: seg3a_contact_activity_daily
Revises: seg2b_contact_event
Create Date: 2026-07-30 18:00:00

----
D1: UUID PKs/FKs — contact_id is UUID FK to recipients.id.
    segment_tier_snapshot.id is UUID PK (gen_random_uuid() server default).
D4: segment_tier_snapshot is used by the nightly blast-radius guardrail.
D11: tenant text NOT NULL on every new table.

contact_activity_daily is the L2 daily rollup of contact_event data.
segment_tier_snapshot captures per-tenant engagement-tier counts at each
nightly pass so the blast-radius guardrail can compare against prior runs.
----
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "seg3a_contact_activity_daily"
down_revision = "seg2b_contact_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # contact_activity_daily — L2 daily rollup per contact/channel        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "contact_activity_daily",
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column("channel", sa.SmallInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "sends",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "delivers",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "opens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "opens_mpp",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "clicks",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "clicks_bot",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "converts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "bounces",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "complaints",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["recipients.id"],
            name="fk_contact_activity_daily_contact_id",
        ),
        sa.PrimaryKeyConstraint("tenant", "contact_id", "channel", "day"),
    )

    # Efficient window-range queries: fetch all days for a contact within a
    # tenant ordered newest-first.
    op.execute(
        """
        CREATE INDEX ix_contact_activity_daily_tenant_contact_day
        ON contact_activity_daily (tenant, contact_id, day DESC)
        """
    )

    # ------------------------------------------------------------------ #
    # segment_tier_snapshot — nightly blast-radius guardrail (D4)         #
    # ------------------------------------------------------------------ #
    op.create_table(
        "segment_tier_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column(
            "run_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("contact_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Nightly pass fetches the most recent snapshot per tenant.
    op.execute(
        """
        CREATE INDEX ix_segment_tier_snapshot_tenant_run_at
        ON segment_tier_snapshot (tenant, run_at DESC)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_segment_tier_snapshot_tenant_run_at"
    )
    op.drop_table("segment_tier_snapshot")

    op.execute(
        "DROP INDEX IF EXISTS ix_contact_activity_daily_tenant_contact_day"
    )
    op.drop_table("contact_activity_daily")
