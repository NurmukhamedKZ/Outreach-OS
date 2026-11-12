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
