"""seg1: domain_provider table + seed data for mailbox provider classification.

Revision ID: seg1_domain_provider
Revises: d1e2f3a4b5c6
Create Date: 2026-07-30 15:00:00

----
Tenant model
------------
Multi-tenant. PRIMARY KEY is `(tenant, domain)` so a tenant can insert a
per-tenant override row without colliding with the global seed. Seed rows
carry `tenant = 'global'`. Lookup path: prefer tenant-specific row when
present, fall back to 'global'.

An index on `domain` alone supports the fallback lookup and cross-tenant
reporting queries.
----
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "seg1_domain_provider"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Integer codes — mirror of app/domain/isp.py constants; copied here so the
# migration is self-contained and readable without importing application code.
# ---------------------------------------------------------------------------
_MP_GMAIL = 1
_MP_MICROSOFT = 2
_MP_YAHOO = 3
_MP_CABLE = 4
_MP_APPLE = 5
# _MP_CORPORATE = 6  # not seeded; assigned by MX-lookup path
# _MP_OTHER = 0      # fallthrough; not seeded

_RG_GMAIL = 1
_RG_MICROSOFT = 2
_RG_YAHOO = 3
_RG_APPLE = 5
_RG_OTHER = 0

_SRC_SEED = 1


def _row(
    domain: str,
    mailbox_provider: int,
    reputation_group: int,
) -> dict:
    return {
        "domain": domain,
        "tenant": "global",
        "mailbox_provider": mailbox_provider,
        "reputation_group": reputation_group,
        "source": _SRC_SEED,
        "mx_host": None,
        "checked_at": None,
    }


_SEED_ROWS = [
    # --- Gmail ---
    _row("gmail.com", _MP_GMAIL, _RG_GMAIL),
    _row("googlemail.com", _MP_GMAIL, _RG_GMAIL),
    # --- Microsoft ---
    _row("hotmail.com", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("hotmail.co.uk", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("hotmail.fr", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("hotmail.de", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("msn.com", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("msn.co.uk", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("msn.fr", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("msn.de", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("outlook.com", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("outlook.co.uk", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("outlook.fr", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("outlook.de", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("live.com", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("live.co.uk", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("live.fr", _MP_MICROSOFT, _RG_MICROSOFT),
    _row("live.de", _MP_MICROSOFT, _RG_MICROSOFT),
    # --- Yahoo (brand + ccTLDs) ---
    _row("yahoo.com", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.co.uk", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.co.jp", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.fr", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.de", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.it", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.es", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.com.br", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.com.au", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.com.mx", _MP_YAHOO, _RG_YAHOO),
    _row("yahoo.ca", _MP_YAHOO, _RG_YAHOO),
    _row("ymail.com", _MP_YAHOO, _RG_YAHOO),
    _row("rocketmail.com", _MP_YAHOO, _RG_YAHOO),
    _row("aol.com", _MP_YAHOO, _RG_YAHOO),
    _row("aim.com", _MP_YAHOO, _RG_YAHOO),
    # --- Cable: generic operators (reputation_group = other until MX refines) ---
    _row("comcast.net", _MP_CABLE, _RG_OTHER),
    _row("charter.net", _MP_CABLE, _RG_OTHER),
    _row("spectrum.net", _MP_CABLE, _RG_OTHER),
    _row("rr.com", _MP_CABLE, _RG_OTHER),
    _row("twc.com", _MP_CABLE, _RG_OTHER),
    _row("cox.net", _MP_CABLE, _RG_OTHER),
    _row("optonline.net", _MP_CABLE, _RG_OTHER),
    _row("mediacombb.net", _MP_CABLE, _RG_OTHER),
    _row("windstream.net", _MP_CABLE, _RG_OTHER),
    _row("frontier.com", _MP_CABLE, _RG_OTHER),
    _row("earthlink.net", _MP_CABLE, _RG_OTHER),
    _row("juno.com", _MP_CABLE, _RG_OTHER),
    _row("netzero.net", _MP_CABLE, _RG_OTHER),
    # --- Cable: AT&T-family + verizon.net → reputation_group = yahoo (§3) ---
    _row("att.net", _MP_CABLE, _RG_YAHOO),
    _row("sbcglobal.net", _MP_CABLE, _RG_YAHOO),
    _row("bellsouth.net", _MP_CABLE, _RG_YAHOO),
    _row("ameritech.net", _MP_CABLE, _RG_YAHOO),
    _row("pacbell.net", _MP_CABLE, _RG_YAHOO),
    _row("swbell.net", _MP_CABLE, _RG_YAHOO),
    _row("verizon.net", _MP_CABLE, _RG_YAHOO),
    # --- Apple ---
    _row("icloud.com", _MP_APPLE, _RG_APPLE),
    _row("me.com", _MP_APPLE, _RG_APPLE),
    _row("mac.com", _MP_APPLE, _RG_APPLE),
]


def upgrade() -> None:
    domain_provider = op.create_table(
        "domain_provider",
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("tenant", sa.Text(), nullable=False),
        sa.Column("mailbox_provider", sa.SmallInteger(), nullable=False),
        sa.Column("reputation_group", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.SmallInteger(), nullable=False),
        sa.Column("mx_host", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "domain"),
    )
    op.create_index(
        "ix_domain_provider_domain",
        "domain_provider",
        ["domain"],
    )
    op.bulk_insert(domain_provider, _SEED_ROWS)


def downgrade() -> None:
    op.drop_index("ix_domain_provider_domain", table_name="domain_provider")
    op.drop_table("domain_provider")
