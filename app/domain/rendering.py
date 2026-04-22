"""Pure merge-tag rendering. No FastAPI/SQLAlchemy imports (domain rule).

Supports `{{ var }}` and `{{ attributes.first_name }}` dotted access.
Missing tags raise `MissingMergeTag` (a `ValueError` subclass).
"""

from __future__ import annotations

import re
from typing import Any

_TAG_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class MissingMergeTag(ValueError):
    """Raised when a merge tag has no value in the rendering context."""

    def __init__(self, tag: str) -> None:
        super().__init__(tag)
        self.tag = tag

    def __str__(self) -> str:
        return self.tag


def _resolve(context: dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    cur: Any = context
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise MissingMergeTag(path)
    if cur is None:
        raise MissingMergeTag(path)
    return cur


def render(template: str, context: dict[str, Any]) -> str:
    """Render `{{var}}` style merge tags against `context`.

    Dotted paths (e.g. `attributes.first_name`) walk nested dicts.
    Missing tags raise `MissingMergeTag`.
    """

    def _sub(match: re.Match[str]) -> str:
        tag = match.group(1)
        value = _resolve(context, tag)
        return str(value)

    return _TAG_RE.sub(_sub, template)


def render_template(
    template_row: Any,
    recipient_row: Any,
    extras: dict[str, Any],
) -> tuple[str, str, str]:
    """Render subject, html, and text bodies for a (template, recipient) pair.

    Context is built from:
    - `email`: recipient.email
    - `tenant`: recipient.tenant
    - flat attribute keys (first_name, last_name, ...): from recipient.attributes
    - `attributes`: the full attributes dict (for dotted access)
    - `unsub_url` and anything else in extras
    """
    attributes: dict[str, Any] = dict(getattr(recipient_row, "attributes", {}) or {})
    context: dict[str, Any] = {
        "email": recipient_row.email,
        "tenant": recipient_row.tenant,
        "attributes": attributes,
    }
    context.update(attributes)
    context.update(extras)

    subject = render(template_row.subject, context)
    body_html = render(template_row.body_html, context)
    body_text = render(template_row.body_text, context)
    return subject, body_html, body_text
