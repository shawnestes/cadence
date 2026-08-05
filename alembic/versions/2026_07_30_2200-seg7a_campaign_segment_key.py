"""seg7a: add segment_key column to campaigns table.

Revision ID: seg7a_campaign_segment_key
Revises: seg6a_provider_daily_rollup
Create Date: 2026-07-30 22:00:00

----
NULL means legacy path; non-NULL means segment-driven path.
No FK constraint — key is resolved against segment_member_current.segment_key
at runtime; a missing key produces an empty send set, not a DB error.
----
"""

from __future__ import annotations

from alembic import op

revision = "seg7a_campaign_segment_key"
down_revision = "seg6a_provider_daily_rollup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE campaigns ADD COLUMN segment_key text")


def downgrade() -> None:
    op.execute("ALTER TABLE campaigns DROP COLUMN segment_key")
