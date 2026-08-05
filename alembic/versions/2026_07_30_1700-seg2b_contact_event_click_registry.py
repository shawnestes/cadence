"""seg2b: contact_event partitioned table and click_registry.

Revision ID: seg2b_contact_event_click_registry
Revises: seg2a_consent_proof_quarantine
Create Date: 2026-07-30 17:00:00

----
D1: UUID PKs — event_id is UUID NOT NULL (not bigserial). contact_id and
    campaign_id are UUID FKs to recipients.id / campaigns.id.
D2: event_type is smallint; dedup_of is UUID (not bigint).
D11: tenant text NOT NULL on every new table.
D5: contact_event is the new L1 event store; existing events table unchanged.

Alembic cannot express PARTITION BY RANGE via op.create_table(), so both the
partitioned parent and its monthly child tables are created via op.execute().
click_registry is a normal unpartitioned table and uses op.create_table().
----
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "seg2b_contact_event"
down_revision = "seg2a_consent_proof_quarantine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # contact_event — partitioned by RANGE(occurred_at), monthly          #
    # Must use raw DDL because Alembic's create_table() cannot emit       #
    # PARTITION BY RANGE.                                                  #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        CREATE TABLE contact_event (
            event_id        uuid        NOT NULL,
            tenant          text        NOT NULL,
            contact_id      uuid        NOT NULL REFERENCES recipients(id),
            channel         smallint    NOT NULL,
            event_type      smallint    NOT NULL,
            occurred_at     timestamptz NOT NULL,
            campaign_id     uuid        REFERENCES campaigns(id),
            source          smallint    NOT NULL,
            provider_msg_id text,
            click_id        text,
            dedup_of        uuid,
            classification  smallint,
            meta            jsonb,
            PRIMARY KEY (event_id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )

    # BRIN index on the partition key — very cheap for append-only data.
    op.execute(
        "CREATE INDEX ix_contact_event_occurred_at_brin "
        "ON contact_event USING brin (occurred_at)"
    )

    # Deduplication guard: one row per (source, provider_msg_id, event_type)
    # within any timestamp bucket. Partial to skip rows without a provider id.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_contact_event_dedup
        ON contact_event (source, provider_msg_id, event_type, occurred_at)
        WHERE provider_msg_id IS NOT NULL
        """
    )

    # Fast lookup for click classification queries.
    op.execute(
        """
        CREATE INDEX ix_contact_event_click_id
        ON contact_event (click_id, event_type)
        WHERE click_id IS NOT NULL
        """
    )

    # ------------------------------------------------------------------ #
    # Monthly partitions for 2026 (12) and 2027 (12) = 24 total          #
    # ------------------------------------------------------------------ #
    _partition_ranges = [
        # 2026
        ("2026m01", "2026-01-01", "2026-02-01"),
        ("2026m02", "2026-02-01", "2026-03-01"),
        ("2026m03", "2026-03-01", "2026-04-01"),
        ("2026m04", "2026-04-01", "2026-05-01"),
        ("2026m05", "2026-05-01", "2026-06-01"),
        ("2026m06", "2026-06-01", "2026-07-01"),
        ("2026m07", "2026-07-01", "2026-08-01"),
        ("2026m08", "2026-08-01", "2026-09-01"),
        ("2026m09", "2026-09-01", "2026-10-01"),
        ("2026m10", "2026-10-01", "2026-11-01"),
        ("2026m11", "2026-11-01", "2026-12-01"),
        ("2026m12", "2026-12-01", "2027-01-01"),
        # 2027
        ("2027m01", "2027-01-01", "2027-02-01"),
        ("2027m02", "2027-02-01", "2027-03-01"),
        ("2027m03", "2027-03-01", "2027-04-01"),
        ("2027m04", "2027-04-01", "2027-05-01"),
        ("2027m05", "2027-05-01", "2027-06-01"),
        ("2027m06", "2027-06-01", "2027-07-01"),
        ("2027m07", "2027-07-01", "2027-08-01"),
        ("2027m08", "2027-08-01", "2027-09-01"),
        ("2027m09", "2027-09-01", "2027-10-01"),
        ("2027m10", "2027-10-01", "2027-11-01"),
        ("2027m11", "2027-11-01", "2027-12-01"),
        ("2027m12", "2027-12-01", "2028-01-01"),
    ]

    for suffix, start, end in _partition_ranges:
        op.execute(
            f"CREATE TABLE contact_event_y{suffix} "
            f"PARTITION OF contact_event "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )

    # ------------------------------------------------------------------ #
    # click_registry — flat, unpartitioned                                 #
    # ------------------------------------------------------------------ #
    op.create_table(
        "click_registry",
        sa.Column("click_id", sa.Text(), nullable=False),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("channel", sa.SmallInteger(), nullable=False),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column("clicked_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "classification",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("reclassified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "convert_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["recipients.id"],
            name="fk_click_registry_contact_id",
        ),
        sa.PrimaryKeyConstraint("click_id"),
    )

    op.create_index(
        "ix_click_registry_contact_clicked",
        "click_registry",
        ["contact_id", "clicked_at"],
    )

    # Partial index for scanner false-positive measurement (§6).
    op.execute(
        """
        CREATE INDEX ix_click_registry_scanner_converted
        ON click_registry (classification, converted_at)
        WHERE classification = 2
        """
    )


def downgrade() -> None:
    # Drop click_registry first (no dependents), then contact_event.
    # Dropping the partitioned parent automatically drops all child partitions.
    op.execute(
        "DROP INDEX IF EXISTS ix_click_registry_scanner_converted"
    )
    op.drop_index("ix_click_registry_contact_clicked", table_name="click_registry")
    op.drop_table("click_registry")

    op.execute("DROP TABLE IF EXISTS contact_event CASCADE")
