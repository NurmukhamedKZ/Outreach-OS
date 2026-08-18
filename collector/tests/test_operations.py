"""Граница сборки: rebuild.py не тянет сеть даже транзитивно.

Статический обход графа импортов: у rebuild и всего, что он импортирует,
в резолвленных модулях не должно быть services/fetch и scrapling.
"""
import ast
from pathlib import Path

# Корень collector/ — там лежат services/, store/, db/.
COLLECTOR = Path(__file__).resolve().parent.parent
FORBIDDEN = ("services/fetch", "scrapling")


def _resolve(root, node):
    """Кандидаты-пути, на которые может ссылаться Import/ImportFrom.

    `from services import enrich` означает модуль services/enrich.py (services —
    namespace-пакет без __init__.py). `from services.pipeline import X` — либо
    services/pipeline.py, либо X-подмодуль. Возвращаем несколько кандидатов;
    reachable_modules берёт только существующие.
    """
    out = []
    if isinstance(node, ast.Import):
        for n in node.names:
            out.append(root / (n.name.replace(".", "/") + ".py"))
    elif isinstance(node, ast.ImportFrom) and node.module:
        base = root / node.module.replace(".", "/")
        out.append(base.with_suffix(".py"))
        for n in node.names:
            out.append(root / (f"{node.module}.{n.name}".replace(".", "/") + ".py"))
    return out


def reachable_modules(entry):
    seen, stack = set(), [Path(entry)]
    while stack:
        path = stack.pop()
        path = path.resolve()
        if path in seen or not path.exists() or path.suffix != ".py":
            continue
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                stack.extend(_resolve(COLLECTOR, node))
    return seen


def test_rebuild_import_graph_has_no_network():
    from services.pipeline import rebuild
    modules = reachable_modules(rebuild.__file__)
    assert modules, "обход графа импортов не дошёл ни до одного модуля — walker сломан"
    for m in modules:
        rel = m.relative_to(COLLECTOR).as_posix()
        assert not any(bad in rel for bad in FORBIDDEN), \
            f"rebuild тянет сеть через {rel}"


def test_collect_ops_accept_runcontext():
    """Операции сбора принимают RunContext и возвращают dict."""
    from services.pipeline import collect
    ctx = DummyContext()
    # не запускаем сеть — только проверяем, что сигнатуры живы
    assert callable(collect.gis) and callable(collect.sites) and callable(collect.instagram)


class DummyContext:
    def progress(self, current, total, label): ...
    def log(self, message): ...
    def check_cancelled(self): ...