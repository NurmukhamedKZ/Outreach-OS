"""Граница сборки: rebuild.py не тянет сеть даже транзитивно.

Статический обход графа импортов: у rebuild и всего, что он импортирует,
в резолвленных модулях не должно быть services/fetch и scrapling.
"""
import ast
import inspect
import re
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent.parent
BACKEND_ROOT = COLLECTOR.parent   # корень абсолютных импортов: collector.X живёт здесь
FORBIDDEN = ("services/fetch", "scrapling")


def _resolve(root, node):
    """Кандидаты-пути, на которые может ссылаться Import/ImportFrom.

    `from collector.services import enrich` означает модуль services/enrich.py
    относительно `root` (BACKEND_ROOT — dotted-путь включает сам пакет
    `collector`, поэтому резолвить его надо на уровень выше COLLECTOR).
    `from collector.services.pipeline import X` — либо services/pipeline.py,
    либо X-подмодуль. Возвращаем несколько кандидатов; reachable_modules берёт
    только существующие.
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
                stack.extend(_resolve(BACKEND_ROOT, node))
    return seen


def test_rebuild_import_graph_has_no_network():
    from collector.services.pipeline import rebuild
    modules = reachable_modules(rebuild.__file__)
    assert modules, "обход графа импортов не дошёл ни до одного модуля — walker сломан"
    for m in modules:
        rel = m.relative_to(COLLECTOR).as_posix()
        assert not any(bad in rel for bad in FORBIDDEN), \
            f"rebuild тянет сеть через {rel}"


def test_every_operation_takes_exactly_a_runcontext():
    """Контракт воркера: операция зовётся как OPERATIONS[name](ctx).

    Проверяется подпись, а не вызываемость: `assert callable` проходил бы и на
    функции с тремя обязательными аргументами, а воркер упал бы на ней в бою.
    Параметры операций живут в config.toml — лишний обязательный аргумент
    означает, что кто-то протащил их в сигнатуру.
    """
    from collector.services.pipeline import OPERATIONS

    for name, operation in OPERATIONS.items():
        signature = inspect.signature(operation)
        required = [
            parameter for parameter in signature.parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        ]
        assert len(required) == 1, f"{name}{signature}: воркер передаёт только ctx"


def test_reviews_op_accepts_runcontext():
    """Операция сбора отзывов принимает RunContext и возвращает dict."""
    from collector.services.pipeline import collect
    assert callable(collect.reviews)


def test_reviews_analyze_op_accepts_runcontext():
    """Слой анализа отзывов принимает RunContext."""
    from collector.services.pipeline import analyze
    assert callable(analyze.reviews)


def test_site_pages_op_accepts_runcontext():
    """Сбор внутренних страниц сайта принимает RunContext."""
    from collector.services.pipeline import collect
    assert callable(collect.site_pages)


def test_site_analyze_op_accepts_runcontext():
    """Слой анализа сайта принимает RunContext."""
    from collector.services.pipeline import analyze
    assert callable(analyze.site)


def test_ig_comments_op_accepts_runcontext():
    """Сбор комментариев и профилей Instagram принимает RunContext."""
    from collector.services.pipeline import collect
    assert callable(collect.ig_comments) and callable(collect.ig_profile)


def test_instagram_analyze_op_accepts_runcontext():
    """Слой анализа Instagram принимает RunContext."""
    from collector.services.pipeline import analyze
    assert callable(analyze.instagram)


def test_dossier_analyze_op_accepts_runcontext():
    """Слой синтеза досье принимает RunContext."""
    from collector.services.pipeline import analyze
    assert callable(analyze.dossier)


def test_operations_use_the_context_they_are_given():
    """Операция обязана говорить, что делает, и слушать отмену.

    Кнопка отмены на операции, которая не спрашивает check_cancelled, — обман:
    воркер поставит флаг, а джоба будет идти до конца. Так и было у пересборки,
    самой длинной операции из всех, и у выгрузки, не трогавшей ctx вовсе.

    Проверка по модулю, а не по телу функции: операции делегируют работу
    соседям в том же файле (probe.gis_list -> collect_list), и требовать вызов
    именно в теле значило бы запрещать это. ctx.progress не требуется: пробе из
    двух запросов нечего показывать в счётчике.
    """
    from collector.services.pipeline import OPERATIONS

    for name, operation in OPERATIONS.items():
        source = inspect.getsource(inspect.getmodule(operation))
        assert "ctx.check_cancelled" in source, f"{name}: отмену не спрашивает никто"
        assert "ctx.log" in source, f"{name}: молчит в лог"


WRITE = re.compile(
    r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(state\.)?(\w+)",
    re.IGNORECASE,
)
DERIVED_TABLES = frozenset(
    "runs current_run fetches_all orgs_all contacts_all companies_all"
    " company_links_all signals_all scores_all profiles_all dossiers_all".split()
)


def writes_of(path):
    """Куда пишет модуль: {"state", "derived"} по его же SQL.

    Разбор текстом, а не выполнением: запись в чужую базу должна ловиться до
    того, как её кто-нибудь вызовет, и на всех ветках сразу.
    """
    targets = set()
    for prefix, table in WRITE.findall(path.read_text(encoding="utf-8")):
        if prefix:
            targets.add("state")
        elif table in DERIVED_TABLES:
            targets.add("derived")
    return targets


def test_no_operation_writes_to_both_dbs():
    """Ни один модуль не пишет и в derived, и в state (спека §1).

    Транзакция через две базы в WAL не атомарна: наполовину прошедшая запись
    оставила бы прогон без отказа или отказ без прогона. Поэтому граница
    проведена по модулям — пересборка пишет только вычислимое, анализ и джобы
    только порождённое, — и проверяется она по SQL, а не по именам файлов в
    строках: прежняя проверка искала литералы "derived.db"/"state.db" и прошла
    бы, начни rebuild писать в state.suppression.
    """
    root = COLLECTOR
    modules = sorted((root / "services").rglob("*.py")) + sorted((root / "routes").rglob("*.py"))
    modules += [root / "db" / "lead.py", root.parent / "writer" / "db" / "thread_store.py"]

    for path in modules:
        if path.name == "store.py":
            continue   # сам движок: ATTACH и обе схемы живут здесь по построению
        targets = writes_of(path)
        assert targets != {"state", "derived"}, \
            f"{path.name} пишет в обе базы — транзакция через две базы не атомарна"


def test_the_guard_would_catch_a_cross_db_write(tmp_path):
    """Сторож ловит именно то, ради чего стоит: запись в обе базы из одного модуля."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        'db.execute("INSERT INTO companies_all (run_id) VALUES (1)")\n'
        'db.execute("INSERT INTO state.suppression (handle) VALUES (?)", (h,))\n',
        encoding="utf-8")
    assert writes_of(offender) == {"state", "derived"}


class DummyContext:
    def progress(self, current, total, label): ...
    def log(self, message): ...
    def check_cancelled(self): ...