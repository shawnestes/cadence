from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.suppression import Suppression
from app.schemas.suppression import SuppressionIn, SuppressionListOut, SuppressionOut

router = APIRouter(tags=["suppressions"])


@router.get("/suppressions", response_model=SuppressionListOut)
async def list_suppressions(
    tenant: str = Query(default="otto"),
    email: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> SuppressionListOut:
    base = select(Suppression).where(Suppression.tenant == tenant)
    count_q = (
        select(func.count()).select_from(Suppression).where(Suppression.tenant == tenant)
    )
    if email is not None:
        base = base.where(Suppression.email == email)
        count_q = count_q.where(Suppression.email == email)

    base = base.order_by(Suppression.suppressed_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return SuppressionListOut(
        suppressions=[SuppressionOut.model_validate(r) for r in rows],
        total=int(total),
    )


@router.post("/suppressions", response_model=SuppressionOut)
async def create_suppression(
    payload: SuppressionIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> SuppressionOut:
    email = str(payload.email)
    row = Suppression(
        tenant=payload.tenant,
        email=email,
        reason=payload.reason,
        source=payload.source,
        campaign_id=payload.campaign_id,
        notes=payload.notes,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(Suppression)
                .where(Suppression.tenant == payload.tenant)
                .where(Suppression.email == email)
            )
        ).scalar_one()
        response.status_code = status.HTTP_200_OK
        return SuppressionOut.model_validate(existing)

    response.status_code = status.HTTP_201_CREATED
    return SuppressionOut.model_validate(row)


@router.delete("/suppressions/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suppression(
    email: str,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(
        delete(Suppression)
        .where(Suppression.tenant == tenant)
        .where(Suppression.email == email)
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
