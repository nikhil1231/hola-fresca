"""baseline: the schema as it stood before accounts

Deliberately empty. It exists so an existing single-user database can be stamped
with it and then upgraded, rather than alembic having to reconstruct a schema
that ``Base.metadata.create_all`` and the runtime-column patches in
``app.db.session`` built up over the phases before this one.

Fresh databases are still created with ``create_all`` and stamped straight to
head — see :func:`app.db.session.init_db`. Alembic's job here is evolving the one
real database, not defining the schema.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-04
"""
from __future__ import annotations

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
