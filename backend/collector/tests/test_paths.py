"""Путь к невосстановимому слою: один владелец на три системы.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import paths


def test_production_database_by_default():
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.state_db().name == "state.db"


def test_data_layer_lives_in_backend_data():
    """#9: единая база, страницы и прогоны песочницы — в одном каталоге данных."""
    backend = paths.DATA.parent
    assert paths.DATA == backend / "data"
    assert (backend / "main.py").exists()
    assert paths.PRODUCTION_STATE == paths.DATA / "state.db"
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
