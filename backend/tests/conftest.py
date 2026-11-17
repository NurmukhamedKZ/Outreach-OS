"""Fixtures of the offline stand shared by its end-to-end tests: no model key, no way off the machine."""

import socket
from types import SimpleNamespace

import pytest

from config import settings
from core import pages as core_pages

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def offline(monkeypatch):
    go_offline(monkeypatch)


def go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offline", True)
    monkeypatch.setattr(settings, "openrouter_api_key", None)


@pytest.fixture
def no_outbound(monkeypatch):
    shut_off_the_network(monkeypatch)


def shut_off_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test on any request off this machine: Python sockets and scrapling's curl alike."""
    connect, resolve = socket.socket.connect, socket.getaddrinfo

    def local_only(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] not in LOOPBACK:
            raise AssertionError(f"went out to {address}")
        return connect(sock, address, *args, **kwargs)

    def resolve_local(host, *args, **kwargs):
        if host not in LOOPBACK:
            raise AssertionError(f"looked up {host}")
        return resolve(host, *args, **kwargs)

    def no_curl(url, **kwargs):
        raise AssertionError(f"scrapling fetched {url}")

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket.socket, "connect_ex", local_only)
    monkeypatch.setattr(socket, "getaddrinfo", resolve_local)
    monkeypatch.setattr(core_pages, "Fetcher", SimpleNamespace(get=no_curl))
