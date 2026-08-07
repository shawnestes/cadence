"""seg8a: unique index for consent_proof re-import dedup.

Revision ID: seg8a_consent_proof_dedup
Revises: seg7a_campaign_segment_key
Create Date: 2026-08-05 10:00:00

----
The bulk record loader has no resume ledger; recovery from a failed run is a
full re-run of the file. consent_proof is append-only evidence, so a re-run
would otherwise duplicate every proof. Genuinely distinct consent events differ
in captured_at, so only literal re-imports of the same event collapse.
----
"""

from __future__ import annotations

from alembic import op

revision = "seg8a_consent_proof_dedup"
down_revision = "seg7a_campaign_segment_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_consent_proof_dedup"
        " ON consent_proof (tenant, contact_id, disclosure_text_hash, captured_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_consent_proof_dedup")
