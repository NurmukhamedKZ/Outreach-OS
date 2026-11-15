"""Путь к невосстановимому слою: один владелец на три системы."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import paths
from config import Settings, settings

BACKEND = Path(__file__).resolve().parent.parent


def test_production_database_by_default():
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.state_db().name == "state.db"


def test_data_layer_lives_in_backend_data_by_default(monkeypatch):
    """#9: единая база, страницы и прогоны песочницы — в одном каталоге данных."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert Settings(_env_file=None).data_dir == BACKEND / "data"
    assert paths.DATA == settings.data_dir
    # conftest.py points PRODUCTION_STATE at a temp file; the name is what is checked.
    assert paths.PRODUCTION_STATE.name == "state.db"
    assert paths.PAGES == paths.DATA / "pages"
    assert paths.SANDBOX_RUNS == paths.DATA / "sandbox"


def test_run_overrides_production_and_none_returns_it_back(tmp_path):
    paths.use_run(tmp_path / "run.db")
    try:
        assert paths.state_db() == tmp_path / "run.db"
    finally:
        paths.use_run(None)
    assert paths.state_db() == paths.PRODUCTION_STATE


def test_use_run_accepts_a_string(tmp_path):
    """Путь приезжает из json ручки песочницы строкой, а внутри всё на Path."""
    paths.use_run(str(tmp_path / "run.db"))
    try:
        assert paths.state_db() == tmp_path / "run.db"
    finally:
        paths.use_run(None)


def test_data_dir_moves_the_whole_data_layer_but_not_the_sessions(tmp_path):
    """#51: a stand gets a data directory of its own; WhatsApp sessions stay Node's."""
    probe = (
        "import json, paths; print(json.dumps([str(p) for p in (paths.PRODUCTION_STATE,"
        " paths.PAGES, paths.SANDBOX_RUNS, paths.SESSIONS)]))"
    )
    env = {"PATH": "", "DATA_DIR": str(tmp_path / "data")}
    output = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    state, pages, runs, sessions = map(Path, json.loads(output))
    assert state == tmp_path / "data" / "state.db"
    assert pages == tmp_path / "data" / "pages"
    assert runs == tmp_path / "data" / "sandbox"
    assert sessions == paths.SESSIONS


@pytest.mark.parametrize("value", ["", "data", "../data"])
def test_data_dir_is_absolute_or_the_backend_does_not_start(value, monkeypatch):
    """An empty DATA_DIR is the cwd and a relative one depends on it: an empty base, refusals too."""
    monkeypatch.setenv("DATA_DIR", value)
    with pytest.raises(ValidationError, match="DATA_DIR"):
        Settings(_env_file=None)
