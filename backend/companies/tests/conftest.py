import gzip
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import paths
from core import db as core_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A migrated database in a temp file, also served to request dependencies."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    core_db.migrate(path)
    return path


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection


def fixture_html(name: str) -> str:
    with gzip.open(FIXTURES / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def page_of(state: dict) -> str:
    """Wrap a state the way 2GIS does: JSON inside a JS string literal."""
    text = json.dumps(state).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>var initialState = JSON.parse('{text}');</script>"


def rubric_page(branches: list[dict], current: int, pages: int = 5, total: int = 60) -> str:
    """A rubric list page; each branch is {"id", "org", "name", "reviews"?}."""
    profile = {b["id"]: {"data": {
        "name": b["name"], "org": {"id": b["org"], "name": b.get("org_name", b["name"])},
        "address_name": b.get("address"), "reviews": {"general_review_count": b.get("reviews")},
    }} for b in branches}
    return page_of({"data": {
        "search": {"profile": {"q": {"data": {"total": total, "pages": pages,
                                              "currentPage": current}}}},
        "entity": {"profile": profile}}})


def card_page(branch_id: str, contacts: list[tuple[str, str]], city: str = "almaty") -> str:
    """A firm card with the given (type, value) contacts."""
    group = [{"type": kind, "value": value} for kind, value in contacts]
    return page_of({"data": {"entity": {"profile": {branch_id: {"data": {
        "city_alias": city, "contact_groups": [{"contacts": group}]}}}}}})


@pytest.fixture
def gis():
    return SimpleNamespace(fixture=fixture_html, page_of=page_of, rubric=rubric_page, card=card_page)
