"""Persist the household's selected Ocado connection.

Revision ID: 0019_ocado_shared_settings
Revises: 0018_ocado_shared_queue
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_ocado_shared_settings"
down_revision = "0018_ocado_shared_queue"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("ocado_shared_settings"):
        return
    op.create_table(
        "ocado_shared_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("retailer_accounts.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ocado_shared_settings_singleton"),
    )


def downgrade():
    op.drop_table("ocado_shared_settings")
