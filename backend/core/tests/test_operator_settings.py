from contextlib import closing

import pytest
from pydantic import BaseModel, Field, model_validator

from core import db, operator_settings


class Sending(BaseModel):
    batch_size: int = Field(default=50, ge=1)
    window: tuple[int, int] = (10, 18)
    stopwords: list[str] = ["стоп"]


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "state.db"
    db.migrate(path)
    return path


@pytest.fixture
def conn(path):
    with closing(db.connect(path)) as conn:
        yield conn


def save(conn, module, values):
    with conn:
        operator_settings.save(conn, module, values)


def test_nothing_saved_returns_defaults(conn):
    assert operator_settings.load(conn, "sending", Sending) == Sending()


def test_saved_section_replaces_defaults_and_stays_per_module(conn):
    save(conn, "sending", Sending(batch_size=30, stopwords=[]))

    assert operator_settings.load(conn, "sending", Sending) == Sending(
        batch_size=30, stopwords=[])
    assert operator_settings.load(conn, "companies", Sending) == Sending()


def test_saved_keys_overlay_defaults_and_unknown_keys_are_ignored(conn):
    with conn:
        conn.execute("INSERT INTO operator_settings (module, data) VALUES (?, ?)",
                     ("sending", '{"batch_size": 7, "removed_long_ago": true}'))

    assert operator_settings.load(conn, "sending", Sending) == Sending(batch_size=7)


def test_value_the_schema_no_longer_accepts_falls_back_to_its_default(conn):
    with conn:
        conn.execute("INSERT INTO operator_settings (module, data) VALUES (?, ?)",
                     ("sending", '{"batch_size": 0, "window": [9, 21]}'))

    assert operator_settings.load(conn, "sending", Sending) == Sending(window=(9, 21))


def test_edit_from_another_connection_is_read_on_the_next_call(conn, path):
    save(conn, "sending", Sending(batch_size=30))
    assert operator_settings.load(conn, "sending", Sending).batch_size == 30

    with closing(db.connect(path)) as other:
        save(other, "sending", Sending(batch_size=40))

    assert operator_settings.load(conn, "sending", Sending).batch_size == 40


def test_save_is_part_of_the_callers_transaction(conn):
    with pytest.raises(RuntimeError):
        with conn:
            operator_settings.save(conn, "sending", Sending(batch_size=30))
            raise RuntimeError("operation failed after saving")

    assert operator_settings.load(conn, "sending", Sending) == Sending()


class Window(BaseModel):
    start: int = 10
    end: int = 18

    @model_validator(mode="after")
    def start_before_end(self):
        if self.start >= self.end:
            raise ValueError("errors.windowEmpty")
        return self


def test_section_the_schema_rejects_as_a_whole_falls_back_to_defaults(conn):
    with conn:
        conn.execute("INSERT INTO operator_settings (module, data) VALUES (?, ?)",
                     ("window", '{"start": 20, "end": 9}'))

    assert operator_settings.load(conn, "window", Window) == Window()
