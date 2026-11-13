"""sender keeps its old path (the suppression view) until #7; writer reads research."""

from datetime import UTC, datetime

from refusals import public as refusals
from sender.services import worker

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def test_sender_sees_a_refusal_written_through_the_facade(db):
    refusals.record(db, "8 701 000 00 01", "стоп-слово: отпишитесь", NOW)

    assert worker._suppressed(db, "+77010000001") is True
    assert worker._suppressed(db, "+77019999999") is False


def test_the_sender_seam_writes_through_the_facade(db, monkeypatch):
    from collector import api
    from sender.services import refusal

    # Another test may have unregistered it; the assembler's hook is what counts.
    monkeypatch.setattr(refusal, "_hook", api._write_refusal)

    assert refusal.refuse("+77010000001", "стоп-слово: отпишитесь") is True
    assert refusal.refuse("+77010000001", "агент: просил не писать") is True
    assert refusals.is_refused(db, ["8 701 000 00 01"]) is True
    rows = db.execute("SELECT reason FROM refusals").fetchall()
    assert [row[0] for row in rows] == ["стоп-слово: отпишитесь"]
