"""Путь к невосстановимому слою: один владелец на три системы.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import paths


def test_production_database_by_default():
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.state_db().name == "state.db"


def test_production_path_lives_in_collector_data():
    """Путь тот же, что раньше называла константа store.STATE: переезда файла
    эта задача не делает."""
    assert paths.PRODUCTION_STATE.parent.name == "data"
    assert paths.PRODUCTION_STATE.parent.parent.name == "collector"


def test_run_overrides_production_and_none_returns_it_back(tmp_path):
    paths.use_run(tmp_path / "run.db")
    try:
        assert paths.state_db() == tmp_path / "run.db"
        assert paths.run_path() == tmp_path / "run.db"
    finally:
        paths.use_run(None)
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.run_path() is None


def test_use_run_accepts_a_string(tmp_path):
    """Путь приезжает из json ручки песочницы строкой, а внутри всё на Path."""
    paths.use_run(str(tmp_path / "run.db"))
    try:
        assert paths.state_db() == tmp_path / "run.db"
    finally:
        paths.use_run(None)
