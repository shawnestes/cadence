"""addendum 003: sending_identities + identity FKs on campaigns/sends

Seeds one default 'ses' identity per tenant found in campaigns/sends
(from CADENCE_* settings when present) and backfills existing sends.

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-07-23 09:30:00
"""

from __future__ import annotations

import json
import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sending_identities",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("esp_provider", sa.Text(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("sending_domain", sa.Text(), nullable=False),
        sa.Column("credentials_ref", sa.Text(), nullable=True),
        sa.Column(
            "provider_config",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("daily_quota", sa.Integer(), nullable=False, server_default=sa.text("50000")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant", "name", name="uq_sending_identities_tenant_name"),
    )
    op.create_index(
        "uq_sending_identities_tenant_default",
        "sending_identities",
        ["tenant"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.add_column(
        "campaigns",
        sa.Column(
            "sending_identity_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sending_identities.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "sends",
        sa.Column(
            "sending_identity_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sending_identities.id"),
            nullable=True,
        ),
    )

    # Seed one default SES identity per tenant that has data, from env settings.
    conn = op.get_bind()
    from_address = os.environ.get("CADENCE_SES_FROM_ADDRESS") or "no-reply@example.com"
    domain = os.environ.get("CADENCE_SES_SENDING_DOMAIN") or from_address.split("@")[-1].rstrip(">")
    provider_config = {
        "region": os.environ.get("CADENCE_AWS_REGION", "us-east-1"),
        "configuration_set": os.environ.get("CADENCE_SES_CONFIGURATION_SET"),
        "managed_pool_name": os.environ.get("CADENCE_SES_MANAGED_POOL_NAME"),
        "account_key": "default",
    }
    tenants = {
        r[0]
        for r in conn.execute(
            sa.text("SELECT DISTINCT tenant FROM campaigns UNION SELECT DISTINCT tenant FROM sends")
        )
    }
    for tenant in sorted(tenants):
        conn.execute(
            sa.text(
                "INSERT INTO sending_identities "
                "(id, tenant, name, esp_provider, from_address, sending_domain, "
                " provider_config, is_default) "
                "VALUES (:id, :tenant, 'default', 'ses', :from_address, :domain, "
                " CAST(:cfg AS jsonb), true)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant": tenant,
                "from_address": from_address,
                "domain": domain,
                "cfg": json.dumps(provider_config),
            },
        )
        conn.execute(
            sa.text(
                "UPDATE sends SET sending_identity_id = "
                "(SELECT id FROM sending_identities WHERE tenant = sends.tenant AND is_default) "
                "WHERE tenant = :tenant AND sending_identity_id IS NULL"
            ),
            {"tenant": tenant},
        )
    op.create_index("ix_sends_sending_identity_id", "sends", ["sending_identity_id"])


def downgrade() -> None:
    op.drop_index("ix_sends_sending_identity_id", table_name="sends")
    op.drop_column("sends", "sending_identity_id")
    op.drop_column("campaigns", "sending_identity_id")
    op.drop_index("uq_sending_identities_tenant_default", table_name="sending_identities")
    op.drop_table("sending_identities")
