"""ocado_auth_events: record what the auth ladder did, so session life is measurable

The ladder has always logged its rungs, but only to the process log. That is a
bad place for it: journald retention is not the app's to rely on, and the
question this table exists to answer — how long an Ocado session actually lives
before somebody has to log in by hand — needs weeks of history before it has an
answer at all. Grepping two weeks of journald for it yielded seven events.

Nothing reads this table to make a decision. It is written by
:mod:`app.ocado.heartbeat` and read by a person, which is why ``outcome`` has no
CHECK constraint: a new outcome should not need a migration.

Revision ID: 0003_ocado_auth_events
Revises: 0002_accounts
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_ocado_auth_events"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("ocado_auth_events"):
        return

    op.create_table(
        "ocado_auth_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("rung", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="request"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "rung in ('probe', 'silent', 'login', 'otp')",
            name="ck_ocado_auth_event_rung",
        ),
    )
    op.create_index(
        "ix_ocado_auth_events_account_id", "ocado_auth_events", ["account_id"]
    )
    op.create_index("ix_ocado_auth_events_rung", "ocado_auth_events", ["rung"])
    op.create_index("ix_ocado_auth_events_outcome", "ocado_auth_events", ["outcome"])
    op.create_index("ix_ocado_auth_events_trigger", "ocado_auth_events", ["trigger"])
    op.create_index("ix_ocado_auth_events_created_at", "ocado_auth_events", ["created_at"])
    # The query this table exists for is "this account, most recent first".
    op.create_index(
        "ix_ocado_auth_event_account_created",
        "ocado_auth_events",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ocado_auth_event_account_created", table_name="ocado_auth_events")
    op.drop_index("ix_ocado_auth_events_created_at", table_name="ocado_auth_events")
    op.drop_index("ix_ocado_auth_events_trigger", table_name="ocado_auth_events")
    op.drop_index("ix_ocado_auth_events_outcome", table_name="ocado_auth_events")
    op.drop_index("ix_ocado_auth_events_rung", table_name="ocado_auth_events")
    op.drop_index("ix_ocado_auth_events_account_id", table_name="ocado_auth_events")
    op.drop_table("ocado_auth_events")
