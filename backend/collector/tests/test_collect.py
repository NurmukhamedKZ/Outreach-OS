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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures

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


import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def test_pacer_spaces_repeat_requests_to_one_host():
    """Второй запрос к тому же хосту ждёт интервал, первый — не ждёт ничего."""
    pacer = collect.Pacer({}, 0.05)
    started = time.monotonic()
    for _ in range(3):
        pacer.wait("https://example.kz/page")
    assert time.monotonic() - started >= 0.10, "интервал между запросами к одному хосту не выдержан"


def test_pacer_does_not_space_different_hosts():
    """Три разных домена не ждут друг друга — ради этого пауза и переезжает
    с воркера на хост: 2599 сайтов компаний живут на 2599 доменах."""
    pacer = collect.Pacer({}, 0.05)
    started = time.monotonic()
    for host in ("a.kz", "b.kz", "c.kz"):
        pacer.wait(f"https://{host}/")
    assert time.monotonic() - started < 0.05, "разные хосты заставили друг друга ждать"


def test_pacer_longest_match_wins():
    """Точное имя сильнее суффикса, длинный суффикс — короткого: иначе
    поддомен нельзя было бы выделить, не переписывая общее правило."""
    pacer = collect.Pacer({"2gis.kz": 1.0, "catalog.2gis.kz": 2.0}, 0.5)
    assert pacer.interval_for("catalog.2gis.kz") == 2.0
    assert pacer.interval_for("sub.catalog.2gis.kz") == 2.0
    assert pacer.interval_for("www.2gis.kz") == 1.0
    assert pacer.interval_for("example.kz") == 0.5


def test_pacer_holds_one_host_across_threads():
    """Слот резервируется под локом: двадцать потоков в один хост дают
    девятнадцать интервалов, а не двадцать одновременных запросов."""
    pacer = collect.Pacer({}, 0.02)
    started = time.monotonic()
    with ThreadPoolExecutor(20) as pool:
        list(pool.map(lambda _: pacer.wait("https://one.kz/"), range(20)))
    assert time.monotonic() - started >= 0.02 * 19


def test_pacer_lets_other_hosts_through_while_one_waits():
    """Ожидание очереди к 2GIS не держит поток, идущий на чужой домен —
    сон обязан быть вне общего лока, иначе правка бессмысленна."""
    pacer = collect.Pacer({"slow.kz": 0.4}, 0.0)
    pacer.wait("https://slow.kz/")            # занять слот
    started = time.monotonic()

    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(pacer.wait, "https://slow.kz/")   # будет спать 0.4
        time.sleep(0.05)
        quick_started = time.monotonic()
        pool.submit(pacer.wait, "https://fast.kz/").result()
        quick = time.monotonic() - quick_started
        waiting.result()

    assert quick < 0.1, f"запрос к чужому хосту прождал {quick:.2f} с — лок держится во время сна"
    assert time.monotonic() - started >= 0.3


def test_budget_paces_only_network_requests(monkeypatch):
    """Страница из raw/ бесплатна и очереди к хосту не ждёт: на повторном
    прогоне сбор не делает ни одного запроса и не должен ничего проспать."""
    asked = []
    pacer = SimpleNamespace(wait=asked.append)
    monkeypatch.setattr(collect.fetch, "is_cached", lambda url: url.endswith("cached"))
    monkeypatch.setattr(collect.fetch, "get", lambda url, **kw: "<html></html>")

    budget = collect.Budget(None, pacer)
    budget.get("https://example.kz/cached")
    budget.get("https://example.kz/fresh")

    assert asked == ["https://example.kz/fresh"]


def test_two_budgets_share_one_host_limit(monkeypatch):
    """Две операции, идущие разными дорожками в один хост, не должны удвоить
    темп: Pacer один на процесс, потому что темп принадлежит хосту, а не
    операции. До дорожек операции шли по очереди, и это было безразлично."""
    collect._PACER = None
    monkeypatch.setattr(collect.fetch, "is_cached", lambda url: False)
    monkeypatch.setattr(collect.fetch, "get", lambda url, **kw: "<html></html>")

    first, second = collect.Budget(None), collect.Budget(None)
    assert first.pacer is second.pacer, \
        "у каждой операции свой Pacer — лимит хоста удвоится молча"


def test_recalibration_keeps_taken_slots():
    """Правка config.toml меняет интервалы, но не забывает занятые слоты:
    обнулить их значило бы выпустить пул залпом ровно в тот момент, когда
    оператор крутит ручку из-за капчи."""
    pacer = collect.Pacer({"one.kz": 0.05}, 0.0)
    pacer.wait("https://one.kz/")
    taken = dict(pacer.free_at)

    pacer.recalibrate({"one.kz": 0.9}, 0.0)

    assert pacer.interval_for("one.kz") == 0.9
    assert pacer.free_at == taken


def test_pacing_config_keeps_2gis_faster_than_unknown_hosts():
    """Калибровка из config.toml: 2GIS — один хост на тысячи запросов, и его
    интервал короче, чем у незнакомого домена, которому достанется пара
    страниц. Ровно эта пропорция и была потеряна, пока пауза жила на воркере.
    """
    pacer = collect.default_pacer()
    assert pacer.interval_for("2gis.kz") < pacer.default
    assert pacer.interval_for("public-api.reviews.2gis.com") < pacer.default


def test_comments_are_asked_only_for_posts_the_model_will_see(tmp_path, monkeypatch):
    """Комментарии запрашиваются к тем же постам, что уйдут в промпт.

    В эталонной ленте 12 постов, и единственный пост с комментариями —
    одиннадцатый. Анализ берёт первые posts_limit (10) и до него не доходит,
    значит запрос за его комментариями оплачивается риском бана и
    выбрасывается. По живому raw/ таких запросов 221 из 1441.
    """
    import gzip
    import hashlib
    import json

    url = collect.IG_FEED.format(username="adalservice__", count=collect.IG_POST_COUNT)
    sha = hashlib.sha1(url.encode()).hexdigest()
    raw = tmp_path / "raw"
    raw.mkdir()
    with gzip.open(FIXTURES / "ig_feed.html.gz", "rb") as src, \
         gzip.open(raw / f"{sha}.html.gz", "wb") as dst:
        dst.write(src.read())
    (raw / f"{sha}.json").write_text(json.dumps(
        {"url": url, "final_url": url, "status": 200,
         "fetched_at": "2026-08-20T09:00:00Z"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(storage, "RAW", raw)

    assert collect.posts_with_comments() == []


def test_default_pacer_is_one_instance_under_concurrent_lanes(monkeypatch):
    """Две дорожки строят Budget одновременно — это и есть стадия discover.

    Между проверкой «_PACER is None» и присваиванием есть окно, и обе успевали
    в него: каждая уносила свой Pacer со своей картой занятых слотов, то есть
    по своему лимиту на один и тот же хост. Окно расширено намеренно — в бою
    его открывает обычное переключение потоков.
    """
    collect._PACER = None
    real_init = collect.Pacer.__init__

    def slow_init(self, intervals, default):
        time.sleep(0.05)
        real_init(self, intervals, default)

    monkeypatch.setattr(collect.Pacer, "__init__", slow_init)
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(lambda _: collect.default_pacer(), range(2)))

    assert first is second, "дорожки получили разные Pacer — темп хоста удвоится"
