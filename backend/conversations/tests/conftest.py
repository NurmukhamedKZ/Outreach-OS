from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import clock
import paths
from conversations import api, dependencies
from conversations.tests.leads import NOW
from core import db as core_db
from core.errors import DomainError, domain_error_handler
from core.model import FakeModel


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A migrated database standing in for the production one; the clock stops at NOW."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    core_db.migrate(path)
    return path


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection


@pytest.fixture
def model():
    """The model every request gets; queue its answers with model.answers.append."""
    return FakeModel()


@pytest.fixture
def client(state, model):
    app = FastAPI()
    app.include_router(api.router)
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]  # Starlette types handlers loosely
    app.dependency_overrides[dependencies.build_model] = lambda: model
    with TestClient(app) as client:
        yield client
