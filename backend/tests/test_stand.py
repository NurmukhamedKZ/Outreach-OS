"""scripts/stand: the environment it gives the backend, checked by the real guards.

The processes are not started here: that is the manual check in the PR.
"""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest
from fastapi import FastAPI

import sandbox
from backup.service import BackupService
from config import Settings
from core.activity import Journal
from core.whatsapp import NodeWhatsApp
from sandbox import runs

ROOT = Path(__file__).resolve().parents[2]
_loader = SourceFileLoader("stand", str(ROOT / "scripts" / "stand"))
_spec = spec_from_loader("stand", _loader)
assert _spec is not None
stand = module_from_spec(_spec)
_loader.exec_module(stand)

# A worktree .env as production has it: every line the stand must beat.
PRODUCTION_ENV = """\
SANDBOX=0
SENDER_NODE_URL=http://127.0.0.1:8788
SANDBOX_SELF_URL=http://127.0.0.1:8787
UVICORN_HOST=0.0.0.0
TELEGRAM_BOT_TOKEN=123:live
TELEGRAM_CHAT_ID=42
"""


@pytest.fixture
def stand_settings(tmp_path, monkeypatch):
    """Settings as the stand's backend reads them: its variables over a production .env and shell."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(PRODUCTION_ENV, encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:from-the-shell")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "icloud" / "backup"))
    (tmp_path / "icloud").mkdir()
    for key, value in stand.backend_env(tmp_path / "worktree", 9123).items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=dotenv)


def test_the_stand_passes_the_sandbox_guard_on_its_own_port(stand_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    app = FastAPI()
    app.state.whatsapp = NodeWhatsApp(stand_settings.sender_node_url)
    try:
        sandbox.mount(app, stand_settings)
    finally:
        runs.deactivate()
    assert stand_settings.sandbox
    assert stand_settings.uvicorn_port == 9123
    assert stand_settings.uvicorn_host == "127.0.0.1"


def test_the_stand_tells_nobody_and_backs_up_nothing(stand_settings, tmp_path):
    assert not stand_settings.telegram_bot_token
    assert not stand_settings.telegram_chat_id
    backup = BackupService(
        stand_settings.backup_dir,
        tmp_path / "state.db",
        tmp_path / "pages",
        tmp_path / "sessions",
        Journal(lambda: tmp_path / "state.db"),
    )
    assert not backup.status().enabled


def test_the_stand_keeps_its_data_inside_its_directory(stand_settings, tmp_path):
    assert stand_settings.data_dir == tmp_path / "worktree" / ".stand" / "data"


def test_a_process_group_is_the_stands_only_when_it_runs_from_this_worktree():
    """A pid from a stale state file may now belong to someone else: down must not kill it."""
    root = Path("/w/tree")
    listing = (
        "  501 /w/tree/backend/.venv/bin/python /w/tree/backend/main.py\n"
        "  502 node /w/tree-2/frontend/node_modules/.bin/next dev\n"
        "  503 node /w/tree/.claude/worktrees/x/frontend/node_modules/.bin/next dev\n"
        "  504 /bin/zsh -l\n"
    )
    assert stand.owns(listing, 501, root)
    assert not stand.owns(listing, 502, root), "another worktree's prefix"
    assert not stand.owns(listing, 503, root), "a worktree nested under this tree"
    assert not stand.owns(listing, 504, root), "a reused pid"
    assert not stand.owns(listing, 999, root)
