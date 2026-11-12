import sqlite3
from contextlib import closing

import pytest

from collector.services import store
from core import db as core_db

LEGACY = ("CREATE TABLE suppression ("
          " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")


def legacy_database(path, rows) -> None:
    """A database as it was before 0002: the old table, filled by the old code."""
    core_db.migrate(path)
    core_db.downgrade(path, "0001")
    with closing(sqlite3.connect(path)) as db:
        db.execute(LEGACY)
        db.executemany("INSERT INTO suppression VALUES (?, ?, ?)", rows)
        db.commit()


def refusals_in(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            "SELECT handle, reason, refused_at FROM refusals ORDER BY handle").fetchall()


def legacy_rows_in(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            "SELECT handle, added_at, reason FROM suppression ORDER BY handle").fetchall()


def schema_of(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT type, name, sql FROM sqlite_master"
                          " WHERE tbl_name IN ('refusals', 'suppression')"
                          " ORDER BY type, name").fetchall()


def test_legacy_rows_move_in_canonical_form_with_utc_time(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("8 701 000 00 01", "2026-09-02", "стоп-слово: отпишитесь"),
                           (" Info@Kofe.KZ", "2026-09-03T15:30:00+05:00", "просил по почте"),
                           ("kofe_almaty", "2026-09-04T08:00:00", None)])

    core_db.migrate(path)

    assert refusals_in(path) == [
        ("+77010000001", "стоп-слово: отпишитесь", "2026-09-02T00:00:00+00:00"),
        ("info@kofe.kz", "просил по почте", "2026-09-03T10:30:00+00:00"),
        ("kofe_almaty", "причина не записана", "2026-09-04T08:00:00+00:00"),
    ]


def test_handles_that_meet_after_normalising_keep_the_earlier_record(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("+77010000001", "2026-09-05", "поздняя"),
                           ("87010000001", "2026-09-02", "ранняя"),
                           ("7010000001", "2026-09-03", "средняя")])

    core_db.migrate(path)

    assert refusals_in(path) == [("+77010000001", "ранняя", "2026-09-02T00:00:00+00:00")]


def test_a_malformed_legacy_handle_is_kept_rather_than_dropped(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("8 701 000", "2026-09-02", "кривой номер"),
                           (" a ", "2026-09-02", "старая форма проверяла длину до обрезки")])

    core_db.migrate(path)

    assert refusals_in(path) == [
        ("8 701 000", "кривой номер", "2026-09-02T00:00:00+00:00"),
        ("a", "старая форма проверяла длину до обрезки", "2026-09-02T00:00:00+00:00")]


def test_a_legacy_whatsapp_link_becomes_its_phone(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("https://wa.me/77010000001?text=%D0%9F", "2026-09-03", "форма"),
                           ("+77010000001", "2026-09-02", "стоп-слово")])

    core_db.migrate(path)

    assert refusals_in(path) == [("+77010000001", "стоп-слово", "2026-09-02T00:00:00+00:00")]


@pytest.mark.parametrize("reason,kept", [
    ("ок", "причина не записана (ок)"), ("  ", "причина не записана"),
    (None, "причина не записана"), (" стоп ", "стоп"),
])
def test_a_legacy_reason_shorter_than_three_characters_is_marked(tmp_path, reason, kept):
    path = tmp_path / "state.db"
    legacy_database(path, [("+77010000001", "2026-09-02", reason)])

    core_db.migrate(path)

    assert refusals_in(path)[0][1] == kept


def test_after_the_move_the_old_name_is_a_view_over_refusals(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("+77010000001", "2026-09-02", "стоп-слово")])

    core_db.migrate(path)

    kinds = {name: kind for kind, name, _sql in schema_of(path)
             if name in ("refusals", "suppression")}
    assert kinds == {"refusals": "table", "suppression": "view"}
    assert legacy_rows_in(path) == [
        ("+77010000001", "2026-09-02T00:00:00+00:00", "стоп-слово")]


def test_downgrade_gives_every_refusal_back_to_the_old_table_and_upgrade_takes_it_again(tmp_path):
    path = tmp_path / "state.db"
    legacy_database(path, [("+77010000001", "2026-09-02", "стоп-слово"),
                           ("info@kofe.kz", "2026-09-03", "просил")])
    core_db.migrate(path)
    upgraded = refusals_in(path), schema_of(path)

    core_db.downgrade(path, "0001")
    assert legacy_rows_in(path) == [
        ("+77010000001", "2026-09-02T00:00:00+00:00", "стоп-слово"),
        ("info@kofe.kz", "2026-09-03T00:00:00+00:00", "просил")]
    assert [kind for kind, name, _ in schema_of(path) if name == "suppression"] == ["table"]

    core_db.migrate(path)
    assert (refusals_in(path), schema_of(path)) == upgraded


def test_an_empty_database_goes_down_and_up_again(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    upgraded = schema_of(path)

    core_db.downgrade(path, "0001")
    assert schema_of(path) == []
    core_db.migrate(path)

    assert schema_of(path) == upgraded


def test_a_database_without_the_old_table_ends_with_the_same_schema(tmp_path):
    fresh, legacy = tmp_path / "fresh.db", tmp_path / "legacy.db"
    legacy_database(legacy, [])

    core_db.migrate(fresh)
    core_db.migrate(legacy)

    assert schema_of(fresh) == schema_of(legacy)


@pytest.mark.parametrize("old_schema_first", [True, False])
def test_the_old_state_schema_before_or_after_the_migration_changes_nothing(
        tmp_path, old_schema_first):
    reference, path = tmp_path / "reference.db", tmp_path / "state.db"
    core_db.migrate(reference)

    def old_schema():
        with closing(sqlite3.connect(path)) as db:
            db.executescript(store.state_schema())

    if old_schema_first:
        old_schema()
    core_db.migrate(path)
    old_schema()

    assert schema_of(path) == schema_of(reference)
