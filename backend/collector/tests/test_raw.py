"""Сырьё цело: у каждой страницы сайдкар, у каждого сайдкара страница.

Против снимка из двух эталонных фикстур — те же инварианты, что у бывшего
check_raw, но без боевых гигабайтов.
"""

import gzip
import json

import collector.services.storage as storage

SIDECAR_FIELDS = ("url", "final_url", "status", "fetched_at")


def test_raw_invariants(raw_snapshot):
    pages = sorted(storage.RAW.glob("*.html.gz"))
    sidecars = [f for f in storage.RAW.glob("*.json") if f.name.count(".") == 1]
    assert pages, "raw/ пуст — сырьё потеряно, восстановить нельзя"
    assert len(pages) == len(sidecars), (
        f"страниц {len(pages)}, сайдкаров {len(sidecars)} — "
        "страница без сайдкара слепа к подмене, сайдкар без страницы бесполезен"
    )

    for page in pages:
        sidecar = storage.RAW / f"{page.name.removesuffix('.html.gz')}.json"
        assert sidecar.exists(), f"{page.name} без сайдкара"
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        missing = [f for f in SIDECAR_FIELDS if not meta.get(f)]
        assert not missing, f"{sidecar.name}: пустые поля {missing}"
        assert meta["status"] == 200, f"{sidecar.name}: статус {meta['status']}"
        assert meta["fetched_at"].startswith("20"), meta["fetched_at"]
        if "2gis.kz" in meta["url"]:
            with gzip.open(page, "rt", encoding="utf-8") as fh:
                assert len(fh.read(2000)) > 1000, f"{page.name}: страница источника пуста"


def test_snapshot_pages_match_fixture_urls(raw_snapshot):
    """Снимок содержит обе эталонные страницы — рубрику и карточку."""
    urls = sorted(meta["url"] for meta in storage.iter_pages())
    assert urls == [
        "https://2gis.kz/almaty/firm/70000001017502602",
        "https://2gis.kz/almaty/rubric/653",
    ], urls