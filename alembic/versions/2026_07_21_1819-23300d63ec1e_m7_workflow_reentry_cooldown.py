"""m7_workflow_reentry_cooldown

Add `workflows.reentry_cooldown` (nullable TEXT, ISO-8601 duration) per
SPEC §6.5: re-entry after journey completion is configurable per workflow.
NULL means never re-enter (the spec default); "PT0S" means immediate
re-entry is allowed. Additive-only, no backfill needed since NULL is the
correct default for all existing workflows.

Revision ID: 23300d63ec1e
Revises: c4d5e6f7a8b9
Create Date: 2026-07-21 18:19:21.085789

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '23300d63ec1e'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("reentry_cooldown", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflows", "reentry_cooldown")
