"""add agent_logs table

Revision ID: b7c3d4e5f6a1
Revises: a548ed3f0678
Create Date: 2026-06-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c3d4e5f6a1'
down_revision: Union[str, Sequence[str], None] = 'a548ed3f0678'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('log_type', sa.String(length=50), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('byte_offset', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_logs_agent_id_agents'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_logs')),
    )
    op.create_index(op.f('ix_agent_logs_agent_id'), 'agent_logs', ['agent_id'], unique=False)
    op.create_index(op.f('ix_agent_logs_log_type'), 'agent_logs', ['log_type'], unique=False)
    op.create_index(op.f('ix_agent_logs_created_at'), 'agent_logs', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_logs_created_at'), table_name='agent_logs')
    op.drop_index(op.f('ix_agent_logs_log_type'), table_name='agent_logs')
    op.drop_index(op.f('ix_agent_logs_agent_id'), table_name='agent_logs')
    op.drop_table('agent_logs')
