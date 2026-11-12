"""refusals: one table that is never cleared (F21); suppression becomes a view over it

The old table moves here with handles in canonical form. Its name stays as a
read-only view, because sender, writer and the old collector read it until #7
and #13.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from core import contacts

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# The old table allowed a refusal without a reason; the new one requires it.
NO_REASON = "причина не записана"
MIN_REASON_LENGTH = 3

# A frozen copy of the legacy definition: the old collector created it on every connect.
LEGACY_TABLE = ("CREATE TABLE suppression ("
                " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
LEGACY_VIEW = ("CREATE VIEW suppression AS"
               " SELECT handle, refused_at AS added_at, reason FROM refusals")
# A batch rebuild of refusals in a later revision drops these: recreate them there.
# The INSERT guard covers INSERT OR REPLACE: its implicit delete fires no DELETE
# trigger while recursive_triggers is off, so it would rewrite the first record.
GUARDS = [
    *(f"CREATE TRIGGER refusals_never_{verb.lower()} BEFORE {verb} ON refusals"
      f" BEGIN SELECT RAISE(ABORT, 'refusals are never {done}'); END"
      for verb, done in (("DELETE", "deleted"), ("UPDATE", "edited"))),
    "CREATE TRIGGER refusals_keep_the_first BEFORE INSERT ON refusals"
    " WHEN EXISTS (SELECT 1 FROM refusals WHERE handle = NEW.handle)"
    " BEGIN SELECT RAISE(IGNORE); END",
]

LegacyRow = tuple[str, str, str | None]


def upgrade() -> None:
    refusals = op.create_table(
        "refusals",
        sa.Column("handle", sa.Text, primary_key=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("refused_at", sa.Text, nullable=False),
        # Non-empty only: the service asks for three characters, but a shorter
        # legacy handle is kept as typed rather than dropped.
        sa.CheckConstraint("length(handle) > 0", name="handle_not_empty"),
        sa.CheckConstraint(f"length(reason) >= {MIN_REASON_LENGTH}", name="reason_not_short"),
    )
    bind = op.get_bind()
    legacy = bind.execute(sa.text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'suppression'")).first()
    if legacy:
        rows = bind.execute(sa.text("SELECT handle, added_at, reason FROM suppression")).all()
        op.bulk_insert(refusals, _earliest_per_handle(rows))
        op.drop_table("suppression")
    op.execute(LEGACY_VIEW)
    for guard in GUARDS:
        op.execute(guard)


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT handle, refused_at, reason FROM refusals")).all()
    op.execute("DROP VIEW suppression")
    # With nothing to give back the table is left to the pre-0002 schema.sql,
    # which creates it on connect; downgrading a fresh database to base leaves it empty.
    if rows:
        op.execute(LEGACY_TABLE)
        bind.execute(sa.text("INSERT INTO suppression VALUES (:handle, :added_at, :reason)"),
                     [{"handle": h, "added_at": at, "reason": r} for h, at, r in rows])
    op.drop_table("refusals")


def _earliest_per_handle(rows: Sequence[LegacyRow]) -> list[dict[str, str]]:
    """Normalise legacy rows; handles that meet in canonical form keep the earlier one."""
    earliest: dict[str, dict[str, str]] = {}
    for handle, added_at, reason in rows:
        row = {"handle": _canonical(handle), "reason": _reason(reason),
               "refused_at": _utc(added_at)}
        kept = earliest.get(row["handle"])
        if kept is None or row["refused_at"] < kept["refused_at"]:
            earliest[row["handle"]] = row
    return list(earliest.values())


def _canonical(handle: str) -> str:
    # The live rule, not a frozen copy: a migrated handle must match the form the
    # service looks refusals up in. What is no contact is kept as typed: dropping
    # it would drop a refusal.
    try:
        return contacts.canonical(handle)
    except ValueError:
        return handle.strip()


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if len(text) >= MIN_REASON_LENGTH:
        return text
    return f"{NO_REASON} ({text})" if text else NO_REASON


def _utc(added_at: str) -> str:
    # The old code wrote the operator's date; a date or a naive time is read as UTC.
    moment = datetime.fromisoformat(added_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")
