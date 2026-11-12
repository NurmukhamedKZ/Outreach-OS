import sqlite3
from contextlib import closing

from alembic.config import Config
from alembic.script import ScriptDirectory

from core import db as core_db

TABLES = ("segments", "companies", "branches", "branch_segments", "branch_contacts")


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

    core_db.downgrade(path, "0002")
    assert schema_of(path) == []
    core_db.migrate(path)

    assert schema_of(path) == upgraded


def test_the_history_has_one_head():
    assert len(ScriptDirectory.from_config(Config(core_db.ALEMBIC_INI)).get_heads()) == 1


def test_the_schema_refuses_an_unknown_status_and_a_branch_without_a_company(tmp_path):
    import pytest
    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO segments (city, rubric, status, updated_at)"
                       " VALUES ('almaty', '653', 'bogus', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO branches (branch_id, company_id, name, city, source_url,"
                       " rule, confidence) VALUES ('1', 'nobody', 'А', 'almaty', 'u', 'self', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO companies VALUES ('c', 'А', 'almaty', 'now')")
            db.execute("INSERT INTO branches (branch_id, company_id, name, city, source_url,"
                       " rule, confidence) VALUES ('1', 'c', 'А', 'almaty', 'u', 'self', 1)")
            db.execute("INSERT INTO branch_contacts VALUES ('1', 'fax', '+77010000001')")
