"""conversations: threads keyed by company, their messages and outreach runs; no suppression view

Before this revision writer and sender created threads and messages themselves
(CREATE TABLE IF NOT EXISTS plus columns added on start), keyed by the lead's
phone. Their rows move here under the company; the phone stays as the channel.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

THREAD_STATUSES = (
    "queued",
    "active",
    "exhausted",
    "escalated",
    "unreachable",
    "closed_refused",
    "closed_junk",
    "blocked_channel",
)
STAGES = ("contact", "probing", "offer", "closing")
OUTCOMES = ("meeting_agreed", "meeting_held", "refused", "lost")
ROLES = ("outgoing", "incoming")
RUN_STATUSES = ("running", "done", "stopped", "failed")

# Columns of the old tables with what a row gets when an owner never added one.
OLD_THREAD_COLUMNS = {
    "stage": "'contact'",
    "outcome": "NULL",
    "meeting_at": "NULL",
    # Added by sender on its first start: a thread a human already wrote in
    # stays with the human, a draft waiting for its first send is queued.
    "status": (
        "CASE WHEN EXISTS (SELECT 1 FROM messages s WHERE s.thread_id = t.thread_id"
        " AND s.sent_text IS NOT NULL) THEN 'escalated' ELSE 'queued' END"
    ),
    "status_reason": "NULL",
    "our_number": "NULL",
    "auto_replies": "0",
    "next_touch_at": "NULL",
    "touch_no": "0",
}
OLD_MESSAGE_COLUMNS = {
    "draft_text": "NULL",
    "sent_text": "NULL",
    "queued_text": "NULL",
    "angle": "NULL",
    "prompt": "NULL",
    "model": "NULL",
    "offer_variant": "NULL",
    "provider_id": "NULL",
    "handled_at": "NULL",
    "handle_attempts": "0",
    "created_at": "created_at",
    "sent_at": "NULL",
}


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return "{} IN ({})".format(column, ", ".join(f"'{v}'" for v in values))


def _columns(table: str) -> set[str]:
    return {row[1] for row in op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})")}


def _has_table(name: str) -> bool:
    found = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return found.first() is not None


def _pick(table: str, defaults: dict[str, str]) -> str:
    """Select list of the old table: its own column, or the default it never got."""
    present = _columns(table)
    return ", ".join(
        f"{column}" if column in present else f"{default} AS {column}"
        for column, default in defaults.items()
    )


def _create_threads(name: str) -> None:
    op.create_table(
        name,
        # One thread per company: the conversation goes on where it began.
        sa.Column("company_id", sa.Text, primary_key=True),
        # The WhatsApp number the thread was opened on; it never changes.
        sa.Column("channel", sa.Text, nullable=False, unique=True),
        # The lead's context frozen when we decided to write: every move reads it.
        sa.Column("seed", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=False, server_default="contact"),
        sa.Column("outcome", sa.Text),
        sa.Column("meeting_at", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        # Why the status is what it is: escalated without a reason helps nobody.
        sa.Column("status_reason", sa.Text),
        # Our number the lead sees the chat from; set once.
        sa.Column("our_number", sa.Text),
        sa.Column("auto_replies", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_touch_at", sa.Text),
        sa.Column("touch_no", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint(_one_of("status", THREAD_STATUSES), name="status_known"),
        sa.CheckConstraint(_one_of("stage", STAGES), name="stage_known"),
        sa.CheckConstraint(
            f"outcome IS NULL OR {_one_of('outcome', OUTCOMES)}", name="outcome_known"
        ),
        sa.CheckConstraint("json_valid(seed)", name="seed_json"),
    )


def _create_messages(name: str, threads: str) -> None:
    op.create_table(
        name,
        sa.Column("message_id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Text, sa.ForeignKey(f"{threads}.company_id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        # What the model proposed; empty for incoming.
        sa.Column("draft_text", sa.Text),
        # What actually went out or came in; empty is a draft. History is only this.
        sa.Column("sent_text", sa.Text),
        # What the operator confirmed and the transport has not yet.
        sa.Column("queued_text", sa.Text),
        sa.Column("angle", sa.Text),
        # The full request that went to the model, as it was.
        sa.Column("prompt", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("offer_variant", sa.Text),
        # The transport's id: a repeated webhook event is recognised by it.
        sa.Column("provider_id", sa.Text),
        sa.Column("handled_at", sa.Text),
        sa.Column("handle_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("sent_at", sa.Text),
        sa.CheckConstraint(_one_of("role", ROLES), name="role_known"),
        sa.CheckConstraint("prompt IS NULL OR json_valid(prompt)", name="prompt_json"),
    )


def _index_messages() -> None:
    op.create_index("messages_thread", "messages", ["company_id", "message_id"])
    op.execute(
        "CREATE UNIQUE INDEX messages_provider ON messages (provider_id)"
        " WHERE provider_id IS NOT NULL"
    )


def upgrade() -> None:
    _create_threads("threads_v5")
    _create_messages("messages_v5", "threads")
    if _has_table("threads"):
        # The first thread of a company stays; a second one (#17: a new channel
        # opened another) gives its messages to it.
        defaults = dict(OLD_THREAD_COLUMNS)
        if not _has_table("messages"):
            defaults["status"] = "'queued'"
        op.execute(
            "INSERT INTO threads_v5 (company_id, channel, seed, created_at,"
            f" {', '.join(defaults)})"
            f" SELECT company_id, thread_id, seed, created_at, {_pick('threads', defaults)}"
            " FROM threads t WHERE t.thread_id = (SELECT f.thread_id FROM threads f"
            "   WHERE f.company_id = t.company_id ORDER BY f.created_at, f.thread_id LIMIT 1)"
        )
        if _has_table("messages"):
            # A message whose thread is gone fails NOT NULL: loud, not lost.
            op.execute(
                f"INSERT INTO messages_v5 (message_id, company_id, role, {', '.join(OLD_MESSAGE_COLUMNS)})"
                " SELECT message_id,"
                " (SELECT t.company_id FROM threads t WHERE t.thread_id = m.thread_id), role,"
                f" {_pick('messages', OLD_MESSAGE_COLUMNS)} FROM messages m"
            )
        if _has_table("outbox"):
            if {"status", "error"} <= _columns("outbox"):
                # A row waiting to go on a number whose thread did not stay (the
                # company's second thread, or none at all) would go to the kept
                # thread's number, or break the tick: it is cancelled instead.
                op.execute(
                    "UPDATE outbox SET status = 'cancelled',"
                    " error = 'миграция 0005: тред на этом номере слит с первым тредом компании'"
                    " WHERE status = 'pending' AND thread_id IS NOT NULL"
                    " AND thread_id NOT IN (SELECT channel FROM threads_v5)"
                )
            op.execute(
                "UPDATE outbox SET thread_id = (SELECT company_id FROM threads"
                "  WHERE threads.thread_id = outbox.thread_id)"
                " WHERE thread_id IN (SELECT thread_id FROM threads)"
            )
    # Drop the old before renaming the new: renaming the old instead would make
    # SQLite repoint outbox's foreign key at the table about to be dropped.
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS threads")
    op.rename_table("threads_v5", "threads")
    op.rename_table("messages_v5", "messages")
    _index_messages()

    op.create_table(
        "outreach_runs",
        sa.Column("run_id", sa.Integer, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        # How many new leads the run was asked to write to.
        sa.Column("size", sa.Integer, nullable=False),
        # Leads found with something fresh to write about: at most size.
        sa.Column("taken", sa.Integer, nullable=False, server_default="0"),
        sa.Column("drafted", sa.Integer, nullable=False, server_default="0"),
        # Leads the model advised not to write to: nothing new to say.
        sa.Column("skipped", sa.Integer, nullable=False, server_default="0"),
        # Checked before each next lead: the lead in progress is finished.
        sa.Column("stop_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        # Why the run ended on its own, in words for the operator.
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
        sa.CheckConstraint(_one_of("status", RUN_STATUSES), name="status_known"),
        sa.CheckConstraint("size >= 1", name="size_positive"),
        sa.CheckConstraint("taken >= 0 AND drafted >= 0 AND skipped >= 0", name="counts_positive"),
    )
    # One run at a time, held by the schema: two start requests race otherwise.
    op.execute(
        "CREATE UNIQUE INDEX one_running_outreach ON outreach_runs ((status = 'running'))"
        " WHERE status = 'running'"
    )
    # Its last reader, the send queue's gate, asks refusals.public now (#10, #36).
    op.execute("DROP VIEW IF EXISTS suppression")


def downgrade() -> None:
    op.execute(
        "CREATE VIEW suppression AS SELECT handle, refused_at AS added_at, reason FROM refusals"
    )
    op.execute("DROP INDEX one_running_outreach")
    op.drop_table("outreach_runs")
    # Back to the shape the old owners would have left: the phone is the key.
    op.execute(
        "CREATE TABLE threads_v4 (thread_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,"
        " seed TEXT NOT NULL, created_at TEXT NOT NULL,"
        " stage TEXT NOT NULL DEFAULT 'contact', outcome TEXT, meeting_at TEXT,"
        " status TEXT NOT NULL DEFAULT 'queued', status_reason TEXT, our_number TEXT,"
        " auto_replies INTEGER NOT NULL DEFAULT 0, next_touch_at TEXT,"
        " touch_no INTEGER NOT NULL DEFAULT 0)"
    )
    op.execute(
        "CREATE TABLE messages_v4 (message_id INTEGER PRIMARY KEY,"
        " thread_id TEXT NOT NULL REFERENCES threads (thread_id),"
        " role TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),"
        " draft_text TEXT, sent_text TEXT, angle TEXT, prompt TEXT, model TEXT,"
        " offer_variant TEXT, created_at TEXT NOT NULL, sent_at TEXT, provider_id TEXT,"
        " handled_at TEXT, queued_text TEXT, handle_attempts INTEGER NOT NULL DEFAULT 0)"
    )
    columns = ", ".join(OLD_THREAD_COLUMNS)
    op.execute(
        f"INSERT INTO threads_v4 (thread_id, company_id, seed, created_at, {columns})"
        f" SELECT channel, company_id, seed, created_at, {columns} FROM threads"
    )
    columns = ", ".join(OLD_MESSAGE_COLUMNS)
    op.execute(
        f"INSERT INTO messages_v4 (message_id, thread_id, role, {columns})"
        " SELECT message_id, (SELECT t.channel FROM threads t WHERE t.company_id = m.company_id),"
        f" role, {columns} FROM messages m"
    )
    if _has_table("outbox"):
        op.execute(
            "UPDATE outbox SET thread_id = (SELECT channel FROM threads"
            "  WHERE threads.company_id = outbox.thread_id)"
            " WHERE thread_id IN (SELECT company_id FROM threads)"
        )
    op.execute("DROP TABLE messages")
    op.execute("DROP TABLE threads")
    op.execute("ALTER TABLE threads_v4 RENAME TO threads")
    op.execute("ALTER TABLE messages_v4 RENAME TO messages")
    op.execute("CREATE INDEX messages_thread ON messages (thread_id, message_id)")
    op.execute(
        "CREATE UNIQUE INDEX messages_provider ON messages (provider_id)"
        " WHERE provider_id IS NOT NULL"
    )
