from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.ai.base import CohortDescription, GenerationBrief
from app.adapters.ai.factory import get_ai_client
from app.core.db import async_session_factory
from app.domain.cohort import derive_recipient_cohort
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.template import Template

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class GenerationRunResult:
    cohorts_processed: int
    templates_created: int
    variants_total: int
    errors: list[str]


def _default_dimensions() -> list[str]:
    return ["state", "age_band"]


async def generate_for_campaign(campaign_id, variants_per_cohort: int = 5) -> GenerationRunResult:
    errors: list[str] = []
    templates_created = 0
    variants_total = 0
    cohorts_processed = 0

    async with async_session_factory() as session:
        campaign = (await session.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None:
            return GenerationRunResult(0, 0, 0, [f"campaign not found: {campaign_id}"])

        campaign.status = "generating"
        await session.commit()

        cohort_filter = campaign.cohort_filter or {}
        dimensions = cohort_filter.get("dimensions") or _default_dimensions()

        recipients = (await session.execute(
            select(Recipient)
            .where(Recipient.tenant == campaign.tenant)
            .where(Recipient.vertical == campaign.vertical)
            .where(Recipient.status == "active")
        )).scalars().all()

        cohort_map: dict[str, dict] = {}
        for r in recipients:
            cohort_key = derive_recipient_cohort(r.vertical, r.attributes or {}, dimensions)
            entry = cohort_map.setdefault(cohort_key, {
                "dims": {d: (r.attributes or {}).get(d, "unknown") for d in dimensions},
                "count": 0,
            })
            entry["count"] += 1

        campaign_tenant = campaign.tenant
        campaign_vertical = campaign.vertical
        campaign_name = campaign.name
        campaign_brief_text = campaign.ai_brief or "(no brief provided)"

        if not cohort_map:
            campaign.status = "ready"
            await session.commit()
            return GenerationRunResult(0, 0, 0, ["no active recipients matched tenant+vertical"])

    client = get_ai_client()
    for cohort_key, meta in cohort_map.items():
        brief = GenerationBrief(
            tenant=campaign_tenant,
            campaign_name=campaign_name,
            campaign_brief=campaign_brief_text,
            vertical=campaign_vertical,
            cohort=CohortDescription(
                tenant=campaign_tenant,
                vertical=campaign_vertical,
                cohort_key=cohort_key,
                dimensions=meta["dims"],
                recipient_count=meta["count"],
            ),
            variants_requested=variants_per_cohort,
            merge_tags_available=["first_name", "unsub_url"],
        )
        try:
            result = await client.generate_variants(brief)
        except Exception as exc:  # noqa: BLE001
            log.exception("ai_generator.generate_failed", cohort_key=cohort_key, error=str(exc))
            errors.append(f"cohort {cohort_key}: {exc}")
            continue

        async with async_session_factory() as session:
            for v in result.variants:
                t = Template(
                    tenant=campaign_tenant,
                    campaign_id=campaign_id,
                    cohort_key=cohort_key,
                    variant_label=v.variant_label,
                    subject=v.subject,
                    preheader=v.preheader,
                    body_html=v.body_html,
                    body_text=v.body_text,
                    ai_meta={
                        "model": result.model,
                        "prompt_version": result.prompt_version,
                        "emotional_driver": v.emotional_driver,
                        "usage": result.raw_usage,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    status="generated",
                )
                session.add(t)
                try:
                    await session.commit()
                    templates_created += 1
                    variants_total += 1
                except IntegrityError:
                    await session.rollback()
                    log.info(
                        "ai_generator.duplicate_variant_skipped",
                        cohort_key=cohort_key,
                        variant_label=v.variant_label,
                    )
        cohorts_processed += 1

    async with async_session_factory() as session:
        c = (await session.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one()
        c.status = "ready"
        await session.commit()

    return GenerationRunResult(
        cohorts_processed=cohorts_processed,
        templates_created=templates_created,
        variants_total=variants_total,
        errors=errors,
    )
