"""seg3b: contact_profile (L3) table — exclusive segment surface.

Revision ID: seg3b_contact_profile
Revises: seg3a_contact_activity_daily
Create Date: 2026-07-30 19:00:00

----
D1: UUID PKs/FKs — contact_id UUID FK to recipients.id;
    consent_proof_id UUID FK to consent_proof(consent_proof_id).
D2: channel, mailbox_provider, reputation_group, engagement_score,
    engagement_tier, consent_state, suppress_reason are all smallint
    enum-like codes.
D3: consent_proof_id NOT NULL and consent_state NOT NULL enforce that
    every profile row carries proof of consent.
D11: tenant text NOT NULL; tenanted queries always filter by tenant.

The sendable GENERATED ALWAYS AS column and WHERE-clause partial indexes
are both expressed via op.execute() raw DDL because SQLAlchemy Column
objects cannot represent either construct.
----
"""

from __future__ import annotations

from alembic import op

revision = "seg3b_contact_profile"
down_revision = "seg3a_contact_activity_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # contact_profile — L3 segment surface                                #
    # ------------------------------------------------------------------ #
    # Created entirely via raw DDL so that the GENERATED ALWAYS AS column
    # is expressed correctly (SQLAlchemy Column cannot model it).
    op.execute(
        """
        CREATE TABLE contact_profile (
            contact_id          UUID        NOT NULL
                                            REFERENCES recipients(id),
            tenant              TEXT        NOT NULL,
            channel             SMALLINT    NOT NULL,
            email_domain        TEXT,
            mailbox_provider    SMALLINT,
            reputation_group    SMALLINT,
            country             CHAR(2),
            tz                  TEXT,
            first_send_at       TIMESTAMPTZ,
            last_send_at        TIMESTAMPTZ,
            last_open_at        TIMESTAMPTZ,
            last_click_at       TIMESTAMPTZ,
            last_convert_at     TIMESTAMPTZ,
            sends_30d           INT,
            opens_30d           INT,
            clicks_30d          INT,
            sends_90d           INT,
            opens_90d           INT,
            clicks_90d          INT,
            converts_90d        INT,
            bounces_90d         INT,
            complaints_90d      INT,
            sends_lifetime      INT,
            opens_lifetime      INT,
            clicks_lifetime     INT,
            engagement_score    SMALLINT,
            engagement_tier     SMALLINT,
            consent_state       SMALLINT    NOT NULL,
            consent_at          TIMESTAMPTZ,
            consent_proof_id    UUID        NOT NULL
                                            REFERENCES consent_proof(consent_proof_id),
            suppressed_at       TIMESTAMPTZ,
            suppress_reason     SMALLINT,
            sendable            BOOLEAN
                                    GENERATED ALWAYS AS (
                                        consent_state = 1 AND suppressed_at IS NULL
                                    ) STORED,
            profile_version     BIGINT      NOT NULL DEFAULT 0,
            tier_config_version TEXT        NOT NULL DEFAULT '',
            windows_valid_to    DATE,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant, contact_id, channel)
        )
        """
    )

    # ------------------------------------------------------------------ #
    # Indexes                                                              #
    # ------------------------------------------------------------------ #

    # Partial indexes on sendable contacts — expressed via op.execute()
    # because Alembic's create_index() cannot use WHERE on generated columns.

    # Segmentation by channel + mailbox_provider + recency.
    op.execute(
        """
        CREATE INDEX ix_contact_profile_channel_mbp_open
        ON contact_profile (channel, mailbox_provider, last_open_at)
        WHERE sendable = true
        """
    )

    # Segmentation by channel + reputation_group + recency.
    op.execute(
        """
        CREATE INDEX ix_contact_profile_channel_repgrp_open
        ON contact_profile (channel, reputation_group, last_open_at)
        WHERE sendable = true
        """
    )

    # Fast engagement-tier fan-out (blast-radius guardrail & sends).
    op.execute(
        """
        CREATE INDEX ix_contact_profile_channel_tier
        ON contact_profile (channel, engagement_tier)
        WHERE sendable = true
        """
    )

    # Non-partial: incremental profile refresh lookups by version.
    op.execute(
        """
        CREATE INDEX ix_contact_profile_profile_version
        ON contact_profile (profile_version)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_contact_profile_profile_version"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_contact_profile_channel_tier"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_contact_profile_channel_repgrp_open"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_contact_profile_channel_mbp_open"
    )
    op.execute("DROP TABLE IF EXISTS contact_profile")
