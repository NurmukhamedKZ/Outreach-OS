"""Сбор сырья: потолок пагинации пойман, собрано ровно то, что задано конфигом.

Обе проверки идут по живому снимку raw/: подмена страницы — свойство источника,
а не разбора, и на двух эталонных страницах её не воспроизвести. Нет снимка —
раздел пропускается, как и остальные проверки на данных.
"""

import re
import tomllib
from pathlib import Path

import pytest

import collector.services.storage as storage
from collector.services import sources
from collector.services.pipeline import collect, rebuild

RUBRIC_URL = re.compile(r"2gis\.kz/([a-z]+)/rubric/(\d+)(?:/page/(\d+))?$")


@pytest.fixture
def rubric_pages():
    """Страницы рубрик из живого raw/ с разобранным адресом."""
    if not storage.RAW.exists():
        pytest.skip("data/raw нет — раздел требует собранного сырья")
    pages = [
        {**page, "match": RUBRIC_URL.search(page["url"])}
        for page in storage.iter_pages()
        if RUBRIC_URL.search(page["url"])
    ]
    if not pages:
        pytest.skip("в снимке нет страниц рубрик 2GIS")
    return pages


def test_pagination_guard_catches_substitution(rubric_pages):
    """2GIS отдаёт HTTP 200 с первой страницей вместо запрошенной шестой.

    Единственный честный признак потолка — currentPage в ответе: total и pages
    продолжают обещать больше. Подменённые страницы обязаны находиться и обязаны
    не попадать в организации прогона.
    """
    substituted = []
    for page in rubric_pages:
        requested = int(page["match"].group(3) or 1)
        state = sources.parse_initial_state(rebuild.html_of(page))
        _, _, current = sources.parse_search_meta(state)
        if current != requested:
            substituted.append((page["url"], requested, current))

    assert substituted, "ни одной подмены в снимке — проверка потолка ничего не сторожит"
    for url, requested, current in substituted:
        assert current < requested, f"{url}: запрошено {requested}, отдано {current}"


def test_plan_coverage_matches_config(rubric_pages):
    """Собрано ровно то, что задано config.toml, и не глубже потолка источника."""
    config = tomllib.loads((Path(__file__).resolve().parent.parent / "config.toml").read_text(encoding="utf-8"))
    cities, rubrics = set(config["cities"]), set(config["rubrics"]["include"])

    for page in rubric_pages:
        city, rubric, number = page["match"].groups()
        assert city in cities, f"{page['url']}: город не из config.toml"
        assert int(rubric) in rubrics, f"{page['url']}: рубрика не из белого списка"
        assert int(number or 1) <= collect.PAGE_LIMIT, f"{page['url']}: глубже потолка"

    collected = {(m.group(1), int(m.group(2))) for m in
                 (page["match"] for page in rubric_pages)}
    planned = {(city, rubric) for city in cities for rubric in rubrics}
    assert collected <= planned, f"собрано вне плана: {sorted(collected - planned)}"


def test_in_parallel_propagates_job_id_and_isolates_entity_per_task(monkeypatch):
    """ThreadPoolExecutor переиспользует воркер-потоки между заданиями: без
    явного проброса job_id и reset entity на каждое задание чужой домен или
    чужая джоба утекли бы в лог следующего задания, выполненного на том же
    потоке пула."""
    import logctx
    from collector.services.pipeline import collect

    monkeypatch.setattr(collect, "MAX_WORKERS", 2)   # 2 потока на 6 заданий — гарантированное переиспользование

    seen = []

    def worker(budget, job):
        seen.append((job, logctx.current_job_id(), logctx.current_entity()))
        return None

    jobs = [f"job-{i}" for i in range(6)]
    with logctx.job("outer-job-1"):
        list(collect.in_parallel(worker, None, jobs))

    assert logctx.current_job_id() is None, "job_id утёк из in_parallel в вызывающий поток"
    assert len(seen) == 6
    for job, job_id, entity in seen:
        assert job_id == "outer-job-1", f"{job}: неверный job_id внутри потока — {job_id}"
        assert entity == job, f"{job}: чужая entity внутри потока — {entity}"
