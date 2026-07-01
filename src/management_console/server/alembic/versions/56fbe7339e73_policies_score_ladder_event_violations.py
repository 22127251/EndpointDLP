"""policies score ladder + event-centric violations

Revision ID: 56fbe7339e73
Revises: c8d9e0f1a2b3
Create Date: 2026-06-26 20:05:06.247829

Phase 1 of the agent<->server integration:
- policies: drop the single `action`, add the confidence-score ladder
  (score_base / score_context_boost / actions) + user_message. The old `action`
  is backfilled into an always-on single-rung ladder before being dropped.
- violations become event-centric: violation_logs loses the single policy_id/action
  and gains decision/reason; a new violation_policy_matches child table holds one row
  per triggered policy (mirrors the agent's events.jsonl).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '56fbe7339e73'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_ACTIONS = '[{"min_score": 1.0, "action": "block"}, {"min_score": 0.0, "action": "allow_log"}]'


def upgrade() -> None:
    # ── policies: score ladder replaces the single `action` ──
    op.add_column('policies', sa.Column('user_message', sa.Text(), server_default='', nullable=True))
    op.add_column('policies', sa.Column('score_base', sa.Float(), server_default='0.5', nullable=False))
    op.add_column('policies', sa.Column('score_context_boost', sa.Float(), server_default='0.5', nullable=False))
    op.add_column('policies', sa.Column('actions', postgresql.JSONB(astext_type=sa.Text()),
                                        server_default=_DEFAULT_ACTIONS, nullable=False))
    # Preserve old behavior: an always-on single-action ladder == the old `action`.
    op.execute(
        "UPDATE policies SET actions = "
        "jsonb_build_array(jsonb_build_object('min_score', 0.0, 'action', action))"
    )
    op.drop_column('policies', 'action')

    # ── violation_logs: event-centric (drop single policy_id/action) ──
    op.drop_constraint('fk_violation_logs_policy_id_policies', 'violation_logs', type_='foreignkey')
    op.drop_column('violation_logs', 'policy_id')
    op.drop_column('violation_logs', 'action')
    # add decision with a temporary default to fill any existing rows, then drop the
    # default so the column matches the model (no server_default).
    op.add_column('violation_logs', sa.Column('decision', sa.String(length=20),
                                              server_default='BLOCK', nullable=False))
    op.alter_column('violation_logs', 'decision', server_default=None)
    op.add_column('violation_logs', sa.Column('reason', sa.String(length=50), nullable=True))

    # ── new child table: one row per triggered policy ──
    op.create_table(
        'violation_policy_matches',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('violation_log_id', sa.Uuid(), nullable=False),
        sa.Column('policy_id', sa.Uuid(), nullable=True),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('with_context', sa.Integer(), nullable=False),
        sa.Column('context_words_triggered', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['policy_id'], ['policies.id'],
                                name=op.f('fk_violation_policy_matches_policy_id_policies'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['violation_log_id'], ['violation_logs.id'],
                                name=op.f('fk_violation_policy_matches_violation_log_id_violation_logs'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_violation_policy_matches')),
    )


def downgrade() -> None:
    op.drop_table('violation_policy_matches')

    # violation_logs back to the single policy_id/action shape
    op.drop_column('violation_logs', 'reason')
    op.drop_column('violation_logs', 'decision')
    op.add_column('violation_logs', sa.Column('action', sa.String(length=50),
                                              server_default='block', nullable=False))
    op.alter_column('violation_logs', 'action', server_default=None)
    op.add_column('violation_logs', sa.Column('policy_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_violation_logs_policy_id_policies', 'violation_logs', 'policies',
                          ['policy_id'], ['id'], ondelete='SET NULL')

    # policies back to the single `action`
    op.add_column('policies', sa.Column('action', sa.String(length=50),
                                        server_default='block', nullable=False))
    op.execute("UPDATE policies SET action = COALESCE(actions->0->>'action', 'block')")
    op.alter_column('policies', 'action', server_default=None)
    op.drop_column('policies', 'actions')
    op.drop_column('policies', 'score_context_boost')
    op.drop_column('policies', 'score_base')
    op.drop_column('policies', 'user_message')
