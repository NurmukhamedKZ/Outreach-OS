"""operator settings: one JSON section per module (ADR 0003)

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_settings",
        sa.Column("module", sa.Text, primary_key=True),
        sa.Column("data", sa.Text, nullable=False),
        sa.CheckConstraint("json_valid(data) AND json_type(data) = 'object'",
                           name="data_is_json_object"),
    )


def downgrade() -> None:
    op.drop_table("operator_settings")
