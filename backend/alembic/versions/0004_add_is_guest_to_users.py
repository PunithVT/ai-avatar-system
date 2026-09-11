"""add is_guest to users

Guest ("Continue as Guest") sessions used to share one global `demo-user`
row, which meant every guest could read every other guest's avatars and
transcripts. Guests now get their own anonymous user row instead; this flag
marks those rows so the retention sweep can reap them and the UI can tell a
guest apart from a registered account.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default backfills existing rows in the same statement; the
    # column stays NOT NULL afterwards so application code never has to
    # treat NULL as a third state.
    op.add_column(
        "users",
        sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_users_is_guest", "users", ["is_guest"])


def downgrade() -> None:
    op.drop_index("ix_users_is_guest", table_name="users")
    op.drop_column("users", "is_guest")
