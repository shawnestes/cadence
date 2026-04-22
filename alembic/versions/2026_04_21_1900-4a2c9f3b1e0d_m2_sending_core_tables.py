"""m2_sending_core_tables

Revision ID: 4a2c9f3b1e0d
Revises: b46eaf8c6e47
Create Date: 2026-04-21 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4a2c9f3b1e0d'
down_revision: Union[str, None] = 'b46eaf8c6e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'campaigns',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('workflow_id', sa.UUID(), nullable=True),
        sa.Column('node_id', sa.Text(), nullable=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('vertical', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column('cohort_filter', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('ai_brief', sa.Text(), nullable=True),
        sa.Column(
            'mab_config',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(r"""'{"algo":"thompson","explore_pct"\:0.1}'::jsonb"""),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_campaigns_tenant_status', 'campaigns', ['tenant', 'status'], unique=False)

    op.create_table(
        'templates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('campaign_id', sa.UUID(), nullable=False),
        sa.Column('cohort_key', sa.Text(), nullable=False),
        sa.Column('variant_label', sa.Text(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('preheader', sa.Text(), nullable=True),
        sa.Column('body_html', sa.Text(), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=False),
        sa.Column('ai_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.Text(), server_default=sa.text("'generated'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'campaign_id', 'cohort_key', 'variant_label', name='uq_templates_campaign_cohort_variant'
        ),
    )
    op.create_index(
        'ix_templates_campaign_cohort_status',
        'templates',
        ['campaign_id', 'cohort_key', 'status'],
        unique=False,
    )

    op.create_table(
        'sends',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant', sa.Text(), nullable=False),
        sa.Column('journey_id', sa.UUID(), nullable=True),
        sa.Column('campaign_id', sa.UUID(), nullable=False),
        sa.Column('template_id', sa.UUID(), nullable=False),
        sa.Column('recipient_id', sa.UUID(), nullable=False),
        sa.Column('cohort_key', sa.Text(), nullable=False),
        sa.Column('variant_label', sa.Text(), nullable=False),
        sa.Column('esp', sa.Text(), nullable=False),
        sa.Column('esp_message_id', sa.Text(), nullable=True),
        sa.Column('rendered_subject', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('queued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id']),
        sa.ForeignKeyConstraint(['template_id'], ['templates.id']),
        sa.ForeignKeyConstraint(['recipient_id'], ['recipients.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sends_esp_message_id', 'sends', ['esp_message_id'], unique=False)
    op.execute("CREATE INDEX ix_sends_recipient_sent_at ON sends (recipient_id, sent_at DESC);")
    op.execute(
        "CREATE INDEX ix_sends_campaign_variant_sent_at ON sends (campaign_id, variant_label, sent_at DESC);"
    )
    op.create_index('ix_sends_status_queued_at', 'sends', ['status', 'queued_at'], unique=False)

    op.execute(
        """
        CREATE TABLE events (
            id uuid NOT NULL,
            tenant text NOT NULL,
            send_id uuid REFERENCES sends(id),
            recipient_id uuid NOT NULL,
            campaign_id uuid,
            variant_label text,
            cohort_key text,
            event_type text NOT NULL,
            event_subtype text,
            url text,
            user_agent text,
            ip_address inet,
            raw_payload jsonb,
            occurred_at timestamptz NOT NULL,
            ingested_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at);
        """
    )
    op.execute("CREATE INDEX ix_events_send_id ON events (send_id);")
    op.execute("CREATE INDEX ix_events_recipient_occurred ON events (recipient_id, occurred_at DESC);")
    op.execute("CREATE INDEX ix_events_type_occurred ON events (event_type, occurred_at DESC);")
    op.execute(
        "CREATE TABLE events_2026_04 PARTITION OF events "
        "FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');"
    )
    op.execute(
        "CREATE TABLE events_2026_05 PARTITION OF events "
        "FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS events_2026_05;")
    op.execute("DROP TABLE IF EXISTS events_2026_04;")
    op.execute("DROP TABLE IF EXISTS events;")

    op.drop_index('ix_sends_status_queued_at', table_name='sends')
    op.execute("DROP INDEX IF EXISTS ix_sends_campaign_variant_sent_at;")
    op.execute("DROP INDEX IF EXISTS ix_sends_recipient_sent_at;")
    op.drop_index('ix_sends_esp_message_id', table_name='sends')
    op.drop_table('sends')

    op.drop_index('ix_templates_campaign_cohort_status', table_name='templates')
    op.drop_table('templates')

    op.drop_index('ix_campaigns_tenant_status', table_name='campaigns')
    op.drop_table('campaigns')
