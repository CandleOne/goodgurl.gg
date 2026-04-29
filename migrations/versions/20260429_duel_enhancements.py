"""Duel enhancements migration.

Adds duel_wins/duel_losses counters on User, a reference for reporting comments,
queue duration, and a chat log table for in-duel messages.
"""

from alembic import op
import sqlalchemy as sa

revision = '20260429_duel_enhancements'
down_revision = '877f1eadb63f0051170802fd6f3fe036d426d42b'
branch_labels = None
depends_on = None


def upgrade():
    # User stats for duels
    op.add_column('user', sa.Column('duel_wins', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('user', sa.Column('duel_losses', sa.Integer(), nullable=False, server_default='0'))

    # Reports: optionally reference a comment that was reported
    op.add_column('report', sa.Column('comment_id', sa.Integer(), nullable=True))

    # Matchmaking queue: duration per duel (seconds)
    op.add_column('duel_queue', sa.Column('duration_seconds', sa.Integer(), nullable=False, server_default='180'))

    # In-duel chat messages (live chat during a duel)
    op.create_table(
        'duel_chat',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('duel_id', sa.Integer(), sa.ForeignKey('duel.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('body', sa.String(length=280), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )


def downgrade():
    op.drop_table('duel_chat')
    op.drop_column('duel_queue', 'duration_seconds')
    op.drop_column('report', 'comment_id')
    op.drop_column('user', 'duel_losses')
    op.drop_column('user', 'duel_wins')
