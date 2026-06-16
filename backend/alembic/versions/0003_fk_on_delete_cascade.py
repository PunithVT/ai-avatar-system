"""add ON DELETE CASCADE to all child foreign keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11

The initial schema created every foreign key with the default NO ACTION.
That made two user-facing flows return HTTP 500:

  * DELETE /sessions/{id} — every chatted session has an auto-titled row in
    `conversations` referencing it; the ORM only cascaded `messages`, so the
    delete hit an FK violation on `conversations.session_id`.
  * DELETE /avatars/{id} — any avatar that had ever been used in a session
    hit an FK violation on `sessions.avatar_id`.

The ORM relationships now declare delete-orphan cascades (which fixes the
SQLite test path too); this migration aligns the database so direct SQL or
out-of-band deletes behave identically.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (table, constraint, column, referenced table)
_FKS = [
    ("avatars", "avatars_user_id_fkey", "user_id", "users"),
    ("sessions", "sessions_user_id_fkey", "user_id", "users"),
    ("sessions", "sessions_avatar_id_fkey", "avatar_id", "avatars"),
    ("messages", "messages_session_id_fkey", "session_id", "sessions"),
    ("conversations", "conversations_session_id_fkey", "session_id", "sessions"),
]


def upgrade() -> None:
    # SQLite's unnamed FKs (created via plain sa.ForeignKey) aren't reachable by
    # name through batch mode, and the ORM's delete-orphan cascades already give
    # SQLite the same behavior at the application level — so this migration is a
    # Postgres-only DDL change.
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, constraint, column, ref_table in _FKS:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(
            constraint, table, ref_table, [column], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, constraint, column, ref_table in _FKS:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, ref_table, [column], ["id"])
