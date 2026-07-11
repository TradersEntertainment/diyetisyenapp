"""lab_results table for blood-test tracking

Revision ID: a1b2c3d4e5f6
Revises: f8a3b4c9d1e2
Create Date: 2026-07-11 02:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'f8a3b4c9d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'lab_results',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('panel', sa.String(length=120), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(length=32), nullable=True),
        sa.Column('taken_on', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_lab_results_user_id'), 'lab_results', ['user_id'])
    op.create_index(op.f('ix_lab_results_ts'), 'lab_results', ['ts'])


def downgrade() -> None:
    op.drop_index(op.f('ix_lab_results_ts'), table_name='lab_results')
    op.drop_index(op.f('ix_lab_results_user_id'), table_name='lab_results')
    op.drop_table('lab_results')
