"""companies: segments with their collection state, companies, branches and contacts

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STATUSES = ("new", "collecting", "collected", "failed")
CONTACT_KINDS = ("phone", "website", "email", "instagram", "whatsapp")


def upgrade() -> None:
    op.create_table(
        "segments",
        sa.Column("city", sa.Text, primary_key=True),
        sa.Column("rubric", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("branches", sa.Integer, nullable=False, server_default="0"),
        # 2GIS showed fewer organisations than it promised: the pagination ceiling.
        sa.Column("capped", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.CheckConstraint("status IN ({})".format(", ".join(f"'{s}'" for s in STATUSES)),
                           name="status_known"),
        sa.CheckConstraint("branches >= 0", name="branches_not_negative"),
    )
    op.create_table(
        "companies",
        # Stable: dossiers and threads point at it, so a later collection never changes it.
        sa.Column("company_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_table(
        "branches",
        sa.Column("branch_id", sa.Text, primary_key=True),
        sa.Column("company_id", sa.Text, sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("org_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("org_name", sa.Text),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("address", sa.Text),
        sa.Column("review_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("source_url", sa.Text, nullable=False),
        # The rule that put the branch into its company: a fuzzy name must stay visible.
        sa.Column("rule", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.CheckConstraint("review_count >= 0", name="review_count_not_negative"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_in_range"),
    )
    op.create_index("branches_by_company", "branches", ["company_id"])
    op.create_table(
        "branch_segments",
        sa.Column("branch_id", sa.Text, sa.ForeignKey("branches.branch_id"), primary_key=True),
        sa.Column("city", sa.Text, primary_key=True),
        sa.Column("rubric", sa.Text, primary_key=True),
        sa.ForeignKeyConstraint(["city", "rubric"], ["segments.city", "segments.rubric"]),
    )
    op.create_table(
        "branch_contacts",
        sa.Column("branch_id", sa.Text, sa.ForeignKey("branches.branch_id"), primary_key=True),
        sa.Column("kind", sa.Text, primary_key=True),
        # Canonical form (core.contacts), the one refusals are kept in.
        sa.Column("handle", sa.Text, primary_key=True),
        sa.CheckConstraint("kind IN ({})".format(", ".join(f"'{k}'" for k in CONTACT_KINDS)),
                           name="kind_known"),
    )


def downgrade() -> None:
    op.drop_table("branch_contacts")
    op.drop_table("branch_segments")
    op.drop_index("branches_by_company", "branches")
    op.drop_table("branches")
    op.drop_table("companies")
    op.drop_table("segments")
