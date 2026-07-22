from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.domain.segment import normalize
from app.models.recipient import Recipient
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
    """Preview how many (and which) recipients a segment matches.

    Validates ``payload.segment`` via ``app.domain.segment.normalize``
    (400 with the validation error message on structurally invalid input),
    then runs a count query and a limited sample query against the
    compiled SQL predicate — never materializing more than
    ``_SAMPLE_SIZE`` full rows.
    """
    try:
        normalize(payload.segment)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    predicate = compile_segment(payload.segment)
    tenant_predicate = Recipient.tenant == payload.tenant

    count = (
        await db.execute(
            select(func.count())
            .select_from(Recipient)
            .where(tenant_predicate)
            .where(predicate)
        )
    ).scalar_one()

    sample_rows = (
        await db.execute(
            select(Recipient)
            .where(tenant_predicate)
            .where(predicate)
            .order_by(Recipient.id)
            .limit(_SAMPLE_SIZE)
        )
    ).scalars().all()

    return SegmentPreviewResponse(
        count=int(count),
        sample=[
            SegmentPreviewRecipient(
                id=str(r.id),
                email=r.email,
                vertical=r.vertical,
                engagement_tier=r.engagement_tier,
            )
            for r in sample_rows
        ],
    )
