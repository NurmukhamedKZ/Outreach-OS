"""Граница песочницы: она знает про системы, системы про неё — нет.

Тот же статический обход графа импортов, что сторожит отсутствие сети в
rebuild.py и в системе 3. Единственное исключение — collector/api.py: он и есть
место, где приложение собирается, и все швы живут там.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent
ASSEMBLER = "collector/api.py"

# Обходятся корни систем и модули верхнего уровня, а не весь backend: rglob по
# нему заодно прочёл бы .venv — тысячи чужих файлов на каждом прогоне сюиты.
ROOTS = ("collector", "writer", "sender")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def sources() -> list[Path]:
    found = list(BACKEND.glob("*.py"))
    for root in ROOTS:
        found.extend((BACKEND / root).rglob("*.py"))
    return sorted(found)


def test_nobody_imports_the_sandbox():
    offenders = []
    for path in sources():
        rel = path.relative_to(BACKEND).as_posix()
        if rel == ASSEMBLER:
            continue
        if any(module == "sandbox" or module.startswith("sandbox.")
               for module in imports(path)):
            offenders.append(rel)
    assert offenders == [], f"песочницу импортирует не только сборщик: {offenders}"


def test_the_guard_itself_sees_the_assembler():
    """Страж бесполезен, если перестал находить файлы: сборщик обязан
    импортировать песочницу."""
    assert any(module.startswith("sandbox") for module in imports(BACKEND / ASSEMBLER))
