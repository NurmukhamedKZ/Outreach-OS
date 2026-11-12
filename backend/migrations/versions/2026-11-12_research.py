"""research: batches, the companies of each batch, dossier versions and their signals

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

BATCH_STATUSES = ("collecting", "researching", "done", "stopped", "interrupted")
RUNNING = ("collecting", "researching")
RESEARCH_STATUSES = ("pending", "researching", "done", "failed")
SIGNAL_TYPES = ("reviews_missed_lead", "reviews_unanswered_complaint", "site_hiring_sales",
                "site_no_pricing", "crm_widget", "ads_platform", "inbound_widget")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return "{} IN ({})".format(column, ", ".join(f"'{v}'" for v in values))


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("batch_id", sa.Integer, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        # Checked between companies: research already started is finished.
        sa.Column("stop_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
        sa.CheckConstraint(_one_of("status", BATCH_STATUSES), name="status_known"),
        sa.CheckConstraint("size >= 1", name="size_positive"),
    )
    # One batch at a time, held by the schema: two start requests race otherwise.
    op.execute("CREATE UNIQUE INDEX one_running_batch ON batches"
               f" (({_one_of('status', RUNNING)})) WHERE {_one_of('status', RUNNING)}")
    op.create_table(
        "batch_companies",
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.batch_id"), primary_key=True),
        sa.Column("company_id", sa.Text, sa.ForeignKey("companies.company_id"),
                  primary_key=True),
        # The order of research: the priority the company was taken by.
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        # Which attempt of the company this is, counted over every batch.
        sa.Column("attempt", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("rejected_quotes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.Text),
        sa.Column("finished_at", sa.Text),
        sa.CheckConstraint(_one_of("status", RESEARCH_STATUSES), name="status_known"),
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
        sa.CheckConstraint("rejected_quotes >= 0", name="rejected_not_negative"),
    )
    op.create_index("batch_companies_by_company", "batch_companies", ["company_id"])
    op.create_table(
        "dossiers",
        # A new version per research; the current one is the latest, none is deleted.
        sa.Column("dossier_id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Text, sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.batch_id"), nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        # The full request and the reply as they were: the record of a paid call.
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        # What of the reply stands: verified hooks with their place, sorted pains.
        sa.Column("dossier", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.CheckConstraint("json_valid(prompt) AND json_valid(answer) AND json_valid(dossier)",
                           name="json_valid"),
    )
    op.create_index("dossiers_by_company", "dossiers", ["company_id", "dossier_id"])
    op.create_table(
        "signals",
        sa.Column("dossier_id", sa.Integer, sa.ForeignKey("dossiers.dossier_id"),
                  primary_key=True),
        # One signal per type per research: Bitrix and amoCRM are one fact, one weight.
        sa.Column("type", sa.Text, primary_key=True),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.CheckConstraint(_one_of("type", SIGNAL_TYPES), name="type_known"),
    )


def downgrade() -> None:
    op.drop_table("signals")
    op.drop_index("dossiers_by_company", "dossiers")
    op.drop_table("dossiers")
    op.drop_index("batch_companies_by_company", "batch_companies")
    op.drop_table("batch_companies")
    op.execute("DROP INDEX one_running_batch")
    op.drop_table("batches")
