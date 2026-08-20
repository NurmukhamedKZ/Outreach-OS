"""export.OUT: путь anchored к collector, а не к cwd процесса.

main.py запускается из backend/ (Dockerfile WORKDIR /app), а не из
collector/ — относительный "data/leads.csv" резолвился бы в несуществующий
backend/data/ и валил бы шаг export FileNotFoundError на каждой пересборке.
"""

import collector.services.store as engine
from collector.services.pipeline import export


def test_out_path_anchored_to_collector_package_not_cwd():
    assert export.OUT.is_absolute(), "относительный путь зависит от cwd процесса, а main.py её не фиксирует"
    assert export.OUT.parent == engine.DATA, "leads.csv должен лежать там же, где derived.db и state.db"
