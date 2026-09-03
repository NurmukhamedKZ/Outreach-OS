"""Сырьё цело: у каждой страницы сайдкар, у каждого сайдкара страница.

Против снимка из двух эталонных фикстур — те же инварианты, что у бывшего
check_raw, но без боевых гигабайтов.
"""

import gzip
import json
import pathlib

import pytest

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

def test_sidecar_never_appears_half_written(tmp_path, monkeypatch):
    """Обрыв посреди записи сайдкара не оставляет пустой файл на его месте.

    Дорожка Instagram (ig_comments, ig_profile) сканирует все сайдкары raw/
    через iter_pages, пока дорожка сайтов пишет их же — эти две идут одной
    стадией discover. Читатель обязан увидеть «сайдкара нет» (страница
    недокачана, возьмётся заново), а не «сайдкар пуст» — то есть
    JSONDecodeError посреди трёхчасового прогона.
    """
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://example.kz/"
    sidecar = tmp_path / f"{storage.sha_of(url)}.json"
    original = storage.Path.replace

    def fail_on_sidecar(self, target):
        if pathlib.Path(target) == sidecar:
            raise OSError("обрыв ровно на подмене сайдкара")
        return original(self, target)

    monkeypatch.setattr(storage.Path, "replace", fail_on_sidecar)
    with pytest.raises(OSError):
        storage.put(url, "<html>тело</html>",
                    {"url": url, "final_url": url, "status": 200,
                     "fetched_at": "2026-09-03T10:00:00Z"})

    assert not sidecar.exists(), "на месте сайдкара остался обрывок"
    assert not storage.exists(url), "страница без сайдкара обязана считаться недокачанной"


def test_iter_pages_survives_a_half_written_sidecar(tmp_path, monkeypatch):
    """Соседняя дорожка застала сайдкар в момент записи — снимок читается
    дальше без него, а не падает целиком: страница вернётся на следующем
    прогоне, а прогон уже идёт третий час."""
    monkeypatch.setattr(storage, "RAW", tmp_path)
    for name in ("good", "torn"):
        url = f"https://{name}.kz/"
        storage.put(url, "<html></html>",
                    {"url": url, "final_url": url, "status": 200,
                     "fetched_at": "2026-09-03T10:00:00Z"})
    torn = tmp_path / f"{storage.sha_of('https://torn.kz/')}.json"
    torn.write_text("", encoding="utf-8")          # ровно то, что видит читатель

    pages = storage.iter_pages()

    assert [p["url"] for p in pages] == ["https://good.kz/"]


def test_iter_pages_survives_a_sidecar_deleted_mid_scan(tmp_path, monkeypatch):
    """storage.discard снёс пару между проверкой страницы и чтением сайдкара —
    это состояние гонки, а не порча снимка."""
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://vanishing.kz/"
    storage.put(url, "<html></html>",
                {"url": url, "final_url": url, "status": 200,
                 "fetched_at": "2026-09-03T10:00:00Z"})
    sidecar = tmp_path / f"{storage.sha_of(url)}.json"
    original = storage.Path.read_text

    def vanish(self, *args, **kwargs):
        if self == sidecar:
            sidecar.unlink()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(storage.Path, "read_text", vanish)

    assert storage.iter_pages() == []
