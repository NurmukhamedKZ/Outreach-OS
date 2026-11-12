"""backup depends on core, the journal and the top-level infrastructure only."""

import ast
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
BACKEND = MODULE.parent
# Top-level packages and modules that carry business logic; core and the shared
# top-level infrastructure (clock, paths, config) are allowed.
# The journal (activity) is allowed: a daemon does not work silently.
DOMAIN = ("collector", "writer", "sender", "sandbox", "companies", "research", "refusals",
          "conversations", "delivery", "overview", "analytics", "tasks")


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def sources() -> list[Path]:
    return [path for path in MODULE.rglob("*.py")
            if "tests" not in path.relative_to(MODULE).parts]


def test_backup_imports_no_domain_module():
    offenders = {
        path.relative_to(BACKEND).as_posix(): sorted(
            name for name in imports(path) if name.split(".")[0] in DOMAIN)
        for path in sources()
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_guard_sees_the_module():
    seen = {path.name: imports(path) for path in sources()}
    assert "core.errors" in seen["exceptions.py"]
    assert "activity" in seen["service.py"]
