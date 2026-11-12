import sqlite3
from contextlib import closing

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from core import db as core_db

TABLES = ("batches", "batch_companies", "dossiers", "signals")
NOW = "2026-11-12T10:00:00+00:00"


def schema_of(path) -> list[tuple]:
    marks = ",".join("?" * len(TABLES))
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT type, name, sql FROM sqlite_master WHERE tbl_name IN ({marks})"
                          " ORDER BY type, name", TABLES).fetchall()


def test_upgrade_downgrade_upgrade_gives_the_same_schema(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    upgraded = schema_of(path)
    assert {name for kind, name, _ in upgraded if kind == "table"} == set(TABLES)

    core_db.downgrade(path, "0003")
    assert schema_of(path) == []
    core_db.migrate(path)

    assert schema_of(path) == upgraded


def test_the_history_has_one_head():
    assert len(ScriptDirectory.from_config(Config(core_db.ALEMBIC_INI)).get_heads()) == 1


def test_only_one_batch_runs_at_a_time(db):
    db.execute("INSERT INTO batches (status, size, started_at) VALUES ('done', 50, ?)", (NOW,))
    db.execute("INSERT INTO batches (status, size, started_at) VALUES ('collecting', 50, ?)",
               (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO batches (status, size, started_at) VALUES ('researching', 1, ?)",
                   (NOW,))


@pytest.mark.parametrize("sql", [
    "INSERT INTO batches (status, size, started_at) VALUES ('bogus', 50, '{now}')",
    "INSERT INTO batches (status, size, started_at) VALUES ('done', 0, '{now}')",
    "INSERT INTO batch_companies (batch_id, company_id, position, status)"
    " VALUES (1, 'nobody', 0, 'pending')",
    "INSERT INTO dossiers (company_id, batch_id, model, prompt, answer, dossier, created_at)"
    " VALUES ('nobody', 1, 'm', '[]', '{{}}', '{{}}', '{now}')",
])
def test_the_schema_refuses_unknown_statuses_and_rows_without_their_owner(db, sql):
    db.execute("INSERT INTO batches (status, size, started_at) VALUES ('done', 50, ?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql.format(now=NOW))


def test_a_company_has_one_dossier_per_batch(db):
    db.execute("INSERT INTO companies VALUES ('c', 'А', 'almaty', ?)", (NOW,))
    db.execute("INSERT INTO batches (status, size, started_at) VALUES ('done', 1, ?)", (NOW,))
    insert = ("INSERT INTO dossiers (company_id, batch_id, model, prompt, answer, dossier,"
              " created_at) VALUES ('c', 1, 'm', '[]', '{}', '{}', ?)")
    db.execute(insert, (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, (NOW,))


def test_a_signal_of_an_unknown_type_or_twice_the_same_type_is_refused(db):
    db.execute("INSERT INTO companies VALUES ('c', 'А', 'almaty', ?)", (NOW,))
    db.execute("INSERT INTO batches (status, size, started_at) VALUES ('done', 1, ?)", (NOW,))
    db.execute("INSERT INTO dossiers (company_id, batch_id, model, prompt, answer, dossier,"
               " created_at) VALUES ('c', 1, 'm', '[]', '{}', '{}', ?)", (NOW,))
    insert = "INSERT INTO signals VALUES (1, ?, ?, 'q', 'u')"
    db.execute(insert, ("crm_widget", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("crm_widget", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("ig_dormant", NOW))
