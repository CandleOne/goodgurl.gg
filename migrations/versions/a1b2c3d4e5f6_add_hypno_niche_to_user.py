"""add hypno_niche to user

Revision ID: a1b2c3d4e5f6
Revises: d7632fa65589
Create Date: 2026-04-23

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'd7632fa65589'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hypno_niche', sa.String(length=80), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('hypno_niche')
