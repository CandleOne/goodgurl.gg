"""add stream_key to user

Revision ID: 8d25d91d93b4
Revises: 
Create Date: 2026-04-22 21:26:32.405182

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8d25d91d93b4'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stream_key', sa.String(32), nullable=True))
        batch_op.create_index('ix_user_stream_key', ['stream_key'], unique=True)


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index('ix_user_stream_key')
        batch_op.drop_column('stream_key')
