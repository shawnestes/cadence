import structlog
from sqlalchemy import text, update

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.domain.mab import VariantStats, decide_retirement
from app.models.template import Template

log = structlog.get_logger(__name__)


async def run_once() -> dict:
    s = get_settings()
    retired_count = 0

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tenant, campaign_id, cohort_key, variant_label, "
                    "sent, unique_clicks, unique_opens, bounces, complaints "
                    "FROM variant_stats"
                )
            )
        ).all()

        grouped: dict[tuple, list[VariantStats]] = {}
        for r in rows:
            key = (r.tenant, r.campaign_id, r.cohort_key)
            grouped.setdefault(key, []).append(
                VariantStats(
                    variant_label=r.variant_label,
                    sent=int(r.sent or 0),
                    unique_clicks=int(r.unique_clicks or 0),
                    unique_opens=int(r.unique_opens or 0),
                    bounces=int(r.bounces or 0),
                    complaints=int(r.complaints or 0),
                )
            )

        for (tenant, campaign_id, cohort_key), stats in grouped.items():
            decisions = decide_retirement(stats, min_retire_sends=s.mab_min_retire_sends)
            to_retire = [d.variant_label for d in decisions if d.retire]
            if not to_retire:
                continue
            await session.execute(
                update(Template)
                .where(Template.tenant == tenant)
                .where(Template.campaign_id == campaign_id)
                .where(Template.cohort_key == cohort_key)
                .where(Template.variant_label.in_(to_retire))
                .where(Template.status == "approved")
                .values(status="retired")
            )
            retired_count += len(to_retire)
            log.info(
                "variant_retirement.retired",
                tenant=tenant,
                campaign_id=str(campaign_id),
                cohort_key=cohort_key,
                variants=to_retire,
                reasons=[d.reason for d in decisions if d.retire],
            )
        await session.commit()

    return {"retired": retired_count}
