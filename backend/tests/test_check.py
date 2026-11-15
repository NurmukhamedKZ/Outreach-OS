"""`scripts/check` — проверки одной командой и хук pre-push (#50).

Настоящие проверки тест не запускает (`pytest` → `check` → `pytest`): команды
подставные, `sys.executable -c …`.
"""

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND.parent / "scripts" / "check"


def load():
    loader = importlib.machinery.SourceFileLoader("check_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader("check_script", loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


check = load()
ZERO = "0" * 40


def ok(name, **kw):
    return check.Check(name, ".", [[sys.executable, "-c", "print('тихо')"]], **kw)


def bad(name, text="сломалось"):
    code = f"import sys; print({text!r}); sys.exit(1)"
    return check.Check(name, ".", [[sys.executable, "-c", code]])


def git(root, *args):
    # Hooks run with GIT_DIR and other GIT_* set; without stripping them
    # the test would write to the project repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "a")
    return tmp_path


# --- выбор -------------------------------------------------------------------


def test_frontend_path_selects_frontend():
    front = ok("frontend", triggers=("frontend/",))
    chosen = check.select([ok("pytest"), front], ["frontend/app/page.tsx"])
    assert [c.name for c, why in chosen if why is None] == ["pytest", "frontend"]


def test_backend_or_docs_diff_skips_frontend():
    front = ok("frontend", triggers=("frontend/",))
    chosen = dict((c.name, why) for c, why in check.select([front], ["backend/x.py", "docs/a"]))
    assert chosen["frontend"]


def test_all_or_unknown_diff_selects_everything():
    front = ok("frontend", triggers=("frontend/",))
    assert check.select([front], None) == [(front, None)]


def test_without_origin_main_changed_is_none(tmp_path):
    assert check.changed_files(repo(tmp_path)) is None


def test_changed_files_include_untracked(tmp_path):
    root = repo(tmp_path)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "frontend").mkdir()
    (root / "frontend" / "new.tsx").write_text("x")
    (root / "a.txt").write_text("b")
    assert sorted(check.changed_files(root)) == ["a.txt", "frontend/new.tsx"]


# --- прогон ------------------------------------------------------------------


def test_all_green_exits_zero(tmp_path, capsys):
    assert check.run([(ok("ruff"), None), (ok("mypy"), None)], tmp_path) == 0
    out = capsys.readouterr().out
    assert "ok   ruff" in out and "ok   mypy" in out
    assert "тихо" not in out


def test_one_red_exits_one_and_shows_only_its_output(tmp_path, capsys):
    assert check.run([(ok("ruff"), None), (bad("pytest"), None)], tmp_path) == 1
    out = capsys.readouterr().out
    assert "FAIL pytest" in out and "сломалось" in out
    assert "тихо" not in out


def test_first_failing_command_stops_the_check(tmp_path, capsys):
    marker = tmp_path / "ran"
    second = [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
    two = check.Check("frontend", ".", [bad("x").commands[0], second])
    assert check.run([(two, None)], tmp_path) == 1
    assert not marker.exists()


def test_skipped_check_is_reported(tmp_path, capsys):
    assert check.run([(ok("frontend"), "дифф не трогает frontend/")], tmp_path) == 0
    assert "skip frontend" in capsys.readouterr().out


def test_missing_node_modules_is_a_hint_not_a_trace(tmp_path, capsys):
    (tmp_path / "frontend").mkdir()
    front = check.Check("frontend", "frontend", [["npm", "run", "build"]],
                        needs="node_modules", hint="cd frontend && npm ci")
    assert check.run([(front, None)], tmp_path) == 1
    out = capsys.readouterr().out
    assert "нет frontend/node_modules: `cd frontend && npm ci`" in out
    assert "Traceback" not in out


def test_missing_tool_is_a_hint(tmp_path, capsys):
    gone = check.Check("x", ".", [["нет-такой-программы-50"]])
    assert check.run([(gone, None)], tmp_path) == 1
    assert "нет нет-такой-программы-50 в PATH" in capsys.readouterr().out


# --- pre-push ----------------------------------------------------------------


def test_pre_push_only_deletions_runs_nothing(tmp_path):
    stdin = io.StringIO(f"(delete) {ZERO} refs/heads/old abc\n")
    assert check.main(["--pre-push"], stdin, repo(tmp_path), [bad("pytest")]) == 0


def test_pre_push_dirty_tree_refuses(tmp_path, capsys):
    root = repo(tmp_path)
    (root / "a.txt").write_text("правка")
    stdin = io.StringIO(f"refs/heads/b abc refs/heads/b {ZERO}\n")
    assert check.main(["--pre-push"], stdin, root, [ok("pytest")]) == 1
    assert "незакоммиченные" in capsys.readouterr().out


def test_pre_push_clean_tree_runs_checks(tmp_path, capsys):
    stdin = io.StringIO(f"refs/heads/b abc refs/heads/b {ZERO}\n")
    assert check.main(["--pre-push"], stdin, repo(tmp_path), [bad("pytest")]) == 1
    assert "FAIL pytest" in capsys.readouterr().out
