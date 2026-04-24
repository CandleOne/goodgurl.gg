"""add_duel_spectator_queue

Revision ID: 9f51d9f4ef2d
Revises: c4d5e6f7a8b9
Create Date: 2026-04-24 00:56:38.577715

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9f51d9f4ef2d'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'duel_spectator_queue',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id')
    )
    with op.batch_alter_table('duel_spectator_queue', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_duel_spectator_queue_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('duel_spectator_queue', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_duel_spectator_queue_user_id'))
    op.drop_table('duel_spectator_queue')
