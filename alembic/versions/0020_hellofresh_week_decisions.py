"""Remember which HelloFresh weeks the auto-skip has already settled.

Revision ID: 0020_hellofresh_week_decisions
Revises: 0019_ocado_shared_settings
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_hellofresh_week_decisions"
down_revision = "0019_ocado_shared_settings"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("hellofresh_week_decisions"):
        return
    op.create_table(
        "hellofresh_week_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("retailer_accounts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("week", sa.String(16), nullable=False),
        sa.Column("subscription_id", sa.String(32), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("account_id", "week", name="uq_hellofresh_week_decision"),
    )


def downgrade():
    op.drop_table("hellofresh_week_decisions")
