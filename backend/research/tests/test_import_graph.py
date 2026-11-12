"""research depends on core, companies and refusals, the last two through public only."""

import ast
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
BACKEND = MODULE.parent
DOMAIN = ("collector", "writer", "sender", "sandbox", "refusals", "companies",
          "conversations", "delivery", "overview", "analytics", "tasks", "activity")
ALLOWED = {"companies.public", "refusals.public"}


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


def test_research_reaches_other_modules_only_through_their_facades():
    offenders = {
        path.relative_to(BACKEND).as_posix(): sorted(
            name for name in imports(path)
            if name.split(".")[0] in DOMAIN and name not in ALLOWED)
        for path in sources()
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_guard_sees_the_facades():
    seen = {path.name: imports(path) for path in sources()}
    assert {"companies.public", "refusals.public"} <= seen["service.py"]
    assert "research.service" in seen["public.py"]
