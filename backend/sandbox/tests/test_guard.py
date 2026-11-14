"""Предохранитель: песочница с боевым транспортом — это письма живым людям."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import paths
import sandbox
from core.whatsapp import FakeWhatsApp, NodeWhatsApp
from sandbox import runs

SANDBOX_NODE = "http://127.0.0.1:8787/api/sandbox/node"


@pytest.fixture(autouse=True)
def sandbox_runs(tmp_path, monkeypatch):
    """mount() активирует прогон, а значит создаёт файлы: без подмены каталога
    холостая база осела бы в репозитории."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    yield
    runs.deactivate()


def _settings(sandbox_on: bool, node_url: str, self_url: str = "http://127.0.0.1:8787"):
    return SimpleNamespace(sandbox=sandbox_on, sender_node_url=node_url, sandbox_self_url=self_url)


def _app(transport_url: str = SANDBOX_NODE) -> FastAPI:
    """The app as the assembler leaves it: the transport the ticks send through is set."""
    app = FastAPI()
    app.state.whatsapp = NodeWhatsApp(transport_url)
    return app


def test_off_by_default_mounts_nothing():
    app = FastAPI()
    sandbox.mount(app, _settings(False, "http://127.0.0.1:8788"))
    assert not [
        route for route in app.routes if getattr(route, "path", "").startswith("/api/sandbox")
    ]


def test_on_mounts_both_routers():
    app = _app()
    sandbox.mount(app, _settings(True, SANDBOX_NODE))
    # FastAPI 0.115+ хранит включенные роутеры как _IncludedRouter — собираем пути
    # и оттуда, чтобы тест не хрупнул от версии фреймворка.
    mounted: set[str] = set()
    for route in app.routes:
        mounted.add(getattr(route, "path", ""))
        for inner in getattr(getattr(route, "original_router", None), "routes", []):
            mounted.add(getattr(inner, "path", ""))
    assert "/api/sandbox/runs" in mounted
    assert "/api/sandbox/node/send" in mounted


def test_on_with_the_real_transport_refuses_to_start():
    """Тестовый прогон с боевым SENDER_NODE_URL ушёл бы живым людям с боевых
    номеров. Падать на старте — единственный момент, когда это ещё дёшево."""
    with pytest.raises(sandbox.SandboxMisconfigured) as failure:
        sandbox.mount(_app(), _settings(True, "http://127.0.0.1:8788"))
    assert "SENDER_NODE_URL" in str(failure.value)


def test_on_refuses_when_the_ticks_send_through_the_real_transport():
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(_app("http://127.0.0.1:8788"), _settings(True, SANDBOX_NODE))
    assert paths.state_db() == paths.PRODUCTION_STATE


@pytest.mark.parametrize(
    "url",
    [
        "https://node.example.com/api/sandbox/node",
        "http://127.0.0.1:8788/?to=/api/sandbox/node",
        "http://127.0.0.1:8788/api/sandbox/node/send",
    ],
)
def test_on_refuses_a_transport_that_only_mentions_the_sandbox_path(url):
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(_app(), _settings(True, url))
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(_app(url), _settings(True, SANDBOX_NODE))


def test_on_refuses_a_client_it_cannot_read_the_address_of():
    app = FastAPI()
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(app, _settings(True, SANDBOX_NODE))
    app.state.whatsapp = FakeWhatsApp()
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(app, _settings(True, SANDBOX_NODE))


def test_on_refuses_to_send_its_webhook_secret_off_this_machine():
    with pytest.raises(sandbox.SandboxMisconfigured):
        sandbox.mount(_app(), _settings(True, SANDBOX_NODE, "https://hooks.example.com"))


def test_on_accepts_localhost_and_a_trailing_slash():
    sandbox.mount(_app("http://localhost:8787/api/sandbox/node/"), _settings(True, SANDBOX_NODE))
    assert paths.state_db() != paths.PRODUCTION_STATE


def test_mount_points_the_backend_away_from_the_production_database():
    """Транспорт подменён с первой секунды процесса — база обязана быть
    подменена тогда же. Иначе прогрев, обработка входящего и очередь успевают
    поработать с настоящей перепиской через фейк: строки помечаются
    отправленными, лимиты боевых номеров тратятся, лид не получает ничего."""
    sandbox.mount(_app(), _settings(True, SANDBOX_NODE))
    assert paths.state_db() != paths.PRODUCTION_STATE
    assert runs.active() is not None


def test_mount_without_the_flag_leaves_the_production_database():
    sandbox.mount(FastAPI(), _settings(False, "http://127.0.0.1:8788"))
    assert paths.state_db() == paths.PRODUCTION_STATE
