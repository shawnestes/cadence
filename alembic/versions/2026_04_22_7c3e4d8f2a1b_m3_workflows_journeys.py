"""m3_workflows_journeys

Revision ID: 7c3e4d8f2a1b
Revises: 4a2c9f3b1e0d
Create Date: 2026-04-22 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '7c3e4d8f2a1b'
down_revision: Union[str, None] = '4a2c9f3b1e0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workflows',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('vertical', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column('definition', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('entry_segment', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workflows_tenant_status', 'workflows', ['tenant', 'status'], unique=False)

    op.create_table(
        'journeys',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('workflow_id', sa.UUID(), nullable=False),
        sa.Column('workflow_version', sa.Integer(), nullable=False),
        sa.Column('recipient_id', sa.UUID(), nullable=False),
        sa.Column('current_node_id', sa.Text(), nullable=False),
        sa.Column('next_action_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column(
            'context',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id']),
        sa.ForeignKeyConstraint(['recipient_id'], ['recipients.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workflow_id', 'recipient_id', name='uq_journeys_workflow_recipient'),
    )
    op.create_index(
        'ix_journeys_active_next_action',
        'journeys',
        ['status', 'next_action_at'],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index('ix_journeys_recipient_id', 'journeys', ['recipient_id'], unique=False)

    op.create_foreign_key(
        "fk_campaigns_workflow_id",
        "campaigns",
        "workflows",
        ["workflow_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_campaigns_workflow_id", "campaigns", type_="foreignkey")

    op.drop_index('ix_journeys_recipient_id', table_name='journeys')
    op.drop_index(
        'ix_journeys_active_next_action',
        table_name='journeys',
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table('journeys')

    op.drop_index('ix_workflows_tenant_status', table_name='workflows')
    op.drop_table('workflows')
