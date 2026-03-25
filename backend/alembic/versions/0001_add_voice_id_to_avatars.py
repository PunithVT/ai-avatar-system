"""add voice_id to avatars

Revision ID: 0001
Revises:
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = '0000'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('avatars', sa.Column('voice_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('avatars', 'voice_id')
