"""#13 и #38: старый конвейер и сборщик collector удалены — никто не импортирует их.

#63: дашборд не берёт шрифты у Google.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RETIRED = re.compile(
    r"^\s*(from|import) (collector|logctx|tasks|analytics|activity)\b"
    r"|derived\.db|serper_api_key|LEGACY_STATE")


def test_no_code_reaches_the_retired_pipeline():
    this = Path(__file__).resolve()
    offenders = [f"{path.relative_to(BACKEND)}:{n}"
                 for path in BACKEND.rglob("*.py")
                 if ".venv" not in path.parts and path.resolve() != this
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if RETIRED.search(line)]
    assert offenders == []


def test_the_dashboard_takes_no_fonts_from_google():
    """#63: next/font/google под Turbopack роняет сборку ответом Google в кэше (bugs.md, #24)."""
    frontend = BACKEND.parent / "frontend"
    google = re.compile(r"next/font/google|fonts\.(googleapis|gstatic)\.com")
    offenders = [str(path.relative_to(frontend))
                 for path in frontend.rglob("*")
                 if path.suffix in {".ts", ".tsx", ".js", ".mjs", ".css"}
                 and not {"node_modules", ".next"} & set(path.relative_to(frontend).parts)
                 and google.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
