"""overview: the activity journal moves under Alembic; the job queue and llm_answers go

Before this revision activity.py created the journal itself (CREATE TABLE IF NOT
EXISTS on start); its rows move here as they are. jobs held the queue of the
old dashboard, llm_answers the answers of the pipeline removed in #13: nothing
reads either (jobs held one log of a «Черновики топ-N» run before #36, whose
drafts live in messages); the backup's snapshots, when it is on, keep them for 14 days.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

COLUMNS = "id, at, last_at, repeats, actor, outcome, subject, detail"
OLD_ACTIVITY = """
CREATE TABLE activity (
  id      INTEGER PRIMARY KEY,
  at      TEXT NOT NULL,
  last_at TEXT NOT NULL,
  repeats INTEGER NOT NULL DEFAULT 1,
  actor   TEXT NOT NULL,
  outcome TEXT NOT NULL,
  subject TEXT,
  detail  TEXT
)
"""
OLD_JOBS = """
CREATE TABLE jobs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  title      TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  step       INTEGER NOT NULL DEFAULT 0,
  steps      TEXT NOT NULL,
  log        TEXT NOT NULL DEFAULT '',
  progress   TEXT,
  result     TEXT,
  exit_code  INTEGER,
  error      TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
)
"""
OLD_LLM_ANSWERS = """
CREATE TABLE llm_answers (
  id      INTEGER PRIMARY KEY,
  kind    TEXT NOT NULL,
  subject TEXT NOT NULL,
  model   TEXT NOT NULL,
  prompt  TEXT NOT NULL,
  answer  TEXT NOT NULL
)
"""


def _has_table(name: str) -> bool:
    found = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return found.first() is not None


def upgrade() -> None:
    op.create_table(
        "activity_v7",
        sa.Column("id", sa.Integer, primary_key=True),
        # When it happened first and last: a repeat of the last row raises repeats.
        sa.Column("at", sa.Text, nullable=False),
        sa.Column("last_at", sa.Text, nullable=False),
        sa.Column("repeats", sa.Integer, nullable=False, server_default="1"),
        # sender.tick | sender.warmup | sender.monitor | webhook | backup
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("subject", sa.Text),
        sa.Column("detail", sa.Text),
    )
    if _has_table("activity"):
        op.execute(f"INSERT INTO activity_v7 ({COLUMNS}) SELECT {COLUMNS} FROM activity")
    # The old index goes with its table.
    op.execute("DROP TABLE IF EXISTS activity")
    op.rename_table("activity_v7", "activity")
    op.execute("CREATE INDEX activity_recent ON activity (last_at DESC)")
    # The worker of the job queue goes with it: «Процессы» would show it silent for two weeks.
    op.execute("DELETE FROM activity WHERE actor = 'jobs'")
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TABLE IF EXISTS llm_answers")


def downgrade() -> None:
    # The journal goes back to the shape activity.py would have left, with its rows;
    # the queue and the answers come back empty, as their old owners expect them.
    op.execute(OLD_ACTIVITY.replace("CREATE TABLE activity", "CREATE TABLE activity_v6"))
    op.execute(f"INSERT INTO activity_v6 ({COLUMNS}) SELECT {COLUMNS} FROM activity")
    op.execute("DROP TABLE activity")
    op.execute("ALTER TABLE activity_v6 RENAME TO activity")
    op.execute("CREATE INDEX activity_recent ON activity (last_at DESC)")
    op.execute(OLD_JOBS)
    op.execute(OLD_LLM_ANSWERS)
