"""core knows no domain module: everything else may depend on it, never back."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
BACKEND = CORE.parent
DOMAIN = ("collector", "writer", "sender", "sandbox", "companies", "research",
          "refusals", "conversations", "delivery", "overview", "analytics", "tasks")


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def sources() -> list[Path]:
    return [path for path in [*CORE.rglob("*.py"), *(BACKEND / "migrations").rglob("*.py")]
            if "tests" not in path.relative_to(BACKEND).parts]


def test_core_and_migrations_import_no_domain_module():
    offenders = {
        path.relative_to(BACKEND).as_posix(): sorted(
            name for name in imports(path) if name.split(".")[0] in DOMAIN)
        for path in sources()
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_guard_sees_the_adapters():
    seen = {path.name: imports(path) for path in sources()}
    assert "scrapling.fetchers" in seen["pages.py"]
    assert "langchain_openrouter" in seen["model.py"]
