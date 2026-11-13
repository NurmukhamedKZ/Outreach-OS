"""#13: старый конвейер удалён целиком — никто не импортирует и не зовёт его."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
RETIRED = re.compile(
    r"collector\.services\.(store|storage|sources|resolve|enrich|score|fetch|leads)\b"
    r"|collector\.services\.pipeline\.(analyze|collect|rebuild|export|llm|probe|dossier)\b"
    r"|collector\.db\.lead\b|derived\.db|serper_api_key|LEGACY_STATE")


def test_no_code_reaches_the_retired_pipeline():
    this = Path(__file__).resolve()
    offenders = [f"{path.relative_to(BACKEND)}:{n}"
                 for path in BACKEND.rglob("*.py")
                 if ".venv" not in path.parts and path.resolve() != this
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if RETIRED.search(line)]
    assert offenders == []
