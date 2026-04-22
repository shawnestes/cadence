"""m4_variant_stats

Revision ID: 9d1a5f6b2e3c
Revises: 7c3e4d8f2a1b
Create Date: 2026-04-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = '9d1a5f6b2e3c'
down_revision: Union[str, None] = '7c3e4d8f2a1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE MATERIALIZED VIEW variant_stats AS
SELECT
  s.tenant,
  s.campaign_id,
  s.cohort_key,
  s.variant_label,
  count(*) FILTER (WHERE s.status IN ('sent','delivered','bounced','complained')) AS sent,
  count(*) FILTER (WHERE s.delivered_at IS NOT NULL) AS delivered,
  count(DISTINCT e.recipient_id) FILTER (WHERE e.event_type='open') AS unique_opens,
  count(DISTINCT e.recipient_id) FILTER (WHERE e.event_type='click') AS unique_clicks,
  count(*) FILTER (WHERE e.event_type='bounce') AS bounces,
  count(*) FILTER (WHERE e.event_type='complaint') AS complaints,
  count(*) FILTER (WHERE e.event_type='unsubscribe') AS unsubs,
  max(s.sent_at) AS last_sent_at
FROM sends s
LEFT JOIN events e ON e.send_id = s.id
GROUP BY 1,2,3,4;
""")
    op.execute(
        "CREATE UNIQUE INDEX variant_stats_pk ON variant_stats "
        "(tenant, campaign_id, cohort_key, variant_label);"
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS variant_stats CASCADE;")
