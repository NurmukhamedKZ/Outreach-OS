"""Предохранитель: песочница с боевым транспортом — это письма живым людям."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import paths
import sandbox
from sandbox import runs


@pytest.fixture(autouse=True)
def sandbox_runs(tmp_path, monkeypatch):
    """mount() активирует прогон, а значит создаёт файлы: без подмены каталога
    холостая база осела бы в репозитории."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    yield
    runs.deactivate()


def _settings(sandbox_on: bool, node_url: str):
    return SimpleNamespace(sandbox=sandbox_on, sender_node_url=node_url)


def test_off_by_default_mounts_nothing():
    app = FastAPI()
    sandbox.mount(app, _settings(False, "http://127.0.0.1:8788"))
    assert not [route for route in app.routes
                if getattr(route, "path", "").startswith("/api/sandbox")]


def test_on_mounts_both_routers():
    app = FastAPI()
    sandbox.mount(app, _settings(True, "http://127.0.0.1:8787/api/sandbox/node"))
    # FastAPI 0.115+ хранит включенные роутеры как _IncludedRouter — собираем пути
    # и оттуда, чтобы тест не хрупнул от версии фреймворка.
    mounted: set[str] = set()
    for route in app.routes:
        if getattr(route, "path", None):
            mounted.add(route.path)  # type: ignore[attr-defined]
        if hasattr(route, "original_router"):
            for inner in route.original_router.routes:  # type: ignore[attr-defined]
                if getattr(inner, "path", None):
                    mounted.add(inner.path)
    assert "/api/sandbox/runs" in mounted
    assert "/api/sandbox/node/send" in mounted


def test_on_with_the_real_transport_refuses_to_start():
    """Тестовый прогон с боевым SENDER_NODE_URL ушёл бы живым людям с боевых
    номеров. Падать на старте — единственный момент, когда это ещё дёшево."""
    with pytest.raises(sandbox.SandboxMisconfigured) as failure:
        sandbox.mount(FastAPI(), _settings(True, "http://127.0.0.1:8788"))
    assert "SENDER_NODE_URL" in str(failure.value)


def test_mount_points_the_backend_away_from_the_production_database():
    """Транспорт подменён с первой секунды процесса — база обязана быть
    подменена тогда же. Иначе прогрев, обработка входящего и очередь успевают
    поработать с настоящей перепиской через фейк: строки помечаются
    отправленными, лимиты боевых номеров тратятся, лид не получает ничего."""
    sandbox.mount(FastAPI(), _settings(True, "http://127.0.0.1:8787/api/sandbox/node"))
    assert paths.state_db() != paths.PRODUCTION_STATE
    assert runs.active() is not None


def test_mount_without_the_flag_leaves_the_production_database():
    sandbox.mount(FastAPI(), _settings(False, "http://127.0.0.1:8788"))
    assert paths.state_db() == paths.PRODUCTION_STATE
