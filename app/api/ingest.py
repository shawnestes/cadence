from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.recipient import Recipient
from app.schemas.recipient import RecipientIngestIn, RecipientIngestOut

router = APIRouter(tags=["ingest"])


@router.post("/ingest/recipients", response_model=RecipientIngestOut)
async def ingest_recipient(
    payload: RecipientIngestIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RecipientIngestOut:
    now = datetime.now(timezone.utc)
    email = str(payload.email)

    insert_values: dict = {
        "tenant": payload.tenant,
        "email": email,
        "external_id": payload.external_id,
        "vertical": payload.vertical,
        "first_seen_at": now,
        "last_seen_at": now,
        "consent_source": payload.consent_source,
        "consent_at": payload.consent_at,
        "attributes": payload.attributes or {},
        "status": "active",
    }

    stmt = pg_insert(Recipient).values(**insert_values)

    update_set: dict = {
        "last_seen_at": now,
        "consent_source": stmt.excluded.consent_source,
        "consent_at": stmt.excluded.consent_at,
        "attributes": Recipient.__table__.c.attributes.op("||")(stmt.excluded.attributes),
        "updated_at": now,
    }
    if payload.external_id is not None:
        update_set["external_id"] = stmt.excluded.external_id

    stmt = stmt.on_conflict_do_update(
        index_elements=[Recipient.tenant, Recipient.email],
        set_=update_set,
    ).returning(
        Recipient.id,
        Recipient.tenant,
        Recipient.email,
        Recipient.status,
        literal_column("(xmax = 0)").label("created"),
    )

    result = await db.execute(stmt)
    row = result.one()
    await db.commit()

    response.status_code = (
        status.HTTP_201_CREATED if row.created else status.HTTP_200_OK
    )
    return RecipientIngestOut(
        id=str(row.id),
        tenant=row.tenant,
        email=row.email,
        status=row.status,
        created=bool(row.created),
    )
