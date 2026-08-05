"""DB writes for tenant record ingestion.

One row becomes three writes: recipients, consent_proof, contact_profile.
Callers (the CLI and the batch endpoint) own transaction boundaries; this
module never commits.

Two invariants live here and nowhere else:

* A re-import must never resurrect a contact who unsubscribed. Nothing else
  writes contact_profile.consent_state on unsubscribe (unsub_server writes
  suppressions and recipients.status), so the guard is applied on every write.
* contact_profile.consent_at moves forward only. Replaying a stale export must
  not regress a newer consent timestamp.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.isp import (
    MAILBOX_PROVIDER_CODES,
    REPUTATION_GROUP_CODES,
    classify_domain_two_axis,
)
from app.domain.record_mapping import NormalizedRecord, disclosure_hash
from app.models.base import uuid7

log = structlog.get_logger(__name__)

_CHANNEL_EMAIL = 1
_CONSENT_OPTED_IN = 1
_CONSENT_OPTED_OUT = 2
_SUPPRESS_REASON_UNSUBSCRIBE = 1


@dataclass
class IngestCounters:
    read: int = 0
    loaded: int = 0
    updated: int = 0
    quarantined: int = 0
    rejected: int = 0
    enrolled: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_UPSERT_RECIPIENT = text(
    """
    INSERT INTO recipients
        (id, tenant, email, external_id, vertical, first_seen_at, last_seen_at,
         consent_source, consent_at, attributes, status)
    VALUES
        (:id, :tenant, :email, :external_id, :vertical, :now, :now,
         :consent_source, :consent_at, CAST(:attributes AS jsonb), 'active')
    ON CONFLICT (tenant, email) DO UPDATE SET
        last_seen_at   = :now,
        external_id    = COALESCE(EXCLUDED.external_id, recipients.external_id),
        consent_source = EXCLUDED.consent_source,
        consent_at     = GREATEST(recipients.consent_at, EXCLUDED.consent_at),
        -- Replace, do not merge: each file row is a complete snapshot, and a
        -- shallow `||` merge would keep a stale phone the new row omits.
        attributes     = EXCLUDED.attributes,
        updated_at     = :now
    RETURNING id, (xmax = 0) AS created
    """
)

_INSERT_CONSENT_PROOF = text(
    """
    INSERT INTO consent_proof
        (consent_proof_id, tenant, contact_id, captured_at, source_url, form_id,
         ip, user_agent, disclosure_text_hash, channels, raw)
    VALUES
        (:id, :tenant, :contact_id, :captured_at, :source_url, :form_id,
         CAST(:ip AS inet), :user_agent, :hash, CAST(:channels AS smallint[]),
         CAST(:raw AS jsonb))
    ON CONFLICT (tenant, contact_id, disclosure_text_hash, captured_at) DO NOTHING
    RETURNING consent_proof_id
    """
)

_SELECT_EXISTING_PROOF = text(
    """
    SELECT consent_proof_id FROM consent_proof
     WHERE tenant = :tenant AND contact_id = :contact_id
       AND disclosure_text_hash = :hash AND captured_at = :captured_at
    """
)

_LOOKUP_DOMAIN_PROVIDER = text(
    """
    SELECT mailbox_provider, reputation_group
      FROM domain_provider
     WHERE tenant = :tenant AND domain = :domain
    """
)

_UPSERT_PROFILE = text(
    """
    INSERT INTO contact_profile
        (contact_id, tenant, channel, email_domain, mailbox_provider,
         reputation_group, consent_state, consent_at, consent_proof_id,
         suppressed_at, suppress_reason, updated_at)
    SELECT
        :contact_id, :tenant, :channel, :email_domain, :mailbox_provider,
        :reputation_group,
        CASE WHEN sup.is_suppressed
             THEN CAST(:consent_opted_out AS smallint)
             ELSE CAST(:consent_opted_in AS smallint)
        END,
        :consent_at, :consent_proof_id,
        CASE WHEN sup.is_suppressed THEN CAST(:now AS timestamptz) ELSE NULL END,
        CASE WHEN sup.is_suppressed
             THEN CAST(:suppress_reason AS smallint)
             ELSE NULL
        END,
        :now
    FROM (
        -- Suppression is re-derived here, in the same statement as the write,
        -- so the check and the write share one snapshot. A separate read
        -- earlier in the transaction would leave a TOCTOU gap: a suppression
        -- committed by another transaction between the read and this write
        -- would be invisible to the read and the profile would be written
        -- sendable for a contact that is, at write time, suppressed.
        SELECT EXISTS (
            SELECT 1 FROM suppressions WHERE tenant = :tenant AND email = :email
        ) OR EXISTS (
            SELECT 1 FROM recipients
             WHERE tenant = :tenant AND id = :contact_id AND status = 'suppressed'
        ) AS is_suppressed
    ) sup
    ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
        email_domain     = EXCLUDED.email_domain,
        mailbox_provider = EXCLUDED.mailbox_provider,
        reputation_group = EXCLUDED.reputation_group,
        -- consent_state: 1 = opted in, 2 = opted out. Opted-out is the higher
        -- code, so GREATEST is the ratchet that never moves a suppressed
        -- profile back to opted-in on a re-import.
        consent_state    = GREATEST(contact_profile.consent_state, EXCLUDED.consent_state),
        -- Never clear an existing suppression.
        suppressed_at    = COALESCE(contact_profile.suppressed_at, EXCLUDED.suppressed_at),
        suppress_reason  = COALESCE(contact_profile.suppress_reason, EXCLUDED.suppress_reason),
        consent_at       = GREATEST(contact_profile.consent_at, EXCLUDED.consent_at),
        consent_proof_id = CASE
            WHEN contact_profile.consent_at IS NULL
              OR EXCLUDED.consent_at > contact_profile.consent_at
            THEN EXCLUDED.consent_proof_id
            ELSE contact_profile.consent_proof_id
        END,
        updated_at       = :now
    """
)

_INSERT_QUARANTINE = text(
    """
    INSERT INTO contact_quarantine (quarantine_id, tenant, rejected_at, contact_id, reason, raw)
    VALUES (:id, :tenant, :now, NULL, :reason, CAST(:raw AS jsonb))
    """
)


async def _resolve_provider(
    session: AsyncSession, tenant: str, domain: str
) -> tuple[int, int, str | None]:
    """Return (mailbox_provider, reputation_group, unknown_domain_or_None).

    domain_provider wins when it has a row — it carries MX-resolved and manual
    corrections that the static seed table in isp.py does not.
    """
    row = (
        await session.execute(
            _LOOKUP_DOMAIN_PROVIDER, {"tenant": tenant, "domain": domain}
        )
    ).mappings().one_or_none()
    if row is not None:
        return int(row["mailbox_provider"]), int(row["reputation_group"]), None

    mailbox, reputation = classify_domain_two_axis(domain)
    mp = MAILBOX_PROVIDER_CODES.get(mailbox, MAILBOX_PROVIDER_CODES["other"])
    rg = REPUTATION_GROUP_CODES.get(reputation, REPUTATION_GROUP_CODES["other"])
    unknown = domain if (mailbox == "other" and reputation == "other") else None
    return mp, rg, unknown


async def ingest_record(
    session: AsyncSession,
    *,
    tenant: str,
    vertical: str,
    record: NormalizedRecord,
    now: datetime,
) -> tuple[str, str | None]:
    """Write recipients + consent_proof + contact_profile for one record.

    Returns (status, unknown_domain) where status is "created" or "updated"
    and unknown_domain is a domain needing an MX lookup, or None.
    """
    row = (
        await session.execute(
            _UPSERT_RECIPIENT,
            {
                "id": str(uuid7()),
                "tenant": tenant,
                "email": record.email,
                "external_id": record.external_id,
                "vertical": record.vertical or vertical,
                "now": now,
                "consent_source": record.consent_source,
                "consent_at": record.consent.captured_at,
                "attributes": json.dumps(record.attributes),
            },
        )
    ).one()
    contact_id = row.id
    status = "created" if row.created else "updated"

    text_hash = disclosure_hash(record.consent.text)
    proof_params = {
        "tenant": tenant,
        "contact_id": str(contact_id),
        "captured_at": record.consent.captured_at,
        "hash": text_hash,
    }
    proof_id = (
        await session.execute(
            _INSERT_CONSENT_PROOF,
            {
                **proof_params,
                "id": str(uuid7()),
                "source_url": record.consent.source_url,
                "form_id": record.consent.form_id,
                "ip": record.consent.ip,
                "user_agent": record.consent.user_agent,
                "channels": list(record.consent.channels),
                "raw": json.dumps(
                    {
                        "disclosure_text": record.consent.text,
                        "attributes": record.attributes,
                    },
                    default=str,
                ),
            },
        )
    ).scalar_one_or_none()
    if proof_id is None:
        # Dedup hit: this exact consent event is already on file.
        proof_id = (
            await session.execute(_SELECT_EXISTING_PROOF, proof_params)
        ).scalar_one()

    domain = record.email.rsplit("@", 1)[1]
    mp, rg, unknown_domain = await _resolve_provider(session, tenant, domain)

    await session.execute(
        _UPSERT_PROFILE,
        {
            "contact_id": str(contact_id),
            "tenant": tenant,
            "email": record.email,
            "channel": _CHANNEL_EMAIL,
            "email_domain": domain,
            "mailbox_provider": mp,
            "reputation_group": rg,
            "consent_opted_in": _CONSENT_OPTED_IN,
            "consent_opted_out": _CONSENT_OPTED_OUT,
            "consent_at": record.consent.captured_at,
            "consent_proof_id": str(proof_id),
            "suppress_reason": _SUPPRESS_REASON_UNSUBSCRIBE,
            "now": now,
        },
    )
    return status, unknown_domain


async def quarantine_row(
    session: AsyncSession,
    *,
    tenant: str,
    reason: str,
    raw_row: dict,
    now: datetime,
) -> None:
    """Record a row rejected for want of consent (SPEC-segmentation §9)."""
    await session.execute(
        _INSERT_QUARANTINE,
        {
            "id": str(uuid7()),
            "tenant": tenant,
            "now": now,
            "reason": reason[:500],
            "raw": json.dumps(raw_row, default=str),
        },
    )


async def flush_unknown_domains(
    tenant: str, domains: set[str], *, concurrency: int = 50
) -> None:
    """Enqueue MX lookups and await them.

    enqueue_mx_lookup fires a background task and returns. That is correct for
    a long-lived worker, but a CLI's event loop closes when main() returns and
    cancels those tasks, losing the classification. Bounded and awaited here.
    """
    if not domains:
        return

    from app.workers.domain_provider_mx_worker import enqueue_mx_lookup

    semaphore = asyncio.Semaphore(concurrency)

    async def _one(domain: str) -> None:
        async with semaphore:
            await enqueue_mx_lookup(domain, tenant)

    await asyncio.gather(*(_one(d) for d in sorted(domains)), return_exceptions=True)
    # enqueue_mx_lookup spawns the DNS work with create_task; drain those too.
    pending = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and "_run_mx_lookup" in repr(t)
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("record_ingest.mx_enqueued", tenant=tenant, domains=len(domains))
