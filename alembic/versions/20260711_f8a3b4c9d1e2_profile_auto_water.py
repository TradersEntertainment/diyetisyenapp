"""profile.auto_water for hands-off water logging

Revision ID: f8a3b4c9d1e2
Revises: e7f2a3b8c9d0
Create Date: 2026-07-11 01:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f8a3b4c9d1e2'
down_revision = 'e7f2a3b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'profiles',
        sa.Column('auto_water', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('profiles', 'auto_water')
