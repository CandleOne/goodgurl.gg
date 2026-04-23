"""add_social_links_to_user

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b3c4d5e6f7a8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('social_onlyfans', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('social_reddit', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('social_x', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('social_x')
        batch_op.drop_column('social_reddit')
        batch_op.drop_column('social_onlyfans')
