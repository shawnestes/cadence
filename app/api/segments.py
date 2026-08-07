from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_engagement_config
from app.core.db import get_db
from app.domain.segment import normalize
from app.schemas.segment import (
    SegmentPreviewRecipient,
    SegmentPreviewRequest,
    SegmentPreviewResponse,
)
from app.services.segment_query import compile_segment

router = APIRouter(tags=["segments"])

_SAMPLE_SIZE = 10


@router.post("/segments/preview", response_model=SegmentPreviewResponse)
async def preview_segment(
    payload: SegmentPreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> SegmentPreviewResponse:
    """Preview how many (and which) contacts a segment matches.

    Validates ``payload.segment`` via ``app.domain.segment.normalize``
    (400 with the validation error message on structurally invalid input),
    then runs a count query and a limited sample query against the
    compiled SQL predicate targeting ``contact_profile`` — never
    materializing more than ``_SAMPLE_SIZE`` full rows.
    """
    try:
        normalize(payload.segment)
        where_sql, params = compile_segment(
            payload.segment,
            get_engagement_config(),
            acknowledges_cold=payload.acknowledges_cold,
            include_unsendable=payload.include_unsendable,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    params["tenant"] = payload.tenant

    count_sql = text(
        f"SELECT count(*) FROM contact_profile cp WHERE cp.tenant = :tenant AND {where_sql}"
    )
    count: int = (await db.execute(count_sql, params)).scalar_one()

    sample_sql = text(
        f"SELECT cp.contact_id, cp.email_domain, cp.mailbox_provider,"
        f" cp.engagement_tier, cp.engagement_score"
        f" FROM contact_profile cp"
        f" WHERE cp.tenant = :tenant AND {where_sql}"
        f" ORDER BY cp.contact_id LIMIT {_SAMPLE_SIZE}"
    )
    rows = (await db.execute(sample_sql, params)).mappings().all()

    return SegmentPreviewResponse(
        count=int(count),
        sample=[
            SegmentPreviewRecipient(
                contact_id=str(row["contact_id"]),
                email_domain=row["email_domain"],
                mailbox_provider=row["mailbox_provider"],
                engagement_tier=row["engagement_tier"],
                engagement_score=row["engagement_score"],
            )
            for row in rows
        ],
    )
