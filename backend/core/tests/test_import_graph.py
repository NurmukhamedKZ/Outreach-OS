"""Migrations know no domain module: a migration outlives the code it was written against.

The boundaries of core and the domain modules are import-linter contracts
(pyproject.toml, [tool.importlinter]); migrations are not a package, so the
linter cannot see them and this test guards them instead.
"""

import ast
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
DOMAIN = (
    "collector",
    "writer",
    "sender",
    "sandbox",
    "conversations",
    "delivery",
    "companies",
    "research",
    "refusals",
    "backup",
    "overview",
)


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_migrations_import_no_domain_module():
    seen = {path.name: imports(path) for path in MIGRATIONS.rglob("*.py")}
    offenders = {
        name: sorted(n for n in names if n.split(".")[0] in DOMAIN) for name, names in seen.items()
    }
    assert {name: names for name, names in offenders.items() if names} == {}
    assert "paths" in seen["env.py"]  # the guard reads the files it guards
