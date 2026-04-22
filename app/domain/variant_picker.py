from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.mab import VariantStats, select_variant
from app.models.template import Template


async def pick_variant(
    session: AsyncSession,
    *,
    tenant: str,
    campaign_id: UUID,
    cohort_key: str | None,
) -> Template | None:
    """Pick an approved template for (tenant, campaign, cohort) using MAB.

    If cohort_key is None or no template exists for the cohort, falls back to
    any approved template for the campaign.
    Returns None if no approved template is available.
    """
    base_stmt = (
        select(Template)
        .where(Template.tenant == tenant)
        .where(Template.campaign_id == campaign_id)
        .where(Template.status == "approved")
    )
    if cohort_key is not None:
        stmt = base_stmt.where(Template.cohort_key == cohort_key)
        templates = (await session.execute(stmt)).scalars().all()
    else:
        templates = []

    if not templates:
        templates = (await session.execute(base_stmt)).scalars().all()

    if not templates:
        return None

    if cohort_key is not None:
        stats_rows = (await session.execute(
            text(
                "SELECT variant_label, sent, unique_clicks, unique_opens, bounces, complaints "
                "FROM variant_stats "
                "WHERE tenant = :tenant AND campaign_id = :campaign_id AND cohort_key = :cohort_key"
            ),
            {"tenant": tenant, "campaign_id": campaign_id, "cohort_key": cohort_key},
        )).all()
    else:
        stats_rows = []

    stats_by_label = {r.variant_label: r for r in stats_rows}
    stats: list[VariantStats] = []
    for t in templates:
        r = stats_by_label.get(t.variant_label)
        stats.append(VariantStats(
            variant_label=t.variant_label,
            sent=int(r.sent) if r else 0,
            unique_clicks=int(r.unique_clicks) if r else 0,
            unique_opens=int(r.unique_opens) if r else 0,
            bounces=int(r.bounces) if r else 0,
            complaints=int(r.complaints) if r else 0,
        ))

    min_explore = get_settings().mab_min_explore_sends
    chosen_label = select_variant(stats, min_explore_sends=min_explore)
    if chosen_label is None:
        return None

    for t in templates:
        if t.variant_label == chosen_label:
            return t
    return None
