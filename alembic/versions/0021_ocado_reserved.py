"""Saved Reserved quantities and once-per-delivery processing records."""
import sqlalchemy as sa
from alembic import op

revision = "0021_ocado_reserved"
down_revision = "0020_hellofresh_week_decisions"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("ocado_reserved_rules"):
        op.create_table("ocado_reserved_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("retailer_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("enabled", sa.Integer(), nullable=False),
            sa.Column("rules_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("account_id", name="uq_ocado_reserved_rules_account"))
    if not inspector.has_table("ocado_reserved_runs"):
        op.create_table("ocado_reserved_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("retailer_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("order_id", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("rules_json", sa.Text(), nullable=False),
            sa.Column("warning", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("account_id", "order_id", name="uq_ocado_reserved_run_order"),
            sa.CheckConstraint("status in ('running', 'completed', 'warning')", name="ck_ocado_reserved_run_status"))


def downgrade():
    op.drop_table("ocado_reserved_runs")
    op.drop_table("ocado_reserved_rules")
