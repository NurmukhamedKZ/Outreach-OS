"""#13 и #38: старый конвейер и сборщик collector удалены — никто не импортирует их."""

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
