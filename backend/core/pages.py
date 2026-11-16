"""Network collection adapter: "give me the page at this address".

Downloaded pages are kept as files (sha1(url).html.gz plus a json sidecar), so a
parser can be fixed and rerun for a company without going back to the network.
The offline mode of a stand (OFFLINE=1) answers from these files alone.
"""

import gzip
import hashlib
import json
import logging
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from curl_cffi.curl import CurlError
from scrapling.fetchers import Fetcher

log = logging.getLogger(__name__)

# Seconds between requests to one host. 2GIS answers thousands of requests and
# 0.15 s (6.7 per second) passed the production collection; an unknown company
# site gets one to seven pages, so half a second is cheap politeness. A captcha
# means raising these, not touching the code around them.
HOST_INTERVALS = {"2gis.kz": 0.15, "public-api.reviews.2gis.com": 0.15}
DEFAULT_INTERVAL = 0.5


@dataclass(frozen=True)
class Page:
    text: str
    # Sources swap pages silently with HTTP 200 (a page past the last one, an
    # unknown slug): only the landing address tells.
    final_url: str
    fetched_at: datetime


class FetchError(Exception):
    """The page could not be fetched: network failure, non-200 or a bot check."""


class HttpStatusError(FetchError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status


class CertificateError(FetchError):
    """The host's TLS certificate is bad: another name, expired, self-signed.

    Telling it apart is for the caller: a company site may be read over http.
    """


class BotCheckError(FetchError):
    """The source answered 200 with an "are you a robot" page.

    It is a refusal, not a page: stored under sha1(url) it would take the real
    page's place forever.
    """


class Pages(Protocol):
    """Source of web pages; scrapling in production, FakePages in tests."""

    def get(
        self,
        url: str,
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Page:
        """Return the page, from files when it was fetched before.

        The page is kept by its address alone: cookies and headers only help the
        source answer (2GIS sends a request without its cookie to /museum).

        Raises:
            FetchError: the network gave no usable page (a malformed address
                included); nothing is stored.
        """
        ...


class Pacer:
    """Minimum interval between requests to one host, shared by all threads.

    The slot is booked under the lock and the sleep happens outside it, so a
    thread queued for 2GIS does not hold up threads going to other domains.
    """

    def __init__(self, intervals: Mapping[str, float], default: float):
        self.intervals = intervals
        self.default = default
        self.free_at: dict[str, float] = {}
        self.lock = Lock()

    def interval_for(self, host: str) -> float:
        """Exact host, else the longest matching suffix, else the default."""
        if host in self.intervals:
            return self.intervals[host]
        matches = [
            (len(name), value)
            for name, value in self.intervals.items()
            if host.endswith(f".{name}")
        ]
        return max(matches)[1] if matches else self.default

    def wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        interval = self.interval_for(host)
        with self.lock:
            start = max(time.monotonic(), self.free_at.get(host, 0.0))
            self.free_at[host] = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class ScraplingPages:
    """Pages through scrapling with a file cache and a per-host pace.

    Build one per process: the pace belongs to the host, and two instances
    would each keep their own slots and double it.
    """

    def __init__(
        self,
        root: Path,
        now: Callable[[], datetime],
        *,
        intervals: Mapping[str, float] = HOST_INTERVALS,
        default_interval: float = DEFAULT_INTERVAL,
    ):
        self.root = root
        self.now = now
        self.pacer = Pacer(intervals, default_interval)

    def get(
        self,
        url: str,
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Page:
        if (page := stored(self.root, url)) is not None:
            return page
        page = self._fetch(url, cookies, headers)
        store(self.root, url, page)
        return page

    def _fetch(
        self, url: str, cookies: Mapping[str, str] | None, headers: Mapping[str, str] | None
    ) -> Page:
        self.pacer.wait(url)
        # Only what the caller gave: an empty value would replace scrapling's own headers.
        extra: dict[str, Any] = {
            name: dict(value)
            for name, value in (("cookies", cookies), ("headers", headers))
            if value
        }
        try:
            response = Fetcher.get(url, impersonate="chrome", **extra)
        # curl reports a malformed address the same way (InvalidURL is a CurlError).
        except CurlError as error:
            # curl names TLS failures in its message only (codes 35, 51, 58, 60…).
            text = str(error).lower()
            kind = CertificateError if "ssl" in text or "certificate" in text else FetchError
            raise kind(f"{url}: {error}") from error
        if response.status != 200:
            raise HttpStatusError(response.status, url)
        landed = str(response.url)
        if "captcha" in landed.lower():
            raise BotCheckError(f"{url}: source redirected to a bot check {landed}")
        # Response.text is the root element's text (empty); the markup is html_content.
        return Page(text=str(response.html_content), final_url=landed, fetched_at=self.now())


def stored(root: Path, url: str) -> Page | None:
    """Return the page kept for `url` under `root`; None when there is none.

    An unreadable page is removed and counts as none: the network adapter fetches it again.
    """
    body, sidecar = _paths(root, url)
    if not sidecar.exists():
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        return Page(
            text=gzip.decompress(body.read_bytes()).decode("utf-8"),
            final_url=meta["final_url"],
            fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        )
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError) as error:
        log.warning("stored page for %s is unreadable (%s), dropped", url, error)
        body.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        return None


def store(root: Path, url: str, page: Page) -> None:
    """Keep `page` as the page at `url` under `root`: the page, then the sidecar, each atomically.

    A sidecar marks a complete page, so it goes last: a crash in between
    leaves a page without a sidecar, which is fetched again.
    """
    body, sidecar = _paths(root, url)
    root.mkdir(parents=True, exist_ok=True)
    _write_atomically(body, gzip.compress(page.text.encode("utf-8")))
    meta = {
        "url": url,
        "final_url": page.final_url,
        "status": 200,
        "fetched_at": page.fetched_at.isoformat(timespec="seconds"),
    }
    _write_atomically(sidecar, json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def _paths(root: Path, url: str) -> tuple[Path, Path]:
    sha = hashlib.sha1(url.encode()).hexdigest()
    return root / f"{sha}.html.gz", root / f"{sha}.json"


def _write_atomically(path: Path, data: bytes) -> None:
    # A unique temp name: two threads fetching one url must not share it.
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


class StoredPages:
    """Pages from the kept files alone, never the network: the offline mode of a stand.

    An address with no page kept answers 404, as a page that is gone: the
    offline world is what was stored, and nothing else exists.
    """

    def __init__(self, root: Path):
        self.root = root

    def get(
        self,
        url: str,
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Page:
        if (page := stored(self.root, url)) is None:
            log.info("offline: no stored page for %s", url)
            raise HttpStatusError(404, url)
        return page


class FakePages:
    """Pages for tests: known urls answer, an Exception value is raised, others 404."""

    def __init__(
        self, pages: Mapping[str, str | Page | Exception], fetched_at: datetime | None = None
    ):
        self.pages = pages
        self.fetched_at = fetched_at or datetime.fromisoformat("2026-11-12T09:00:00+00:00")
        self.requested: list[str] = []
        self.cookies: list[Mapping[str, str] | None] = []

    def get(
        self,
        url: str,
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Page:
        self.requested.append(url)
        self.cookies.append(cookies)
        if url not in self.pages:
            raise HttpStatusError(404, url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        if isinstance(page, Page):
            return page
        return Page(text=page, final_url=url, fetched_at=self.fetched_at)
