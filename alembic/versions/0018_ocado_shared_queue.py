"""durable shared Ocado debounce queue

Revision ID: 0018_ocado_shared_queue
Revises: 0017_purchases
Create Date: 2026-10-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_ocado_shared_queue"
down_revision = "0017_purchases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("ocado_shared_queue"):
        op.create_table(
            "ocado_shared_queue",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("target_id", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("actions_json", sa.Text(), nullable=False),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["account_id"], ["retailer_accounts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("account_id", name="uq_ocado_shared_queue_account"),
            sa.CheckConstraint(
                "status in ('idle', 'pending', 'running', 'failed', 'succeeded')",
                name="ck_ocado_shared_queue_status",
            ),
        )
        op.create_index("ix_ocado_shared_queue_user_id", "ocado_shared_queue", ["user_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("ocado_shared_queue"):
        op.drop_table("ocado_shared_queue")
