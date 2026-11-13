"""delivery: the send queue and the pool of our numbers move under Alembic; a queue row has an author

Before this revision sender/db/migrate.py created outbox and numbers itself
(CREATE TABLE IF NOT EXISTS plus columns added on start), without a check on
their values. Their rows move here as they are; a row queued before it gets the
operator as its author, since only a row of the automaton is held in manual.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ROW_STATUSES = ("pending", "sending", "sent", "failed", "cancelled", "stuck")
KINDS = ("cold", "followup", "reply", "warmup")
AUTHORS = ("operator", "automat")
NUMBER_STATUSES = ("new", "warming", "active", "quarantined", "banned")
# A row that ended may be queued again: a transport failure must not bury a lead.
DEAD = "status NOT IN ('failed', 'stuck', 'cancelled')"

# Columns sender added on start, with what a row gets where it never ran.
OLD_OUTBOX_COLUMNS = {
    "message_id": "message_id",
    "thread_id": "thread_id",
    "our_number": "our_number",
    "recipient": "NULL",
    "send_after": "send_after",
    "status": "status",
    "kind": "'cold'",
    "attempts": "attempts",
    "provider_id": "provider_id",
    "delivered_at": "delivered_at",
    "read_at": "read_at",
    "error": "error",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
OLD_NUMBER_COLUMNS = {
    "session_dir": "session_dir",
    "status": "status",
    "started_at": "started_at",
    "note": "note",
    "skip_warmup": "0",
}
OLD_OUTBOX = """
CREATE TABLE outbox (
  outbox_id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages (message_id),
  thread_id TEXT, our_number TEXT NOT NULL, send_after TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, provider_id TEXT, delivered_at TEXT, read_at TEXT,
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, recipient TEXT,
  kind TEXT NOT NULL DEFAULT 'cold')
"""
OLD_NUMBERS = """
CREATE TABLE numbers (
  number TEXT PRIMARY KEY, session_dir TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, note TEXT, skip_warmup INTEGER NOT NULL DEFAULT 0)
"""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return "{} IN ({})".format(column, ", ".join(f"'{v}'" for v in values))


def _columns(table: str) -> set[str]:
    return {row[1] for row in op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})")}


def _has_table(name: str) -> bool:
    found = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return found.first() is not None


def _pick(table: str, columns: dict[str, str]) -> str:
    """Select list of the old table: its own column, or what a row gets without it."""
    present = _columns(table)
    return ", ".join(
        column if column in present else f"{default} AS {column}"
        for column, default in columns.items()
    )


def _create_outbox(name: str) -> None:
    op.create_table(
        name,
        sa.Column("outbox_id", sa.Integer, primary_key=True),
        # The message is in messages; a warmup row has none.
        sa.Column("message_id", sa.Integer, sa.ForeignKey("messages.message_id")),
        # The company of the thread; empty for warmup.
        sa.Column("thread_id", sa.Text),
        sa.Column("our_number", sa.Text, nullable=False),
        # Whom a warmup row went to: the passive phase asks when a number last got one.
        sa.Column("recipient", sa.Text),
        # Not earlier than this moment, UTC.
        sa.Column("send_after", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        # Who queued the row: manual holds the automaton's until the operator confirms.
        sa.Column("author", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        # The transport's id of the message: delivery receipts find the row by it.
        sa.Column("provider_id", sa.Text),
        sa.Column("delivered_at", sa.Text),
        sa.Column("read_at", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.CheckConstraint(_one_of("status", ROW_STATUSES), name="status_known"),
        sa.CheckConstraint(_one_of("kind", KINDS), name="kind_known"),
        sa.CheckConstraint(_one_of("author", AUTHORS), name="author_known"),
        sa.CheckConstraint("attempts >= 0", name="attempts_positive"),
    )


def _create_numbers(name: str) -> None:
    op.create_table(
        name,
        sa.Column("number", sa.Text, primary_key=True),
        sa.Column("session_dir", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        # The day of registration: the warmup calendar counts from it.
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("note", sa.Text),
        # Warmed outside our system: active at once, past the calendar.
        sa.Column("skip_warmup", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint(_one_of("status", NUMBER_STATUSES), name="status_known"),
    )


def _index_outbox() -> None:
    op.create_index("outbox_due", "outbox", ["status", "send_after"])
    op.create_index("outbox_by_number", "outbox", ["our_number", "status", "updated_at"])
    op.execute(
        f"CREATE UNIQUE INDEX outbox_one_live_per_message ON outbox (message_id) WHERE {DEAD}"
    )


def upgrade() -> None:
    _create_outbox("outbox_v6")
    _create_numbers("numbers_v6")
    if _has_table("outbox"):
        columns = ", ".join(OLD_OUTBOX_COLUMNS)
        op.execute(
            f"INSERT INTO outbox_v6 (outbox_id, {columns}, author)"
            f" SELECT outbox_id, {_pick('outbox', OLD_OUTBOX_COLUMNS)}, 'operator' FROM outbox"
        )
    if _has_table("numbers"):
        columns = ", ".join(OLD_NUMBER_COLUMNS)
        op.execute(
            f"INSERT INTO numbers_v6 (number, {columns})"
            f" SELECT number, {_pick('numbers', OLD_NUMBER_COLUMNS)} FROM numbers"
        )
    # The old indexes go with their table.
    op.execute("DROP TABLE IF EXISTS outbox")
    op.execute("DROP TABLE IF EXISTS numbers")
    op.rename_table("outbox_v6", "outbox")
    op.rename_table("numbers_v6", "numbers")
    _index_outbox()


def downgrade() -> None:
    # Back to the shape sender/db/migrate.py would have left, with the rows: the
    # queue and the pool are the irreplaceable layer.
    op.execute(OLD_OUTBOX.replace("CREATE TABLE outbox", "CREATE TABLE outbox_v5"))
    op.execute(OLD_NUMBERS.replace("CREATE TABLE numbers", "CREATE TABLE numbers_v5"))
    columns = ", ".join(OLD_OUTBOX_COLUMNS)
    op.execute(
        f"INSERT INTO outbox_v5 (outbox_id, {columns}) SELECT outbox_id, {columns} FROM outbox"
    )
    columns = ", ".join(OLD_NUMBER_COLUMNS)
    op.execute(f"INSERT INTO numbers_v5 (number, {columns}) SELECT number, {columns} FROM numbers")
    op.execute("DROP TABLE outbox")
    op.execute("DROP TABLE numbers")
    op.execute("ALTER TABLE outbox_v5 RENAME TO outbox")
    op.execute("ALTER TABLE numbers_v5 RENAME TO numbers")
    op.execute("CREATE INDEX outbox_due ON outbox (status, send_after)")
    op.execute("CREATE INDEX outbox_by_number ON outbox (our_number, status, updated_at)")
    op.execute(
        f"CREATE UNIQUE INDEX outbox_one_live_per_message ON outbox (message_id) WHERE {DEAD}"
    )
