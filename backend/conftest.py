"""Тесты не касаются боевой базы: приложение `main` пишет журнал и читает базу в
`paths.state_db()`, и без подмены `uv run pytest` писал бы в базу работающего
сервера. Подмена — до сбора тестов, поэтому здесь, а не в фикстуре."""

import tempfile
from pathlib import Path

import paths

paths.PRODUCTION_STATE = Path(tempfile.mkdtemp(prefix="outreach-tests-")) / "state.db"
