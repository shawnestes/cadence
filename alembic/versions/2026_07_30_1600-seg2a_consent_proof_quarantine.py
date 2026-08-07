"""seg2a: consent_proof and contact_quarantine tables.

Revision ID: seg2a_consent_proof_quarantine
Revises: seg1_domain_provider
Create Date: 2026-07-30 16:00:00

----
D1: UUID PKs (application generates UUID v7; server default gen_random_uuid() is
    v4 but satisfies DB-level uniqueness for migrations/seeds/tests).
D3: No consent backfill — existing recipients without a consent_proof row land
    in contact_quarantine on day 1.
D11: tenant text NOT NULL on every new table.
----
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "seg2a_consent_proof_quarantine"
down_revision = "seg1_domain_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consent_proof",
        sa.Column(
            "consent_proof_id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("form_id", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("disclosure_text_hash", sa.Text(), nullable=False),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.SmallInteger()),
            nullable=False,
        ),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["recipients.id"],
            name="fk_consent_proof_contact_id",
        ),
        sa.PrimaryKeyConstraint("consent_proof_id"),
    )
    op.create_index(
        "ix_consent_proof_tenant_contact",
        "consent_proof",
        ["tenant", "contact_id"],
    )

    op.create_table(
        "contact_quarantine",
        sa.Column(
            "quarantine_id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column(
            "rejected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("quarantine_id"),
    )
    op.create_index(
        "ix_contact_quarantine_tenant_contact",
        "contact_quarantine",
        ["tenant", "contact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_quarantine_tenant_contact",
        table_name="contact_quarantine",
    )
    op.drop_table("contact_quarantine")
    op.drop_index(
        "ix_consent_proof_tenant_contact",
        table_name="consent_proof",
    )
    op.drop_table("consent_proof")
