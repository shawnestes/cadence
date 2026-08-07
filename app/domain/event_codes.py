"""SES event type code lookup (M-SEG-2).

Maps SES event name strings (as emitted by SNS notifications) to smallint
codes stored in the event_store table. Pure domain logic: no FastAPI, no
SQLAlchemy.
"""

from __future__ import annotations

SES_EVENT_TYPE_CODES: dict[str, int] = {
    "Send": 1,
    "Delivery": 2,
    "Bounce": 3,
    "Complaint": 4,
    "Reject": 5,
    "Open": 6,
    "Click": 7,
    "RenderingFailure": 8,
    "DeliveryDelay": 9,
    "Subscription": 10,
    "Conversion": 11,
}

EVENT_TYPE_NAMES: dict[int, str] = {code: name for name, code in SES_EVENT_TYPE_CODES.items()}


def translate_ses_event_type(name: str) -> int:
    """Return the smallint code for a SES event type name.

    Raises ValueError for unknown names so callers can surface the bad event
    rather than silently storing a wrong code.
    """
    try:
        return SES_EVENT_TYPE_CODES[name]
    except KeyError:
        raise ValueError(f"Unknown SES event type: {name!r}")
