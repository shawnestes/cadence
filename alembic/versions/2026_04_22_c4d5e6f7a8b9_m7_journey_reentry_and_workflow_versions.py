"""m7_journey_reentry_and_workflow_versions

- Replace the full unique constraint (workflow_id, recipient_id) on
  `journeys` with a partial unique index gated on non-terminal statuses,
  so a recipient can re-enter a workflow after completion (SPEC §6.5).
- Add `workflow_versions` table for pinning journey.workflow_version
  to the historical definition snapshot.

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-04-22 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Replace full unique constraint with partial unique index on
    #    non-terminal statuses. 'active' is the only non-terminal status
    #    used today; future 'scheduled' / 'waiting' are included so they
    #    also prevent concurrent enrollments without a migration later.
    op.drop_constraint(
        "uq_journeys_workflow_recipient", "journeys", type_="unique"
    )
    op.create_index(
        "uq_journeys_active_workflow_recipient",
        "journeys",
        ["workflow_id", "recipient_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('active','scheduled','waiting')"
        ),
    )

    # 2. workflow_versions historical snapshot.
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_wf_version"
        ),
    )

    # 3. Backfill snapshots for existing workflows using their current
    #    definition/version so in-flight journeys keep advancing.
    op.execute(
        """
        INSERT INTO workflow_versions (id, workflow_id, version, definition, created_at)
        SELECT gen_random_uuid(), w.id, w.version, w.definition, now()
        FROM workflows w
        WHERE NOT EXISTS (
            SELECT 1 FROM workflow_versions wv
            WHERE wv.workflow_id = w.id AND wv.version = w.version
        )
        """
    )


def downgrade() -> None:
    op.drop_table("workflow_versions")

    op.drop_index(
        "uq_journeys_active_workflow_recipient",
        table_name="journeys",
        postgresql_where=sa.text("status IN ('active','scheduled','waiting')"),
    )
    op.create_unique_constraint(
        "uq_journeys_workflow_recipient",
        "journeys",
        ["workflow_id", "recipient_id"],
    )
