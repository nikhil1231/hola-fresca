"""database-backed, user-owned retailer account registry

The legacy account id remains the stable key used by session/profile paths and
cart/auth history, so no secret files or historical rows need to move.  Passwords
are deliberately absent: only the retailer session survives an interactive
login.

Revision ID: 0012_retailer_accounts
Revises: 0011_cart_ledger_retailer
Create Date: 2026-08-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app import config
from app.db.retailer_accounts import seed_legacy_ocado_accounts

revision = "0012_retailer_accounts"
down_revision = "0011_cart_ledger_retailer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("retailer_accounts"):
        return
    op.create_table(
        "retailer_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("retailer", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("otp_markers", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="never"),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_retailer_account_key"),
        sa.UniqueConstraint(
            "user_id", "retailer", name="uq_retailer_account_user_retailer"
        ),
    )
    op.create_index(
        "ix_retailer_accounts_user_id", "retailer_accounts", ["user_id"]
    )
    op.create_index(
        "ix_retailer_accounts_retailer", "retailer_accounts", ["retailer"]
    )
    op.create_index(
        "ix_retailer_accounts_status", "retailer_accounts", ["status"]
    )
    seed_legacy_ocado_accounts(op.get_bind(), config.OCADO_ACCOUNTS)


def downgrade() -> None:
    op.drop_index("ix_retailer_accounts_status", table_name="retailer_accounts")
    op.drop_index("ix_retailer_accounts_retailer", table_name="retailer_accounts")
    op.drop_index("ix_retailer_accounts_user_id", table_name="retailer_accounts")
    op.drop_table("retailer_accounts")
