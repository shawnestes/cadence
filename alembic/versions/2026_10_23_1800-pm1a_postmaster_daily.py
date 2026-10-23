"""pm1a: daily store for Google Postmaster metrics.

Revision ID: pm1a_postmaster_daily
Revises: seg8a_consent_proof_dedup
Create Date: 2026-10-23 18:00:00

----
The Postmaster sync had nowhere to put a reading. Alerting only writes a row
when a threshold is breached, so healthy days -- the baseline you need to see a
trend -- were logged and discarded. Without history there is no way to
correlate reputation against the warm-up ramp, which is the whole reason for
ramping.

Keyed on (domain, day, metric) and upserted: Postmaster restates recent days as
more data arrives, so a re-read of the same day must overwrite rather than
accumulate. The lookback window re-reads the last 7 days on every run, making
the upsert the normal path, not an edge case.

value is numeric, not float: these are ratios compared against 0.001 and 0.003,
and binary float drift around thresholds that decide sending reputation is not
worth the saved bytes.
----
"""

from __future__ import annotations

from alembic import op

revision = "pm1a_postmaster_daily"
down_revision = "seg8a_consent_proof_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE postmaster_daily (
            domain      text        NOT NULL,
            day         date        NOT NULL,
            metric      text        NOT NULL,
            value       numeric     NOT NULL,
            fetched_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (domain, day, metric)
        )
        """
    )
    # Trend reads are "this metric, this domain, recent days first".
    op.execute(
        "CREATE INDEX ix_postmaster_daily_metric_day"
        " ON postmaster_daily (domain, metric, day DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_postmaster_daily_metric_day")
    op.execute("DROP TABLE IF EXISTS postmaster_daily")
