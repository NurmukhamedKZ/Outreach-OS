"""Граница системы 3: в сеть ходит только transport.py.

Тот же статический обход графа импортов, что сторожит отсутствие сети в
rebuild.py (collector/tests/test_operations.py). Проверяется не дисциплина, а
факт: если pool или health начнут импортировать httpx, тест покраснеет.
"""

import ast
from pathlib import Path

SENDER = Path(__file__).resolve().parent.parent
BACKEND_ROOT = SENDER.parent
NETWORK = ("httpx", "requests", "urllib.request", "scrapling")
ALLOWED = ("sender/transport.py", "sender/notify.py")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_only_transport_and_notify_touch_the_network():
    offenders = []
    for path in sorted(SENDER.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        if rel.startswith("sender/tests/") or rel in ALLOWED:
            continue
        if any(module.startswith(NETWORK) for module in imports(path)):
            offenders.append(rel)
    assert offenders == [], f"в сеть ходит не только transport.py: {offenders}"


def test_the_guard_itself_sees_transport():
    """Страж бесполезен, если перестал находить файлы: transport.py обязан
    попадать в выборку и обязан импортировать httpx."""
    assert any(module.startswith("httpx") for module in imports(SENDER / "transport.py"))
