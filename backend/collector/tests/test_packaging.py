"""Образ обязан содержать все модули верхнего уровня, которые импортирует api.

Список COPY в Dockerfile перечислен руками, и забытый в нём файл не виден
ничем до `docker compose up`: контейнер падает ModuleNotFoundError на импорте
main.py. Так уже случилось дважды подряд — с activity.py и analytics.py.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = BACKEND / "Dockerfile"

# Приватное и точка входа тестов образу не нужны.
SKIP = {"conftest.py"}


def test_dockerfile_copies_every_top_level_module():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    modules = {path.name for path in BACKEND.glob("*.py")} - SKIP

    missing = sorted(name for name in modules if name not in dockerfile)

    assert not missing, (
        f"модули верхнего уровня не попадают в образ: {missing}."
        " collector/api.py импортирует их напрямую, и контейнер упадёт"
        " ModuleNotFoundError на старте"
    )


def test_api_imports_only_modules_that_exist_at_top_level():
    """Обратная сторона того же контракта: `import X` верхнего уровня в api.py
    обязан иметь файл рядом, иначе тест выше проверял бы пустое множество."""
    api = (BACKEND / "collector" / "api.py").read_text(encoding="utf-8")
    imported = set(re.findall(r"^import (\w+)$", api, re.MULTILINE))
    local = {path.stem for path in BACKEND.glob("*.py")}

    assert imported & local, "в api.py не осталось импортов модулей верхнего уровня"
    for name in imported & local:
        assert (BACKEND / f"{name}.py").exists()
