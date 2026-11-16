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
from conversations import dependencies as conversations_dependencies
from core.activity import Journal
from core.model import OfflineModel
from core.whatsapp import NodeWhatsApp
from research import dependencies as research_dependencies
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
OPENROUTER_API_KEY=sk-or-live
"""


def _stand_settings(tmp_path, monkeypatch, dotenv_text: str = PRODUCTION_ENV, **flags):
    """Settings as the stand's backend reads them: its variables over a .env and the shell."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(dotenv_text, encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:from-the-shell")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "icloud" / "backup"))
    (tmp_path / "icloud").mkdir(exist_ok=True)
    for key, value in stand.backend_env(tmp_path / "worktree", 9123, **flags).items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=dotenv)


@pytest.fixture
def stand_settings(tmp_path, monkeypatch):
    return _stand_settings(tmp_path, monkeypatch)


def test_the_stand_passes_the_sandbox_guard_on_its_own_port(stand_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    app = FastAPI()
    app.state.whatsapp = NodeWhatsApp(stand_settings.sender_node_url)
    try:
        sandbox.mount(app, stand_settings)
    finally:
        runs.deactivate()
    assert stand_settings.sandbox
    assert stand_settings.offline, "the guard checked the offline mode too"
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


@pytest.mark.parametrize(
    ("dependencies", "build"),
    [(research_dependencies, "get_model"), (conversations_dependencies, "build_model")],
)
def test_the_stand_asks_no_model_even_with_a_key_in_the_env_file(
    stand_settings, monkeypatch, dependencies, build
):
    """Batches and drafts on a stand cost nothing and answer no 503, whatever backend/.env holds."""
    assert stand_settings.openrouter_api_key == "sk-or-live"
    monkeypatch.setattr(dependencies, "settings", stand_settings)
    assert isinstance(getattr(dependencies, build)(), OfflineModel)


def test_an_online_stand_has_the_real_model_and_network(tmp_path, monkeypatch):
    online = _stand_settings(tmp_path, monkeypatch, PRODUCTION_ENV + "OFFLINE=1\n", online=True)

    assert not online.offline, "--online beats the env file too"
    assert online.sandbox, "online or not, the stand stays in the sandbox"


def _cwds(*folders: str) -> str:
    """What `lsof -a -g <pgid> -d cwd -Fn` prints for a group whose processes work in `folders`."""
    return "".join(f"p{500 + i}\nfcwd\nn{folder}\n" for i, folder in enumerate(folders))


def test_a_process_group_is_the_stands_only_when_it_works_in_this_worktree():
    """A pgid from a stale state file may now belong to someone else: down must not kill it."""
    root = Path("/w/tree")
    assert stand.owns(_cwds("/w/tree/backend", "/w/tree/backend"), root), "uv and python"
    assert stand.owns(_cwds("/w/tree/frontend"), root), "next-server: renamed, its parent gone"
    assert not stand.owns(_cwds("/w/tree-2/frontend"), root), "another worktree's prefix"
    assert not stand.owns(_cwds("/w/tree/.claude/worktrees/x/backend"), root), "a nested worktree"
    assert not stand.owns(_cwds("/w/tree/backend/migrations"), root), "a shell in a subfolder"
    assert not stand.owns(_cwds("/Users/someone"), root), "a reused pid"
    assert not stand.owns("", root), "the group is gone"
