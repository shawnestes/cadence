"""seg6a: provider_daily_rollup table — daily per-tenant ISP reputation metrics.

Revision ID: seg6a_provider_daily_rollup
Revises: seg5a_segment_definition_member
Create Date: 2026-07-30 21:00:00

----
D1: natural composite PK (tenant, day, reputation_group, channel, source) — no UUID FK here.
D11: tenant TEXT NOT NULL is present; all tenanted queries must filter by tenant.

provider_daily_rollup — one row per (tenant, day, reputation_group, channel, source).
  source: 1=ses_events, 2=gpt_api
----
"""

from __future__ import annotations

from alembic import op

revision = "seg6a_provider_daily_rollup"
down_revision = "seg5a_segment_definition_member"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_daily_rollup (
            day               DATE        NOT NULL,
            tenant            TEXT        NOT NULL,
            reputation_group  SMALLINT    NOT NULL,
            channel           SMALLINT    NOT NULL,
            source            SMALLINT    NOT NULL,
            sends             INT         NOT NULL DEFAULT 0,
            delivers          INT         NOT NULL DEFAULT 0,
            bounces_hard      INT         NOT NULL DEFAULT 0,
            bounces_soft      INT         NOT NULL DEFAULT 0,
            complaints        INT         NOT NULL DEFAULT 0,
            complaint_rate    NUMERIC(8,6),
            complaint_rate_7d  NUMERIC(8,6),
            complaint_rate_30d NUMERIC(8,6),
            bounce_rate       NUMERIC(8,6),
            PRIMARY KEY (tenant, day, reputation_group, channel, source)
        )
        """
    )

    op.execute("CREATE INDEX ON provider_daily_rollup (tenant, day)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_daily_rollup")
