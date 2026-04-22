"""add hypno_chain_watch table

Revision ID: d7632fa65589
Revises: 8d25d91d93b4
Create Date: 2026-04-22 22:43:43.904941

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7632fa65589'
down_revision = '8d25d91d93b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'hypno_chain_watch',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('reddit_id', sa.String(length=20), nullable=False),
        sa.Column('watched_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'reddit_id', name='uq_hypno_watch_user_post'),
    )
    with op.batch_alter_table('hypno_chain_watch', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_hypno_chain_watch_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hypno_chain_watch_watched_at'), ['watched_at'], unique=False)


def downgrade():
    with op.batch_alter_table('hypno_chain_watch', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_hypno_chain_watch_watched_at'))
        batch_op.drop_index(batch_op.f('ix_hypno_chain_watch_user_id'))
    op.drop_table('hypno_chain_watch')
