"""profile.wake_time for wake-relative reminders

Revision ID: e7f2a3b8c9d0
Revises: c4d1a2b3e5f6
Create Date: 2026-07-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e7f2a3b8c9d0'
down_revision = 'c4d1a2b3e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('profiles', sa.Column('wake_time', sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column('profiles', 'wake_time')
