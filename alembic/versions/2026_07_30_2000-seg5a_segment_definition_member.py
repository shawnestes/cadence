"""seg5a: segment_definition, segment_member, segment_tier_snapshot tables + segment_member_current view.

Revision ID: seg5a_segment_definition_member
Revises: seg3b_contact_profile
Create Date: 2026-07-30 20:00:00

----
D1: segment_id is UUID PK (gen_random_uuid()); contact_id UUID FK to recipients.id.
D11: tenant text NOT NULL on every table; segment_member_current view filters by tenant on every join.

segment_definition — one row per named segment predicate.
segment_member    — append-only membership log; PK is (segment_id, contact_id, added_at).
segment_tier_snapshot — created with IF NOT EXISTS; profile_refresh.py may have already
                        created it; downgrade intentionally leaves it in place.
segment_member_current — view of active, sendable members; the canonical read surface for
                         send workers.
----
"""

from __future__ import annotations

from alembic import op

revision = "seg5a_segment_definition_member"
down_revision = "seg3b_contact_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # segment_definition                                                   #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        CREATE TABLE segment_definition (
            segment_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant                  TEXT        NOT NULL,
            key                     TEXT        NOT NULL,
            name                    TEXT        NOT NULL,
            channel                 SMALLINT    NOT NULL,
            predicate               JSONB       NOT NULL,
            spec_hash               TEXT        NOT NULL,
            active                  BOOLEAN     NOT NULL DEFAULT true,
            acknowledges_cold       BOOLEAN     NOT NULL DEFAULT false,
            include_unsendable      BOOLEAN     NOT NULL DEFAULT false,
            excluded_by_guard_count INT         NOT NULL DEFAULT 0,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Uniqueness is per-tenant, not global.
    op.execute(
        "CREATE UNIQUE INDEX ON segment_definition (tenant, key)"
    )

    # ------------------------------------------------------------------ #
    # segment_member                                                       #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        CREATE TABLE segment_member (
            segment_id  UUID        NOT NULL REFERENCES segment_definition(segment_id),
            contact_id  UUID        NOT NULL REFERENCES recipients(id),
            tenant      TEXT        NOT NULL,
            channel     SMALLINT    NOT NULL,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            removed_at  TIMESTAMPTZ,
            PRIMARY KEY (segment_id, contact_id, added_at)
        )
        """
    )

    # Partial index for fast active-member lookups.
    op.execute(
        """
        CREATE INDEX ON segment_member (segment_id)
        WHERE removed_at IS NULL
        """
    )

    # ------------------------------------------------------------------ #
    # segment_tier_snapshot (IF NOT EXISTS — profile_refresh may own it)  #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_tier_snapshot (
            id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant        TEXT        NOT NULL,
            run_at        TIMESTAMPTZ NOT NULL,
            tier          SMALLINT    NOT NULL,
            contact_count INT         NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------ #
    # segment_member_current view                                          #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        CREATE VIEW segment_member_current AS
        SELECT
            sm.segment_id,
            sd.key              AS segment_key,
            sm.contact_id,
            sm.channel,
            cp.email_domain,
            cp.mailbox_provider,
            cp.reputation_group,
            cp.engagement_tier,
            cp.engagement_score
        FROM segment_member sm
        JOIN segment_definition sd
            ON sd.segment_id = sm.segment_id
           AND sd.tenant      = sm.tenant
        JOIN contact_profile cp
            ON cp.contact_id  = sm.contact_id
           AND cp.tenant       = sm.tenant
           AND cp.channel      = sm.channel
        WHERE sm.removed_at IS NULL
          AND sd.active        = true
          AND cp.sendable      = true
        """
    )

    # GRANT to cadence_reader — silently skip when role does not exist (dev).
    op.execute(
        """
        DO $$ BEGIN
            GRANT SELECT ON segment_member_current TO cadence_reader;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS segment_member_current")
    op.execute("DROP TABLE IF EXISTS segment_member")
    op.execute("DROP TABLE IF EXISTS segment_definition")
    # segment_tier_snapshot is intentionally left in place — profile_refresh.py
    # depends on it and a future migration may own it.
