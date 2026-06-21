"""rewrite policies to match local format

Revision ID: c8d9e0f1a2b3
Revises: b7c3d4e5f6a1
Create Date: 2026-06-19 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'b7c3d4e5f6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Drop FK constraints that reference policies.id from other tables
    op.drop_constraint(
        'fk_policy_agent_assignments_policy_id_policies',
        'policy_agent_assignments', type_='foreignkey'
    )
    op.drop_constraint(
        'fk_policy_group_assignments_policy_id_policies',
        'policy_group_assignments', type_='foreignkey'
    )
    op.drop_constraint(
        'fk_violation_logs_policy_id_policies',
        'violation_logs', type_='foreignkey'
    )

    # 2) Drop old columns from policies
    op.drop_column('policies', 'rule_type')
    op.drop_column('policies', 'rule')
    op.drop_column('policies', 'channel')

    # 3) Add new columns
    op.add_column('policies', sa.Column('type', sa.String(length=50), nullable=False, server_default='regex'))
    op.add_column('policies', sa.Column('patterns', postgresql.JSONB(), nullable=True))
    op.add_column('policies', sa.Column('keywords', postgresql.JSONB(), nullable=True))
    op.add_column('policies', sa.Column('channels', postgresql.JSONB(), nullable=False, server_default='["browser","clipboard","peripheral_storage"]'))
    op.add_column('policies', sa.Column('context_words', postgresql.JSONB(), nullable=True))
    op.add_column('policies', sa.Column('context_range', sa.Integer(), server_default='0', nullable=False))

    # 4) Recreate FK constraints
    op.create_foreign_key(
        'fk_policy_agent_assignments_policy_id_policies',
        'policy_agent_assignments', 'policies',
        ['policy_id'], ['id'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_policy_group_assignments_policy_id_policies',
        'policy_group_assignments', 'policies',
        ['policy_id'], ['id'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_violation_logs_policy_id_policies',
        'violation_logs', 'policies',
        ['policy_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_policy_agent_assignments_policy_id_policies', 'policy_agent_assignments', type_='foreignkey')
    op.drop_constraint('fk_policy_group_assignments_policy_id_policies', 'policy_group_assignments', type_='foreignkey')
    op.drop_constraint('fk_violation_logs_policy_id_policies', 'violation_logs', type_='foreignkey')

    op.drop_column('policies', 'context_range')
    op.drop_column('policies', 'context_words')
    op.drop_column('policies', 'channels')
    op.drop_column('policies', 'keywords')
    op.drop_column('policies', 'patterns')
    op.drop_column('policies', 'type')

    op.add_column('policies', sa.Column('channel', sa.String(length=50), server_default='all', nullable=False))
    op.add_column('policies', sa.Column('rule', postgresql.JSONB(), nullable=False))
    op.add_column('policies', sa.Column('rule_type', sa.String(length=50), nullable=False))

    op.create_foreign_key(
        'fk_policy_agent_assignments_policy_id_policies',
        'policy_agent_assignments', 'policies',
        ['policy_id'], ['id'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_policy_group_assignments_policy_id_policies',
        'policy_group_assignments', 'policies',
        ['policy_id'], ['id'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_violation_logs_policy_id_policies',
        'violation_logs', 'policies',
        ['policy_id'], ['id'], ondelete='SET NULL'
    )
