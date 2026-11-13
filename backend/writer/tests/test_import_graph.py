"""writer reads its leads through research.public and reaches no other module."""

import ast
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
BACKEND = MODULE.parent
DOMAIN = ("collector", "sender", "sandbox", "refusals", "companies", "research",
          "conversations", "delivery", "overview", "analytics", "tasks", "activity")
ALLOWED = {"research.public"}


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.update(f"{node.module}.{alias.name}" if node.module in DOMAIN else node.module
                         for alias in node.names)
    return found


def sources() -> list[Path]:
    return [path for path in MODULE.rglob("*.py")
            if "tests" not in path.relative_to(MODULE).parts]


def test_writer_reaches_other_modules_only_through_research_public():
    offenders = {
        path.relative_to(BACKEND).as_posix(): sorted(
            name for name in imports(path)
            if name.split(".")[0] in DOMAIN and name not in ALLOWED)
        for path in sources()
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_guard_sees_the_facade():
    assert "research.public" in imports(MODULE / "db" / "leads_source.py")
