"""add_duel_tables

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-24 00:00:00

"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('duel',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('activity_id', sa.Integer(), nullable=False),
        sa.Column('challenger_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('opponent_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='waiting'),
        sa.Column('winner_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('challenger_votes', sa.Integer(), server_default='0'),
        sa.Column('opponent_votes', sa.Integer(), server_default='0'),
        sa.Column('duration_seconds', sa.Integer(), server_default='180'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('duel_queue',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('activity_id', sa.Integer(), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id')
    )
    op.create_table('duel_spectator',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('duel_id', sa.Integer(), sa.ForeignKey('duel.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.Column('has_voted', sa.Boolean(), server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('duel_id', 'user_id', name='uq_duel_spectator')
    )


def downgrade():
    op.drop_table('duel_spectator')
    op.drop_table('duel_queue')
    op.drop_table('duel')
