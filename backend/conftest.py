"""Тесты не касаются боевой базы: `collector.api` при импорте открывает журнал
в `paths.state_db()`, и без подмены `uv run pytest` писал бы в базу работающего
сервера. Подмена — до сбора тестов, поэтому здесь, а не в фикстуре."""

import tempfile
from pathlib import Path

import paths

paths.PRODUCTION_STATE = Path(tempfile.mkdtemp(prefix="outreach-tests-")) / "state.db"
