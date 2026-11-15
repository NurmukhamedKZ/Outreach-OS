"""`scripts/check` — one-command checks and the pre-push hook (#50).

The real checks never run here (pytest → check → pytest would recurse):
commands are stand-ins, `sys.executable -c …`.
"""

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND.parent / "scripts" / "check"


def load():
    loader = importlib.machinery.SourceFileLoader("check_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader("check_script", loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = module
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
    done = subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True,
                          text=True)
    return done.stdout.strip()


def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "a")
    return tmp_path


def commit(root, path, text="x"):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", path)


def push_of_head(root):
    return io.StringIO(f"refs/heads/b {git(root, 'rev-parse', 'HEAD')} refs/heads/b {ZERO}\n")


# --- selection -------------------------------------------------------------


FRONT_TRIGGER = ("frontend/",)


def verdicts(changed):
    checks = [ok("pytest"), ok("frontend", triggers=FRONT_TRIGGER)]
    return {c.name: why for c, why in check.select(checks, changed)}


def test_frontend_path_selects_frontend():
    assert verdicts(["frontend/app/page.tsx"]) == {"pytest": None, "frontend": None}


def test_backend_or_docs_diff_skips_frontend():
    assert verdicts(["backend/x.py", "docs/a"]) == {
        "pytest": None, "frontend": "дифф не трогает frontend/"}


def test_empty_diff_skips_frontend_and_runs_backend():
    assert verdicts([]) == {"pytest": None, "frontend": "дифф не трогает frontend/"}


def test_lookalike_paths_do_not_select_frontend():
    assert verdicts(["frontend-old/x", "docs/frontend/x"])["frontend"]


def test_unknown_diff_selects_everything():
    assert verdicts(None) == {"pytest": None, "frontend": None}


def test_real_table_builds_frontend_before_tsc_and_only_on_its_diff():
    by_name = {c.name: c for c in check.CHECKS}
    front = by_name.pop("frontend")
    assert front.triggers == FRONT_TRIGGER and front.needs == "node_modules"
    assert [cmd[:2] for cmd in front.commands] == [["npm", "run"], ["npx", "tsc"]]
    assert set(by_name) == {"ruff", "mypy", "lint-imports", "pytest"}
    assert all(not c.triggers for c in by_name.values())


def test_without_origin_main_changed_is_none(tmp_path):
    assert check.changed_files(repo(tmp_path)) is None


def test_changed_files_are_committed_worktree_and_untracked(tmp_path):
    root = repo(tmp_path)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(root, "frontend/committed.tsx")
    (root / "frontend" / "новый.tsx").write_text("x")
    (root / "a.txt").write_text("b")
    assert sorted(check.changed_files(root)) == [
        "a.txt", "frontend/committed.tsx", "frontend/новый.tsx"]


def test_changes_on_main_after_the_fork_are_not_ours(tmp_path):
    root = repo(tmp_path)
    git(root, "checkout", "-qb", "main-later")
    commit(root, "frontend/main-only.tsx")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "checkout", "-q", "-")
    commit(root, "backend/ours.py")
    assert check.changed_files(root) == ["backend/ours.py"]


def test_children_do_not_inherit_the_hooks_git_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    fresh = load()
    (tmp_path / "r").mkdir()
    root = repo(tmp_path / "r")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert fresh.changed_files(root) == []
    probe = "import os, sys; sys.exit(any(k.startswith('GIT_') for k in os.environ))"
    assert fresh.execute(fresh.Check("env", ".", [[sys.executable, "-c", probe]]), root)[0]


# --- running ---------------------------------------------------------------


def test_all_green_exits_zero(tmp_path, capsys):
    assert check.run([(ok("ruff"), None), (ok("mypy"), None)], tmp_path) == 0
    out = capsys.readouterr().out
    assert "ok   ruff" in out and "ok   mypy" in out
    assert "тихо" not in out and "---" not in out


def test_one_red_exits_one_and_shows_only_its_output(tmp_path, capsys):
    assert check.run([(ok("ruff"), None), (bad("pytest"), None)], tmp_path) == 1
    out = capsys.readouterr().out
    summary, details = out.split("\n--- pytest ---\n")
    assert "FAIL pytest" in summary and "ok   ruff" in summary
    assert "сломалось" in details
    assert "тихо" not in out


def test_failure_shows_stderr_and_earlier_commands(tmp_path, capsys):
    first = [sys.executable, "-c", "print('собрал')"]
    second = [sys.executable, "-c", "import sys; sys.exit('ошибка в stderr')"]
    assert check.run([(check.Check("frontend", ".", [first, second]), None)], tmp_path) == 1
    out = capsys.readouterr().out
    assert "собрал" in out and "ошибка в stderr" in out


def test_first_failing_command_stops_the_check(tmp_path):
    marker = tmp_path / "ran"
    first = [sys.executable, "-c", "import sys; sys.exit(1)"]
    second = [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
    assert check.run([(check.Check("frontend", ".", [first, second]), None)], tmp_path) == 1
    assert not marker.exists()


def test_skipped_check_is_reported(tmp_path, capsys):
    assert check.run([(ok("frontend"), "дифф не трогает frontend/")], tmp_path) == 0
    assert "skip frontend  дифф не трогает frontend/" in capsys.readouterr().out


def front_with_needs():
    return check.Check("frontend", "frontend", [[sys.executable, "-c", "pass"]],
                       needs="node_modules", hint="cd frontend && npm ci")


def test_missing_node_modules_is_a_hint_not_a_trace(tmp_path, capsys):
    (tmp_path / "frontend").mkdir()
    assert check.run([(front_with_needs(), None)], tmp_path) == 1
    out = capsys.readouterr().out
    assert "нет frontend/node_modules: `cd frontend && npm ci`" in out
    assert "Traceback" not in out


def test_present_node_modules_runs_the_check(tmp_path):
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    assert check.run([(front_with_needs(), None)], tmp_path) == 0


def test_missing_tool_is_a_hint(tmp_path, capsys):
    gone = check.Check("x", ".", [["нет-такой-программы-50"]])
    assert check.run([(gone, None)], tmp_path) == 1
    assert "нет нет-такой-программы-50 в PATH" in capsys.readouterr().out


def test_undecodable_output_does_not_crash_the_report(tmp_path, capsys):
    code = "import sys; sys.stdout.buffer.write(bytes([0xff])); sys.exit(1)"
    garbled = check.Check("mypy", ".", [[sys.executable, "-c", code]])
    assert check.run([(ok("ruff"), None), (garbled, None)], tmp_path) == 1
    assert "FAIL mypy" in capsys.readouterr().out


# --- main --------------------------------------------------------------------


def repo_with_backend_diff(tmp_path):
    root = repo(tmp_path)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(root, "backend/x.py")
    return root


def test_main_follows_the_diff(tmp_path, capsys):
    root = repo_with_backend_diff(tmp_path)
    front = check.Check("frontend", ".", bad("f").commands, triggers=FRONT_TRIGGER)
    assert check.main([], io.StringIO(""), root, [front]) == 0
    assert "skip frontend" in capsys.readouterr().out


def test_main_all_ignores_the_diff(tmp_path, capsys):
    root = repo_with_backend_diff(tmp_path)
    front = check.Check("frontend", ".", ok("f").commands, triggers=FRONT_TRIGGER)
    assert check.main(["--all"], io.StringIO(""), root, [front]) == 0
    assert "ok   frontend" in capsys.readouterr().out


def test_unknown_or_abbreviated_flag_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        check.main(["--al"], io.StringIO(""), tmp_path, [ok("pytest")])


def test_manual_run_on_a_dirty_tree_still_checks(tmp_path, capsys):
    root = repo(tmp_path)
    (root / "a.txt").write_text("правка")
    assert check.main([], io.StringIO(""), root, [ok("pytest")]) == 0


# --- pre-push ----------------------------------------------------------------


def test_pre_push_only_deletions_runs_nothing(tmp_path):
    stdin = io.StringIO(f"(delete) {ZERO} refs/heads/old abc\n")
    assert check.main(["--pre-push"], stdin, repo(tmp_path), [bad("pytest")]) == 0


def test_pre_push_sha256_deletion_runs_nothing(tmp_path):
    stdin = io.StringIO(f"(delete) {'0' * 64} refs/heads/old abc\n")
    assert check.main(["--pre-push"], stdin, repo(tmp_path), [bad("pytest")]) == 0


def test_pre_push_empty_stdin_pushes_nothing(tmp_path):
    assert check.main(["--pre-push"], io.StringIO(""), repo(tmp_path), [bad("pytest")]) == 0


def test_pre_push_deletion_plus_branch_runs_checks(tmp_path, capsys):
    root = repo(tmp_path)
    stdin = io.StringIO(f"(delete) {ZERO} refs/heads/old abc\n" + push_of_head(root).getvalue())
    assert check.main(["--pre-push"], stdin, root, [bad("pytest")]) == 1
    assert "FAIL pytest" in capsys.readouterr().out


def test_pre_push_dirty_tree_refuses(tmp_path, capsys):
    root = repo(tmp_path)
    (root / "a.txt").write_text("правка")
    assert check.main(["--pre-push"], push_of_head(root), root, [ok("pytest")]) == 1
    assert "незакоммиченные" in capsys.readouterr().out


def test_pre_push_untracked_file_does_not_block(tmp_path):
    root = repo(tmp_path)
    (root / "new.txt").write_text("x")
    assert check.main(["--pre-push"], push_of_head(root), root, [ok("pytest")]) == 0


def test_pre_push_of_another_commit_refuses(tmp_path, capsys):
    root = repo(tmp_path)
    other = git(root, "rev-parse", "HEAD")
    commit(root, "b.txt")
    stdin = io.StringIO(f"refs/heads/old {other} refs/heads/old {ZERO}\n")
    assert check.main(["--pre-push"], stdin, root, [ok("pytest")]) == 1
    assert "не HEAD" in capsys.readouterr().out


def test_pre_push_clean_tree_runs_checks(tmp_path, capsys):
    root = repo(tmp_path)
    assert check.main(["--pre-push"], push_of_head(root), root, [bad("pytest")]) == 1
    assert "FAIL pytest" in capsys.readouterr().out
