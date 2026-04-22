"""m6_alerts

Revision ID: a1b2c3d4e5f6
Revises: 9d1a5f6b2e3c
Create Date: 2026-04-22 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9d1a5f6b2e3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alerts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('metric', sa.Text(), nullable=False),
        sa.Column('severity', sa.Text(), nullable=False),
        sa.Column('threshold', sa.Float(), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('pending_since', sa.DateTime(timezone=True), nullable=False),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_alerts_tenant_metric_status', 'alerts', ['tenant', 'metric', 'status'])
    op.create_index('ix_alerts_tenant_created_at', 'alerts', ['tenant', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_alerts_tenant_created_at', table_name='alerts')
    op.drop_index('ix_alerts_tenant_metric_status', table_name='alerts')
    op.drop_table('alerts')
