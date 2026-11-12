"""Page adapter with the network call replaced: scrapling never leaves the process."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from curl_cffi.curl import CurlError
from curl_cffi.requests.exceptions import InvalidURL

from core import pages

URL = "https://2gis.kz/almaty/firm/70000001017502602"
FETCHED_AT = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


class Network:
    """Stands in for scrapling's Fetcher and counts what reached the network."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(html="<html>ok</html>", landed=URL, status=200):
    return SimpleNamespace(status=status, url=landed, html_content=html)


@pytest.fixture
def network(monkeypatch):
    def install(*responses):
        fake = Network(*responses)
        monkeypatch.setattr(pages, "Fetcher", fake)
        return fake
    return install


@pytest.fixture
def source(tmp_path):
    return pages.ScraplingPages(tmp_path / "pages", now=lambda: FETCHED_AT,
                                intervals={}, default_interval=0)


def test_page_carries_text_final_url_and_fetch_time(network, source):
    network(response(landed=URL + "/tab/info"))

    page = source.get(URL)

    assert page == pages.Page(text="<html>ok</html>", final_url=URL + "/tab/info",
                              fetched_at=FETCHED_AT)


def test_second_request_is_served_from_files_without_network(network, source, tmp_path):
    net = network(response(landed=URL + "/tab/info"))
    first = source.get(URL)

    again = pages.ScraplingPages(tmp_path / "pages", now=lambda: datetime.now(timezone.utc),
                                 intervals={}, default_interval=0)
    assert again.get(URL) == first
    assert net.requested == [URL]


def test_bot_check_page_is_refused_and_never_stored(network, source):
    net = network(response(landed="https://2gis.kz/captcha?back=x"), response())

    with pytest.raises(pages.BotCheckError):
        source.get(URL)
    assert source.get(URL).text == "<html>ok</html>"
    assert net.requested == [URL, URL]


@pytest.mark.parametrize("status", [204, 403, 404, 429, 500])
def test_non_200_is_an_error_with_status_and_nothing_is_stored(network, source, status):
    net = network(response(status=status), response())

    with pytest.raises(pages.HttpStatusError) as error:
        source.get(URL)
    assert error.value.status == status
    source.get(URL)
    assert len(net.requested) == 2


@pytest.mark.parametrize("failure", [CurlError("Failed to connect"),
                                     InvalidURL("URL rejected: Bad hostname")])
def test_network_failure_or_malformed_address_surfaces_as_fetch_error(network, source,
                                                                      failure):
    network(failure)

    with pytest.raises(pages.FetchError):
        source.get(URL)


def corrupt_body(root):
    for stored in root.glob("*.html.gz"):
        stored.write_bytes(b"not gzip at all")


def sidecar(content):
    def corrupt(root):
        for stored in root.glob("*.json"):
            stored.write_text(content, encoding="utf-8")
    return corrupt


def delete_body(root):
    for stored in root.glob("*.html.gz"):
        stored.unlink()


@pytest.mark.parametrize("corrupt", [corrupt_body, sidecar("{trunc"), sidecar("[]"),
                                     sidecar("{}"), delete_body])
def test_corrupted_page_is_fetched_again(network, source, tmp_path, corrupt):
    net = network(response(html="<html>first</html>"), response(html="<html>second</html>"))
    source.get(URL)
    corrupt(tmp_path / "pages")

    assert source.get(URL).text == "<html>second</html>"
    assert len(net.requested) == 2


def test_crash_before_the_sidecar_leaves_the_page_unfinished(network, source, monkeypatch):
    net = network(response(html="<html>first</html>"), response(html="<html>second</html>"))
    write = pages._write_atomically
    written = []

    def crash_on_second_file(path, data):
        written.append(path.name)
        if len(written) == 2:
            raise OSError("disk full")
        write(path, data)

    monkeypatch.setattr(pages, "_write_atomically", crash_on_second_file)
    with pytest.raises(OSError):
        source.get(URL)
    monkeypatch.setattr(pages, "_write_atomically", write)

    assert written[0].endswith(".html.gz")
    assert source.get(URL).text == "<html>second</html>"
    assert len(net.requested) == 2


def test_same_host_waits_its_interval_other_hosts_do_not():
    pacer = pages.Pacer({"2gis.kz": 0.2}, default=0)

    started = time.monotonic()
    pacer.wait("https://2gis.kz/almaty/rubric/653")
    pacer.wait("https://example.kz/")
    assert time.monotonic() - started < 0.1

    pacer.wait("https://public.2gis.kz/almaty/rubric/653/page/2")
    pacer.wait("https://2gis.kz/almaty/rubric/653/page/3")
    assert time.monotonic() - started >= 0.2


def test_longest_host_suffix_wins():
    pacer = pages.Pacer({"2gis.kz": 0.15, "reviews.2gis.kz": 1.0}, default=0.5)

    assert pacer.interval_for("2gis.kz") == 0.15
    assert pacer.interval_for("api.reviews.2gis.kz") == 1.0
    assert pacer.interval_for("site.kz") == 0.5


def test_fake_returns_known_pages_and_404_for_the_rest():
    fake = pages.FakePages({URL: "<html>card</html>",
                            "https://down.kz/": pages.FetchError("timeout")})

    assert fake.get(URL).text == "<html>card</html>"
    assert fake.get(URL).final_url == URL
    with pytest.raises(pages.FetchError):
        fake.get("https://down.kz/")
    with pytest.raises(pages.HttpStatusError):
        fake.get("https://unknown.kz/")
    assert fake.requested == [URL, URL, "https://down.kz/", "https://unknown.kz/"]


def test_cookies_and_headers_reach_the_network_and_the_page_is_kept_by_address(network, source):
    net = network(response())
    calls = []
    original = net.get

    def get(url, **kwargs):
        calls.append(kwargs)
        return original(url, **kwargs)

    net.get = get
    source.get(URL, cookies={"dg5_museum_accept": "true"}, headers={"Accept": "application/json"})
    source.get(URL)

    assert calls == [{"impersonate": "chrome", "cookies": {"dg5_museum_accept": "true"},
                      "headers": {"Accept": "application/json"}}]


def test_without_cookies_and_headers_scrapling_keeps_its_defaults(network, source):
    net = network(response())
    calls = []
    original = net.get
    net.get = lambda url, **kwargs: calls.append(kwargs) or original(url, **kwargs)

    source.get(URL)

    assert calls == [{"impersonate": "chrome"}]


def test_fake_records_the_cookies_of_each_request():
    fake = pages.FakePages({URL: "<html>card</html>"})

    fake.get(URL, cookies={"dg5_museum_accept": "true"})
    fake.get(URL, headers={"Accept": "application/json"})

    assert fake.cookies == [{"dg5_museum_accept": "true"}, None]
