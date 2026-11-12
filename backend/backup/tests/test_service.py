import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

import activity
from backup import service as backup_service
from backup.exceptions import BackupDisabledError, BackupFailedError, BackupRunningError
from backup.service import KEEP, BackupService
from core import db as core_db
from core import operator_settings
from refusals import public as refusals

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


class Choice(BaseModel):
    city: str = "almaty"


def snapshots(target):
    return sorted(path.name for path in target.iterdir() if path.name != "pages")


def test_a_snapshot_holds_the_database_and_the_sessions(service, icloud):
    snapshot = service.run(NOW)

    assert snapshot.name == "2026-11-12T090000Z"
    assert (icloud / snapshot.name / "state.db").exists()
    assert (icloud / snapshot.name / "sessions" / "+77001112233" / "creds.json").read_text() == "{}"
    # One self-contained database file: no -wal/-shm to restore by mistake.
    assert sorted(path.name for path in (icloud / snapshot.name).iterdir()) == ["sessions",
                                                                                "state.db"]
    assert snapshots(icloud) == [snapshot.name]


def test_a_restored_database_returns_refusals_and_settings(service, db, icloud, tmp_path):
    refusals.record(db, "+77010000001", "просил не писать", NOW)
    with db:
        operator_settings.save(db, "companies", Choice())

    snapshot = service.run(NOW)
    # What README tells the operator to do: copy the file back in place.
    restored = tmp_path / "restored" / "state.db"
    restored.parent.mkdir()
    shutil.copy(icloud / snapshot.name / "state.db", restored)

    with closing(core_db.connect(restored)) as copy:
        assert refusals.is_refused(copy, ["+77010000001"])
        assert copy.execute("SELECT count(*) FROM operator_settings").fetchone()[0] == 1


def test_a_snapshot_during_an_open_write_is_whole(service, state, icloud, tmp_path):
    activity.use(tmp_path / "journal.db")  # the open write would hold the journal back
    with closing(sqlite3.connect(state)) as writer:
        writer.execute("CREATE TABLE t (x)")
        writer.commit()
        writer.execute("INSERT INTO t VALUES (1)")  # left open

        snapshot = service.run(NOW)

    with closing(sqlite3.connect(icloud / snapshot.name / "state.db")) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_intact_tells_a_database_from_garbage(state, tmp_path):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)

    assert backup_service.intact(state)
    assert not backup_service.intact(garbage)


def test_a_broken_copy_fails_and_leaves_nothing(service, icloud, monkeypatch):
    monkeypatch.setattr(backup_service, "intact", lambda path: False)

    with pytest.raises(BackupFailedError):
        service.run(NOW)

    assert snapshots(icloud) == []
    assert activity.recent()[0]["outcome"] == "failed"


def test_rotation_keeps_one_per_day_and_waits_for_a_good_one(service, icloud, monkeypatch):
    for day in range(KEEP):
        service.run(NOW + timedelta(days=day))
    oldest = snapshots(icloud)[0]

    monkeypatch.setattr(backup_service, "intact", lambda path: False)
    with pytest.raises(BackupFailedError):
        service.run(NOW + timedelta(days=KEEP))
    assert len(snapshots(icloud)) == KEEP and snapshots(icloud)[0] == oldest

    monkeypatch.undo()
    newest = service.run(NOW + timedelta(days=KEEP))
    assert len(snapshots(icloud)) == KEEP
    assert oldest not in snapshots(icloud) and snapshots(icloud)[-1] == newest.name


def test_the_page_mirror_copies_only_new_pages(service, sources, icloud):
    _, pages, _ = sources
    assert service.run(NOW).pages_added == 1
    (icloud / "pages" / "a.html.gz").write_bytes(b"mirror copy")  # a recopy would overwrite it
    (pages / "b.html.gz").write_bytes(b"page b")

    assert service.run(NOW + timedelta(days=1)).pages_added == 1
    assert (icloud / "pages" / "a.html.gz").read_bytes() == b"mirror copy"
    assert (icloud / "pages" / "b.html.gz").read_bytes() == b"page b"


def test_without_the_folder_backup_is_off_and_says_so(sources, tmp_path):
    state, pages, sessions = sources
    service = BackupService(tmp_path / "no-icloud" / "Outreach-OS-backup", state, pages, sessions)

    assert not service.status().enabled
    with pytest.raises(BackupDisabledError):
        service.run(NOW)
    assert activity.recent()[0]["outcome"] == "disabled"


def test_a_second_run_while_one_is_going_is_refused(service):
    with service.running():
        with pytest.raises(BackupRunningError):
            service.run(NOW)


def test_it_is_due_when_there_is_none_or_the_last_is_a_day_old(service):
    assert service.due(NOW)
    service.run(NOW)
    assert not service.due(NOW + timedelta(hours=23))
    assert service.due(NOW + timedelta(hours=24))


def test_status_reports_the_last_good_snapshot(service, icloud):
    service.run(NOW)
    service.run(NOW + timedelta(days=1))

    status = service.status()
    assert status.enabled and not status.running
    assert status.last_at == NOW + timedelta(days=1)
    assert status.snapshots == 2
    assert status.target == str(icloud)


def test_a_good_run_is_in_the_journal(service):
    snapshot = service.run(NOW)

    event = activity.recent()[0]
    assert (event["actor"], event["outcome"], event["subject"]) == ("backup", "done", snapshot.name)


def test_overdue_after_two_days_or_when_off(service, sources, tmp_path):
    assert service.overdue(NOW) is not None
    service.run(NOW)
    assert service.overdue(NOW + timedelta(days=2) - timedelta(minutes=1)) is None
    assert service.overdue(NOW + timedelta(days=2)) is not None

    state, pages, sessions = sources
    off = BackupService(tmp_path / "none" / "x", state, pages, sessions)
    assert "нет каталога iCloud Drive" in off.overdue(NOW)


def test_tick_runs_only_when_due_and_survives_failure(service, icloud, monkeypatch):
    backup_service.tick(service, NOW)
    backup_service.tick(service, NOW + timedelta(hours=1))
    assert len(snapshots(icloud)) == 1

    monkeypatch.setattr(backup_service, "intact", lambda path: False)
    backup_service.tick(service, NOW + timedelta(days=1))  # does not raise
    assert activity.recent()[0]["outcome"] == "failed"


def test_a_burst_of_manual_runs_leaves_the_previous_days_untouched(service, icloud):
    for day in range(KEEP):
        service.run(NOW + timedelta(days=day))
    before = snapshots(icloud)

    today = NOW + timedelta(days=KEEP)
    for second in range(15):
        last = service.run(today + timedelta(seconds=second))

    assert snapshots(icloud) == before[1:] + [last.name]


def test_a_second_run_in_the_same_second_is_refused_and_keeps_the_first(service, icloud):
    first = service.run(NOW)

    with pytest.raises(BackupRunningError):
        service.run(NOW)

    assert snapshots(icloud) == [first.name]
    assert activity.recent()[0]["outcome"] == "done"


def test_without_sessions_the_snapshot_holds_the_database(sources, icloud, tmp_path):
    state, pages, _ = sources
    snapshot = BackupService(icloud, state, pages, tmp_path / "no-sessions").run(NOW)

    assert sorted(path.name for path in (icloud / snapshot.name).iterdir()) == ["state.db"]


def test_temp_files_and_links_are_not_mirrored(service, sources, icloud, tmp_path):
    _, pages, _ = sources
    (pages / "c.html.gz.1f2e.tmp").write_bytes(b"half written")
    outside = tmp_path / "secret.txt"
    outside.write_text("not a page")
    (pages / "link.html.gz").symlink_to(outside)

    assert service.run(NOW).pages_added == 1
    assert sorted(path.name for path in (icloud / "pages").iterdir()) == ["a.html.gz"]


def test_an_evicted_icloud_page_is_not_copied_again(service, icloud):
    service.run(NOW)
    (icloud / "pages" / "a.html.gz").rename(icloud / "pages" / ".a.html.gz.icloud")

    assert service.run(NOW + timedelta(days=1)).pages_added == 0


def test_a_temp_folder_left_by_a_killed_run_is_cleaned(service, icloud):
    icloud.mkdir()
    (icloud / ".2026-11-11T090000Z.tmp").mkdir()

    service.run(NOW)

    assert not list(icloud.glob(".*.tmp"))


def test_an_unreachable_folder_reads_as_disabled(service, icloud):
    service.run(NOW)
    icloud.chmod(0)
    try:
        assert not service.status().enabled
        assert "выключен" in service.overdue(NOW)
    finally:
        icloud.chmod(0o700)


def test_a_busy_journal_does_not_fail_a_written_snapshot(service, icloud):
    activity.use(None)  # every journal write now raises

    snapshot = service.run(NOW)

    assert snapshots(icloud) == [snapshot.name]


def test_a_crashing_tick_is_journaled(service, monkeypatch):
    def broken(now):
        raise RuntimeError("boom")
    monkeypatch.setattr(service, "due", broken)

    backup_service.tick(service, NOW)

    assert activity.recent()[0]["outcome"] == "crashed"


def test_stale_keeps_the_newest_of_each_of_the_last_days():
    names = ["2026-11-01T090000Z", "2026-11-01T120000Z", "2026-11-02T090000Z"]

    assert backup_service.stale(names) == ["2026-11-01T090000Z"]
